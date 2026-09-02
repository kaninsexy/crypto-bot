#!/usr/bin/env python
"""Pre-flight power gate for discovery screens and confirmation trials.

Implements `.claude/rules/backtest.md` § "Discovery / confirmation split"
item 5. Import `require_power()` at the top of a screen or trial so the gate
REFUSES an underpowered run, rather than relying on whoever wrote the script
having remembered to check.

    MDE = t_bar * sigma / sqrt(N_expected)

`sigma` is the UNCONDITIONAL dispersion of the outcome variable over the same
window and universe. It is a design input, measured from data the test is not
about — never the conditional statistic being tested. Passing the conditional
statistic here would make the gate circular and is the one way to misuse it.

WHY THIS EXISTS. On 2026-09-02 the deleveraging-reversal screen was about to
run on 30 symbols, a bound chosen to cap a download cost. That gives ~189
events and MDE = 2.11 % against a pre-registered 1.5 % bar: a TRUE 1.5 %
effect returns t = 2.13 and gets logged "killed". The ledger row would have
recorded the sample size, not the substrate.

An underpowered null is the most expensive wrong answer available here. It
looks exactly like evidence, it is cheap to produce, and it closes a question
that was never actually asked. It is also invisible to every other gate in the
harness: CPCV, DSR and the verdict tree all take N as given and none of them
can tell "no effect" from "no power".

Usage (library):

    from scripts.screen_power_check import require_power
    require_power(sigma=0.0969, n_expected=630, t_bar=3.0,
                  effect_threshold=0.015, label="deleveraging_reversal")

Usage (CLI):

    python scripts/screen_power_check.py --sigma 0.0969 --n 630 \\
        --t-bar 3.0 --threshold 0.015
    python scripts/screen_power_check.py --outcome-csv rets.csv --n 630 ...
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass
from typing import Iterable, Optional

import numpy as np


class UnderpoweredScreen(SystemExit):
    """Raised (as a SystemExit) when MDE exceeds the pre-registered threshold.

    Deliberately a SystemExit subclass: a screen that cannot detect its own
    effect size must STOP, and should not be catchable by a generic
    ``except Exception`` in a batch runner that would then log a null.
    """


@dataclass(frozen=True)
class PowerResult:
    sigma: float
    n_expected: int
    t_bar: float
    effect_threshold: float
    mde: float
    passes: bool
    n_required: int

    def render(self, label: str = "") -> str:
        head = f"power check{(' — ' + label) if label else ''}"
        verdict = "PASS" if self.passes else "REFUSED (underpowered)"
        lines = [
            f"── {head} ──",
            f"  sigma (unconditional) : {self.sigma:.6g}",
            f"  N expected            : {self.n_expected}",
            f"  significance bar      : t > {self.t_bar}",
            f"  effect threshold      : {self.effect_threshold:.6g}",
            f"  MDE = t*sigma/sqrt(N) : {self.mde:.6g}",
            f"  N required for MDE<=thr: {self.n_required}",
            f"  verdict               : {verdict}",
        ]
        if not self.passes:
            lines.append(
                f"  -> at N={self.n_expected} a TRUE effect of exactly "
                f"{self.effect_threshold:.6g} returns t = "
                f"{self.effect_threshold / (self.sigma / math.sqrt(self.n_expected)):.2f}"
                f", below the {self.t_bar} bar: it would be logged as a null."
            )
        return "\n".join(lines)


def unconditional_sigma(outcome: Iterable[float]) -> float:
    """Sample sd of the outcome variable, NaNs dropped.

    Feed this the UNCONDITIONAL outcome distribution (e.g. every symbol-day
    3-day return over the window), not the event subset.
    """
    arr = np.asarray(list(outcome), dtype=float)
    arr = arr[np.isfinite(arr)]
    if arr.size < 2:
        raise ValueError("need >= 2 finite observations to estimate sigma")
    return float(arr.std(ddof=1))


def compute_power(
    *,
    sigma: float,
    n_expected: int,
    t_bar: float,
    effect_threshold: float,
) -> PowerResult:
    """Return the MDE and whether it clears the pre-registered threshold."""
    if not math.isfinite(sigma) or sigma <= 0:
        raise ValueError(f"sigma must be finite and > 0; got {sigma!r}")
    if n_expected < 2:
        raise ValueError(f"n_expected must be >= 2; got {n_expected!r}")
    if not math.isfinite(t_bar) or t_bar <= 0:
        raise ValueError(f"t_bar must be finite and > 0; got {t_bar!r}")
    if not math.isfinite(effect_threshold) or effect_threshold <= 0:
        raise ValueError(
            f"effect_threshold must be finite and > 0; got {effect_threshold!r}")

    mde = t_bar * sigma / math.sqrt(n_expected)
    # Smallest N with t_bar*sigma/sqrt(N) <= threshold.
    n_required = int(math.ceil((t_bar * sigma / effect_threshold) ** 2))
    return PowerResult(
        sigma=float(sigma),
        n_expected=int(n_expected),
        t_bar=float(t_bar),
        effect_threshold=float(effect_threshold),
        mde=float(mde),
        passes=bool(mde <= effect_threshold),
        n_required=n_required,
    )


def require_power(
    *,
    n_expected: int,
    t_bar: float,
    effect_threshold: float,
    sigma: Optional[float] = None,
    outcome: Optional[Iterable[float]] = None,
    label: str = "",
    verbose: bool = True,
) -> PowerResult:
    """Compute the gate and REFUSE the run if it is underpowered.

    Exactly one of `sigma` or `outcome` must be given. Raises
    `UnderpoweredScreen` (a SystemExit) when MDE > effect_threshold.
    """
    if (sigma is None) == (outcome is None):
        raise ValueError("pass exactly one of sigma= or outcome=")
    if sigma is None:
        sigma = unconditional_sigma(outcome)  # type: ignore[arg-type]

    res = compute_power(
        sigma=sigma, n_expected=n_expected, t_bar=t_bar,
        effect_threshold=effect_threshold,
    )
    if verbose:
        print(res.render(label))
    if not res.passes:
        raise UnderpoweredScreen(
            f"POWER GATE REFUSED{(' [' + label + ']') if label else ''}: "
            f"MDE {res.mde:.6g} > pre-registered threshold "
            f"{res.effect_threshold:.6g} at N={res.n_expected}. "
            f"This test cannot pass at the effect size it was designed to "
            f"detect, so a null from it would record the sample size, not the "
            f"substrate. Widen the universe/horizon/window to N >= "
            f"{res.n_required}, or close the family as untestable on the "
            f"available data. Widening to satisfy this gate COMPLETES the "
            f"pre-registered test and does NOT increment N_disc "
            f"(.claude/rules/backtest.md, discovery split item 5)."
        )
    return res


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--sigma", type=float,
                     help="unconditional sd of the outcome variable")
    src.add_argument("--outcome-csv",
                     help="path to a one-column CSV/txt of unconditional outcomes")
    ap.add_argument("--n", type=int, required=True, dest="n_expected",
                    help="expected sample size (events / days)")
    ap.add_argument("--t-bar", type=float, default=3.0,
                    help="pre-registered significance bar (default 3.0)")
    ap.add_argument("--threshold", type=float, required=True,
                    dest="effect_threshold",
                    help="pre-registered effect threshold, same units as sigma")
    ap.add_argument("--label", default="")
    ap.add_argument("--soft", action="store_true",
                    help="report only; exit 0 even when underpowered")
    args = ap.parse_args(argv[1:])

    sigma = args.sigma
    if sigma is None:
        vals = [float(x) for x in open(args.outcome_csv, encoding="utf-8")
                .read().replace(",", "\n").split() if x.strip()]
        sigma = unconditional_sigma(vals)

    res = compute_power(
        sigma=sigma, n_expected=args.n_expected, t_bar=args.t_bar,
        effect_threshold=args.effect_threshold,
    )
    print(res.render(args.label))
    if res.passes or args.soft:
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))

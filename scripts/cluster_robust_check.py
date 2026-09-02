#!/usr/bin/env python
"""Dependence-corrected significance for discovery-screen statistics.

Decision rule: `research/discovery/README.md` § "Dependence-corrected
significance", committed at `a410b68` BEFORE any number here was computed.

WHAT THIS IS NOT. It is not a new screen. It recomputes the STANDARD ERROR of
a statistic already in a ledger — same signal, same universe, same horizon,
same window, same point estimate. `N_disc` is unchanged for every family, and
no trials.log row is written. Like the 2026-09-02 `funding_dispersion` verdict
fix, it can only make a test stricter.

WHY. Ordinary standard errors assume independent observations. All three
2026-09-02 screens made that assumption and none of them satisfies it:

- liquidation cascades hit every coin on the same few days, so a 220-event
  sample is a much smaller number of market-wide episodes seen across symbols;
- listing CAR windows of [+0,+20] days physically overlap for nearby listings,
  so their abnormal returns share one market path;
- the daily funding 10-1 spread is a persistent time series.

The pre-flight power gate does not catch this. The gate fixes sample SIZE and
says nothing about whether observations within that sample are independent —
which is exactly how a correctly-powered study can still report a t-stat that
is too large by a factor of two or more.

Two estimators, both standard:

- **Cluster-robust (CR1)** for grouped data. The variance of the mean is
  estimated from cluster SUMS rather than individual observations, so
  within-cluster correlation inflates it as it should. Finite-sample
  correction `G/(G-1)`, inference on `G-1` degrees of freedom (Cameron &
  Miller 2015). With few clusters the estimator is itself unreliable, so the
  small-G caveat is surfaced, never suppressed.
- **Newey–West HAC** for a single time series, with Bartlett weights out to
  `lag` (Newey & West 1987).

Usage:
    python scripts/cluster_robust_check.py --demo
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass
from typing import Iterable, Sequence

import numpy as np

SMALL_G = 30          # below this, the cluster-robust estimator is itself noisy
T_BAR = 3.0           # Harvey-Liu, the project's significance bar


@dataclass(frozen=True)
class RobustResult:
    mean: float
    n: int
    se_ordinary: float
    se_robust: float
    t_ordinary: float
    t_robust: float
    n_groups: int
    dof: int
    method: str

    @property
    def design_effect(self) -> float:
        """t_ordinary / t_robust — how much the naive t was overstated."""
        if self.t_robust == 0 or not math.isfinite(self.t_robust):
            return float("nan")
        return abs(self.t_ordinary) / abs(self.t_robust)

    @property
    def mde_robust(self) -> float:
        """Minimum detectable effect at the project bar, under dependence."""
        return T_BAR * self.se_robust

    @property
    def small_g(self) -> bool:
        return self.n_groups < SMALL_G

    def render(self, label: str = "") -> str:
        head = f"{self.method}{(' — ' + label) if label else ''}"
        lines = [
            f"── {head} ──",
            f"  mean                 : {self.mean:+.4f}",
            f"  N observations       : {self.n}",
            f"  G groups             : {self.n_groups}   (dof {self.dof})",
            f"  SE ordinary          : {self.se_ordinary:.4f}",
            f"  SE robust            : {self.se_robust:.4f}",
            f"  t ordinary           : {self.t_ordinary:+.3f}",
            f"  t ROBUST             : {self.t_robust:+.3f}",
            f"  design effect        : {self.design_effect:.2f}x overstated",
            f"  MDE robust (3*SE)    : {self.mde_robust:.4f}",
            f"  |effect| > MDE       : {abs(self.mean) > self.mde_robust}",
            f"  |t_robust| > {T_BAR}      : {abs(self.t_robust) > T_BAR}",
        ]
        if self.small_g:
            lines.append(
                f"  CAVEAT: G={self.n_groups} < {SMALL_G}; with few clusters the "
                "robust estimator is itself unreliable and typically still "
                "ANTI-conservative. Treat as a lower bound on the correction."
            )
        return "\n".join(lines)


def cluster_robust_mean(values: Sequence[float],
                        groups: Sequence) -> RobustResult:
    """Cluster-robust SE of a sample mean, clustering on `groups`.

    The mean is the OLS coefficient of a regression on a constant, so the
    CR1 sandwich reduces to: var(mean) = G/(G-1) * sum_g(s_g^2) / N^2, where
    s_g is the sum of within-cluster residuals. Within-cluster correlation
    makes those sums larger than independence implies, which is the whole
    point.
    """
    v = np.asarray(values, dtype=float)
    g = np.asarray(groups)
    ok = np.isfinite(v)
    v, g = v[ok], g[ok]
    n = v.size
    if n < 2:
        raise ValueError("need >= 2 finite observations")

    mean = float(v.mean())
    sd = float(v.std(ddof=1))
    se_ord = sd / math.sqrt(n)
    t_ord = mean / se_ord if se_ord > 0 else float("nan")

    resid = v - mean
    uniq = np.unique(g)
    sums = np.array([resid[g == u].sum() for u in uniq], dtype=float)
    G = uniq.size
    if G < 2:
        raise ValueError("need >= 2 clusters")
    var = (G / (G - 1.0)) * float((sums ** 2).sum()) / (n ** 2)
    se_rob = math.sqrt(max(var, 0.0))
    t_rob = mean / se_rob if se_rob > 0 else float("nan")

    return RobustResult(mean=mean, n=n, se_ordinary=se_ord, se_robust=se_rob,
                        t_ordinary=t_ord, t_robust=t_rob, n_groups=G,
                        dof=G - 1, method="cluster-robust (CR1)")


def newey_west_mean(series: Sequence[float], lag: int) -> RobustResult:
    """Newey-West HAC SE of the mean of a time series (Bartlett kernel)."""
    x = np.asarray(series, dtype=float)
    x = x[np.isfinite(x)]
    n = x.size
    if n < 3:
        raise ValueError("need >= 3 finite observations")
    if lag < 0:
        raise ValueError("lag must be >= 0")

    mean = float(x.mean())
    sd = float(x.std(ddof=1))
    se_ord = sd / math.sqrt(n)
    t_ord = mean / se_ord if se_ord > 0 else float("nan")

    e = x - mean
    gamma0 = float((e ** 2).sum()) / n
    s = gamma0
    for k in range(1, min(lag, n - 1) + 1):
        w = 1.0 - k / (lag + 1.0)
        gk = float((e[k:] * e[:-k]).sum()) / n
        s += 2.0 * w * gk
    s = max(s, 1e-300)
    se_rob = math.sqrt(s / n)
    t_rob = mean / se_rob if se_rob > 0 else float("nan")

    return RobustResult(mean=mean, n=n, se_ordinary=se_ord, se_robust=se_rob,
                        t_ordinary=t_ord, t_robust=t_rob, n_groups=n,
                        dof=n - 1, method=f"Newey-West HAC (lag {lag})")


def merge_dates_into_episodes(dates: Sequence, window_days: int = 5) -> list:
    """Collapse dates within `window_days` of each other into one episode id.

    A cascade is not a calendar day: a market-wide deleveraging can run over a
    long weekend and appear as three "independent" dates. Merging adjacent
    dates is the more conservative clustering, and reporting both is how the
    reader sees how much the answer depends on that choice.
    """
    import pandas as pd

    d = pd.to_datetime(pd.Series(list(dates))).dt.normalize()
    order = d.argsort().to_numpy()
    ids = np.empty(len(d), dtype=int)
    cur, last = 0, None
    for pos in order:
        day = d.iloc[pos]
        if last is not None and (day - last).days > window_days:
            cur += 1
        ids[pos] = cur
        last = day if last is None or day > last else last
    return ids.tolist()


def _demo() -> int:
    """Show the estimator detecting clustering it is supposed to detect."""
    rng = np.random.default_rng(0)
    G, per = 20, 30
    shock = rng.normal(0, 0.10, G)                       # one shared shock/cluster
    vals, grp = [], []
    for i in range(G):
        vals.extend(shock[i] + rng.normal(0.02, 0.01, per))
        grp.extend([i] * per)
    r = cluster_robust_mean(vals, grp)
    print(r.render("synthetic: 20 clusters sharing a common shock"))
    print("\nIndependence would claim a t of "
          f"{r.t_ordinary:.1f}; clustering says {r.t_robust:.1f}.")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--demo", action="store_true",
                    help="run the synthetic clustered example")
    args = ap.parse_args(argv[1:])
    if args.demo:
        return _demo()
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

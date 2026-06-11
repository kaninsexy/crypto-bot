"""
backtest/families.py — Strategy-family taxonomy + cross-trial Sharpe stats.

Gate spec v2 (2026-06-11).  BLP eq.7's E[max SR | null] term scales with
the realized cross-trial Sharpe standard deviation, sqrt(V[{SR_n}]).
The pre-v2 harness omitted that factor (implicit variance 1.0 in
ANNUALISED units — wildly conservative at per-bar scale and meaningless
across mixed frequencies; see docs/gate_recalibration_audit_2026-06.md
§1 "Additional finding").  v2 computes V[{SR_n}] over the observed
annualised Sharpes of prior trials in the SAME strategy family, and
sets N to the per-family trial count.

The taxonomy lives in `backtest/strategy_families.json` (non-sacred,
additive-edit-autonomous).  Assignments are ex-ante — by anomaly
mechanism, never by observed Sharpe — per the audit's Appendix A.

Fallback rule (spec'd in the 2026-06-11 work order): when a family has
fewer than 2 finite-Sharpe trials, V[{SR_n}] falls back to 1.0
(conservative: a 1.0-Sharpe cross-trial sigma is wider than any
realized family sigma in trials.log to date) and a warning is emitted.
"""

from __future__ import annotations

import json
import math
import warnings
from dataclasses import dataclass
from pathlib import Path

from backtest.logs import iter_jsonl_filtered

# Path constants are module attributes so tests can monkeypatch them,
# mirroring backtest/trials.py convention.
_FAMILIES_PATH: Path = Path("backtest/strategy_families.json")

_VALID_FAMILIES: frozenset[str] = frozenset({
    "cs-momentum-rotation",
    "reversal",
    "carry",
    "trend-tsmom",
    "seasonality",
    "sentiment-attention",
    "microstructure-flow",
    "other",
})

# Conservative fallback variance (annualised-Sharpe units) when a
# family has < 2 finite-Sharpe trials.
_FALLBACK_SR_VAR: float = 1.0


class FamilyConfigError(ValueError):
    """strategy_families.json is missing, malformed, or names an
    unknown family."""


@dataclass(frozen=True)
class FamilyStats:
    """Cross-trial Sharpe statistics for one strategy's family.

    Attributes:
      family:         Family name, or None when the strategy_id has no
                      taxonomy entry (fallback semantics apply).
      n_trials:       Per-family trial count: finite-Sharpe,
                      non-superseded full_cpcv + final_gate rows in
                      trials.log whose strategy_id maps to the same
                      family.  Excludes smoke rows (diagnostic) and
                      CPCVError rows (no observed Sharpe — they are
                      not draws from the Sharpe distribution the
                      Gumbel term models).  The CALLER adds +1 for
                      the trial currently being deflated, mirroring
                      the count_trials_for_dsr pre-append convention.
      sr_var:         V[{SR_n}] — population variance of the observed
                      ANNUALISED Sharpes across those rows.  1.0
                      (fallback) when fewer than 2 finite Sharpes
                      exist or the strategy has no family entry.
      used_fallback:  True when sr_var is the 1.0 fallback.
      sharpes:        The Sharpe values the variance was computed
                      over (forensics / tests).
    """
    family: str | None
    n_trials: int
    sr_var: float
    used_fallback: bool
    sharpes: tuple[float, ...]


def load_families() -> dict:
    """Return the {strategy_id: {family, neutral?}} mapping.

    Keys starting with '_' are metadata and are stripped.
    Raises FamilyConfigError on a missing file, malformed JSON, or an
    entry naming an unknown family.
    """
    path = _FAMILIES_PATH
    if not path.exists():
        raise FamilyConfigError(
            f"strategy families file not found: {path}"
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FamilyConfigError(
            f"strategy families JSON parse error: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise FamilyConfigError("strategy families root must be an object")
    out: dict = {}
    for sid, entry in raw.items():
        if sid.startswith("_"):
            continue
        if not isinstance(entry, dict) or "family" not in entry:
            raise FamilyConfigError(
                f"families entry for {sid!r} must be an object with a "
                f"'family' key; got {entry!r}"
            )
        fam = entry["family"]
        if fam not in _VALID_FAMILIES:
            raise FamilyConfigError(
                f"families entry for {sid!r} names unknown family "
                f"{fam!r}; valid: {sorted(_VALID_FAMILIES)}"
            )
        out[sid] = entry
    return out


def family_of(strategy_id: str) -> str | None:
    """Return the family name for strategy_id, or None when absent
    from the taxonomy (callers fall back to conservative defaults and
    warn — an unmapped strategy is a bookkeeping gap, not an error)."""
    entry = load_families().get(strategy_id)
    return entry["family"] if entry else None


def is_neutral(strategy_id: str) -> bool:
    """True when the taxonomy marks strategy_id as market/delta-neutral
    (verdict baseline = PSR vs 0).  Absent entries default to False
    (directional path)."""
    entry = load_families().get(strategy_id)
    return bool(entry.get("neutral", False)) if entry else False


def family_sharpe_stats(strategy_id: str) -> FamilyStats:
    """Compute the per-family trial count and cross-trial Sharpe
    variance for `strategy_id` from trials.log.

    Row filter (per family member): trial_type in {full_cpcv,
    final_gate}, superseded_by unset, sharpe finite.  See FamilyStats
    docstring for the rationale on excluding CPCVError rows.

    Fallback (warns): no family entry, or < 2 finite Sharpes in the
    family -> sr_var = 1.0, n_trials = however many finite rows were
    found (0 or 1).
    """
    fam = family_of(strategy_id)
    if fam is None:
        warnings.warn(
            f"[families] strategy_id {strategy_id!r} has no entry in "
            f"{_FAMILIES_PATH}; falling back to V[SR]=1.0 and a "
            "per-strategy trial count of its own finite rows. Add the "
            "strategy to the taxonomy.",
            stacklevel=2,
        )
        members = {strategy_id}
    else:
        fams = load_families()
        members = {
            sid for sid, entry in fams.items() if entry["family"] == fam
        }

    # Lazy import to avoid a circular import (trials.py does not import
    # families.py; dsr.py imports both).
    from backtest import trials as _trials

    sharpes: list[float] = []
    for ev in iter_jsonl_filtered(
        _trials._TRIALS_LOG_PATH,
        lambda e: (
            e.get("strategy_id") in members
            and e.get("trial_type") in {"full_cpcv", "final_gate"}
            and not e.get("superseded_by")
        ),
    ):
        sh = ev.get("sharpe")
        if isinstance(sh, (int, float)) and math.isfinite(sh):
            sharpes.append(float(sh))

    n = len(sharpes)
    if n < 2:
        if fam is not None:
            warnings.warn(
                f"[families] family {fam!r} has {n} finite-Sharpe "
                f"trial(s) in trials.log; falling back to V[SR]=1.0 "
                "(conservative) for the eq.7 scaling.",
                stacklevel=2,
            )
        return FamilyStats(
            family=fam,
            n_trials=n,
            sr_var=_FALLBACK_SR_VAR,
            used_fallback=True,
            sharpes=tuple(sharpes),
        )

    mean = sum(sharpes) / n
    var = sum((s - mean) ** 2 for s in sharpes) / n  # population variance
    return FamilyStats(
        family=fam,
        n_trials=n,
        sr_var=float(var),
        used_fallback=False,
        sharpes=tuple(sharpes),
    )


__all__ = [
    "FamilyConfigError",
    "FamilyStats",
    "family_of",
    "family_sharpe_stats",
    "is_neutral",
    "load_families",
]

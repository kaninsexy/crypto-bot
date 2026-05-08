"""phase5/sizer.py -- quarter-Kelly sizer for Polymarket candidates.

Mirrors `.claude/agents/sizer.md` step-by-step. Pure-Python module so
the scanner orchestrator can call it without spawning the Haiku
sizer subagent for the v1 pipeline (the agent path is preserved for
future Strategist-driven sessions; the architecture allows both).

Public API
----------
    size_candidate(p_calibrated, yes_price, no_price, bankroll_usd,
                   existing_exposure_frac=0.0,
                   edge_threshold=0.02,
                   kelly_multiplier=0.25,
                   max_position_frac=0.05,
                   max_single_market_frac=0.10)
        -> SizingDecision

    SizingDecision is a dataclass carrying action ("BUY_YES" | "BUY_NO"
    | "HOLD"), size_usd, edge, kelly fractions, and a rationale string.

Why a Python module not just the agent
--------------------------------------
The sizer's logic is fully deterministic per the kelly-discipline
skill (no LLM judgment). Implementing it in Python lets us call it
directly from the scanner orchestrator + write reproducible unit
tests against it. The agent definition (.claude/agents/sizer.md)
remains the canonical spec and is preserved for the Strategist-
driven workflow path (architecture.md D.3 step 7).

Hard caps
---------
- `edge_threshold = 0.02`: HOLD if abs(edge) < 2 percentage points.
- `kelly_multiplier = 0.25`: quarter-Kelly default.
- `max_position_frac = 0.05`: 5 % of bankroll per position cap.
- `max_single_market_frac = 0.10`: 10 % combined cap on a single
  market across stacked sides (sizer caller tracks existing exposure).

These four constants are kelly-discipline defaults; overriding them
is a Strategist + human-approval call per the agent spec, not an
arbitrary parameter tweak.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional


# Kelly-discipline defaults (architecture D.3 + sizer agent spec).
DEFAULT_EDGE_THRESHOLD: float = 0.02
DEFAULT_KELLY_MULTIPLIER: float = 0.25
DEFAULT_MAX_POSITION_FRAC: float = 0.05
DEFAULT_MAX_SINGLE_MARKET_FRAC: float = 0.10


@dataclass(frozen=True)
class SizingDecision:
    """Output of `size_candidate` for one Polymarket market.

    Attributes:
      market_id:   pass-through identifier supplied by the caller.
      action:      "BUY_YES" | "BUY_NO" | "HOLD".
      size_usd:    USD notional to deploy. 0.0 for HOLD.
      size_frac:   Fraction of bankroll. 0.0 for HOLD.
      edge:        Signed edge on the chosen side (positive for action;
                   undefined sign for HOLD).
      kelly_fraction_raw:        f_kelly before quarter multiplier.
      kelly_fraction_quarter:    f_qk = 0.25 * f_kelly.
      kelly_fraction_post_caps:  size_frac after applying hard caps.
      yes_price:   Pass-through; what the market charged for YES.
      no_price:    Pass-through; what the market charged for NO.
      p_calibrated: Pass-through; the input probability that drove
                   the decision.
      rationale:   One-sentence English explanation.
    """
    market_id: str
    action: str
    size_usd: float
    size_frac: float
    edge: float
    kelly_fraction_raw: float
    kelly_fraction_quarter: float
    kelly_fraction_post_caps: float
    yes_price: float
    no_price: float
    p_calibrated: float
    rationale: str

    def to_dict(self) -> dict:
        return asdict(self)


def size_candidate(
    market_id: str,
    p_calibrated: float,
    yes_price: float,
    no_price: float,
    bankroll_usd: float,
    *,
    existing_exposure_frac: float = 0.0,
    edge_threshold: float = DEFAULT_EDGE_THRESHOLD,
    kelly_multiplier: float = DEFAULT_KELLY_MULTIPLIER,
    max_position_frac: float = DEFAULT_MAX_POSITION_FRAC,
    max_single_market_frac: float = DEFAULT_MAX_SINGLE_MARKET_FRAC,
) -> SizingDecision:
    """Quarter-Kelly position sizing for one Polymarket binary market.

    Args:
        market_id:                pass-through identifier.
        p_calibrated:             probability of YES per the calibrator
                                  / research aggregator. In [0, 1].
        yes_price:                what the market charges to BUY YES (in [0, 1]).
        no_price:                 what the market charges to BUY NO  (in [0, 1]).
                                  Typically yes_price + no_price ~= 1.
        bankroll_usd:             total bankroll the cap percentages
                                  apply to.
        existing_exposure_frac:   fraction of bankroll already deployed
                                  on this same market (across previously
                                  stacked sides). Used in the
                                  max_single_market_frac cap.
        edge_threshold:           HOLD if abs(edge) < this. Default 0.02.
        kelly_multiplier:         f_qk = multiplier * f_kelly. Default 0.25.
        max_position_frac:        per-position bankroll cap. Default 0.05.
        max_single_market_frac:   total per-market bankroll cap across
                                  stacked sides. Default 0.10.

    Returns:
        SizingDecision capturing the action + size + rationale.
    """
    # Validate inputs strictly: bad inputs are caller bugs, not HOLD signals.
    if not (0.0 <= p_calibrated <= 1.0):
        raise ValueError(
            f"p_calibrated must be in [0, 1]; got {p_calibrated!r}"
        )
    if not (0.0 < yes_price < 1.0):
        raise ValueError(
            f"yes_price must be in (0, 1); got {yes_price!r}"
        )
    if not (0.0 < no_price < 1.0):
        raise ValueError(
            f"no_price must be in (0, 1); got {no_price!r}"
        )
    if bankroll_usd <= 0:
        raise ValueError(
            f"bankroll_usd must be positive; got {bankroll_usd!r}"
        )

    # Step 2: edges per side. Pick the side with the larger SIGNED edge
    # (not abs) -- when YES + NO prices sum to ~1.0, edge_yes ~= -edge_no
    # by construction, so abs is degenerate. The intent of the agent
    # spec is to pick the profitable side; max(raw_edge) does that
    # whether prices sum to 1 (typical) or sum to >1 (with spread).
    edge_yes = p_calibrated - yes_price
    edge_no = (1.0 - p_calibrated) - no_price

    if edge_yes >= edge_no:
        side = "YES"
        chosen_edge = edge_yes
        chosen_price = yes_price
    else:
        side = "NO"
        chosen_edge = edge_no
        chosen_price = no_price

    # Step 2 closing rule: if larger edge is negative -> HOLD.
    if chosen_edge <= 0:
        return SizingDecision(
            market_id=market_id,
            action="HOLD",
            size_usd=0.0,
            size_frac=0.0,
            edge=chosen_edge,
            kelly_fraction_raw=0.0,
            kelly_fraction_quarter=0.0,
            kelly_fraction_post_caps=0.0,
            yes_price=yes_price,
            no_price=no_price,
            p_calibrated=p_calibrated,
            rationale=(
                f"both sides priced at-or-above research probability; "
                f"max edge={chosen_edge:+.4f}"
            ),
        )

    # Step 3: edge threshold.
    if chosen_edge < edge_threshold:
        return SizingDecision(
            market_id=market_id,
            action="HOLD",
            size_usd=0.0,
            size_frac=0.0,
            edge=chosen_edge,
            kelly_fraction_raw=0.0,
            kelly_fraction_quarter=0.0,
            kelly_fraction_post_caps=0.0,
            yes_price=yes_price,
            no_price=no_price,
            p_calibrated=p_calibrated,
            rationale=(
                f"edge {chosen_edge:.4f} below {edge_threshold:.2%} "
                f"threshold (kelly-discipline default)"
            ),
        )

    # Step 4: Kelly fraction.
    # Binary-outcome formula: f = edge / (price * (1 - price)).
    denom = chosen_price * (1.0 - chosen_price)
    if denom <= 0:
        raise ValueError(
            f"chosen_price {chosen_price} produced non-positive Kelly "
            f"denominator; should be unreachable given the [0,1] bounds"
        )
    f_kelly = chosen_edge / denom

    # Step 5: quarter-Kelly multiplier.
    f_qk = kelly_multiplier * f_kelly

    # Step 6: hard caps.
    cap_position = max_position_frac
    cap_market = max_single_market_frac - existing_exposure_frac
    size_frac = min(f_qk, cap_position, cap_market)

    if size_frac <= 0:
        return SizingDecision(
            market_id=market_id,
            action="HOLD",
            size_usd=0.0,
            size_frac=0.0,
            edge=chosen_edge,
            kelly_fraction_raw=f_kelly,
            kelly_fraction_quarter=f_qk,
            kelly_fraction_post_caps=size_frac,
            yes_price=yes_price,
            no_price=no_price,
            p_calibrated=p_calibrated,
            rationale=(
                f"cap-bound: existing single-market exposure "
                f"{existing_exposure_frac:.2%} consumes the "
                f"{max_single_market_frac:.0%} slot"
            ),
        )

    # Step 7: USD size.
    size_usd = size_frac * bankroll_usd

    # Compose rationale.
    binding_cap = "kelly"
    if size_frac == cap_position and f_qk > cap_position:
        binding_cap = f"position-cap ({max_position_frac:.0%})"
    elif size_frac == cap_market and f_qk > cap_market:
        binding_cap = (
            f"single-market-cap ({max_single_market_frac:.0%} "
            f"minus existing {existing_exposure_frac:.2%})"
        )

    rationale = (
        f"BUY_{side}: edge {chosen_edge:+.4f} at price "
        f"{chosen_price:.4f}; quarter-Kelly {f_qk:.4f} (raw "
        f"{f_kelly:.4f}); binding={binding_cap}"
    )

    return SizingDecision(
        market_id=market_id,
        action=f"BUY_{side}",
        size_usd=size_usd,
        size_frac=size_frac,
        edge=chosen_edge,
        kelly_fraction_raw=f_kelly,
        kelly_fraction_quarter=f_qk,
        kelly_fraction_post_caps=size_frac,
        yes_price=yes_price,
        no_price=no_price,
        p_calibrated=p_calibrated,
        rationale=rationale,
    )

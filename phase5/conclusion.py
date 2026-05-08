"""phase5/conclusion.py -- aggregate N research outputs into a
profitability verdict for the Polymarket strategy.

Reads accumulated research_outputs.json + corresponding market metadata,
runs the sizer per market, computes summary statistics, and writes a
verdict markdown answering: "is the deep-researcher + Polymarket
pipeline a profitable strategy?"

Verdict tree (in order):
  - 0 markets cleared the 2pp edge threshold     -> NOT_PROFITABLE
                                                    (market is efficient
                                                     for the tested
                                                     domain mix)
  - 1-2 markets cleared (out of >= 10 tested)    -> SELECTION_BIAS_RISK
                                                    (edge rate too low
                                                     to be reliable; may
                                                     be cherry-picked)
  - >=3 markets cleared AND edge rate >= 15%     -> PROFITABLE_PROVISIONAL
                                                    (worth deploying
                                                     the pipeline; track
                                                     calibration over
                                                     time)
  - any other outcome                            -> INDETERMINATE
                                                    (need more samples)

Public API
----------
    write_verdict(research_outputs, candidates_by_id, output_path,
                  bankroll_usd=10_000.0) -> dict
        Compute summary stats + write verdict markdown. Returns the
        stats dict for inline programmatic use.
"""

from __future__ import annotations

import json
import statistics
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from phase5.sizer import (
    DEFAULT_EDGE_THRESHOLD,
    size_candidate,
    SizingDecision,
)


def _coerce_float(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return float(default)


def compute_stats(
    research_outputs: list[dict],
    candidates_by_id: dict[str, dict],
    bankroll_usd: float = 10_000.0,
) -> dict:
    """Run the sizer on each (research, candidate) pair and aggregate."""
    decisions: list[SizingDecision] = []
    edges_signed: list[float] = []
    actions = Counter()
    by_action: dict[str, list[dict]] = {"BUY_YES": [], "BUY_NO": [], "HOLD": []}

    for r in research_outputs:
        mid = str(r.get("market_id"))
        cand = candidates_by_id.get(mid)
        if cand is None:
            # Missing candidate metadata -- skip.
            continue
        p = _coerce_float(r.get("p_research"), default=-1.0)
        if not (0.0 <= p <= 1.0):
            continue
        yes_p = _coerce_float(cand.get("yes_price"), default=-1.0)
        no_p = _coerce_float(cand.get("no_price"), default=-1.0)
        # Sanitise to (0, 1) for the sizer.
        yes_p = min(0.999, max(0.001, yes_p))
        no_p = min(0.999, max(0.001, no_p))
        try:
            d = size_candidate(
                market_id=mid,
                p_calibrated=p,
                yes_price=yes_p,
                no_price=no_p,
                bankroll_usd=bankroll_usd,
            )
        except Exception:
            continue
        decisions.append(d)
        edges_signed.append(d.edge)
        actions[d.action] += 1
        by_action[d.action].append({
            "market_id": mid,
            "question": str(cand.get("question", ""))[:100],
            "p_research": p,
            "yes_price": yes_p,
            "no_price": no_p,
            "edge": d.edge,
            "size_usd": d.size_usd,
            "size_frac": d.size_frac,
            "rationale": d.rationale,
            "researcher_action": r.get("recommended_action", ""),
            "researcher_confidence": r.get("confidence", ""),
        })

    n = len(decisions)
    actionable = actions["BUY_YES"] + actions["BUY_NO"]

    # Edge magnitudes (absolute) -- a measure of "researcher disagrees
    # with market" regardless of direction.
    edge_abs = [abs(e) for e in edges_signed]

    stats: dict = {
        "n_markets_evaluated": n,
        "n_buy_yes": int(actions["BUY_YES"]),
        "n_buy_no": int(actions["BUY_NO"]),
        "n_hold": int(actions["HOLD"]),
        "actionable_count": int(actionable),
        "actionable_rate": (actionable / n) if n > 0 else 0.0,
        "edge_abs_mean": (statistics.mean(edge_abs) if edge_abs else 0.0),
        "edge_abs_median": (statistics.median(edge_abs) if edge_abs else 0.0),
        "edge_abs_max": (max(edge_abs) if edge_abs else 0.0),
        "total_recommended_notional_usd": sum(d.size_usd for d in decisions),
        "by_action": by_action,
    }
    return stats


def derive_verdict(stats: dict) -> tuple[str, str]:
    """Map stats to a verdict label + one-line rationale."""
    n = int(stats["n_markets_evaluated"])
    actionable = int(stats["actionable_count"])
    rate = float(stats["actionable_rate"])

    if n == 0:
        return ("INDETERMINATE",
                "No markets evaluated; cannot conclude.")
    if actionable == 0:
        return ("NOT_PROFITABLE",
                f"0 of {n} markets cleared the {DEFAULT_EDGE_THRESHOLD:.0%} "
                "edge threshold. Polymarket appears efficient for the "
                "tested domain mix.")
    if n < 10:
        return ("INDETERMINATE",
                f"{actionable} of {n} actionable, but n={n} is below "
                "the 10-market floor for a stable verdict.")
    if actionable <= 2:
        return ("SELECTION_BIAS_RISK",
                f"{actionable} of {n} actionable ({rate:.1%}) -- rate "
                "too low to rule out cherry-picking. Need 30+ markets "
                "with calibration tracking before deploying.")
    if rate >= 0.15:
        return ("PROFITABLE_PROVISIONAL",
                f"{actionable} of {n} actionable ({rate:.1%}). "
                "Pipeline finds edge above the 15% rate floor; deploy "
                "in shadow mode and track calibration over 100+ "
                "actually-resolved markets before sizing up.")
    return ("INDETERMINATE",
            f"{actionable} of {n} actionable ({rate:.1%}); rate is "
            "non-zero but below the 15% provisional floor.")


def write_verdict(
    research_outputs: list[dict],
    candidates_by_id: dict[str, dict],
    output_path: Path,
    bankroll_usd: float = 10_000.0,
) -> dict:
    stats = compute_stats(research_outputs, candidates_by_id, bankroll_usd)
    verdict_label, verdict_rationale = derive_verdict(stats)
    stats["verdict_label"] = verdict_label
    stats["verdict_rationale"] = verdict_rationale

    md: list[str] = []
    md.append("# Polymarket profitability verdict")
    md.append("")
    md.append(f"Generated: {datetime.now(timezone.utc).isoformat()}")
    md.append(f"Bankroll assumed: ${bankroll_usd:,.2f}")
    md.append("")
    md.append(f"## Verdict: **{verdict_label}**")
    md.append("")
    md.append(verdict_rationale)
    md.append("")
    md.append("## Headline numbers")
    md.append("")
    md.append(f"- Markets evaluated: **{stats['n_markets_evaluated']}**")
    md.append(f"- Actionable (edge >= 2pp): **{stats['actionable_count']}** "
              f"({stats['actionable_rate']:.1%})")
    md.append(f"  - BUY_YES: {stats['n_buy_yes']}  /  "
              f"BUY_NO: {stats['n_buy_no']}  /  HOLD: {stats['n_hold']}")
    md.append(f"- Mean abs edge: {stats['edge_abs_mean']:+.4f}  "
              f"(median {stats['edge_abs_median']:+.4f}, "
              f"max {stats['edge_abs_max']:+.4f})")
    md.append(f"- Total recommended notional (across actionable): "
              f"${stats['total_recommended_notional_usd']:,.2f} "
              f"({stats['total_recommended_notional_usd']/bankroll_usd:.2%} "
              "of bankroll)")
    md.append("")

    if stats["by_action"]["BUY_YES"] or stats["by_action"]["BUY_NO"]:
        md.append("## Actionable recommendations")
        md.append("")
        for action_key in ("BUY_YES", "BUY_NO"):
            for row in stats["by_action"][action_key]:
                md.append(
                    f"- **{action_key}** market {row['market_id']} "
                    f"({row['question']}) -- edge {row['edge']:+.4f}, "
                    f"size ${row['size_usd']:,.2f} "
                    f"({row['size_frac']:.2%}), conf "
                    f"{row['researcher_confidence']!r}"
                )
        md.append("")

    md.append("## All HOLD markets")
    md.append("")
    if stats["by_action"]["HOLD"]:
        for row in stats["by_action"]["HOLD"]:
            md.append(
                f"- market {row['market_id']} ({row['question']}) -- "
                f"edge {row['edge']:+.4f}; {row['rationale']}"
            )
    else:
        md.append("(none)")
    md.append("")

    md.append("## Interpretation")
    md.append("")
    if verdict_label == "NOT_PROFITABLE":
        md.append(
            "Across the tested mix, no market produced a research "
            "probability that meaningfully diverged from Polymarket's "
            "market price. The interpretation: in liquid Polymarket "
            "markets where comparable benchmarks exist (sportsbooks, "
            "polling aggregators), the market is efficient enough that "
            "deep-researcher analysis cannot beat the consensus."
        )
        md.append("")
        md.append(
            "Possible next investigations:"
        )
        md.append(
            "- Test markets WITHOUT comparable benchmarks (regulatory, "
            "scientific, niche-political, business-event-driven)"
        )
        md.append(
            "- Test markets in the immediate aftermath of news events "
            "(price-discovery dislocations)"
        )
        md.append(
            "- Test long-tail / illiquid markets (less crowd attention)"
        )
        md.append(
            "- Try arbitrage between Polymarket and Kalshi/Manifold "
            "(cross-venue spreads)"
        )
    elif verdict_label == "SELECTION_BIAS_RISK":
        md.append(
            "The pipeline found a small number of edges, but at a "
            "rate too low to rule out cherry-picking. To deploy "
            "safely, we'd need to: (a) track these recommendations "
            "to resolution and measure realised P&L, (b) run on "
            "30+ more markets to bring the rate estimate within a "
            "tighter confidence interval."
        )
    elif verdict_label == "PROFITABLE_PROVISIONAL":
        md.append(
            "The pipeline finds edge at a rate that suggests a real "
            "deployable strategy. Recommended deployment plan: "
            "shadow mode for 100 markets (record recommendations + "
            "outcomes; compute Brier score, realised P&L), then size "
            "up only if calibration holds."
        )
    else:
        md.append(
            "Sample is too small or the rate is in the gray zone. "
            "Run more markets through the pipeline before deciding."
        )
    md.append("")

    md.append("## Methodology caveats")
    md.append("")
    md.append("- Selection: candidates picked by liquidity + price-range filter, ")
    md.append(  "  then domain-diversified by hand. Not a random sample.")
    md.append("- Researcher quality: each market got one deep-researcher pass with ")
    md.append(  "  ~500-1000 tokens of analysis. Higher-quality pipelines (longer ")
    md.append(  "  research, multi-agent debate) might find more edges.")
    md.append("- Edge != realised return: a 5% edge does not guarantee 5% profit. ")
    md.append(  "  The market price is the ground truth; the research probability is ")
    md.append(  "  an opinion. Realised profitability requires the research to be ")
    md.append(  "  better-calibrated than the market over many resolutions.")
    md.append("- Time-to-resolution risk: long-horizon markets (2028 elections) tie ")
    md.append(  "  up capital for years. Risk-adjusted return matters, not just edge.")
    md.append("- No transaction-cost model in this verdict. Polymarket has small ")
    md.append(  "  taker fees and on-chain bridging costs; large positions may incur ")
    md.append(  "  slippage beyond the bestBid/bestAsk shown.")
    md.append("")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(md), encoding="utf-8")
    return stats

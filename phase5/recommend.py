"""phase5/recommend.py -- aggregate scanner + research outputs into a
Polymarket recommendation packet.

End-to-end glue: takes a list of scanner candidates (from
`scripts/run_polymarket_scan.py`) and a list of researcher outputs
(JSON dicts produced by deep-researcher subagents per the
Polymarket-research prompt template), runs the sizer for each, and
writes a markdown recommendation to a target path.

The output markdown is what the operator reads to decide whether
to place orders manually -- this is the recommend-only execution
boundary architecture.md D.3 calls out.

Public API
----------
    aggregate_recommendations(candidates, research_outputs,
                              bankroll_usd, output_path)
        -> list[dict]  (per-candidate recommendation dicts;
                        also writes markdown to output_path)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from phase5.sizer import size_candidate, SizingDecision


def _coalesce_research(research_outputs: list[dict]) -> dict[str, dict]:
    """Index research outputs by market_id."""
    out: dict[str, dict] = {}
    for r in research_outputs:
        mid = str(r.get("market_id"))
        if not mid:
            continue
        out[mid] = r
    return out


def _format_decision_md(
    candidate: dict, research: dict | None,
    decision: SizingDecision | None, error: str | None,
) -> str:
    """One markdown section per candidate."""
    mid = candidate.get("market_id", "?")
    q = candidate.get("question", "?")
    yes_p = candidate.get("yes_price", 0.0)
    no_p = candidate.get("no_price", 0.0)
    liq = candidate.get("liquidity_usd", 0.0)
    ttr = candidate.get("time_to_resolution_h", 0.0)
    end = candidate.get("end_date_iso", "")

    lines: list[str] = []
    lines.append(f"### Market {mid} -- {q}")
    lines.append("")
    lines.append(f"- Resolution: {end} ({ttr:.0f}h, {ttr/24:.1f}d away)")
    lines.append(f"- Liquidity: ${liq:,.0f}")
    lines.append(f"- Market YES: {yes_p:.4f}  /  Market NO: {no_p:.4f}")
    lines.append("")

    if error:
        lines.append(f"**Status:** error -- {error}")
        lines.append("")
        return "\n".join(lines)

    if research is None:
        lines.append(
            "**Status:** no research output (deep-researcher pending or failed)"
        )
        lines.append("")
        return "\n".join(lines)

    p = research.get("p_research")
    p_low = research.get("p_low")
    p_high = research.get("p_high")
    conf = research.get("confidence", "?")
    rec_act = research.get("recommended_action", "?")
    research_rat = research.get("rationale", "")
    evidence = research.get("key_evidence", []) or []

    lines.append("#### Research")
    lines.append("")
    if p is not None:
        ci = ""
        if p_low is not None and p_high is not None:
            ci = f" (CI {p_low:.2f}-{p_high:.2f}, {conf})"
        lines.append(f"- p_research: **{p:.4f}**{ci}")
    lines.append(f"- Researcher's call: {rec_act}")
    if research_rat:
        lines.append(f"- Researcher rationale: {research_rat}")
    if evidence:
        lines.append("- Key evidence:")
        for e in evidence[:5]:
            lines.append(f"    - {e}")
    lines.append("")

    if decision is None:
        lines.append("**Status:** sizer not run (research probability missing)")
        lines.append("")
        return "\n".join(lines)

    lines.append("#### Sized recommendation (quarter-Kelly w/ caps)")
    lines.append("")
    lines.append(f"- Action: **{decision.action}**")
    if decision.action != "HOLD":
        lines.append(
            f"- Size: ${decision.size_usd:,.2f} "
            f"({decision.size_frac:.4f} of bankroll)"
        )
    lines.append(f"- Edge: {decision.edge:+.4f}")
    lines.append(
        f"- Kelly: raw={decision.kelly_fraction_raw:.4f}, "
        f"quarter={decision.kelly_fraction_quarter:.4f}, "
        f"post-caps={decision.kelly_fraction_post_caps:.4f}"
    )
    lines.append(f"- Rationale: {decision.rationale}")
    lines.append("")
    return "\n".join(lines)


def aggregate_recommendations(
    candidates: list[dict],
    research_outputs: list[dict],
    bankroll_usd: float,
    output_path: Path,
) -> list[dict]:
    """Run the sizer for each candidate that has a research output;
    write a markdown packet to `output_path`. Returns per-candidate
    dicts (sized or HOLD or pending).
    """
    research_by_id = _coalesce_research(research_outputs)
    results: list[dict] = []
    md_sections: list[str] = []

    # Header.
    now = datetime.now(timezone.utc).isoformat()
    md_sections.append(f"# Polymarket scan -- recommendation packet")
    md_sections.append("")
    md_sections.append(f"Generated: {now}  ")
    md_sections.append(f"Bankroll assumed: ${bankroll_usd:,.2f}  ")
    md_sections.append(
        f"Candidates scanned: {len(candidates)}  "
        f"with research: {len(research_outputs)}"
    )
    md_sections.append("")
    md_sections.append("**Recommend-only.** Phase 5 architecture D.3: ")
    md_sections.append("the bot does NOT place orders. The operator reviews ")
    md_sections.append("the recommendations below and executes manually on ")
    md_sections.append("Polymarket.")
    md_sections.append("")
    md_sections.append("---")
    md_sections.append("")

    for c in candidates:
        mid = str(c.get("market_id"))
        research = research_by_id.get(mid)
        decision: SizingDecision | None = None
        error: str | None = None

        if research is not None:
            p = research.get("p_research")
            try:
                if p is None:
                    raise ValueError("research output missing p_research")
                p = float(p)
                yes_p = float(c.get("yes_price") or 0.0)
                no_p = float(c.get("no_price") or 0.0)
                # Sanitize edge cases that would crash sizer.
                yes_p = min(0.999, max(0.001, yes_p))
                no_p = min(0.999, max(0.001, no_p))
                p = min(1.0, max(0.0, p))
                decision = size_candidate(
                    market_id=mid,
                    p_calibrated=p,
                    yes_price=yes_p,
                    no_price=no_p,
                    bankroll_usd=bankroll_usd,
                )
            except Exception as e:
                error = f"sizer raised {e.__class__.__name__}: {e}"

        md_sections.append(
            _format_decision_md(c, research, decision, error)
        )
        results.append({
            "market_id": mid,
            "candidate": c,
            "research": research,
            "decision": (decision.to_dict() if decision is not None else None),
            "error": error,
        })

    # Footer summary.
    actionable = [r for r in results
                  if r.get("decision") and r["decision"]["action"] != "HOLD"]
    md_sections.append("---")
    md_sections.append("")
    md_sections.append("## Summary")
    md_sections.append("")
    md_sections.append(
        f"- {len(actionable)} actionable recommendation(s); "
        f"{len(results) - len(actionable)} HOLD/pending."
    )
    if actionable:
        total = sum(r["decision"]["size_usd"] for r in actionable)
        md_sections.append(
            f"- Total recommended notional: ${total:,.2f} "
            f"({total/bankroll_usd:.2%} of bankroll)"
        )
    md_sections.append("")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(md_sections), encoding="utf-8")

    return results


# -- CLI ---------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="Aggregate scanner candidates + deep-researcher outputs "
                    "into a Polymarket recommendation packet (markdown).",
    )
    parser.add_argument("--candidates", required=True,
                        help="Path to candidates JSON (from run_polymarket_scan.py)")
    parser.add_argument("--research", required=True,
                        help="Path to research JSON list (one entry per "
                             "market_id with p_research field)")
    parser.add_argument("--bankroll-usd", type=float, default=10_000.0)
    parser.add_argument("--output", required=True,
                        help="Output markdown path")
    args = parser.parse_args()

    candidates = json.loads(Path(args.candidates).read_text(encoding="utf-8"))
    research = json.loads(Path(args.research).read_text(encoding="utf-8"))
    if not isinstance(research, list):
        research = [research]

    results = aggregate_recommendations(
        candidates=candidates,
        research_outputs=research,
        bankroll_usd=args.bankroll_usd,
        output_path=Path(args.output),
    )
    print(f"Wrote {len(results)} recommendation rows to {args.output}",
          file=sys.stderr)
    print(json.dumps(results, indent=2, default=str))

"""scripts/propose_next_variation.py — Phase 4.D proposal agent.

Reads the current trial state (trials.log, strategies.md, research/,
proposal_history.json), asks an LLM with web search to discover
strategy categories with peer-reviewed crypto evidence, deep-dives
each candidate for citations, and queues qualifying proposals into
backtest/trial_queue.json.

Primary API: OpenRouter (Gemini 2.5 Pro :online — native grounded search).
Fallback   : Anthropic (Claude Sonnet 4 + web_search_20250305 tool).

Quality gate: a proposal needs `overall_quality >= 3.0` (avg of >= 3
qualifying crypto-specific citations, scored 1-5) to enter the queue.
Items enter with `needs_trial_script: true` — the orchestrator skips
them until a human + Claude Code session writes the trial script.

Run:
  python scripts/propose_next_variation.py [--dry-run] [--force]

  --dry-run: no queue write, missing API keys ignored
  --force  : run even if queue still has pending items

The proposal agent runs silently (no email).  Notifications about
proposal-agent state are emitted by `scripts/run_trial_queue.py`,
which is the only Resend caller in the trial-queue pipeline.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path


# ── Path constants ──────────────────────────────────────────────────────────

ROOT = Path(__file__).resolve().parents[1]
QUEUE_PATH = ROOT / "backtest" / "trial_queue.json"
# Runtime-state file written by the orchestrator (file-split design,
# 2026-05-06): trial_queue.json is definitions-only, runtime status
# lives in the gitignored state file. Proposal agent reads it for
# the pending-count check so it does not double-count items the
# orchestrator has already moved off "queued".
STATE_PATH = ROOT / "backtest" / "trial_queue_state.json"
HISTORY_PATH = ROOT / "backtest" / "proposal_history.json"
TRIALS_LOG_PATH = ROOT / "backtest" / "trials.log"
STRATEGIES_MD_PATH = ROOT / "docs" / "strategies.md"
RESEARCH_DIR = ROOT / "research"

OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODEL = "google/gemini-2.5-pro:online"
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = "claude-sonnet-4-20250514"

MIN_CITATION_QUALITY = 3.0
MAX_QUEUED_ITEMS = int(os.environ.get("TRIAL_QUEUE_MAX_QUEUED_ITEMS", "10"))

# Backlog cap: if the merged queue already has this many items in
# `status="queued"` (whether they need a trial script or are
# build-ready), skip proposal research entirely to avoid further
# backlog while the orchestrator drains the existing queue. Each
# queued item consumes future build/run capacity, so the cap counts
# both needs_trial_script=True and build-ready items equally.
MAX_QUEUED_BEFORE_SKIP = 6

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")


# ── Time helper ─────────────────────────────────────────────────────────────

def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── State I/O ───────────────────────────────────────────────────────────────

def load_queue() -> dict:
    if not QUEUE_PATH.exists():
        return {"schema_version": 1, "queue": []}
    text = QUEUE_PATH.read_text(encoding="utf-8").strip()
    if not text:
        return {"schema_version": 1, "queue": []}
    return json.loads(text)


def save_queue(data: dict) -> None:
    tmp = QUEUE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, QUEUE_PATH)


def _load_state_statuses() -> dict[str, str]:
    """Return {item_id: status} from trial_queue_state.json.

    Empty dict if the file is absent or unreadable. The orchestrator
    is the authoritative writer of runtime status (file-split design,
    2026-05-06): definitions-file `status` is set once by
    build_queue_item to "queued" and never updated thereafter, so
    consulting state.json is the only way to see the true current
    status for items the orchestrator has touched.
    """
    if not STATE_PATH.exists():
        return {}
    try:
        text = STATE_PATH.read_text(encoding="utf-8").strip()
        if not text:
            return {}
        data = json.loads(text)
    except (OSError, json.JSONDecodeError):
        return {}
    items = data.get("items", {})
    if not isinstance(items, dict):
        return {}
    out: dict[str, str] = {}
    for item_id, entry in items.items():
        if isinstance(entry, dict) and isinstance(entry.get("status"), str):
            out[str(item_id)] = entry["status"]
    return out


def _effective_status(
    item: dict, state_statuses: dict[str, str],
) -> str | None:
    """Return the runtime-authoritative status for a queue item.

    State file wins when present; otherwise falls back to the
    definitions-file status. Returns None if neither source carries
    a string.
    """
    item_id = item.get("id")
    if isinstance(item_id, str) and item_id in state_statuses:
        return state_statuses[item_id]
    s = item.get("status")
    return s if isinstance(s, str) else None


def load_history() -> dict:
    if not HISTORY_PATH.exists():
        return {
            "schema_version": 1,
            "searched_domains": [],
            "proposed_variations": [],
        }
    text = HISTORY_PATH.read_text(encoding="utf-8").strip()
    if not text:
        return {
            "schema_version": 1,
            "searched_domains": [],
            "proposed_variations": [],
        }
    return json.loads(text)


def save_history(data: dict) -> None:
    tmp = HISTORY_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, HISTORY_PATH)


# ── Context loader ──────────────────────────────────────────────────────────

def _load_trials_brief() -> list[tuple[str, str, str]]:
    """Return (strategy_id, variation_id, verdict) tuples from trials.log."""
    if not TRIALS_LOG_PATH.exists():
        return []
    # Lazy import: backtest.trials is part of the sacred harness;
    # we read its public API only.
    sys.path.insert(0, str(ROOT))
    from backtest.trials import read_trials  # noqa: E402

    out: list[tuple[str, str, str]] = []
    for ev in read_trials():
        sid = ev.get("strategy_id", "?")
        vid = ev.get("variation_id", "?")
        verdict = ev.get("verdict") or ev.get("trial_type") or "unknown"
        out.append((sid, vid, verdict))
    return out


def _load_strategies_oneliners() -> list[tuple[str, str]]:
    """Return (strategy_name, first_bullet_text) per `### NAME` section.

    'first_bullet_text' is the first `- **Phase 3c verdict ...**` line
    or the first `-` bullet, whichever appears first.
    """
    if not STRATEGIES_MD_PATH.exists():
        return []
    text = STRATEGIES_MD_PATH.read_text(encoding="utf-8")
    out: list[tuple[str, str]] = []
    cur_name: str | None = None
    seen_first_bullet = False
    for raw in text.splitlines():
        line = raw.rstrip()
        m = re.match(r"^###\s+([^\n]+)$", line)
        if m:
            cur_name = m.group(1).strip()
            seen_first_bullet = False
            continue
        if cur_name is None or seen_first_bullet:
            continue
        if line.lstrip().startswith("- "):
            out.append((cur_name, line.strip()))
            seen_first_bullet = True
    return out


def _load_literature_categories() -> list[str]:
    if not RESEARCH_DIR.exists():
        return []
    cats: list[str] = []
    for p in sorted(RESEARCH_DIR.glob("*.md")):
        cats.append(p.stem)
    return cats


def load_tested_context() -> str:
    trials = _load_trials_brief()
    oneliners = _load_strategies_oneliners()
    cats = _load_literature_categories()
    history = load_history()

    parts: list[str] = []

    parts.append("=== ALREADY TESTED (do not re-propose) ===")
    if trials:
        for sid, vid, verdict in trials:
            parts.append(f"  {sid} / {vid} / {verdict}")
    else:
        parts.append("  (none)")

    parts.append("")
    parts.append("=== STRATEGY CATEGORIES ALREADY EXPLORED ===")
    explored: set[str] = set()
    for c in cats:
        explored.add(c)
        parts.append(f"  literature: {c}")
    for d in history.get("searched_domains", []):
        explored.add(d)
        parts.append(f"  prior search: {d}")
    if not explored:
        parts.append("  (none)")

    parts.append("")
    parts.append("=== RETIREMENT REASONS (for context) ===")
    if oneliners:
        for name, bullet in oneliners:
            parts.append(f"  {name}: {bullet}")
    else:
        parts.append("  (none)")

    return "\n".join(parts)


# ── LLM clients ─────────────────────────────────────────────────────────────

def _http_post_json(url: str, headers: dict, body: dict, timeout: int):
    """Thin wrapper around requests.post; lazy-imports requests."""
    import requests  # noqa: WPS433 — agent-only dep

    return requests.post(url, headers=headers, json=body, timeout=timeout)


def call_openrouter(prompt: str, system: str) -> str | None:
    if not OPENROUTER_API_KEY:
        return None
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "HTTP-Referer": "crypto-bot-proposal-agent",
        "X-Title": "crypto-bot",
        "Content-Type": "application/json",
    }
    body = {
        "model": OPENROUTER_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    }
    try:
        resp = _http_post_json(OPENROUTER_API_URL, headers, body, 120)
    except Exception as e:  # noqa: BLE001
        print(f"[openrouter] exception: {e}", file=sys.stderr)
        return None
    if resp.status_code != 200:
        print(
            f"[openrouter] HTTP {resp.status_code}: {resp.text[:300]}",
            file=sys.stderr,
        )
        return None
    try:
        data = resp.json()
        return data["choices"][0]["message"]["content"]
    except Exception as e:  # noqa: BLE001
        print(f"[openrouter] parse error: {e}", file=sys.stderr)
        return None


def call_anthropic(prompt: str, system: str) -> str | None:
    if not ANTHROPIC_API_KEY:
        return None
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "interleaved-thinking-2025-05-07",
        "Content-Type": "application/json",
    }
    body = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 4096,
        "tools": [{"type": "web_search_20250305", "name": "web_search"}],
        "system": system,
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        resp = _http_post_json(ANTHROPIC_API_URL, headers, body, 180)
    except Exception as e:  # noqa: BLE001
        print(f"[anthropic] exception: {e}", file=sys.stderr)
        return None
    if resp.status_code != 200:
        print(
            f"[anthropic] HTTP {resp.status_code}: {resp.text[:300]}",
            file=sys.stderr,
        )
        return None
    try:
        data = resp.json()
        chunks: list[str] = []
        for block in data.get("content", []):
            if block.get("type") == "text":
                chunks.append(block.get("text", ""))
        return "".join(chunks) or None
    except Exception as e:  # noqa: BLE001
        print(f"[anthropic] parse error: {e}", file=sys.stderr)
        return None


def call_llm(prompt: str, system: str) -> str | None:
    out = call_openrouter(prompt, system)
    if out is not None:
        return out
    print("[propose] OpenRouter failed, trying Anthropic fallback")
    return call_anthropic(prompt, system)


# ── JSON salvage ────────────────────────────────────────────────────────────

_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?|\n?```$", re.MULTILINE)


def _extract_json_blob(raw: str) -> str:
    text = raw.strip()
    text = _FENCE_RE.sub("", text).strip()
    return text


def _parse_json_array(raw: str) -> list:
    blob = _extract_json_blob(raw)
    try:
        parsed = json.loads(blob)
    except json.JSONDecodeError:
        m = re.search(r"\[.*\]", blob, flags=re.DOTALL)
        if m is None:
            return []
        try:
            parsed = json.loads(m.group(0))
        except json.JSONDecodeError:
            return []
    if not isinstance(parsed, list):
        return []
    return [str(x).strip() for x in parsed if isinstance(x, (str,))]


def _parse_json_object(raw: str) -> dict | None:
    blob = _extract_json_blob(raw)
    try:
        parsed = json.loads(blob)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", blob, flags=re.DOTALL)
        if m is None:
            return None
        try:
            parsed = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return parsed if isinstance(parsed, dict) else None


# ── Phase 1: discovery ──────────────────────────────────────────────────────

_SYSTEM = (
    "You are a crypto trading research agent. Your job is to "
    "identify trading strategy CATEGORIES that have peer-reviewed "
    "empirical evidence of edge in cryptocurrency perpetual futures "
    "markets. You must search the web to find this evidence. Be "
    "thorough and creative — look beyond common archetypes."
)


def discover_candidate_domains(context: str) -> list[str]:
    user = (
        f"{context}\n\n"
        "Search for trading strategy categories that meet ALL of:\n"
        "1. Have at least one peer-reviewed paper or SSRN working paper "
        "showing positive risk-adjusted returns in crypto markets "
        "(2018 or later data preferred)\n"
        "2. Are NOT already in the 'already explored' list above\n"
        "3. Are implementable as a systematic rule-based strategy on "
        "crypto perpetual futures (OKX USDT-M)\n"
        "4. Do not require order book depth or sub-second execution\n\n"
        "Return ONLY a JSON array of 8-12 strategy category names "
        "as compact CamelCase identifiers (2-4 words concatenated, no "
        "spaces, no punctuation, no URLs, no paper titles). Each name "
        "must match the regex ^[A-Z][A-Za-z0-9]+$ -- it will be used "
        "directly as a Python class name prefix.\n"
        "Example: [\"CrossSectionalReversal\", \"AttentionMomentum\", "
        "\"VolatilityRiskPremium\", \"ExchangeNetflowReversal\", "
        "\"FundingRateCarry\", ...]\n"
        "No explanation, just the JSON array. Do NOT return URL "
        "hostnames (e.g. papers.ssrn.com) or paper titles as names."
    )
    raw = call_llm(user, _SYSTEM)
    if raw is None:
        return []
    domains = _parse_json_array(raw)
    history = load_history()
    already = set(history.get("searched_domains", []))
    return [d for d in domains if d and d not in already]


# ── Phase 2: deep research per domain ───────────────────────────────────────

def research_domain(domain: str, context: str) -> dict | None:
    user = (
        f"Strategy category: {domain}\n\n"
        f"{context}\n\n"
        f"Search for peer-reviewed papers and high-quality practitioner "
        f"research (SSRN, CFA Institute, well-known quant funds) showing "
        f"empirical evidence that '{domain}' produces positive risk-adjusted "
        f"returns in cryptocurrency markets.\n\n"
        "For each source found, evaluate:\n"
        "- Does it contain actual backtested or live-traded results?\n"
        "- Is the data from 2018 or later?\n"
        "- Is it specific to crypto (not just equities/FX)?\n"
        "- Could it be implemented as a rule-based system on perpetual "
        "futures?\n\n"
        "Return a JSON object with this exact structure:\n"
        "{\n"
        "  \"domain\": \"<domain name>\",\n"
        "  \"strategy_id\": \"<CamelCase 2-4 word strategy class name; "
        "no URLs, no punctuation, no spaces; must match "
        "^[A-Z][A-Za-z0-9]+$>\",\n"
        "  \"citations\": [\n"
        "    {\n"
        "      \"title\": \"...\",\n"
        "      \"authors\": \"...\",\n"
        "      \"year\": 2023,\n"
        "      \"source\": \"Journal of Finance / SSRN / etc\",\n"
        "      \"key_finding\": \"one sentence: what return/sharpe they found\",\n"
        "      \"crypto_specific\": true,\n"
        "      \"quality_score\": 4\n"
        "    }\n"
        "  ],\n"
        "  \"hypothesis\": \"one sentence: specific testable claim\",\n"
        "  \"implementation_notes\": \"brief notes on how to implement\",\n"
        "  \"variation_id_suggestion\": \"kebab-case-slug\",\n"
        "  \"overall_quality\": 3.5\n"
        "}\n\n"
        "strategy_id rules: must match ^[A-Z][A-Za-z0-9]+$. Do NOT use "
        "URLs (e.g. papers.ssrn.com), paper hostnames, or paper titles. "
        "Examples of GOOD strategy_id: \"CrossSectionalReversal\", "
        "\"AttentionMomentum\", \"VolatilityRiskPremium\". Examples of "
        "BAD strategy_id: \"PapersSsrnCom\" (URL hostname), "
        "\"TheSystematicChangeIn\" (prose paper title).\n"
        "quality_score per citation: 1=blog/opinion, 2=practitioner without "
        "rigorous backtest, 3=SSRN working paper with backtest, "
        "4=peer-reviewed journal, 5=top-tier journal (JF/RFS/JFE/RFS).\n"
        "overall_quality: average of citation quality_scores, only counting "
        "citations that are crypto_specific=true.\n"
        "If fewer than 3 qualifying citations found, still return the object "
        "but set overall_quality accordingly.\n"
        "No explanation outside the JSON."
    )
    raw = call_llm(user, _SYSTEM)
    if raw is None:
        return None
    parsed = _parse_json_object(raw)
    if parsed is None:
        return None
    try:
        overall = float(parsed.get("overall_quality", 0.0))
    except (TypeError, ValueError):
        return None
    if overall < MIN_CITATION_QUALITY:
        return None
    return parsed


# ── Phase 3: queue-item formulation ─────────────────────────────────────────

# Word cap for strategy_id derivation. Replaces the prior 30-char
# hard truncation that produced mid-word cuts like
# "InterExchangeVolumeFlowAnalysi". A whole-word cap leaves the
# resulting id legible regardless of input length.
_STRATEGY_ID_MAX_WORDS = 4


def _to_strategy_id(domain: str) -> str:
    if not domain or "." in domain or domain.lower().startswith("http"):
        return "Unknown"
    cleaned = re.sub(r"[^A-Za-z0-9 ]", " ", domain)
    parts = [p for p in cleaned.split() if len(p) > 1]
    if not parts:
        return "Unknown"
    parts = parts[:_STRATEGY_ID_MAX_WORDS]
    return "".join(p.capitalize() for p in parts)


# Acronym-aware CamelCase -> snake_case: first pass splits before a
# capital that is followed by a lowercase, second pass splits between
# a lowercase/digit and any capital. Keeps acronym runs together so
# "VolumeWeightedTSMOM" -> "volume_weighted_tsmom" rather than the
# old single-pass result "volume_weighted_t_s_m_o_m".
_SNAKE_CASE_RE_1 = re.compile(r"(.)([A-Z][a-z]+)")
_SNAKE_CASE_RE_2 = re.compile(r"([a-z0-9])([A-Z])")


def _to_snake_case(name: str) -> str:
    s = _SNAKE_CASE_RE_1.sub(r"\1_\2", name)
    s = _SNAKE_CASE_RE_2.sub(r"\1_\2", s)
    s = s.lower()
    return re.sub(r"[^a-z0-9_]", "", s)


# CamelCase identifier sanity check: matches strategy_id values the
# LLM is allowed to return directly. URL hostnames, paper titles, and
# punctuation-bearing inputs all fail this check and force a fallback
# through _to_strategy_id (which strips dots, http-prefix etc).
_CAMEL_CASE_RE = re.compile(r"^[A-Z][A-Za-z0-9]+$")


def _summarise_top_citations(citations: list) -> str:
    bits: list[str] = []
    for c in citations[:3]:
        if not isinstance(c, dict):
            continue
        authors = c.get("authors") or "unknown"
        author_first = authors.split(",")[0].split(" and ")[0].strip()
        year = c.get("year") or "n.d."
        bits.append(f"{author_first} ({year})")
    return "; ".join(bits) if bits else "no citations"


def build_queue_item(proposal: dict, next_id: str) -> dict:
    # Prefer the LLM's explicit strategy_id when it matches the
    # CamelCase contract; otherwise derive from the (possibly prose
    # or URL-shaped) domain via the hardened _to_strategy_id which
    # rejects URLs and caps at 4 words. Either way, every downstream
    # path (script_path, literature_doc, snake) is derived from the
    # final strategy_id -- never from the raw domain string -- so a
    # malformed domain can no longer leak into filenames.
    raw_strategy_id = str(proposal.get("strategy_id", "")).strip()
    if _CAMEL_CASE_RE.match(raw_strategy_id):
        strategy_id = raw_strategy_id
    else:
        domain = str(proposal.get("domain", "")).strip() or "unknown-domain"
        strategy_id = _to_strategy_id(domain)
    snake = _to_snake_case(strategy_id) or "unknown"
    kebab = snake.replace("_", "-")
    variation_id = (
        str(proposal.get("variation_id_suggestion", "")).strip()
        or f"{kebab}-v1"
    )
    citations = proposal.get("citations") or []
    return {
        "id": next_id,
        "status": "queued",
        "strategy_id": strategy_id,
        "variation_id": variation_id,
        "script_path": f"scripts/run_{snake}_trial.py",
        "trial_type": "full_cpcv",
        "hypothesis_one_line": str(proposal.get("hypothesis", "")).strip(),
        "source_citation": _summarise_top_citations(citations),
        # Derive literature_doc from strategy_id (kebab-case), NOT
        # from the raw domain string. Prevents URL-hostname-shaped
        # values like "research/papers-ssrn-com-literature.md" or
        # "research/github-com-literature.md" from ever being written.
        "literature_doc": f"research/{kebab}-literature.md",
        "citations": citations,
        "overall_quality": float(proposal.get("overall_quality", 0.0)),
        "implementation_notes": str(
            proposal.get("implementation_notes", "")
        ).strip(),
        "added_by": "proposal-agent",
        "added_at": utcnow_iso(),
        "started_at": None,
        "finished_at": None,
        "verdict": None,
        "trial_id": None,
        "error": None,
        "email_sent": False,
        "needs_trial_script": True,
    }


# ── Citation validation (anti-hallucination, anti-blogspam) ─────────────────

# Hedge markers in a citation's `source` field that indicate the LLM
# fabricated or speculated about the source. Case-insensitive.
_HALLUCINATION_MARKERS = (
    "(hypothetical)", "(fictional)", "(estimated)",
    "(simulated)", "(illustrative)", "(example)",
)

# Source substrings that disqualify a citation from counting toward the
# minimum-three peer-reviewed-or-SSRN bar. Case-insensitive substring
# match on the citation's `source` field.
_NON_QUALIFYING_SOURCES = (
    "tradingview", "reddit", "twitter", "x.com",
    "medium.com", "substack", "wikipedia",
    "coindesk", "cointelegraph", "decrypt",
)


def validate_citations(citations: list) -> tuple[bool, str]:
    """Return (is_valid, rejection_reason).

    Three rejection rules, checked in order, return on first failure:
      1. hallucination markers in any citation's source field
      2. fewer than 3 qualifying citations after filtering
      3. recalculated quality average < 3.0 (counting only crypto-
         specific qualifying citations)
    """
    if not isinstance(citations, list):
        return (False, "invalid_citations: not a list")

    # Rule 1: hallucination markers
    for c in citations:
        if not isinstance(c, dict):
            continue
        source_lc = str(c.get("source", "")).lower()
        for marker in _HALLUCINATION_MARKERS:
            if marker in source_lc:
                authors = c.get("authors", "unknown") or "unknown"
                return (
                    False,
                    "hallucinated_citation: source field contains "
                    "hedge marker in citation by " + str(authors),
                )

    # Rule 2: count qualifying sources
    qualifying = []
    for c in citations:
        if not isinstance(c, dict):
            continue
        source_lc = str(c.get("source", "")).lower()
        if any(nq in source_lc for nq in _NON_QUALIFYING_SOURCES):
            continue
        qualifying.append(c)

    if len(qualifying) < 3:
        return (
            False,
            "insufficient_peer_reviewed: only "
            + str(len(qualifying))
            + " qualifying citations after removing non-academic sources",
        )

    # Rule 3: quality threshold on the recalculated set
    crypto_specific = [
        c for c in qualifying if c.get("crypto_specific")
    ]
    scores: list[float] = []
    for c in crypto_specific:
        try:
            scores.append(float(c.get("quality_score", 0)))
        except (TypeError, ValueError):
            continue
    if not scores:
        return (
            False,
            "quality_below_threshold: no parseable quality_score "
            "values among crypto-specific qualifying citations",
        )
    avg = sum(scores) / len(scores)
    if avg < MIN_CITATION_QUALITY:
        return (
            False,
            "quality_below_threshold: recalculated quality "
            + ("%.2f" % avg)
            + " after removing non-qualifying sources",
        )

    return (True, "")


# ── Phase 4: queue write ────────────────────────────────────────────────────

def propose_and_queue(dry_run: bool) -> tuple[int, list[dict]]:
    queue_data = load_queue()
    state_statuses = _load_state_statuses()
    queued_count = sum(
        1 for item in queue_data.get("queue", [])
        if _effective_status(item, state_statuses) in ("queued", "running")
    )
    if queued_count >= MAX_QUEUED_ITEMS and not dry_run:
        print(
            f"Queue already has {queued_count} pending items "
            f"(max {MAX_QUEUED_ITEMS}). Not proposing."
        )
        return 0, []

    history = load_history()
    context = load_tested_context()
    domains = discover_candidate_domains(context)
    if not domains:
        print(
            "Domain discovery returned empty list; both APIs may have failed."
        )
        return 0, []

    added_items: list[dict] = []
    slots_available = max(0, MAX_QUEUED_ITEMS - queued_count)

    for domain in domains:
        if len(added_items) >= slots_available:
            break
        if domain in history.get("searched_domains", []):
            continue

        print(f"Researching domain: {domain}")
        proposal = research_domain(domain, context)

        history.setdefault("searched_domains", []).append(domain)
        if not dry_run:
            save_history(history)

        if proposal is None:
            print(f"  -> quality bar not met for {domain}, skipping")
            continue

        # Citation validation gate (anti-hallucination, anti-blogspam).
        # Even if the LLM self-reports overall_quality >= MIN, we
        # recompute on the qualifying-source-only subset and reject
        # proposals that fail the structural checks.
        is_valid, reason = validate_citations(proposal.get("citations") or [])
        suggested_vid = (
            str(proposal.get("variation_id_suggestion", "")).strip()
            or "(unknown)"
        )
        if not is_valid:
            print("REJECTED: " + suggested_vid + " -- " + reason)
            history.setdefault("proposals_log", []).append({
                "variation_id": suggested_vid,
                "domain": domain,
                "status": "rejected",
                "rejection_reason": reason,
                "logged_at": utcnow_iso(),
            })
            if not dry_run:
                save_history(history)
            continue

        print(
            f"  -> quality {proposal.get('overall_quality', 0.0):.1f} >= "
            f"{MIN_CITATION_QUALITY}, citations validated, queuing"
        )

        existing_ids = [
            item.get("id", "") for item in queue_data.get("queue", [])
        ]
        _id_nums = [
            int(m.group(1))
            for _raw_id in existing_ids
            if (m := re.match(r"sq-(\d+)$", _raw_id))
        ]
        next_num = (max(_id_nums) + 1) if _id_nums else 1
        next_id = f"sq-{next_num:03d}"

        item = build_queue_item(proposal, next_id)
        queue_data.setdefault("queue", []).append(item)
        history.setdefault("proposed_variations", []).append(
            item["variation_id"]
        )
        history.setdefault("proposals_log", []).append({
            "variation_id": item["variation_id"],
            "domain": domain,
            "status": "queued",
            "logged_at": utcnow_iso(),
        })
        if not dry_run:
            save_history(history)

        if not dry_run:
            save_queue(queue_data)
        else:
            print(
                f"  [dry-run] would add item {next_id}: {item['variation_id']}"
            )

        added_items.append(item)

    return len(added_items), added_items


# ── main ────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run even if queue is not empty",
    )
    args = parser.parse_args()

    # Backlog cap (must run BEFORE any LLM call or web search). Counts
    # items with effective status == "queued" via the merged-view
    # helper, so newly-proposed items the orchestrator has not yet
    # touched are counted alongside items mid-build. >= the cap means
    # the queue already has enough work for the next orchestrator
    # cycle; skip research and exit cleanly.
    queue_data = load_queue()
    state_statuses = _load_state_statuses()
    queued_for_cap = sum(
        1 for i in queue_data.get("queue", [])
        if _effective_status(i, state_statuses) == "queued"
    )
    if queued_for_cap >= MAX_QUEUED_BEFORE_SKIP:
        print(
            f"Queue has {queued_for_cap} queued item(s) "
            f"(cap: MAX_QUEUED_BEFORE_SKIP). "
            "Skipping proposal research to avoid backlog."
        )
        return 0

    if not args.dry_run:
        if not os.environ.get("OPENROUTER_API_KEY"):
            print("ERROR: OPENROUTER_API_KEY not set", file=sys.stderr)
            return 1

    queued = [
        i
        for i in queue_data.get("queue", [])
        if _effective_status(i, state_statuses) in ("queued", "running")
    ]
    if queued and not args.force and not args.dry_run:
        print(
            f"Queue has {len(queued)} pending item(s). "
            f"Use --force to propose anyway."
        )
        return 0

    added, _added_items = propose_and_queue(args.dry_run)
    print(f"Done. {added} item(s) added to queue.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

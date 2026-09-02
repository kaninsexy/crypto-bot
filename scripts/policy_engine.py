# SPDX-License-Identifier: LicenseRef-Proprietary
# Copyright (c) 2026 Kanin Srijundorn. All rights reserved.
"""policy_engine.py — a tool-layer policy DSL engine (playbooks port V6 (BK-0016)).

Re-implemented (clean-room) from ActPlane's policy language + IFC compiler (MIT).
No source copied. This is the ONE enforcement class our per-event hooks could not
express before: **ordering** ("B is forbidden unless A succeeded since the last
relevant change") plus **label** conditions and a uniform corrective-feedback
frame. Findings flow through the finding-policy map
(`scripts/enforcement_policy.py`): a `block` rule → the `POLICY_RULE_BLOCK` code
(blocking by default), a `notify` rule → `POLICY_RULE_NOTIFY` (advisory).

HONEST LIMIT (documented, not hidden): this is creation/tool-time enforcement,
bypassable by a determined agent — ActPlane's own authors deleted their
tool-layer PreToolUse enforcement for exactly this reason. It is defense-in-depth
(same posture as `lifecycle_guard.py`), not a sandbox. `path-allowlist.py` stays
unconditional beneath it, and `forkbomb_detect.py` / `agent_watchdog.py` remain
the run-time backstops. See `.claude/rules/enforcement.md`.

The engine is a PURE event processor:

    policy = compile_policy(text)            # fail-closed: any unknown token disarms
    state  = new_state()
    findings = process_event(policy, state, event)   # evaluate THEN update

so the whole thing is harness-drivable with a synthetic event sequence
(`recon_e2e_harness.py` stages policy_engine.*) — no live tool call required. The
hook adapter (`.claude/hooks/policy-engine.py`) translates real tool calls into
`ToolEvent`s and persists `state` per session under a gitignored runtime path.

GRAMMAR (subset)
    # comment
    source NAME = (file|exec) "PATTERN"
    rule NAME:
      (block|notify) (exec|write|read) "PATTERN" ["ARG"]
        [if LABEL]
        [unless after (exec|write|read) "PATTERN" [exits N]
                [since (exec|write|read) "PATTERN" (or (exec|write|read) "PATTERN")*]]
        because "REASON"

`kill` is rejected at compile ("not portable at the tool layer; use block").
Patterns/args/reasons are DOUBLE-QUOTED; every bare word is a reserved keyword —
anything unexpected is a compile error (fail-closed). Glob matching is
implicit-basename (a pattern with no `/` matches the basename), `**` spans path
separators, `*` does not.

EPOCH ORDERING (ActPlane's two counter maps, one documented deviation)
    on a matching gate event:  gate_epoch[gate]  = ++epoch  (only if exits matches)
    on a matching since event:  inval_epoch[since] = ++epoch
    `after G since S...` holds  iff  max(inval_epoch[S]) == 0        (deviation)
                                 OR  gate_epoch[G] != 0 AND gate_epoch[G] > max(inval_epoch[S])
    plain `after G` (no since) latches once gate_epoch[G] != 0 (strict, as in ActPlane).

    DEVIATION (documented in .claude/rules/enforcement.md): ActPlane's formula
    requires gate_epoch != 0 unconditionally, which would gate EVERY guarded op
    in a fresh session — including ones untouched by any since-event (e.g. a
    docs-only commit before any harness run). The engine sees only the command,
    never the staged set, so "docs-only commits are not gated" is only
    expressible as: no matching invalidation this session ⇒ the unless holds.
    The strict form is preserved for plain `after` (no since clause).

HOOK PHASES: `evaluate()` at PreToolUse (block/notify BEFORE the tool runs, using
current state); `update_gates()` + `update_labels_and_inval()` at PostToolUse
(state reflects only tool calls that actually EXECUTED, and `exits N` needs the
real exit code, which exists only at Post). The pure `process_event()` —
evaluate, then update — remains the single-step form for tests and the harness.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

# ── vocabulary ───────────────────────────────────────────────────────────────
EFFECTS = ("block", "notify")  # `kill` is intentionally excluded (below)
OPS = ("exec", "write", "read")
SOURCE_KINDS = ("file", "exec")
# Reserved bare keywords — a bare token not in here (outside its expected slot)
# is a compile error. Patterns/args/reasons are always quoted.
_KEYWORDS = frozenset(
    {
        "source",
        "rule",
        "block",
        "notify",
        "kill",
        "exec",
        "write",
        "read",
        "file",
        "endpoint",
        "if",
        "unless",
        "after",
        "exits",
        "since",
        "or",
        "because",
    }
)

# Finding codes emitted into the finding-policy map.
CODE_BLOCK = "POLICY_RULE_BLOCK"
CODE_NOTIFY = "POLICY_RULE_NOTIFY"
CODE_DISARMED = "POLICY_ENGINE_DISARMED"
CODE_PRODUCER_ERROR = "POLICY_ENGINE_PRODUCER_ERROR"


# ── compiled model ───────────────────────────────────────────────────────────
@dataclass
class Source:
    name: str
    kind: str  # 'file' | 'exec'
    pattern: str


@dataclass
class Ev:
    """A gate-event or since-event descriptor: an op + a glob pattern."""

    op: str
    pattern: str

    def key(self, exits: int | None = None) -> str:
        # \x1f (unit separator) cannot appear in a shell-safe pattern, so a
        # pattern that literally contains "|exits=0" can't collide with a real
        # exits-constrained key.
        base = f"{self.op}:{self.pattern}"
        return base if exits is None else f"{base}\x1fexits={exits}"


@dataclass
class After:
    gate: Ev
    exits: int | None
    since: list[Ev]  # OR-joined; empty ⇒ plain `after` (latches)


@dataclass
class Rule:
    name: str
    effect: str  # 'block' | 'notify'
    op: str  # 'exec' | 'write' | 'read'
    pattern: str
    arg: str | None
    if_label: str | None
    unless: After | None
    reason: str


@dataclass
class Policy:
    sources: dict[str, Source] = field(default_factory=dict)
    rules: list[Rule] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def armed(self) -> bool:
        # Fail-closed: ANY compile error disarms the whole policy. A disarmed
        # engine never blocks — it surfaces a loud advisory (CODE_DISARMED).
        return not self.errors


# ── tokenizer (quote-aware) ──────────────────────────────────────────────────
_TOKEN_RE = re.compile(r'"(?P<q>(?:[^"\\]|\\.)*)"|(?P<bare>[^\s]+)')


def _tokens(s: str) -> list[tuple[str, bool]]:
    """Return [(text, is_quoted)]. Quoted tokens keep their (unescaped) contents;
    bare tokens are reserved keywords / numbers / source names."""
    out: list[tuple[str, bool]] = []
    for m in _TOKEN_RE.finditer(s):
        if m.group("q") is not None:
            out.append((m.group("q").replace('\\"', '"').replace("\\\\", "\\"), True))
        else:
            out.append((m.group("bare"), False))
    return out


# ── glob matching (implicit-basename, ** spans separators) ───────────────────
def _glob_to_re(pattern: str) -> str:
    out = ["^"]
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            if i + 1 < n and pattern[i + 1] == "*":
                if i + 2 < n and pattern[i + 2] == "/":
                    out.append("(?:.*/)?")  # **/x also matches bare x (zero dirs)
                    i += 3
                    continue
                out.append(".*")  # ** spans '/'
                i += 2
                continue
            out.append("[^/]*")  # * does not span '/'
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    out.append("$")
    return "".join(out)


def _matches(pattern: str, value: str) -> bool:
    """Glob match. A pattern with no '/' is implicit-basename: it matches the
    value's basename (or the whole value). A pattern with '/' matches the full
    (forward-slashed) value."""
    if value is None:
        return False
    value = str(value).replace("\\", "/")
    rx = re.compile(_glob_to_re(pattern))
    if "/" not in pattern:
        base = value.rsplit("/", 1)[-1]
        return bool(rx.match(base) or rx.match(value))
    return bool(rx.match(value))


# ── compile ──────────────────────────────────────────────────────────────────
def _split_statements(text: str) -> list[tuple[int, str]]:
    """Split into (lineno, statement) where a `rule NAME:` header absorbs its
    following MORE-indented continuation lines into one statement. `source` and
    top-level `rule` headers start fresh statements. Comments/blank lines drop."""
    raw = text.splitlines()
    stmts: list[tuple[int, str]] = []
    i = 0
    while i < len(raw):
        line = raw[i]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        indent = len(line) - len(line.lstrip())
        header = stripped
        lineno = i + 1
        # Absorb continuation lines that are MORE indented than this header and
        # not a new source/rule header.
        j = i + 1
        parts = [header]
        while j < len(raw):
            nxt = raw[j]
            nxt_stripped = nxt.strip()
            if not nxt_stripped or nxt_stripped.startswith("#"):
                j += 1
                continue
            nxt_indent = len(nxt) - len(nxt.lstrip())
            if nxt_indent > indent and not (
                nxt_stripped.startswith("source ") or nxt_stripped.startswith("rule ")
            ):
                parts.append(nxt_stripped)
                j += 1
            else:
                break
        stmts.append((lineno, " ".join(parts)))
        i = j
    return stmts


def _parse_source(toks: list[tuple[str, bool]], errors: list[str], ln: int):
    # source NAME = (file|exec) "PATTERN"
    if len(toks) != 5:
        errors.append(f'line {ln}: malformed source (want: source NAME = file|exec "PATTERN")')
        return None
    (_, _), (name, nq), (eq, eqq), (kind, kq), (pat, pq) = toks
    if nq or name in _KEYWORDS:
        errors.append(f"line {ln}: invalid source name {name!r}")
        return None
    if eqq or eq != "=":
        errors.append(f"line {ln}: expected bare '=' in source, got {eq!r}")
        return None
    if kq or kind not in SOURCE_KINDS:
        errors.append(f"line {ln}: unknown source kind {kind!r} (want file|exec)")
        return None
    if not pq:
        errors.append(f"line {ln}: source pattern must be quoted")
        return None
    return Source(name=name, kind=kind, pattern=pat)


class _Cur:
    """A tiny cursor over the token list for the rule-body parser."""

    def __init__(self, toks):
        self.toks, self.i = toks, 0

    def eof(self):
        return self.i >= len(self.toks)

    def peek(self):
        return self.toks[self.i] if not self.eof() else (None, False)

    def next(self):
        t = self.toks[self.i]
        self.i += 1
        return t


def _parse_rule_body(name: str, toks: list[tuple[str, bool]], errors: list[str], ln: int):
    c = _Cur(toks)

    def fail(msg):
        errors.append(f"line {ln}: rule {name!r}: {msg}")
        return

    if c.eof():
        return fail("empty rule body")
    effect, eq = c.next()
    if eq or effect == "kill":
        if effect == "kill":
            return fail("`kill` is not portable at the tool layer; use `block`")
        return fail(f"effect must be a bare keyword, got {effect!r}")
    if effect not in EFFECTS:
        return fail(f"unknown effect {effect!r} (want block|notify)")
    if c.eof():
        return fail("missing operation")
    op, oq = c.next()
    if oq or op not in OPS:
        return fail(f"unknown operation {op!r} (want exec|write|read)")
    if c.eof():
        return fail("missing pattern")
    pat, pq = c.next()
    if not pq:
        return fail("operation pattern must be quoted")

    arg = None
    if_label = None
    unless = None

    # optional ARG (a quoted token immediately after the pattern) — exec only
    tok, quoted = c.peek()
    if quoted:
        arg, _ = c.next()
        if op != "exec":
            return fail(
                "an ARG is only valid on exec rules (write/read rules take a single pattern)"
            )

    # optional `if LABEL`
    tok, quoted = c.peek()
    if not quoted and tok == "if":
        c.next()
        if c.eof():
            return fail("`if` requires a label name")
        lbl, lq = c.next()
        if lq or lbl in _KEYWORDS:
            return fail(f"invalid label after `if`: {lbl!r}")
        if_label = lbl

    # optional `unless after ...`
    tok, quoted = c.peek()
    if not quoted and tok == "unless":
        c.next()
        aft, aq = c.next() if not c.eof() else (None, False)
        if aq or aft != "after":
            return fail("`unless` must be followed by bare `after`")
        if c.eof():
            return fail("`after` requires an operation")
        gop, gq = c.next()
        if gq or gop not in OPS:
            return fail(f"unknown gate operation {gop!r}")
        if c.eof():
            return fail("gate pattern missing")
        gpat, gpq = c.next()
        if not gpq:
            return fail("gate pattern must be quoted")
        exits = None
        since: list[Ev] = []
        tok, quoted = c.peek()
        if not quoted and tok == "exits":
            c.next()
            if c.eof():
                return fail("`exits` requires an integer")
            ntok, nq = c.next()
            if nq or not re.fullmatch(r"-?\d+", ntok):
                return fail(f"`exits` wants an integer, got {ntok!r}")
            exits = int(ntok)
        tok, quoted = c.peek()
        if not quoted and tok == "since":
            c.next()
            while True:
                if c.eof():
                    return fail("`since` requires an operation + pattern")
                sop, sq = c.next()
                if sq or sop not in OPS:
                    return fail(f"unknown since operation {sop!r}")
                if c.eof():
                    return fail("since pattern missing")
                spat, spq = c.next()
                if not spq:
                    return fail("since pattern must be quoted")
                since.append(Ev(op=sop, pattern=spat))
                tok, quoted = c.peek()
                if not quoted and tok == "or":
                    c.next()
                    continue
                break
        unless = After(gate=Ev(op=gop, pattern=gpat), exits=exits, since=since)

    # required `because "REASON"`
    tok, quoted = c.peek()
    if quoted or tok != "because":
        return fail('missing required `because "reason"`')
    c.next()
    if c.eof():
        return fail("`because` requires a quoted reason")
    reason, rq = c.next()
    if not rq:
        return fail("`because` reason must be quoted")

    if not c.eof():
        extra = c.next()[0]
        return fail(f"unexpected trailing token {extra!r}")

    return Rule(
        name=name,
        effect=effect,
        op=op,
        pattern=pat,
        arg=arg,
        if_label=if_label,
        unless=unless,
        reason=reason,
    )


def compile_policy(text: str) -> Policy:
    """Compile policy source. Fail-closed: any unknown token / malformed
    statement / duplicate rule / undeclared `if` label / unterminated quote /
    `kill` effect appends to ``errors`` and disarms the whole policy
    (``armed == False``)."""
    pol = Policy()
    # Per-PHYSICAL-line quote balance: continuation lines are joined with
    # spaces before tokenizing, so an unterminated quote would otherwise span
    # the join and arm a policy with silently altered semantics.
    for i, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if len(re.findall(r'(?<!\\)"', line)) % 2 == 1:
            pol.errors.append(f"line {i}: unterminated quote")
    for ln, stmt in _split_statements(text):
        toks = _tokens(stmt)
        if not toks:
            continue
        head = toks[0][0]
        head_quoted = toks[0][1]
        if head_quoted:
            pol.errors.append(f"line {ln}: statement cannot start with a quoted token")
            continue
        if head == "source":
            src = _parse_source(toks, pol.errors, ln)
            if src is not None:
                if src.name in pol.sources:
                    pol.errors.append(f"line {ln}: duplicate source {src.name!r}")
                else:
                    pol.sources[src.name] = src
        elif head == "rule":
            # `rule NAME: <body...>`  — NAME may carry a trailing ':' or the ':'
            # may be a separate token; normalize both.
            if len(toks) < 2:
                pol.errors.append(f"line {ln}: rule missing a name")
                continue
            name_tok, nq = toks[1]
            if nq:
                pol.errors.append(f"line {ln}: rule name must be bare")
                continue
            body_start = 2
            name = name_tok
            if name.endswith(":"):
                name = name[:-1]
            elif len(toks) >= 3 and toks[2][0] == ":" and not toks[2][1]:
                body_start = 3
            else:
                pol.errors.append(f"line {ln}: rule header must end with ':'")
                continue
            if not name or name in _KEYWORDS:
                pol.errors.append(f"line {ln}: invalid rule name {name!r}")
                continue
            if any(r.name == name for r in pol.rules):
                pol.errors.append(f"line {ln}: duplicate rule name {name!r}")
                continue
            rule = _parse_rule_body(name, toks[body_start:], pol.errors, ln)
            if rule is not None:
                pol.rules.append(rule)
        else:
            pol.errors.append(f"line {ln}: unknown statement keyword {head!r}")
    # Post-pass: an `if LABEL` must name a DECLARED source — a typo'd label can
    # never enter the session set, which would silently disarm that one rule
    # while the policy as a whole looks armed.
    for r in pol.rules:
        if r.if_label and r.if_label not in pol.sources:
            pol.errors.append(f"rule {r.name!r}: `if {r.if_label}` names no declared source")
    return pol


# ── runtime event + state ────────────────────────────────────────────────────
@dataclass
class ToolEvent:
    op: str  # 'exec' | 'write' | 'read'
    target: str = ""  # primary target (path, or argv[0] for exec)
    argv: list[str] = field(default_factory=list)  # exec argv (for ARG match)
    exit_code: int | None = None  # known at PostToolUse / in synthetic sequences
    writes: list[str] = field(default_factory=list)  # extra write targets (bash)


def new_state() -> dict:
    return {
        "epoch": 0,
        "gate_epoch": {},
        "inval_epoch": {},
        "labels": {"session": [], "files": {}},
    }


def _bump(state: dict) -> int:
    state["epoch"] = int(state.get("epoch", 0)) + 1
    return state["epoch"]


# Interpreters whose FIRST NON-FLAG ARGUMENT is the real policy target: at the
# tool layer `python scripts/recon_e2e_harness.py` execs `python`, but the thing
# a gate like `after exec "**/recon_e2e_harness.py"` means is the script. `-c` /
# `-m` forms keep the interpreter itself as the target (no script path on the
# line).
_INTERPRETERS = frozenset(
    {
        "python",
        "python2",
        "python3",
        "bash",
        "sh",
        "zsh",
        "dash",
        "node",
        "deno",
        "bun",
        "ruby",
        "perl",
    }
)


def effective_exec_target(tokens: list[str]) -> str:
    """The argv0-equivalent a policy pattern should match for an exec event.

    ``uv run <cmd...>`` is a RUNNER PREFIX: siamese runs everything through
    ``uv run`` (e.g. ``uv run python scripts/recon_e2e_harness.py``,
    ``uv run pytest``), so ``uv run`` chains — the real policy target is the
    wrapped command, resolved by recursing into ``<cmd...>``. Only ``run``
    chains: any other ``uv`` subcommand (``uv pip list``, ``uv sync``) targets
    ``uv`` itself. Without this, the harness-before-commit gate could NEVER be
    satisfied by a real ``uv run python scripts/recon_e2e_harness.py`` invocation
    and would wedge every gate-layer commit. Documented as fact in
    ``.claude/rules/enforcement.md``.
    """
    if not tokens:
        return ""
    argv0 = tokens[0]
    base = argv0.rsplit("/", 1)[-1]
    if base == "uv":
        if len(tokens) >= 2 and tokens[1] == "run":
            inner = effective_exec_target(tokens[2:])
            return inner or argv0
        return argv0
    if re.sub(r"[\d.]+$", "", base) in _INTERPRETERS or base in _INTERPRETERS:
        for t in tokens[1:]:
            if t in ("-c", "-m"):
                return argv0  # code/module form: no script path
            if t.startswith("-"):
                continue
            return t  # the script path
    return argv0


def _exec_target(event: ToolEvent) -> str:
    """One derivation everywhere (rule trigger, gate stamp, since stamp, labels)."""
    return event.target or (event.argv[0] if event.argv else "")


def _event_targets(event: ToolEvent, op: str) -> list[str]:
    """The candidate targets of `event` as seen by a pattern of kind `op`."""
    if op == "exec":
        return [_exec_target(event)]
    return [t for t in [event.target, *event.writes] if t]


def _rule_triggers(rule: Rule, event: ToolEvent, state: dict) -> bool:
    if rule.op != event.op:
        return False
    if not any(_matches(rule.pattern, t) for t in _event_targets(event, rule.op) if t):
        return False
    if rule.op == "exec" and rule.arg is not None:
        # ARG matches the FIRST NON-FLAG token after argv0 (the subcommand
        # position) — not anywhere in argv, so `git log --grep commit` does not
        # false-block on `block exec "git" "commit"`. The `git -C x commit`
        # dodge is part of the documented bypassability.
        rest = event.argv[1:] if event.argv else []
        sub = next((t for t in rest if not t.startswith("-")), None)
        if sub is None or not (sub == rule.arg or _matches(rule.arg, sub)):
            return False
    return not (rule.if_label and rule.if_label not in state["labels"].get("session", []))


def _unless_satisfied(after: After, state: dict) -> bool:
    gk = after.gate.key(after.exits)
    gate_ep = int(state["gate_epoch"].get(gk, 0))
    if not after.since:
        return gate_ep != 0  # plain `after` latches (strict)
    worst = max(
        (int(state["inval_epoch"].get(s.key(), 0)) for s in after.since),
        default=0,
    )
    if worst == 0:
        # DEVIATION from ActPlane (documented in enforcement.md): no matching
        # invalidation this session ⇒ nothing relevant changed ⇒ the unless
        # holds even if the gate never ran. Without this, EVERY guarded op in a
        # fresh session is gated — including a docs-only commit.
        return True
    return gate_ep != 0 and gate_ep > worst


def update_gates(policy: Policy, state: dict, event: ToolEvent) -> None:
    """Stamp gate epochs for any rule's `unless` gate this event satisfies,
    honoring the `exits` constraint against event.exit_code. Call at
    PostToolUse (the exit code exists only after the tool ran)."""
    for rule in policy.rules:
        if not rule.unless:
            continue
        g = rule.unless.gate
        if event.op != g.op:
            continue
        if not any(_matches(g.pattern, t) for t in _event_targets(event, g.op) if t):
            continue
        if rule.unless.exits is not None and event.exit_code != rule.unless.exits:
            continue
        state["gate_epoch"][g.key(rule.unless.exits)] = _bump(state)


def update_labels_and_inval(policy: Policy, state: dict, event: ToolEvent) -> None:
    """σ label transfers + since (invalidation) stamping. Call at PostToolUse.

    Transfers: exec ⇒ session ∪= matching exec-sources; read ⇒ session ∪=
    matching file-sources ∪ the file's own labels; write ⇒ file ∪= session."""
    labels = state.setdefault("labels", {})
    sess = labels.setdefault("session", [])
    files = labels.setdefault("files", {})
    if event.op == "exec":
        tgt = _exec_target(event)
        for src in policy.sources.values():
            if src.kind == "exec" and _matches(src.pattern, tgt) and src.name not in sess:
                sess.append(src.name)
    elif event.op == "read":
        for src in policy.sources.values():
            if src.kind == "file" and _matches(src.pattern, event.target) and src.name not in sess:
                sess.append(src.name)
        for lbl in files.get(event.target, []):
            if lbl not in sess:
                sess.append(lbl)
    elif event.op == "write":
        for t in _event_targets(event, "write"):
            cur = files.setdefault(t, [])
            for lbl in sess:
                if lbl not in cur:
                    cur.append(lbl)

    # since (invalidation) stamping — uses the SAME target derivation as rule
    # triggering and gate stamping.
    for rule in policy.rules:
        if not rule.unless:
            continue
        for s in rule.unless.since:
            if event.op != s.op:
                continue
            if any(_matches(s.pattern, t) for t in _event_targets(event, s.op) if t):
                state["inval_epoch"][s.key()] = _bump(state)


def _update_state(policy: Policy, state: dict, event: ToolEvent) -> None:
    # Gate stamps land BEFORE invalidation stamps for a single event matching
    # both, so the ambiguous same-command case nets inval > gate — stricter.
    update_gates(policy, state, event)
    update_labels_and_inval(policy, state, event)


def _corrective(rule: Rule) -> str:
    if rule.unless is None:
        return (
            "this operation is not permitted by policy; use a compliant "
            "alternative or, if the rule is wrong, remove it in a foreground session"
        )
    a = rule.unless
    parts = [f"run `{a.gate.op} {a.gate.pattern}`"]
    if a.exits is not None:
        parts.append(f"(it must exit {a.exits})")
    if a.since:
        since = " or ".join(f"{s.op} {s.pattern}" for s in a.since)
        parts.append(f"after your most recent {since}")
    return "satisfy the policy: " + " ".join(parts) + ", then retry"


def make_finding(rule: Rule, event: ToolEvent) -> dict:
    tgt = " ".join(event.argv) if event.argv else event.target
    return {
        "code": CODE_BLOCK if rule.effect == "block" else CODE_NOTIFY,
        "source": "policy-engine",
        "rule": rule.name,
        "effect": rule.effect,
        "op": rule.op,
        "target": tgt,
        "reason": rule.reason,
        "corrective": _corrective(rule),
        "message": f"policy rule `{rule.name}` matched {rule.op} {tgt}",
    }


def evaluate(policy: Policy, state: dict, event: ToolEvent) -> list[dict]:
    """PURE rule evaluation against the CURRENT state — no state update.
    The hook calls this at PreToolUse. A disarmed policy produces NO
    block/notify findings (the hook surfaces the disarm advisory separately)."""
    findings: list[dict] = []
    if policy.armed:
        for rule in policy.rules:
            if _rule_triggers(rule, event, state):
                if rule.unless is not None and _unless_satisfied(rule.unless, state):
                    continue
                findings.append(make_finding(rule, event))
    return findings


def process_event(policy: Policy, state: dict, event: ToolEvent) -> list[dict]:
    """Evaluate rules against ``event`` using the CURRENT state, THEN update the
    state — UNLESS a block finding fired: a blocked action never executed, so it
    must not stamp gates/invalidations/labels (a denied `recon_e2e_harness.py`
    run must not satisfy `after exec "**/recon_e2e_harness.py"`). In the live
    hook the same contract holds structurally: evaluation happens at PreToolUse
    and updates at PostToolUse, which a blocked call never reaches."""
    findings = evaluate(policy, state, event)
    if policy.armed and not any(f.get("effect") == "block" for f in findings):
        _update_state(policy, state, event)
    return findings


def format_feedback(finding: dict) -> str:
    """The uniform corrective-feedback frame (ActPlane's shape, our wording).
    Names the rule, the target, the because-reason, and the corrective next
    step, and states plainly that a naive retry will not help."""
    trailer = json.dumps(
        {
            "rule": finding.get("rule"),
            "effect": finding.get("effect"),
            "retry_useful": False,
        },
        ensure_ascii=False,
    )
    return (
        f"[policy] Operation blocked by rule `{finding.get('rule')}`.\n"
        f"Target: {finding.get('op')} {finding.get('target')}\n"
        f"Reason: {finding.get('reason')}\n"
        f"Retrying the same operation will not succeed.\n"
        f"Next step: {finding.get('corrective')}\n"
        f"{trailer}"
    )


# ── state persistence (per-session, gitignored runtime path) ─────────────────
def load_state(path: str | Path) -> dict:
    path = Path(path)
    if not path.exists():
        return new_state()
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(obj, dict) and "gate_epoch" in obj:
            obj.setdefault("epoch", 0)
            obj.setdefault("inval_epoch", {})
            obj.setdefault("labels", {"session": []})
            return obj
    except (OSError, json.JSONDecodeError):
        pass
    return new_state()


def save_state(path: str | Path, state: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


# ── CLI: compile-check a policy file (read-only) ─────────────────────────────
def _cli(argv: list[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Compile-check a policy file.")
    ap.add_argument("policy", help="path to a .policy file")
    args = ap.parse_args(argv[1:])
    text = Path(args.policy).read_text(encoding="utf-8")
    pol = compile_policy(text)
    if pol.armed:
        print(f"OK: armed — {len(pol.sources)} source(s), {len(pol.rules)} rule(s)")
        return 0
    print("DISARMED (fail-closed) — compile errors:")
    for e in pol.errors:
        print(f"  - {e}")
    return 1


if __name__ == "__main__":
    import sys

    sys.exit(_cli(sys.argv))

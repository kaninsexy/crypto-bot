---
globs: [".claude/**", "docs/handoff_template.md"]
---

# Prompt construction rules — loaded for .claude/ and handoff docs

**D. Pre-trial gates carry verbatim into Claude Code prompts
from chat handoffs.** When the chat agent drafts a Claude
Code prompt from a handoff prompt, every numbered pre-trial
gate, scoping constraint, and "must hold before X" item from
the handoff copies verbatim into the Claude Code prompt — not
summarized, not paraphrased, not dropped because they "feel
covered" by track scope. Constraints not in the prompt do not
bind the agent. Today's Track C drift happened because gate
#8 ("first dev_cpcv trial single-pair before adding alts")
was in the chat handoff prompt but not in the Claude Code
prompt, so the literature stub the agent produced drifted to
multi-pair selection without violating any constraint
visible to it.

**E. Review agent output against original scoping, not just
against tests.** Two passes: (1) Claude Code self-check
before reporting completion — re-read the handoff prompt's
pre-trial gates and verify each produced artifact (literature
file, spec doc, manifest entry, code module) satisfies them.
A test-passing artifact that contradicts a scoping decision
is still wrong; surface as a drift flag, do not report
completion as clean. (2) Chat agent review after Claude Code
reports — open the produced artifact and compare substantive
content against each gate from the handoff. Drift detection
is the chat agent's job, not the implementation agent's, but
the implementation agent's self-check makes drift visible
earlier. Today's review missed Track C drift because the
review was against test results and Claude Code's own report,
not against the literature file's actual content vs. gate #8.

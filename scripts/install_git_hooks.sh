#!/usr/bin/env bash
# install_git_hooks.sh — install the commit-gate hooks.
# Ported 2026-09-02 from siamese-reconcile and adapted to crypto-bot's
# TWO-DIRECTORY layout (governance port, S1.4).
#
# THE PROBLEM THIS FIXES. crypto-bot has hook scripts in `.githooks/`
# (pre-commit: sacred diff block + magic-number gate + `pytest -m fast`;
# commit-msg: empty-message + `[mandate-H]` gate) whose headers say
# "Activated via: git config core.hooksPath .githooks" — but `core.hooksPath`
# was NEVER SET. git therefore ran `.git/hooks/`, which contained only the
# trial-queue validator. So the sacred-diff block and the [mandate-H] gate
# were not running at all, exactly like the jq-less PreToolUse hooks.
#
# Rather than flipping `core.hooksPath` (which would silently DROP the queue
# validator living in `.git/hooks/`), this installs marker-delimited blocks in
# `.git/hooks/` that CHAIN to `.githooks/` and to the Mandate L gate. Every
# existing check keeps running, none is weakened, and re-running this script
# replaces only our own block.
#
#   pre-commit  -> .githooks/pre-commit                  (sacred + magic + pytest)
#              -> scripts/pre_commit_backlog_check.sh    (Mandate L + sentinel)
#   commit-msg  -> .githooks/commit-msg "$@"             (message gate)
#   post-commit -> scripts/post_commit_verify.sh         (--no-verify detection;
#                  post-commit is NOT skipped by --no-verify)
#
# Idempotent and runnable standalone from the repo root.
set -euo pipefail

ROOT="${1:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
cd "$ROOT"
GIT_DIR="$(git rev-parse --git-dir 2>/dev/null || echo .git)"
mkdir -p "$GIT_DIR/hooks"

if [ -n "$(git config --get core.hooksPath || true)" ]; then
  echo "[install-hooks] NOTE: core.hooksPath is set to '$(git config --get core.hooksPath)'." >&2
  echo "[install-hooks]       This installer writes into $GIT_DIR/hooks, which git will IGNORE." >&2
  echo "[install-hooks]       Unset it (git config --unset core.hooksPath) or install by hand." >&2
fi

install_block() { # $1 = hook name, then N repo-relative scripts to chain
  local hook="$1"; shift
  local hf="$GIT_DIR/hooks/$hook"
  local b="# >>> crypto-bot $hook gate >>>"
  local e="# <<< crypto-bot $hook gate <<<"
  [ -f "$hf" ] || printf '#!/usr/bin/env bash\n' > "$hf"
  if grep -qF "$b" "$hf"; then
    awk -v b="$b" -v e="$e" '
      $0 == b { skip = 1; next }
      $0 == e { skip = 0; next }
      !skip   { print }
    ' "$hf" > "$hf.tmp" && mv "$hf.tmp" "$hf"
  fi
  {
    echo "$b"
    echo "# managed by scripts/install_git_hooks.sh — do not edit inside the markers"
    local s
    for s in "$@"; do
      echo "bash \"\$(git rev-parse --show-toplevel)/$s\" \"\$@\" || exit \$?"
    done
    echo "$e"
  } >> "$hf"
  chmod +x "$hf"
  echo "[install-hooks] $hook -> $* (marker block in $hf)"
}

install_block pre-commit  .githooks/pre-commit scripts/pre_commit_backlog_check.sh
install_block commit-msg  .githooks/commit-msg
install_block post-commit scripts/post_commit_verify.sh

echo "[install-hooks] done. Existing hook content outside the markers is untouched."

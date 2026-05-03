# Curator hourly cron

Add to user crontab via `crontab -e`:

```
0 * * * * cd ~/dev/crypto-bot && claude -p "$(cat .claude/agents/_curator_prompt.txt)" --agent curator >> .memory/T1_episodic/_state/curator.log 2>&1
```

Prerequisites:
- Resend account, verified domain, SPF/DKIM/DMARC verified.
- `RESEND_API_KEY` in `~/.crypto-bot.env`, sourced by login shell.
- `claude` CLI on `PATH` for the cron user (test with `which claude`
  outside the cron env; cron's PATH is minimal).
- `.memory/T2_semantic/facts.md` contains a line `kanin_email: <addr>`
  before Notifier can send (Notifier fails with `MISSING_RECIPIENT`
  otherwise).

Notes:
- The cron expression uses local time on macOS by default. If you
  prefer hourly UTC alignment, prefix with `TZ=UTC` in the crontab
  header.
- The curator agent reads both today and yesterday UTC dates and
  filters by mtime, so cross-day boundary runs (evening ICT) are
  handled.
- Path-allowlist hook restricts curator to writing only
  `.memory/T2_semantic/_pending_review.jsonl`. Strategist promotes
  from that queue at session start.
- Logs accumulate in `.memory/T1_episodic/_state/curator.log`.
  Rotate manually if it grows beyond a few MB; not auto-rotated.

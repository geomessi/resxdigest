# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

This repo contains two independent, single-file Python bots that post to Slack. Both use **zero external dependencies** — only Python stdlib (`os`, `json`, `re`, `urllib.request`, `subprocess`, `datetime`, `pathlib`).

- **`bot.py`** — the ResX Digest bot. Posts a curated, executive-briefing-style restaurant industry digest twice a week (Mon/Fri).
  Full docs: [`digest-bot/README.md`](./digest-bot/README.md) (how to run/deploy/troubleshoot) and [`digest-bot/CLAUDE.md`](./digest-bot/CLAUDE.md) (mission, architectural decisions and why, things never to change casually, known limitations, feedback history).
- **`social_bot.py`** — the ResX Social Bot. Posts a daily list of specific, real, immediately-postable social content (Reels/TikToks/carousel ideas/trending audio) to a different Slack channel.
  Full docs: [`social-bot/README.md`](./social-bot/README.md) and [`social-bot/CLAUDE.md`](./social-bot/CLAUDE.md).

**If you're about to edit `bot.py` or `social_bot.py`, read that bot's `CLAUDE.md` first.** Both files have accumulated hard-won, non-obvious design decisions (in particular around duplicate-post prevention and pinned/manual-input handling) that are easy to accidentally regress if you don't know why they're there.

## Required Environment Variables

Set at runtime; in CI they come from GitHub Secrets. The two bots use different variables and post to different channels — don't conflate them.

- `ANTHROPIC_API_KEY` — used by both bots, calls `claude-sonnet-4-6` via the raw Anthropic Messages API
- `SLACK_WEBHOOK_URL` — `bot.py` only, posts to `#news`
- `SLACK_SOCIAL_WEBHOOK_URL` — `social_bot.py` only, posts to `#social`

## Testing

All real end-to-end testing happens via GitHub Actions — not locally. Trigger a manual run via `/run-bot` (digest bot) or `gh workflow run social_bot.yml` (social bot; `-f dry_run=true` first is recommended). Pure functions in both files can and should be sanity-tested locally with fake data first — see each bot's `CLAUDE.md` for the pattern used throughout their development.

## Data Files (Live State)

`data/` contains files each bot manages every run — do not treat them as static. See each bot's own README for the full list and dedup semantics; a few of the less obvious ones:

- `bot.py`'s `data/last_post.json` and `social_bot.py`'s `data/last_social_post.json` are same-day duplicate-post guards — `bot.py`'s version uses atomic claim-before-work git locking (see `digest-bot/CLAUDE.md`); `social_bot.py`'s is a simpler check-then-act guard that has not yet received the same fix.
- `data/pinned_inputs.json` (digest bot) and `data/social_pinned_leads.json` (social bot) are manually-submitted lead queues — anything dropped in is treated as high-priority and is guaranteed to end up either in the next post or logged with a reason in `data/skipped_items_log.json` / `data/social_skipped_log.json`, never silently ignored.

To reset the digest bot's featured openings for a new season: run `/reset-openings` or clear `data/seen_openings.json` to `[]`.

## CI Behavior

- `bot.py` commits its own state files back to `main` internally (via `git_commit_and_push`, with retry-on-race logic) — there is no separate "commit data files" workflow step for it.
- `social_bot.py` does **not** commit its own state; `.github/workflows/social_bot.yml` does it in a plain shell step after the script runs, with no retry logic.
- Commits with `[skip ci]` in the message are expected bot-authored state updates — do not treat them as manual changes.
- `watchdog.yml` polls every 30 minutes and retriggers `news_bot.yml` if it detects a missed Mon/Fri run, by reading `bot.py`'s own claim file rather than GitHub Actions run status.

## Architecture Notes

- No SDK — all HTTP is raw `urllib.request` + `json`
- Uses Anthropic's built-in `web_search_20250305` tool for research
- `bot.py` schedule: `15 12 * * 1,5` (Mon + Fri, 12:15 UTC ≈ 8:15am ET)
- `social_bot.py` schedule: `15 12 * * *` (daily, 12:15 UTC ≈ 8:15am ET)
- Do not add external HTTP libraries or an SDK to either bot without discussing it first — "zero external dependencies" is a deliberate, consistently-maintained constraint across this repo's history.

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

ResX Digest is a single-file Python bot (`bot.py`, ~820 lines) that posts a curated restaurant news digest to Slack twice a week. It uses **zero external dependencies** — only Python stdlib (`os`, `json`, `re`, `urllib.request`, `datetime`, `pathlib`).

## Required Environment Variables

Both must be set at runtime; in CI they come from GitHub Secrets:

- `ANTHROPIC_API_KEY` — calls `claude-sonnet-4-6` via the Anthropic Messages API
- `SLACK_WEBHOOK_URL` — posts to Slack via Incoming Webhook

## Testing

All testing happens via GitHub Actions — not locally. Trigger a manual run from the Actions tab or run `/run-bot` to do it in one command.

## Data Files (Live State)

`data/` contains files the bot manages each run — do not treat them as static:

- `seen_openings.json` — flat list of restaurant names already featured; prevents repeats
- `competitors.json` — grows each run as the bot discovers new competitors; seeded from `SEED_COMPETITORS` in `bot.py`
- `watching.json` — "coming soon" restaurants tracked run-to-run; items graduate to "just opened" when confirmed open

To reset featured openings for a new season: run `/reset-openings` or clear `data/seen_openings.json` to `[]`.

## CI Behavior

After each bot run, the workflow auto-commits the three data files back to `main` with message `bot: update seen openings + competitors + watching [skip ci]`. These commits are expected — do not treat them as manual changes.

## Architecture Notes

- No SDK — all HTTP is raw `urllib.request` + `json`
- Uses Anthropic's built-in `web_search_20250305` tool for research
- CI schedule: `0 12 * * 1,5` (Mon + Fri, 12:00 UTC = 8am EDT)
- The `web_search` tool is enabled via the Anthropic API; do not add external HTTP libraries

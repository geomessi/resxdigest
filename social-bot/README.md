# ResX Social Bot (`social_bot.py`)

> This folder contains documentation only. The actual code lives at the repo root: [`../social_bot.py`](../social_bot.py), state files in [`../data/`](../data/), workflow in [`../.github/workflows/social_bot.yml`](../.github/workflows/social_bot.yml).
>
> For the operating philosophy, architectural decisions, and "don't touch this without thinking" guidance, see [`CLAUDE.md`](./CLAUDE.md) in this folder.

## Purpose

Posts a daily list of specific, real, immediately-postable social content to `#social` so the ResX team can repost it — Instagram Reels, TikToks, carousel ideas backed by real linked posts, and trending audio. It is explicitly **not** a "what's happening" summary: it's a to-do list. Every item tells the team exactly what to do (repost, build a carousel, comment on something) with a real link already in hand, and the team writes their own captions — the bot never generates copy.

**The bot is never empty** (post-2026-07-11 overhaul — see `CLAUDE.md`): there's always something worth posting, so it surfaces the best ~3–5 each day. Scoring *ranks* candidates; it does not gate the list down to zero. It won't pad with junk, but "nothing today" is treated as a failure, not a quiet day.

## How it works end-to-end

`social_bot.py`'s `main()` runs this pipeline:

1. **Same-day guard** — checks `data/last_social_post.json`; skips (unless `FORCE_POST=1` or `DRY_RUN=1`) if already posted today. Unlike the digest bot, this is a simple check-then-act guard with no atomic claim/race protection (see Known Limitations in `CLAUDE.md`).
2. **Load state**: `seen_ugc.json` (permanent dedup: exact URLs / moments / songs), `social_pinned_leads.json`, `social_skipped_log.json`, and `social_tracked_restaurants.json` (the always-check watchlist, both cities).
3. **`research_ugc()`** — one large Claude call with **`web_search` + `web_fetch`** (the article-link-mining engine) carrying the *entire* editorial judgment: the taste rubric (momentum / stop-scroll / desire-fit / timeliness / source-quality), a wide cultural aperture including standalone pop-culture, the tracked-restaurant watchlist, three-bucket classification (repost / post_idea / trending_audio), the link fallback ladder, and 1-5 self-scoring across the taste axes. The workflow is: search fresh (last-24h) coverage → `web_fetch` the article → mine the embedded Instagram/TikTok permalink. `call_anthropic` loops on `stop_reason == "pause_turn"` so multi-step search→fetch chains finish instead of truncating. Returns opportunities, audio, `pinned_rejected`, `considered_and_rejected`.
4. **`resolve_pinned()`** — cross-references pinned leads so every one ends up kept (`origin: "pinned"`) or logged, with a broken-link (`check_broken`) and duplicate (against `seen_urls` / `seen_moments`) backstop plus a safety net for anything the model didn't address.
5. **`tier_and_label()`** — the link fallback ladder: a real permalink → tier `post`; else a specific editorial article + the account → tier `lead` (team grabs the post); only truly linkless moments are dropped. Applies to both pinned and researched items — this replaced the old drop-if-no-permalink gate that was producing empty digests.
6. **Rank & take top-N** — researched items are sorted by `avg_score` (the taste axes; scoring drives *rank order* only, not an absolute cutoff) and the best `DAILY_TARGET_N` (~5) are kept. Pinned items are always kept. Never zero.
7. **`apply_diversity()`** — one item per `subject` per digest, pinned processed first so they win any conflict.
8. **`dedupe_audio()` + cross-run song dedup** — collapse duplicate song+artist within the run, and drop any song already featured before.
9. **`build_slack_blocks()`** — a flat, most-compelling-first list, each item tagged inline (`→ REPOST · NYC`, or `→ POST IDEA · LDN · LEAD`), with a "Trending Audio" section at the bottom and a forced-rerun banner under `FORCE_POST=1`. If the list is somehow empty it renders a loud ⚠️ warning, never a calm "nothing today."
10. **`post_to_slack()`** — skipped entirely under `DRY_RUN=1`, which instead prints the exact payload.
11. **Persist state** — `seen_ugc.json` (permanent retention: exact post/article URLs + `moment` + songs), `last_social_post.json`, `social_pinned_leads.json` (cleared), `social_skipped_log.json` — all skipped under `DRY_RUN=1`.

## Folder structure

```
social_bot.py                        # the entire bot — one file, no other modules
data/
  seen_ugc.json                       # permanent dedup (exact urls / moments / songs)
  last_social_post.json               # simple same-day guard (date + posted_at)
  social_pinned_leads.json            # manually-submitted leads queue (cleared each run)
  social_tracked_restaurants.json     # always-check watchlist, both cities ({name, city, handle?})
  social_skipped_log.json             # 30-day log of why a pinned lead / candidate wasn't included
.github/workflows/
  social_bot.yml                       # scheduled/manual trigger — also does its own git commit step in shell
```

## Environment variables

| Variable | Required | Set by | Purpose |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | Yes | GitHub Secret | Calls `claude-sonnet-4-6` via the raw Messages API |
| `SLACK_SOCIAL_WEBHOOK_URL` | Yes | GitHub Secret | Incoming Webhook for `#social` — **a different variable and channel** than the digest bot's `SLACK_WEBHOOK_URL` (`#news`) |
| `FORCE_POST` | No | `social_bot.yml`'s `force_post` workflow_dispatch input, defaults `"0"` | `"1"` posts even if already posted today; the Slack message gets a visible "forced/manual re-run" banner |
| `DRY_RUN` | No | `social_bot.yml`'s `dry_run` workflow_dispatch input, defaults `"0"` | `"1"` runs the full pipeline (real research) but prints the Slack payload instead of posting, and writes no state at all |

## How scheduling works

- **Cron**: `15 12 * * *` in `social_bot.yml` — every day at 12:15 UTC (8:15am ET).
- **No watchdog exists for this bot.** The digest bot's 2x/week cadence was confirmed to suffer from GitHub's low-frequency-schedule deprioritization badly enough to need `watchdog.yml`; this bot's daily cadence is frequent enough that the same problem hasn't been severe enough to require one *so far* — but the underlying GitHub behavior is the same, so it's not architecturally immune (see Future Improvements).
- **Manual trigger**: `workflow_dispatch` with `force_post` and `dry_run` boolean inputs.

## How to run locally

Set `ANTHROPIC_API_KEY` and `SLACK_SOCIAL_WEBHOOK_URL`, then run **`DRY_RUN=1 python social_bot.py`**. Always set `DRY_RUN=1` for local/manual runs unless you specifically intend to post to the real `#social` channel — unlike the digest bot (which no-ops all git operations locally via an `IN_CI` check), this bot has no automatic "don't actually do the real thing" behavior locally; `DRY_RUN` is the only safeguard, and it's opt-in.

## How to manually trigger it

No existing Claude Code skill targets this bot (the `/run-bot` skill only targets `news_bot.yml`). Use:
- `gh workflow run social_bot.yml` (optionally `-f dry_run=true` to preview without posting, or `-f force_post=true` to override the same-day guard).
- Or the Actions tab → **ResX Social Bot** → **Run workflow**.

## How to deploy

No build step — push to `main`. One-time setup: add `ANTHROPIC_API_KEY` and `SLACK_SOCIAL_WEBHOOK_URL` as repository secrets.

## Common failure modes

- **`research_ugc`'s JSON fails to parse** — caught, returns an empty result (`opportunities: [], audio: []`), which renders as "No social opportunities found today." Not a crash.
- **A pinned lead gets silently missed by the model** — caught by `resolve_pinned`'s safety net, logged as `not_addressed_by_model` in `social_skipped_log.json`, never silently dropped.
- **Zero non-pinned opportunities on a given day** — intended behavior (the score-threshold gate + "quality over quantity" instruction), not a bug.
- **Duplicate posts** — this bot has **no** claim-based git-race locking (unlike the digest bot). The same-day guard is check-then-act, and the git commit happens in `social_bot.yml`'s shell step with no retry logic. This bug class was confirmed and fixed in the digest bot but has not been ported here (see `CLAUDE.md`).
- **A link 403s on verification but is actually real** — `check_broken` is deliberately lenient (only a confirmed 404/410 counts as broken) specifically because Instagram/TikTok routinely 403 scripted requests.

## Troubleshooting

- **`data/social_skipped_log.json`** — every rejected candidate (pinned or researched) with a `reason` (`duplicate` / `broken_link` / `below_score_threshold` / `diversity_cap` / `not_addressed_by_model` / whatever the model self-reported) and a `detail`.
- **Console output** — `main()` prints `"Model returned N candidate opportunities, M audio items"`, then `"Publishing N opportunities (P pinned, R researched); S skipped this run"` — a quick read tells you whether a quiet day is "nothing was found" vs. "things were found but filtered."
- **`DRY_RUN=1`** locally or via `workflow_dispatch` is the fastest way to see exactly what would have posted, including full Slack Block Kit JSON, without touching Slack or any state file.

## How state / deduplication works

- **`seen_ugc.json`** — entries carry `url`/`date`, and (added later) `subject`/`song` for venue- and track-level dedup, not just exact-URL matching. 7-day lookback feeds the "already featured, don't repeat" prompt instruction; 14-day retention on disk.
- **`social_pinned_leads.json`** — a queue of `{"input": "...", "note": "..."}` entries; consumed (cleared to `[]`) every run once `resolve_pinned`'s safety net guarantees each one is accounted for.
- **`social_skipped_log.json`** — 30-day rolling log of every rejection, across both pinned and researched content.
- **`last_social_post.json`** — `{date, posted_at}` only. No `status: "running"/"failed"` states like the digest bot's `last_post.json` — this is the simpler, pre-fix version of that same mechanism.

## Future improvements / TODOs

*(Noted, not fixed, per the documentation-only scope of this pass.)*

- **Port the digest bot's claim-before-work git-race locking here.** This bot's same-day guard has the identical latent duplicate-post vulnerability that was confirmed and fixed in `bot.py` — it just hasn't been observed/triggered here yet, likely because a daily cadence hits GitHub's schedule-deprioritization problem less often than a 2x/week one.
- **Add a watchdog workflow** for `social_bot.yml`, mirroring `watchdog.yml` — currently nothing notices or retriggers a silently-dropped scheduled run for this bot.
- **Split pinned-lead resolution out of the single big `research_ugc` call**, mirroring the digest bot's dedicated `research_pinned_inputs`. Right now a pinned lead's fate depends on the same prompt that's simultaneously juggling three content buckets, trending audio, and scoring — a dedicated call would likely be more reliable, the same way splitting it out improved the digest bot's version.
- **Verify `posted_days_ago` against `web_search`'s own result metadata** instead of trusting the model's self-report. `validate_freshness` (added 2026-07-09) is a real hard gate, but it's only as accurate as what the model reports — `call_anthropic` currently discards everything except top-level `text` blocks, so any date metadata the search tool actually returned isn't available to check against.
- **No automated test suite.**

# CLAUDE.md — ResX Social Bot (`social_bot.py`)

This is the operating manual for `social_bot.py`. Read this before making *any* change to it. See [`README.md`](./README.md) in this folder for the operational/how-to-run side of things.

## Mission

Hand the ResX team a same-day, ready-to-act social content list — not a summary of what's happening, a to-do list of specific real posts to repost, remix into a carousel, or comment on. Every item must be something the team could act on in under 15 minutes, with a real link already attached.

## What success looks like

- Zero generated captions, comments, or hooks anywhere in the output — the team always writes their own words.
- Every link is either verified live, or (for pinned links specifically) at least not a *confirmed* dead link.
- It's normal and correct for the digest to be short (2-3 items) or empty some days — a padded list of mediocre content is treated as a worse outcome than an honest empty one.
- The same restaurant, trend, or song never resurfaces within a week without a genuinely new angle.
- A manually pinned lead always appears in the output or is explained in `social_skipped_log.json` — never silently dropped.

## What the bot should optimize for

1. **Freshness / pre-saturation over completeness.** Explicitly instructed to prefer something just starting to take off over something already fully saturated or only known via an old article.
2. **Source specificity over coverage.** A real, specific Instagram Reel/TikTok link beats a generic article every time; an article is only acceptable if the article itself *is* the story.
3. **Actionability over interestingness.** Every item must map to one of six concrete action types (see below) — "this is culturally relevant" isn't enough; the team must be able to *do* something with it in under 15 minutes.
4. **Brand voice discipline in classification, not in copy.** The "insider, cool girl, lowercase" voice governs how the bot *names* an opportunity (its `headline`/`idea`), not any caption text — the bot no longer writes captions at all (see below).

## Important architectural decisions and why they were made

### No generated captions, comments, or hooks — anywhere
Earlier versions of this bot suggested exact caption copy ("repost with 'your sign to book tonight'"). Direct user feedback reversed this explicitly: *"dont do captions and stuff."* The bot now only classifies the action type and supplies real links; the team always writes their own words. **Do not reintroduce generated copy** — this was a deliberate, explicitly-requested reversal, not an oversight to "fix."

### Six action types instead of a flat "opportunity"
A `repost` needs exactly one real post; a `carousel` needs 2+ real, distinct, linked posts (one per slide). An earlier flat schema produced vague, unbuildable carousel ideas — the canonical bad example baked into the prompt is `"carousel: 'before the match. after the match. the table in between.' fan village just opened, final july 19"`, which has no real posts behind it at all. The six types (`repost`/`carousel`/`story`/`comment`/`meme`/`inspo`) each have their own required fields, enforced by both the prompt schema and `format_ugc_item`'s per-type rendering dispatch.

### Explicit 1-5 self-scoring + a hard `SCORE_THRESHOLD` gate
"Quality over quantity" needed to be an *enforced mechanism*, not just a prompt request. The model self-scores every researched item across 5 axes (freshness/cultural_relevance/resx_relevance/source_quality/actionability); `avg_score()` computes the mean, and anything below `SCORE_THRESHOLD` (3.5) is dropped and logged — this is a real code-level gate, not advisory text. Pinned items explicitly bypass it (see below).

### Diversity cap (`apply_diversity`)
Caps the digest at one item per `subject` per run, so a single restaurant or trend can't crowd out everything else. Pinned items are processed first so they always win a conflict with organically-discovered content on the same subject.

### Pinned leads bypass scoring, get a dedicated resolution pass, and have a hard safety net
"I want a dedicated pinned leads system... it should override normal discovery and ranking... if it does not include it, it must explain exactly why" was explicit, direct user feedback. `resolve_pinned()` cross-references the model's response against the original `social_pinned_leads.json` queue and guarantees every single entry ends up either kept (`origin: "pinned"`, bypasses `SCORE_THRESHOLD` entirely) or logged with a reason — including a fallback `not_addressed_by_model` reason for anything the LLM's response simply forgot to mention. **This pattern was built here first**, then later ported and adapted into the digest bot's `pinned_inputs.json`/`process_pinned_inputs` — if you're improving one, check whether the improvement should also apply to the other.

### Lenient `check_broken`, not the stricter pattern
Only a confirmed 404/410 counts as broken; timeouts, blocks, and other errors are treated as inconclusive. This exists because Instagram/TikTok routinely 403 scripted HEAD requests for perfectly real, live content — rejecting a link Georgia is personally vouching for over a bot-blocking heuristic would be exactly the kind of "the bot ignored what I gave it" failure this system exists to prevent.

## Things that should never be changed without careful consideration

- **The "no captions/comments/hooks" rule.** Explicitly, directly requested by the user as a reversal of prior behavior. Re-adding generated copy repeats a corrected mistake.
- **`SCORE_THRESHOLD`'s value and the fact that pinned items bypass it.** Lowering the threshold re-admits weak content; removing the pinned bypass breaks the "pinned input must never be ignored" guarantee.
- **`resolve_pinned`'s safety net** (the `not_addressed_by_model` fallback, matched via exact `pinned_input` text). This is the actual mechanism that makes "never silently ignored" true.
- **The link-accuracy instructions in `research_ugc`'s prompt** ("never construct, guess, paraphrase, autocomplete, or recall a URL from memory"). This exists because a real incident occurred: a collab link the bot posted pointed to the wrong post entirely. This language is what prevents a repeat.
- **`apply_diversity`'s pinned-first ordering.** Reordering this would let organically-discovered content win a subject conflict against something Georgia explicitly pinned.

## Known limitations

- **No claim-based git-race locking** (see the digest bot's `CLAUDE.md` for what that is and why it exists). This bot's same-day guard is architecturally the *pre-fix* version of that same mechanism — theoretically vulnerable to the identical duplicate-post bug class, just not yet observed/triggered here (probably because a daily cadence hits GitHub's schedule-deprioritization problem less often than the digest bot's 2x/week cadence did).
- **Pinned-lead resolution is folded into the single big `research_ugc` call**, unlike the digest bot's dedicated `research_pinned_inputs` call. Simpler, but means a pinned lead's fate depends on the same prompt simultaneously juggling six action types, trending audio, tiering, and scoring.
- **No watchdog** for missed scheduled runs.
- **No automated test suite.**

## Common user feedback we've received

- *"The text is reading too much like AI slop and it's too long and overwhelming"* + *"dont do captions and stuff"* → led directly to dropping all generated caption/comment/hook text and to a full rewrite of the output format (flat ranked list, action-type tags, no per-item prose beyond a headline and real links).
- *"You keep repeating content week over week... you also are repeating songs across days"* → led to subject-level and song-level dedup (not just exact-URL matching) in `seen_ugc.json`.
- *"The [collab] link you sent is totally wrong... you need to be more careful with sending links"* → led to the explicit "never construct or recall a URL from memory, verify it matches" language now in the system and user prompts.
- *"I want a dedicated pinned leads system... pinned inputs should override normal discovery and ranking... if it does not include it, it must explain exactly why"* → led to `social_pinned_leads.json`, `social_skipped_log.json`, `resolve_pinned`, and the score-threshold bypass for pinned items. **This was built here before the equivalent existed in the digest bot** — the digest bot's later pinned-inputs system is a port/refinement of this one.
- *(From the digest bot, but architecturally relevant here too):* *"I've had to manually trigger the digest almost every time, and sometimes it posts twice."* The root cause (a same-day guard whose write can be lost in a git-push race) is structurally present in this bot too — see Known Limitations.

## Prompting philosophy

- **One large call carries the entire editorial judgment**, deliberately, unlike the digest bot's many-small-calls-plus-one-editor-pass architecture. This works here because the social bot's output is structurally simpler (one ranked list + one audio list) than the digest bot's five distinct sections — splitting it further hasn't been necessary yet, though see Future Improvements re: pinned-lead resolution specifically.
- **Concrete worked examples, including a "bad" one, drive quality more than abstract instructions.** The carousel bad/good example is the clearest instance of this — describing "carousels need real posts behind every slide" abstractly produced vague ideas; showing an actual bad example fixed it.
- **Explicit, numbered inclusion gates** (the four-question "would this make someone stop scrolling" test) rather than "use good judgment."
- **Link accuracy is repeated at every layer that touches a URL** — in the main research prompt, in the pinned-lead handling, and in the system prompt itself. This redundancy is intentional given the real wrong-link incident that motivated it.

## Editorial philosophy

- Brand voice ("insider, cool girl, lowercase, like a friend texting a tip") governs *naming* an opportunity, not writing content for it — the bot classifies and links, the team writes copy.
- Real and specific always beats broad and safe: a single well-verified Reel beats three vague "trend" mentions.
- Pinned/manual input is never second-guessed on relevance, only on being broken, a duplicate, or (rarely) genuinely irrelevant to the business.
- Six action types exist because "post this" is not one instruction — a repost, a carousel, and a comment are different amounts of work with different content requirements, and conflating them produced unbuildable output in the past.

## How to safely make changes

1. **If you change the JSON schema `research_ugc` returns**, update all three of: the prompt's own schema description, `format_ugc_item`'s per-type dispatch (repost/carousel/story/comment/meme/inspo each render differently), and `urls_in_item` (used for dedup extraction across all types) — these must stay in sync or a new field will silently fail to render or dedupe.
2. **Don't lower `SCORE_THRESHOLD` or remove the pinned-bypass** without discussing — both are direct responses to explicit feedback, not arbitrary tuning.
3. **If you're improving the pinned-lead mechanism here, check whether the digest bot's `pinned_inputs.json`/`process_pinned_inputs` should get the same improvement** (and vice versa) — they're related but have diverged; see each bot's Known Limitations for where.
4. **Preserve the "no captions" rule in any prompt rewrite** — it's easy to accidentally reintroduce copy-writing language while adjusting something else nearby (e.g. the `idea`/`headline` field description).

## How to test changes

- **`DRY_RUN=1 python social_bot.py`** locally is the primary safe iteration loop — runs the full pipeline (real API calls, real research) but prints the exact Slack payload instead of posting, and writes no state at all. This bot has no `IN_CI`-style automatic local safety net like the digest bot; `DRY_RUN` is opt-in and you should default to using it.
- **Sanity-test pure functions** (`avg_score`, `check_broken`, `apply_diversity`, `dedupe_audio`, `urls_in_item`, `format_ugc_item`, `format_audio_item`) with fake data and no API key by importing `social_bot` directly.
- Real end-to-end posting verification only via GitHub Actions (`gh workflow run social_bot.yml`, optionally `-f dry_run=true` first).

## Files that are important to understand before editing

- **`social_bot.py`** — everything lives here, no other modules.
- **`data/social_pinned_leads.json`** and **`data/social_skipped_log.json`** — read these for the real shape of pinned-lead state and rejection reasons.
- **`.github/workflows/social_bot.yml`** — note it still performs its own `git add`/`commit`/`push` in a shell step *after* `python social_bot.py` runs, unlike the digest bot where `bot.py` does all git operations itself internally. This is directly relevant to the "no claim-based locking" limitation above.

## Assumptions future Claude instances should know

- **"Georgia" is a real name baked directly into prompt text**, not a placeholder.
- **Posts to a different Slack channel via a different webhook variable** (`SLACK_SOCIAL_WEBHOOK_URL` → `#social`) than the digest bot (`SLACK_WEBHOOK_URL` → `#news`). Don't conflate the two when working across both bots.
- **"Zero external dependencies" applies here too** — stdlib only.
- **This bot and `bot.py` are fully independent** with no shared code, and have diverged in some conventions (this bot pioneered the pinned-leads pattern and the lenient `check_broken`; the digest bot pioneered claim-based git-race locking and the research→pool→editor architecture). A fix in one is not automatically present in, or even directly portable to, the other without adaptation.
- **This bot runs daily; the digest bot runs 2x/week.** That's why this bot hasn't needed a watchdog (yet) while the digest bot did.

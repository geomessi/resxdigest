"""
ResX Social Bot
Runs daily via GitHub Actions. Finds specific, real, repostable social opportunities
(Instagram reels / TikToks, carousel ideas backed by real posts, trending audio) for
NYC and London and posts them to #social for the team to repost. Quality over quantity —
some days may have few or no opportunities.
"""

import os
import json
import re
import urllib.request
import datetime
from pathlib import Path

ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]
SLACK_WEBHOOK_URL  = os.environ["SLACK_SOCIAL_WEBHOOK_URL"]

SEEN_UGC_FILE = Path("data/seen_ugc.json")
LAST_POST_FILE = Path("data/last_social_post.json")
PINNED_LEADS_FILE = Path("data/social_pinned_leads.json")
SKIPPED_LOG_FILE = Path("data/social_skipped_log.json")

SCORE_THRESHOLD = 3.5  # average of 5 axes, each 1-5; below this a researched item is dropped
SCORE_AXES = ("freshness", "cultural_relevance", "resx_relevance", "source_quality", "actionability")

NYC_SIGNAL_ACCOUNTS = [
    "@tinx (aspirational 25-35 NYC city life, Rich Mom energy)",
    "@dinnerserviceny (hospitality insider, restaurant industry pulse)",
    "@nolitadirtbag (downtown NYC cultural barometer, Dimes Square / Nolita scene)",
    "@chatprojectpal (things to do with friends, social plans lens)",
    "@juliamervis (normal cool girl in NYC, 25-35 taste)",
]
LONDON_SIGNAL_ACCOUNTS = [
    "@realhousewivesofclapton (London equivalent of Nolita Dirtbag, east London creative scene)",
    "@socks_house_meeting (art school / high-fashion London scene)",
    "@dinnerbyben (London restaurant insider content)",
    "@prettylittlelondon (aspirational London lifestyle, going out)",
    "@poundlandbandit (broader London culture meme account)",
]


def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text())
    return default


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def call_anthropic(messages: list, system: str, max_tokens: int = 2000) -> str:
    payload = json.dumps({
        "model": "claude-sonnet-4-6",
        "max_tokens": max_tokens,
        "system": system,
        "messages": messages,
        "tools": [{"type": "web_search_20250305", "name": "web_search"}],
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())

    return "".join(
        block.get("text", "")
        for block in data.get("content", [])
        if block.get("type") == "text"
    )


def post_to_slack(blocks: list):
    payload = json.dumps({"blocks": blocks}).encode()
    req = urllib.request.Request(
        SLACK_WEBHOOK_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"Slack error {e.code}: {body}")
        raise


def safe_link(url: str, label: str) -> str:
    url = url.replace("&", "&amp;").replace("<", "").replace(">", "").replace("|", "%7C")
    label = label.replace("<", "").replace(">", "").replace("|", "-").replace("&", "&amp;")
    return f"<{url}|{label}>"


def research_ugc(seen_urls: set, seen_subjects: set, seen_songs: set, pinned_leads: list) -> dict:
    seen_str = "\n".join(f"- {u}" for u in list(seen_urls)[:60]) if seen_urls else "none"
    subjects_str = "\n".join(f"- {s}" for s in sorted(seen_subjects)) if seen_subjects else "none"
    songs_str = "\n".join(f"- {s}" for s in sorted(seen_songs)) if seen_songs else "none"
    signal = ", ".join(NYC_SIGNAL_ACCOUNTS + LONDON_SIGNAL_ACCOUNTS)
    pinned_str = (
        "\n".join(f'- "{p.get("input", "")}"' + (f'  (note: {p["note"]})' if p.get("note") else "")
                   for p in pinned_leads)
        if pinned_leads else "none"
    )

    prompt = f"""
You are a social media strategist for ResX — a last-minute restaurant reservation app for
25-35 year olds in NYC and London. Each morning you send the team the highest-leverage social
opportunities for today. This is not a news summary of what's happening in NYC/London — every
item must be something that makes the team think "we should post this right now."

Target audience vibe (use as calibration only, do NOT cite): {signal}

Today is {datetime.date.today().strftime("%A, %B %d, %Y")}. Search for content from the last
24 hours only. Nothing older — prioritize things just starting to take off (posted minutes ago,
soft-opened yesterday, announced this morning) over stuff that's already fully saturated or that
you'd only know about from an old article.

Before including anything, all four must be true:
1. Would this make someone stop scrolling?
2. Does our audience actually care?
3. Would waiting until tomorrow make it stale?
4. Can the team execute this in under 15 minutes?
If not, drop it. QUALITY OVER QUANTITY — 5 exceptional opportunities beats 25 mediocre ones.
It's completely fine to return fewer than usual, even 0, on a quiet day. Never pad the list.

PINNED LEADS — HIGHEST PRIORITY. Georgia has manually flagged these links/topics. Research each
one and build a real opportunity around it (find the actual specific post(s) behind it if it's
a bare topic). Every pinned lead below MUST end up either as an opportunity with
"origin": "pinned" and "pinned_input" set to the exact text below, OR as an entry in
"pinned_rejected" with the exact "pinned_input" text and a reason — never just omit one
silently. Only reject a pinned lead for one of: it's a duplicate of something already featured
(see the "already featured" lists below), it's genuinely irrelevant to ResX's audience/brand, or
you cannot find any real content behind it. Pinned leads are NOT subject to the stop-scrolling
gate above or the scoring rubric below — Georgia already decided they're worth including.

Pinned leads to address:
{pinned_str}

PRIORITIZE IN THIS ORDER:
- Tier 1 (should be most of the list): celebrity sightings (dining, spotted somewhere, viral
  date nights), pop-culture moments the team could tie a restaurant/going-out angle to (award
  shows, viral sports moments, reality TV, major concerts, TikTok drama), restaurants suddenly
  everyone wants (viral TikTok spot, impossible reservation, secret menu, one-week collab/pop-up).
- Tier 2 (secondary): FOMO-inducing openings and activations (rooftop, hotel, luxury brand café,
  fan villages, food festivals, weekend-only experiences).
- Tier 3 (rare, only with an obvious ResX angle): lifestyle moments (heatwave, marathon, Pride,
  holiday weekends, first day of outdoor dining).

ALSO look for "internet's winners" — a genuinely great post from an adjacent hospitality, hotel,
travel, city-guide, or lifestyle account that makes you think "damn, I wish we'd made that." Not
something to repost as ResX's own content — creative inspiration for the team, tagged as `inspo`.

SOURCE QUALITY: prioritize Instagram Reels, TikTok, X, Threads, and posts from creator/
restaurant/hotel/brand/celebrity accounts. Do NOT send generic news articles, press releases,
tourism sites, event calendars, or "things to do this weekend" listicles — unless the article
itself IS the story. If a real-world moment is worth surfacing but you can't find an actual
specific post about it, DROP IT rather than report it with only an article link.

LINK ACCURACY IS CRITICAL. Only use a URL you actually retrieved from a web_search result —
never construct, guess, paraphrase, autocomplete, or recall a URL from memory. Before including
a post, confirm the URL you're citing is the one the search result actually returned, and that
it genuinely matches the content you're describing (right restaurant, right collab, right
video). If you're not certain a link is correct, drop the item rather than guess.

FRESHNESS ACROSS DAYS. Do not repeat a restaurant, venue, creator, or topic already featured in
the last 7 days (list below) unless there's a genuinely new, specifically-named development —
never repeat just because it's still trending. Same for songs: don't reuse one featured this week.

Restaurants/venues/topics already featured in the last 7 days — do not repeat these:
{subjects_str}

Songs already featured in the last 7 days — do not reuse these:
{songs_str}

Every opportunity needs a concrete action. Do NOT write captions, comments, or hooks — just
identify the opportunity, its type, and the real link(s). The team writes their own copy.
Six action types:
- repost: one specific post (reel/TikTok) worth reposting as-is. Needs: post_url, creator
  (handle/name), creator_url (optional link to their account).
- carousel: an original idea built from several real existing posts, one per slide — only
  valid if every slide has a real post behind it. Needs: slides (2+ entries, each
  {{"label": "...", "url": "..."}}).
- story: one specific reel/post worth reposting to Story specifically. Needs: post_url.
- comment: a brand/account post worth engaging via comment (no comment text — just what to
  comment on). Needs: target_url, target_label (whose post / what it is).
- meme: a meme format or trend taking over that ResX could participate in. Needs:
  reference_url (optional — the origin post/template, if findable).
- inspo: a great post from an adjacent account, for creative inspiration only. Needs:
  source_url, creator, creator_url (optional).

Bad (vague, no real posts behind it): "carousel: 'before the match. after the match. the table
in between.' fan village just opened, final july 19"
Good (real idea, backed by specific linked posts): a carousel titled "5 most-viral ice cream
spots in nyc right now," backed by 5 actual posts, one per spot, each with its own link.

For every opportunity also return:
- headline: what it is, one line, brand voice, no caption copy. Max 10 words.
- subject: the restaurant/venue/creator/topic this is about, max 4 words. Used only for
  dedup, not shown to the team — must name the actual specific thing, not the idea.
- city: "NYC", "LDN", or "BOTH"
- origin: "researched" for everything you found yourself, or "pinned" for a pinned lead
  (see above) — set "pinned_input" too when origin is "pinned".

SCORING (researched items only — skip this for pinned items). Rate each researched opportunity
1-5 on each axis: freshness (how new/pre-saturation), cultural_relevance (does the audience
actually care), resx_relevance (does it fit a restaurant/going-out angle), source_quality (real
Reel/TikTok/account vs. weak source), actionability (can the team execute it in under 15 min).
Be honest — these scores decide what actually gets posted, so don't inflate them.

DIVERSITY. Don't return more than one opportunity about the same venue/topic/creator/song unless
they're genuinely distinct angles — and if they are distinct, give them different `subject`
values so it's clear they're not duplicates of each other.

Also report anything you researched and actively decided NOT to include, so Georgia can see why
something didn't make it — as "considered_and_rejected": brief subject/url/reason for each
(best effort, doesn't need to be exhaustive).

Return opportunities already ordered most-compelling-first.

Separately, look for trending audio: a song/sound worth using over a dining or going-out reel.
Just the track and a link — no explanation of what content it suits. Return 0-3, only if
genuinely trending today. For each: song, artist, url (Spotify/Apple Music/TikTok sound link).

Do NOT include any of these URLs which have already been sent:
{seen_str}

Return ONLY a valid JSON object, no markdown:
{{
  "opportunities": [
    {{"type": "repost|carousel|story|comment|meme|inspo", "origin": "researched|pinned",
      "pinned_input": "...", "headline": "...", "subject": "...", "city": "...",
      "scores": {{"freshness": 1, "cultural_relevance": 1, "resx_relevance": 1,
                  "source_quality": 1, "actionability": 1}},
      "post_url": "...", "creator": "...", "creator_url": "...",
      "slides": [{{"label": "...", "url": "..."}}], "target_url": "...", "target_label": "...",
      "reference_url": "...", "source_url": "..."}}
  ],
  "audio": [
    {{"song": "...", "artist": "...", "url": "..."}}
  ],
  "pinned_rejected": [
    {{"pinned_input": "...", "reason": "duplicate|irrelevant|no_content_found", "detail": "..."}}
  ],
  "considered_and_rejected": [
    {{"subject": "...", "url": "...", "reason": "...", "source_type": "researched"}}
  ]
}}
Only include the fields relevant to that opportunity's type — omit the rest. Omit "scores" for
pinned items.
"""

    result = call_anthropic(
        messages=[{"role": "user", "content": prompt}],
        system=(
            "You are a culturally plugged-in social media strategist writing for ResX — "
            "a NYC and London restaurant reservation app. Brand voice: insider, cool girl, "
            "lowercase, like a friend texting a tip. Never try-hard, never corporate, never "
            "AI-sounding. Specific cultural references over generic ones. "
            "Brevity is the brand voice — cut every word that isn't load-bearing. "
            "You surface opportunities and real links — you never write captions, comments, "
            "or hooks; that's for the team. "
            "Every opportunity must be backed by real, specific, existing posts you actually "
            "found — never invent a concept with nothing behind it. "
            "Accuracy matters more than coverage: only cite a URL you actually got back from "
            "a web_search result, never one you constructed or recalled from memory, and never "
            "attach a URL to a description it doesn't actually match. When in doubt, drop it. "
            "Score researched items honestly — inflated scores defeat the point of scoring. "
            "Never silently drop a pinned lead — always report it as included or rejected. "
            "Return only a valid JSON object, no markdown."
        ),
        max_tokens=3000,
    )

    empty = {"opportunities": [], "audio": [], "pinned_rejected": [], "considered_and_rejected": []}
    try:
        clean = re.sub(r"```[a-z]*", "", result).strip().strip("`").strip()
        start = clean.index("{")
        data, _ = json.JSONDecoder().raw_decode(clean, start)
        if not isinstance(data, dict):
            return empty
        return {
            "opportunities": data.get("opportunities", []) or [],
            "audio": data.get("audio", []) or [],
            "pinned_rejected": data.get("pinned_rejected", []) or [],
            "considered_and_rejected": data.get("considered_and_rejected", []) or [],
        }
    except Exception as e:
        print(f"Error parsing UGC results: {e}")
        return empty


def urls_in_item(item: dict) -> list:
    urls = [item[k] for k in ("post_url", "target_url", "reference_url", "source_url") if item.get(k)]
    urls += [s["url"] for s in item.get("slides", []) or [] if s.get("url")]
    return urls


def avg_score(item: dict) -> float:
    scores = item.get("scores") or {}
    vals = [scores.get(axis, 0) for axis in SCORE_AXES]
    return sum(vals) / len(vals) if vals else 0.0


def check_broken(url: str, timeout: int = 5) -> bool:
    """Best-effort dead-link check. Only returns True on a definitive 404/410 —
    network errors, timeouts, and blocks (common on Instagram/TikTok) are treated
    as inconclusive rather than broken, so we never punish a real link we can't verify."""
    if not url or not url.startswith(("http://", "https://")):
        return True
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status in (404, 410)
    except urllib.error.HTTPError as e:
        return e.code in (404, 410)
    except Exception:
        return False


def resolve_pinned(pinned_leads: list, opportunities: list, pinned_rejected: list,
                    seen_urls: set, seen_subjects: set, today_iso: str) -> tuple:
    """Cross-references the model's response against the pinned-leads queue so every
    pinned lead ends up either kept or logged as skipped — never silently dropped,
    even if the model forgot to mention one."""
    kept = []
    skips = []
    matched_inputs = set()

    for item in opportunities:
        if item.get("origin") != "pinned":
            continue
        pinned_input = item.get("pinned_input", "")
        matched_inputs.add(pinned_input)

        urls = urls_in_item(item)
        subject = (item.get("subject") or "").strip().lower()
        dup_url = next((u for u in urls if u in seen_urls), None)
        if dup_url or (subject and subject in seen_subjects):
            skips.append({
                "date": today_iso, "pinned_input": pinned_input, "subject": item.get("subject", ""),
                "url": dup_url or (urls[0] if urls else ""), "reason": "duplicate",
                "duplicate_match": dup_url or subject, "source_type": "pinned",
            })
            continue

        broken = next((u for u in urls if check_broken(u)), None)
        if broken:
            skips.append({
                "date": today_iso, "pinned_input": pinned_input, "subject": item.get("subject", ""),
                "url": broken, "reason": "broken_link", "source_type": "pinned",
            })
            continue

        kept.append(item)

    for rej in pinned_rejected:
        pinned_input = rej.get("pinned_input", "")
        matched_inputs.add(pinned_input)
        skips.append({
            "date": today_iso, "pinned_input": pinned_input, "subject": "",
            "url": "", "reason": rej.get("reason", "irrelevant"), "detail": rej.get("detail", ""),
            "source_type": "pinned",
        })

    for lead in pinned_leads:
        lead_input = lead.get("input", "")
        if lead_input not in matched_inputs:
            skips.append({
                "date": today_iso, "pinned_input": lead_input, "subject": "", "url": "",
                "reason": "not_addressed_by_model", "source_type": "pinned",
            })
            matched_inputs.add(lead_input)

    return kept, skips, matched_inputs


def apply_diversity(ordered_items: list, today_iso: str) -> tuple:
    """Caps the digest at one item per subject (venue/topic/creator/song), in the given
    priority order — pass pinned items first so they win any conflict with researched ones."""
    kept = []
    skips = []
    seen_this_run = set()
    for item in ordered_items:
        subject = (item.get("subject") or "").strip().lower()
        if subject and subject in seen_this_run:
            skips.append({
                "date": today_iso, "pinned_input": item.get("pinned_input", ""),
                "subject": item.get("subject", ""),
                "url": (urls_in_item(item) or [""])[0],
                "reason": "diversity_cap",
                "detail": f"already have an item for '{item.get('subject', '')}' in this digest",
                "source_type": item.get("origin", "researched"),
            })
            continue
        if subject:
            seen_this_run.add(subject)
        kept.append(item)
    return kept, skips


def dedupe_audio(audio: list) -> list:
    seen = set()
    out = []
    for a in audio:
        key = f"{a.get('song', '').strip().lower()} - {a.get('artist', '').strip().lower()}"
        if key in seen:
            continue
        seen.add(key)
        out.append(a)
    return out


def format_ugc_item(item: dict) -> str:
    item_type = item.get("type", "repost")
    headline  = item.get("headline", "")
    city      = item.get("city", "BOTH").upper()
    tag       = f"→ {item_type.upper()}  ·  {city}"
    header    = f"{tag}\n*{headline}*"

    if item_type == "carousel":
        lines = [header]
        for slide in item.get("slides", []) or []:
            label = slide.get("label", "")
            url   = slide.get("url", "")
            if url:
                lines.append(f"  •  {safe_link(url, label or 'open')}")
        return "\n".join(lines)

    if item_type == "story":
        url = item.get("post_url", "")
        link_str = f"  {safe_link(url, 'reel')}" if url else ""
        return f"{header}{link_str}"

    if item_type == "comment":
        url   = item.get("target_url", "")
        label = item.get("target_label", "post")
        link_str = f"  {safe_link(url, label)}" if url else ""
        return f"{header}{link_str}"

    if item_type == "meme":
        url = item.get("reference_url", "")
        link_str = f"  {safe_link(url, 'reference')}" if url else ""
        return f"{header}{link_str}"

    if item_type == "inspo":
        url        = item.get("source_url", "")
        creator_url = item.get("creator_url", "")
        link_str    = f"  {safe_link(url, 'post')}" if url else ""
        creator_str = f"  ·  {safe_link(creator_url, 'creator')}" if creator_url else ""
        return f"{header}{link_str}{creator_str}"

    # repost (default)
    url         = item.get("post_url", "")
    creator_url = item.get("creator_url", "")
    link_str    = f"  {safe_link(url, 'reel')}" if url else ""
    creator_str = f"  ·  {safe_link(creator_url, 'creator')}" if creator_url else ""
    return f"{header}{link_str}{creator_str}"


def format_audio_item(item: dict) -> str:
    song   = item.get("song", "")
    artist = item.get("artist", "")
    url    = item.get("url", "")
    label  = f"{song} — {artist}" if artist else song
    link_str = f"  {safe_link(url, 'listen')}" if url else ""
    return f"*{label}*{link_str}"


def build_slack_blocks(date_str: str, items: list, audio: list, forced: bool = False) -> list:
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"Social Opportunities  ·  {date_str}"},
        }
    ]

    if forced:
        blocks.append({
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": "⚠️ *forced/manual re-run* — a post already went out today, this is a manual override (FORCE_POST=1)",
            }],
        })

    if items:
        blocks.append({"type": "divider"})
        for item in items:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": format_ugc_item(item)},
            })

    if not items:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "_No social opportunities found today._"},
        })

    if audio:
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*Trending Audio*"},
        })
        for item in audio:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": format_audio_item(item)},
            })

    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": "ResX Social Bot  ·  Powered by Claude  ·  Daily"}],
    })

    return blocks


def main():
    today = datetime.date.today()
    today_str = today.strftime("%B %d, %Y")
    today_iso = today.isoformat()
    dry_run = os.environ.get("DRY_RUN") == "1"
    force_post = os.environ.get("FORCE_POST") == "1"
    print(f"Running ResX Social Bot — {today_str}" + (" [DRY RUN]" if dry_run else ""))

    last_post = load_json(LAST_POST_FILE, {})
    already_posted_today = last_post.get("date") == today_iso
    forced_rerun = already_posted_today and force_post

    if already_posted_today and not force_post and not dry_run:
        print(
            f"Already posted today ({today_iso}) at {last_post.get('posted_at', 'unknown time')} "
            f"— skipping to avoid a duplicate post. Set FORCE_POST=1 to override."
        )
        return

    # Load seen URLs (7-day rolling window)
    seen_raw = load_json(SEEN_UGC_FILE, [])
    cutoff = (today - datetime.timedelta(days=7)).isoformat()
    recent = [e for e in seen_raw if e.get("date", "") >= cutoff]
    seen_urls = {e["url"] for e in recent if e.get("url")}
    seen_subjects = {e["subject"] for e in recent if e.get("subject")}
    seen_songs = {e["song"] for e in recent if e.get("song")}
    print(
        f"Loaded {len(seen_urls)} seen URLs, {len(seen_subjects)} seen subjects, "
        f"{len(seen_songs)} seen songs for dedup"
    )

    pinned_leads = load_json(PINNED_LEADS_FILE, [])
    skipped_log = load_json(SKIPPED_LOG_FILE, [])
    print(f"Loaded {len(pinned_leads)} pinned lead(s)")

    print("Researching...")
    result = research_ugc(seen_urls, seen_subjects, seen_songs, pinned_leads)
    opportunities = result["opportunities"]
    audio = dedupe_audio(result["audio"])
    print(f"Model returned {len(opportunities)} candidate opportunities, {len(audio)} audio items")

    # Resolve pinned leads first — every pinned lead must end up kept or logged, never dropped
    pinned_kept, pinned_skips, _ = resolve_pinned(
        pinned_leads, opportunities, result["pinned_rejected"], seen_urls, seen_subjects, today_iso
    )

    # Researched candidates must clear the scoring gate
    researched_kept = []
    score_skips = []
    for item in opportunities:
        if item.get("origin") == "pinned":
            continue
        score = avg_score(item)
        if score < SCORE_THRESHOLD:
            score_skips.append({
                "date": today_iso, "subject": item.get("subject", ""),
                "url": (urls_in_item(item) or [""])[0], "reason": "below_score_threshold",
                "detail": f"avg score {score:.1f} < {SCORE_THRESHOLD}", "source_type": "researched",
            })
            continue
        researched_kept.append(item)

    # Model's own self-reported misses
    considered_rejected_log = [
        {
            "date": today_iso, "subject": c.get("subject", ""), "url": c.get("url", ""),
            "reason": c.get("reason", "not specified"), "source_type": c.get("source_type", "researched"),
        }
        for c in result["considered_and_rejected"]
    ]

    # Diversity cap: 1 item per subject per digest, pinned wins any conflict
    final_items, diversity_skips = apply_diversity(pinned_kept + researched_kept, today_iso)

    new_skip_entries = pinned_skips + score_skips + considered_rejected_log + diversity_skips
    print(
        f"Publishing {len(final_items)} opportunities "
        f"({len(pinned_kept)} pinned, {len(final_items) - len(pinned_kept)} researched); "
        f"{len(new_skip_entries)} skipped this run"
    )

    blocks = build_slack_blocks(today_str, final_items, audio, forced=forced_rerun)

    if dry_run:
        print("[DRY RUN] Final Slack payload (not posted, no state written):")
        print(json.dumps({"blocks": blocks}, indent=2))
        return

    post_to_slack(blocks)
    print("Posted to #social" + (" [forced re-run]" if forced_rerun else ""))

    # Save seen URLs/subjects/songs (keep last 14 days to cap file size) — only for what posted
    new_entries = [
        {"url": url, "date": today_iso, "subject": item.get("subject", "")}
        for item in final_items
        for url in urls_in_item(item)
    ]
    new_entries += [
        {
            "url": item["url"],
            "date": today_iso,
            "song": f"{item.get('song', '')} - {item.get('artist', '')}",
        }
        for item in audio
        if item.get("url")
    ]
    keep_cutoff = (today - datetime.timedelta(days=14)).isoformat()
    all_entries = [e for e in seen_raw if e.get("date", "") >= keep_cutoff] + new_entries
    save_json(SEEN_UGC_FILE, all_entries)
    save_json(LAST_POST_FILE, {
        "date": today_iso,
        "posted_at": datetime.datetime.utcnow().isoformat() + "Z",
    })

    # Pinned-leads queue is consumed each run — every lead was resolved above
    save_json(PINNED_LEADS_FILE, [])

    # Skipped log: append this run's rejections, capped to a 30-day rolling window
    skip_keep_cutoff = (today - datetime.timedelta(days=30)).isoformat()
    all_skips = [e for e in skipped_log if e.get("date", "") >= skip_keep_cutoff] + new_skip_entries
    save_json(SKIPPED_LOG_FILE, all_skips)

    print("Done ✓")


if __name__ == "__main__":
    main()

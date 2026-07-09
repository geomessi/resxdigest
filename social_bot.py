"""
ResX Social Bot
Runs daily via GitHub Actions, posts to #social. A social media editor, not a hospitality
newsletter: every item answers "what should ResX post today", never "what happened today."

Every opportunity is exactly one of three buckets:
  1. REPOST     — one specific, direct post-level link (IG post/reel, TikTok, X, Threads)
  2. POST_IDEA  — a concrete, immediately executable idea backed by 2+ real linked posts
  3. TRENDING_AUDIO — song + artist + link only, no explanation

No generated captions/comments/copy — the bot surfaces opportunities and real links, the
team writes their own words. A hard, deterministic gate (validate_post_urls) rejects any
repost/post_idea lacking a real post-level URL — missing is better than wrong. Quality over
quantity — some days may have few or no opportunities.
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
FRESHNESS_CUTOFF_DAYS = 3  # hard gate on posted_days_ago; independent of the self-rated freshness axis

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
You are a social media editor for ResX — a last-minute restaurant reservation app for 25-35
year olds in NYC and London — not a hospitality newsletter writer. The question you're
answering every morning is "what should ResX post today?" — never "what happened today?"
You spend all day on Instagram and TikTok and have incredible taste.

Target audience vibe (use as calibration only, do NOT cite): {signal}

Today is {datetime.date.today().strftime("%A, %B %d, %Y")}. Search for content from the last
24 hours only. Nothing older — prioritize things just starting to take off (posted minutes ago,
soft-opened yesterday, announced this morning) over stuff that's already fully saturated or that
you'd only know about from an old article.

For every REPOST or POST_IDEA, report "posted_days_ago" — the actual number of days since the
specific post was published, based on what the web_search result actually shows (a timestamp,
an "X days/weeks ago" snippet, a dateline). Never guess or assume 0 — if you can't tell how old
a post actually is from what you retrieved, report 999 rather than assuming it's fresh. For a
POST_IDEA backed by multiple posts, report the age of the OLDEST of the backing posts.

Before including anything, ask: would this feel native on Instagram Stories today? If not, skip
it. Every item should make the team think "I wish we'd thought of that." If it doesn't create
that reaction, don't include it. QUALITY OVER QUANTITY — 5 exceptional opportunities beats 25
mediocre ones. It's completely fine to return fewer than usual, even 0, on a quiet day. Never
pad the list.

PINNED LEADS — HIGHEST PRIORITY. Georgia has manually flagged these links/topics. Research each
one and build a real opportunity around it (find the actual specific post(s) behind it if it's
a bare topic). Every pinned lead below MUST end up either as an opportunity with
"origin": "pinned" and "pinned_input" set to the exact text below, OR as an entry in
"pinned_rejected" with the exact "pinned_input" text and a reason — never just omit one
silently. Only reject a pinned lead for one of: it's a duplicate of something already featured
(see the "already featured" lists below), it's genuinely irrelevant to ResX's audience/brand, or
you cannot find any real content behind it. Pinned leads are exempt from the "would this feel
native" gate and the scoring rubric below — Georgia already decided they're worth including —
but NOT exempt from needing a real, direct, post-level link (see below); a pinned lead you can't
back with a valid post link still gets rejected with reason "no_content_found".

Pinned leads to address:
{pinned_str}

BEST SOURCES: creators, restaurants, hotels, hospitality brands, food creators, city creators,
lifestyle creators, fashion creators, sports creators, pop culture creators. This is the feed of
someone who spends all day on Instagram and TikTok, not a trade publication. Actively search for
what these accounts are ALREADY posting and discussing — a collaboration or moment that's
blowing up on social but hasn't been written up anywhere yet is exactly what you should surface.

WHAT'S WORTH LOOKING FOR (this shapes what you search for, but every single result still has to
come out the other side as one of the three buckets below — no exceptions):
- Celebrity sightings, viral date nights, restaurants suddenly everyone wants (viral TikTok spot,
  impossible reservation, secret menu, one-week collab/pop-up) — the majority of what you find.
- FOMO-inducing openings and activations (rooftop, hotel, luxury brand café, fan villages, food
  festivals, weekend-only experiences).
- Rarely, only with an obvious ResX angle: lifestyle moments (heatwave, marathon, Pride, holiday
  weekends, first day of outdoor dining).
- Predictive, location-based content: if a post (real estate, gossip, paparazzi, etc.) reveals
  where a celebrity or notable couple lives, is moving to, or was recently spotted near — a
  specific address or tight neighborhood, not just "NYC" — that's the trigger for a POST_IDEA:
  research 3 real, specific, currently-open restaurants/bars in that exact neighborhood they'd
  plausibly be spotted at next. Never guess at restaurants — search for and confirm real spots
  actually in that area.
- A genuinely great post from an adjacent hospitality/hotel/travel/lifestyle account that makes
  you think "damn, I wish we'd made that" — surface it as a REPOST or the seed of a POST_IDEA,
  never invent new content around it that isn't there.

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

EVERY ITEM IS EXACTLY ONE OF THREE BUCKETS. No other shape is valid — if something doesn't
cleanly fit one of these three, drop it rather than force it in.

1. REPOST — a specific Instagram Reel, Instagram post, TikTok, X post, or Threads post ResX
   could repost to Stories today.
   - post_url: REQUIRED, must link DIRECTLY to the post itself.
   - NEVER a profile link. NEVER a restaurant website. NEVER an article. NEVER a guide or
     directory. If you can't find a specific post behind a real moment, skip the item entirely
     — do not report it with only an article or account link.
   - creator_url: optional, the account's profile, for attribution only — this does not count
     as satisfying the post_url requirement.

2. POST_IDEA — a concrete piece of content ResX could create TODAY. Extremely specific and
   immediately executable — someone on the team should be able to make it without researching
   anything else.
   Bad (too vague to execute): "Restaurant opened."
   Good: "It's 95° in NYC. Make a carousel of the 5 ice cream shops everyone is posting this
   week." — backed by the 5 specific Instagram posts, the 5 restaurant accounts, and why each
   post belongs.
   Good: "Everyone is posting from the Rockefeller Fan Village today." — backed by 4-6 specific
   Reels, links to every Reel, and exactly what the carousel/Story should cover.
   - posts: 2+ entries, each {{"post_url": "the exact post/Reel link — required, same strict
     rules as REPOST above", "account_url": "optional, the creator/restaurant's profile, for
     attribution only", "why": "one short, factual reason this specific post belongs in the set
     — a curation reason, NOT a caption (e.g. 'most-viewed of the bunch as of this morning' is
     right, a suggested caption is wrong)"}}.
   - Never invent or pad the list to hit a number. 2 rock-solid real posts beats 5 where 3 are
     guesses.

3. TRENDING_AUDIO (returned separately, see below) — song/sound only, no explanation.

THE BOT NEVER WRITES CAPTIONS, COMMENTS, OR SOCIAL COPY. Its job is to surface opportunities,
not create content — the team always writes their own words. "why" in POST_IDEA is a curation
reason, never a caption suggestion.

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
    {{"type": "repost", "origin": "researched|pinned", "pinned_input": "...", "headline": "...",
      "subject": "...", "city": "...", "posted_days_ago": 0,
      "scores": {{"freshness": 1, "cultural_relevance": 1, "resx_relevance": 1,
                  "source_quality": 1, "actionability": 1}},
      "post_url": "...", "creator_url": "..."}},
    {{"type": "post_idea", "origin": "researched|pinned", "pinned_input": "...", "headline": "...",
      "subject": "...", "city": "...", "posted_days_ago": 0,
      "scores": {{"freshness": 1, "cultural_relevance": 1, "resx_relevance": 1,
                  "source_quality": 1, "actionability": 1}},
      "posts": [{{"post_url": "...", "account_url": "...", "why": "..."}}]}}
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
pinned items. "posted_days_ago" is required for every researched REPOST/POST_IDEA; optional for
pinned items (Georgia's pinned leads aren't subject to the freshness cutoff).
"""

    result = call_anthropic(
        messages=[{"role": "user", "content": prompt}],
        system=(
            "You are a social media editor for ResX — a NYC and London restaurant reservation "
            "app — not a hospitality news writer. You spend all day on Instagram and TikTok "
            "and have incredible taste. Your job is to find what ResX should POST today, "
            "never to summarize what happened today. "
            "Every item is exactly one of two shapes: repost or post_idea — plus trending "
            "audio, returned separately. Nothing else is valid. "
            "A repost or post_idea is worthless without a real, direct, post-level link — "
            "never a profile, website, article, or guide. Missing is better than wrong: if "
            "you can't find a specific post, drop the item. "
            "You never write captions, comments, or social copy — you surface opportunities "
            "and real links; the team writes their own words. A 'why' field is a curation "
            "reason, never a caption. "
            "Every opportunity must be backed by real, specific, existing posts you actually "
            "found — never invent a concept with nothing behind it. "
            "Accuracy matters more than coverage: only cite a URL you actually got back from "
            "a web_search result, never one you constructed or recalled from memory, and never "
            "attach a URL to a description it doesn't actually match. When in doubt, drop it. "
            "The same standard applies to posted_days_ago: report the post's actual age from "
            "what the search result shows, never guess or default to 0 — if you can't tell how "
            "old it is, report 999. "
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
    urls = [item[k] for k in ("post_url", "creator_url") if item.get(k)]
    urls += [p[k] for p in item.get("posts", []) or [] for k in ("post_url", "account_url") if p.get(k)]
    return urls


# A repost/post_idea link must point directly at a specific post, never a profile, website,
# article, or directory. This is a hard, deterministic gate — never trust prompt compliance
# alone for a rule this strict. "Missing is better than wrong."
POST_URL_PATTERNS = [
    re.compile(r"^https?://(www\.)?instagram\.com/p/[\w-]+"),
    re.compile(r"^https?://(www\.)?instagram\.com/reel/[\w-]+"),
    re.compile(r"^https?://(www\.)?tiktok\.com/@[\w.\-]+/video/\d+"),
    re.compile(r"^https?://(www\.)?(x|twitter)\.com/[\w]+/status/\d+"),
    re.compile(r"^https?://(www\.)?threads\.(net|com)/@[\w.\-]+/post/[\w]+"),
]


def is_valid_post_url(url: str) -> bool:
    """True only for a direct post-level URL (Instagram post/reel, TikTok video, X status,
    Threads post). False for a profile, website, article, newsletter, or city guide — even
    if that URL is live and resolves fine. Link validity and link *shape* are separate checks."""
    if not url:
        return False
    return any(p.match(url.strip()) for p in POST_URL_PATTERNS)


def validate_post_urls(items: list, today_iso: str, source_type: str) -> tuple:
    """Deterministic backstop for the strict post-level-URL requirement. A repost with no
    valid post URL, or a post_idea left with fewer than 2 valid posts after filtering out
    bad links, is dropped and logged rather than posted with a wrong or missing link."""
    kept = []
    skips = []
    for item in items:
        if item.get("type") == "repost":
            url = item.get("post_url", "")
            if is_valid_post_url(url):
                kept.append(item)
            else:
                skips.append({
                    "date": today_iso, "subject": item.get("subject", ""), "url": url,
                    "reason": "invalid_post_url",
                    "detail": f"repost post_url {url!r} is not a direct post-level link",
                    "source_type": source_type,
                })
        elif item.get("type") == "post_idea":
            valid_posts = [p for p in item.get("posts", []) or [] if is_valid_post_url(p.get("post_url", ""))]
            if len(valid_posts) >= 2:
                item = dict(item)
                item["posts"] = valid_posts
                kept.append(item)
            else:
                first_url = (item.get("posts") or [{}])[0].get("post_url", "")
                skips.append({
                    "date": today_iso, "subject": item.get("subject", ""), "url": first_url,
                    "reason": "insufficient_valid_posts",
                    "detail": f"only {len(valid_posts)} valid post-level link(s) found, need at least 2",
                    "source_type": source_type,
                })
        else:
            kept.append(item)
    return kept, skips


def validate_freshness(items: list, today_iso: str, source_type: str,
                        cutoff_days: int = FRESHNESS_CUTOFF_DAYS) -> tuple:
    """Deterministic backstop on content age. The self-rated 'freshness' score alone isn't
    enough — it's one of five axes averaged together, so a stale item can still clear
    SCORE_THRESHOLD if the other four axes are strong. This checks the model-reported
    posted_days_ago against a hard cutoff, independent of the average. Missing/unparseable
    values are treated as failing (missing is better than wrong, same as validate_post_urls)."""
    kept = []
    skips = []
    for item in items:
        days_ago = item.get("posted_days_ago")
        try:
            days_ago = int(days_ago)
        except (TypeError, ValueError):
            days_ago = None
        if days_ago is None or days_ago > cutoff_days:
            skips.append({
                "date": today_iso, "subject": item.get("subject", ""),
                "url": (urls_in_item(item) or [""])[0], "reason": "stale_content",
                "detail": f"posted_days_ago={days_ago!r} exceeds the {cutoff_days}-day freshness cutoff",
                "source_type": source_type,
            })
            continue
        kept.append(item)
    return kept, skips


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
    """Exactly two shapes now: repost (one direct post link) and post_idea (2+ real posts,
    each with an optional attribution link and a short curation reason). No other type is
    valid — see validate_post_urls for the hard gate that guarantees this at posting time."""
    item_type = item.get("type", "repost")
    headline  = item.get("headline", "")
    city      = item.get("city", "BOTH").upper()
    tag       = f"→ {item_type.upper()}  ·  {city}"
    header    = f"{tag}\n*{headline}*"

    if item_type == "post_idea":
        lines = [header]
        for post in item.get("posts", []) or []:
            post_url    = post.get("post_url", "")
            account_url = post.get("account_url", "")
            why         = post.get("why", "")
            if not post_url:
                continue
            line = f"  •  {safe_link(post_url, 'post')}"
            if account_url:
                line += f"  ·  {safe_link(account_url, 'account')}"
            if why:
                line += f"  — {why}"
            lines.append(line)
        return "\n".join(lines)

    # repost (default)
    url         = item.get("post_url", "")
    creator_url = item.get("creator_url", "")
    link_str    = f"  {safe_link(url, 'post')}" if url else ""
    creator_str = f"  ·  {safe_link(creator_url, 'account')}" if creator_url else ""
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

    # Hard gate: reported post age, independent of the self-rated freshness score in
    # avg_score above — pinned leads are exempt, same as the scoring rubric they already skip.
    researched_kept, freshness_skips = validate_freshness(researched_kept, today_iso, source_type="researched")

    # Hard gate: strict post-level URL validation, regardless of origin — missing is better
    # than wrong. A pinned lead with no valid post link still gets rejected here.
    pinned_kept, pinned_url_skips = validate_post_urls(pinned_kept, today_iso, source_type="pinned")
    researched_kept, researched_url_skips = validate_post_urls(researched_kept, today_iso, source_type="researched")

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

    new_skip_entries = (
        pinned_skips + score_skips + freshness_skips + pinned_url_skips + researched_url_skips
        + considered_rejected_log + diversity_skips
    )
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

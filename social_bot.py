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


def research_ugc(seen_urls: set, seen_subjects: set, seen_songs: set) -> tuple:
    seen_str = "\n".join(f"- {u}" for u in list(seen_urls)[:60]) if seen_urls else "none"
    subjects_str = "\n".join(f"- {s}" for s in sorted(seen_subjects)) if seen_subjects else "none"
    songs_str = "\n".join(f"- {s}" for s in sorted(seen_songs)) if seen_songs else "none"
    signal = ", ".join(NYC_SIGNAL_ACCOUNTS + LONDON_SIGNAL_ACCOUNTS)

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

Return opportunities already ordered most-compelling-first.

Separately, look for trending audio: a song/sound worth using over a dining or going-out reel.
Just the track and a link — no explanation of what content it suits. Return 0-3, only if
genuinely trending today. For each: song, artist, url (Spotify/Apple Music/TikTok sound link).

Do NOT include any of these URLs which have already been sent:
{seen_str}

Return ONLY a valid JSON object, no markdown:
{{
  "opportunities": [
    {{"type": "repost|carousel|story|comment|meme|inspo", "headline": "...", "subject": "...",
      "city": "...", "post_url": "...", "creator": "...", "creator_url": "...",
      "slides": [{{"label": "...", "url": "..."}}], "target_url": "...", "target_label": "...",
      "reference_url": "...", "source_url": "..."}}
  ],
  "audio": [
    {{"song": "...", "artist": "...", "url": "..."}}
  ]
}}
Only include the fields relevant to that opportunity's type — omit the rest.
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
            "Return only a valid JSON object, no markdown."
        ),
    )

    try:
        clean = re.sub(r"```[a-z]*", "", result).strip().strip("`").strip()
        start = clean.index("{")
        data, _ = json.JSONDecoder().raw_decode(clean, start)
        if not isinstance(data, dict):
            return [], []
        return data.get("opportunities", []) or [], data.get("audio", []) or []
    except Exception as e:
        print(f"Error parsing UGC results: {e}")
        return [], []


def urls_in_item(item: dict) -> list:
    urls = [item[k] for k in ("post_url", "target_url", "reference_url", "source_url") if item.get(k)]
    urls += [s["url"] for s in item.get("slides", []) or [] if s.get("url")]
    return urls


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


def build_slack_blocks(date_str: str, items: list, audio: list) -> list:
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"Social Opportunities  ·  {date_str}"},
        }
    ]

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
    print(f"Running ResX Social Bot — {today_str}")

    last_post = load_json(LAST_POST_FILE, {})
    if last_post.get("date") == today_iso and os.environ.get("FORCE_POST") != "1":
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

    print("Researching UGC...")
    items, audio = research_ugc(seen_urls, seen_subjects, seen_songs)
    print(f"Found {len(items)} opportunities, {len(audio)} audio items")

    blocks = build_slack_blocks(today_str, items, audio)
    post_to_slack(blocks)
    print("Posted to #social")

    # Save seen URLs/subjects/songs (keep last 14 days to cap file size)
    new_entries = [
        {"url": url, "date": today_iso, "subject": item.get("subject", "")}
        for item in items
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
    print("Done ✓")


if __name__ == "__main__":
    main()

"""
ResX Social Bot
Runs daily via GitHub Actions. Finds 5-7 UGC posts (Instagram reels / TikToks)
from food creators in NYC and London and posts them to #social for the team to repost.
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


def research_ugc(seen_urls: set) -> list:
    seen_str = "\n".join(f"- {u}" for u in list(seen_urls)[:60]) if seen_urls else "none"
    signal = ", ".join(NYC_SIGNAL_ACCOUNTS + LONDON_SIGNAL_ACCOUNTS)

    prompt = f"""
You are a social media strategist for ResX — a last-minute restaurant reservation app for
25-35 year olds in NYC and London. Each morning you send the team a list of specific,
actionable social opportunities for that day.

Target audience vibe (use as calibration only, do NOT cite): {signal}

Search for timely content from the past 24-48 hours. For each item you find, your job is not
just to surface it — but to tell the team exactly what to DO with it.

Look for:
- A trending audio or song the team could use over a dining/going-out reel — link to the
  specific audio, name the song and artist, explain what kind of content it suits
- A viral moment in NYC or London the team could engage with or riff on
  (e.g. a couple going viral, a wild local story, a meme format taking over)
- A specific UGC post (reel or TikTok) from a food creator that is worth reposting —
  link to the exact post, not just the account
- A food or drink collab, pop-up, or moment generating buzz that ResX could comment on,
  reshare, or tie to a booking prompt
- A pop culture or celebrity moment (sighting, collab, event) the team could tie to a
  restaurant or going-out angle
- A seasonal or weather-driven trend to capitalise on with specific content ideas
  (e.g. it's a heatwave → frozen cocktail content, ice cream spots, rooftop bookings)

Be specific. "Use Charli XCX's [exact song] audio (link) over a Friday night booking reel"
is good. "@charliXCX" is useless. "NBC NY posted a round-up of July 4th ice cream specials
(link) — repost with 'beat the heat, book a table'" is good. "NBC New York" is useless.

Find 5-7 items. Mix of NYC and London.

For each return:
- content: what specifically it is (name the song/post/moment/trend — be exact)
- action: one specific caption direction or action in ResX brand voice — insider, lowercase,
  cool girl energy, like a friend texting a tip. e.g. "repost with 'your sign to book tonight'"
  or "use this audio over empty-table-to-full-room b-roll" or "comment 'table for 2?' on this".
  Max 15 words. No marketing speak.
- url: primary link — must be an Instagram post/reel, TikTok, or song (Spotify/Apple Music/SoundCloud).
  This is what the team will repost, use the audio from, or engage with directly.
  Search hard for this. If you can only find an article, leave url blank.
- context_url: optional. An article or news link that gives background context on why this is relevant.
  Only include if genuinely useful — not required.
- city: "NYC", "LDN", or "BOTH"
- why_now: one line on timing or cultural context, lowercase, max 8 words

Do NOT include any of these URLs which have already been sent:
{seen_str}

Return ONLY a valid JSON array:
[
  {{"content": "...", "action": "...", "url": "...", "context_url": "...", "city": "...", "why_now": "..."}}
]
"""

    result = call_anthropic(
        messages=[{"role": "user", "content": prompt}],
        system=(
            "You are a culturally plugged-in social media strategist writing for ResX — "
            "a NYC and London restaurant reservation app. The brand voice is: insider, cool girl, "
            "effortlessly elegant, the friend you want at every dinner party. Moody aesthetic. "
            "Lowercase. Specific cultural references. Never try-hard. Never corporate. "
            "Think: 'where the cast of The Bear would eat' or 'your sign to book tonight' or "
            "'spots serving the Knicks energy'. Action copy should sound like a cool friend "
            "texting you a tip, not a marketing brief. "
            "Every suggestion must be specific and immediately actionable — name exact songs, "
            "link to exact posts, suggest exact copy or caption directions. "
            "No generic accounts, no vague trends, no AI-sounding phrases. "
            "Return only a valid JSON array, no markdown."
        ),
    )

    try:
        clean = re.sub(r"```[a-z]*", "", result).strip().strip("`").strip()
        start = clean.index("[")
        data, _ = json.JSONDecoder().raw_decode(clean, start)
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"Error parsing UGC results: {e}")
        return []


def build_slack_blocks(date_str: str, items: list) -> list:
    city_tag = {"NYC": " _NYC_", "LDN": " _LDN_", "BOTH": ""}

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"Social Opportunities  ·  {date_str}"},
        }
    ]

    for item in items:
        content = item.get("content", "")
        action  = item.get("action", "")
        url     = item.get("url", "")
        why_now = item.get("why_now", "")
        tag     = city_tag.get(item.get("city", "BOTH"), "")

        context_url = item.get("context_url", "")
        link_str    = f"  {safe_link(url, 'open')}" if url else ""
        context_str = f"  ·  {safe_link(context_url, 'context')}" if context_url else ""
        why_str     = f"  _{why_now}_" if why_now else ""
        lines = [f"*{content}*{tag}{link_str}{context_str}"]
        if action:
            lines.append(f"→ {action}{why_str}")

        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n".join(lines)},
        })

    if not items:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "_No social opportunities found today._"},
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

    # Load seen URLs (7-day rolling window)
    seen_raw = load_json(SEEN_UGC_FILE, [])
    cutoff = (today - datetime.timedelta(days=7)).isoformat()
    seen_urls = {e["url"] for e in seen_raw if e.get("date", "") >= cutoff}
    print(f"Loaded {len(seen_urls)} seen UGC URLs for dedup")

    print("Researching UGC...")
    items = research_ugc(seen_urls)
    print(f"Found {len(items)} UGC items")

    blocks = build_slack_blocks(today_str, items)
    post_to_slack(blocks)
    print("Posted to #social")

    # Save seen URLs (keep last 14 days to cap file size)
    new_entries = [{"url": item["url"], "date": today_iso} for item in items if item.get("url")]
    keep_cutoff = (today - datetime.timedelta(days=14)).isoformat()
    all_entries = [e for e in seen_raw if e.get("date", "") >= keep_cutoff] + new_entries
    save_json(SEEN_UGC_FILE, all_entries)
    print("Done ✓")


if __name__ == "__main__":
    main()

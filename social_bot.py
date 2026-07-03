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
You are finding UGC (user-generated content) for the team at ResX — a last-minute restaurant
reservation marketplace for 25-35 year olds in NYC and London — to repost on their social channels.

Target audience vibe (use as calibration only, do NOT cite): {signal}

Search Instagram and TikTok for food creator posts from the past 24-48 hours in NYC and London.

You are looking for:
- Instagram reels or TikToks from food bloggers, food creators, or regular diners
- Visually compelling food, drinks, or dining experiences — the kind of thing the ResX audience
  would stop scrolling for
- Restaurant visits, dishes worth ordering, hidden gems, hot spots having a moment
- NOT brand accounts, NOT editorial outlets (Eater, Infatuation, Time Out), NOT PR posts

Find 5-7 posts across NYC and London. For each return:
- handle: creator's Instagram or TikTok handle (e.g. @foodienyc)
- label: one factual sentence on what it shows and why it's repostable (max 12 words)
- url: direct link to the post or reel
- city: "NYC", "LDN", or "BOTH"

Do NOT include any of these URLs which have already been sent:
{seen_str}

Return ONLY a valid JSON array:
[
  {{"handle": "...", "label": "...", "url": "...", "city": "..."}}
]
"""

    result = call_anthropic(
        messages=[{"role": "user", "content": prompt}],
        system=(
            "You are a social media researcher for a food and dining brand. "
            "Find real, verifiable UGC posts. Be factual and specific — no hype. "
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

    lines = []
    for item in items:
        handle = item.get("handle", "")
        label  = item.get("label", "")
        url    = item.get("url", "")
        tag    = city_tag.get(item.get("city", "BOTH"), "")
        if url:
            lines.append(f"• {safe_link(url, handle)}{tag}  {label}")
        else:
            lines.append(f"• {handle}{tag}  {label}")

    body = "\n".join(lines) if lines else "_No UGC found today._"

    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"📱  UGC to Repost  ·  {date_str}"},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": body},
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "ResX Social Bot  ·  Powered by Claude  ·  Daily"}],
        },
    ]


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

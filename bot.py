"""
ResX News Bot
Runs 2x/week (Mon/Fri) via GitHub Actions, posts a Slack digest to #news.

Pipeline: research everything (openings + industry/competitor + city/culture + AI/product,
plus any manually pinned stories) into one flat pool of candidate stories, then a single
"editor" pass (edit_and_rank) merges duplicates, assigns each story to exactly one final
category, and ranks importance within category — before rendering.

Sections (in order):
  1. New Openings (NYC + London) — officially opened
  2. Watching — announced but not yet open; graduates to New Openings automatically
  3. Industry & Competitor Watch
  4. City & Culture
  5. AI & Product — every item includes an explicit "Why it matters" for ResX
"""

import os
import json
import re
import subprocess
import time
import urllib.request
import datetime
from pathlib import Path

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
SLACK_WEBHOOK_URL = os.environ["SLACK_WEBHOOK_URL"]

SEEN_OPENINGS_FILE  = Path("data/seen_openings.json")
COMPETITORS_FILE    = Path("data/competitors.json")
WATCHING_FILE       = Path("data/watching.json")
SEEN_STORIES_FILE   = Path("data/seen_stories.json")
PINNED_STORIES_FILE = Path("data/pinned_stories.json")
LAST_POST_FILE      = Path("data/last_post.json")
RUN_LOG_FILE        = Path("data/run_log.json")

# Only bot.py runs git commands for itself when actually running in CI —
# never touch the caller's working tree during local/manual testing.
IN_CI = os.environ.get("GITHUB_ACTIONS") == "true"

CLAIM_STALE_MINUTES = 15  # a "running" claim older than this is treated as an abandoned/crashed run
MAX_GIT_RETRIES = 5

# ---------------------------------------------------------------------------
# Signal accounts — used as vibe/trend calibration, NOT cited directly
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# Seed competitor list
# ---------------------------------------------------------------------------
SEED_COMPETITORS = [
    "Appointment Trader", "Dorsia", "Diibs", "Quenelle", "Table Agent",
    "Resy Notify", "OpenTable Notify",
    "Tock", "Blackbird", "The Infatuation", "Eater",
    "reservation scalper bots", "Telegram reservation groups",
    "DesignMyNight", "Hot Dinners",
    "The Spot",
]

# ---------------------------------------------------------------------------
# Curated source lists per section
# ---------------------------------------------------------------------------
SOURCES = {
    "openings_nyc": [
        "The Infatuation NYC new openings",
        "Eater NY new restaurant openings",
        "Resy blog new NYC restaurants",
        "New York Times dining new openings",
        "Time Out New York new restaurants",
    ],
    "openings_london": [
        "Hot Dinners new London restaurant openings",
        "DesignMyNight new London restaurants June 2026",
        "Time Out London best new restaurants",
        "ES Magazine London restaurant openings",
        "The Nudge London new openings",
    ],
    "hospitality": [
        "Feed Me Emily Sundberg Substack latest issue",
        "Everything's Toasted newsletter latest",
        "Mercer Street Hospitality Substack latest",
        "On The House Substack restaurant news",
        "Casper Media Instagram hospitality news",
        "Eater restaurant industry news this week",
        "Bloomberg Pursuits dining news",
    ],
    "industry": [
        "Restaurant Business Online reservation technology news",
        "Nation's Restaurant News technology this week",
        "Skift Table hospitality business news",
        "Fast Company restaurant tech news",
        "OpenTable Resy SevenRooms DoorDash news",
    ],
    "city_pulse_nyc": [
        "NY Post lifestyle going out NYC this week",
        "The Cut NYC trend what people are doing",
        "NYT Styles New York culture moment",
        "Curbed NY city life neighborhood news",
        "Rachel Janfaza Up and Up Substack Gen Z culture",
        "Blackbird Spyplane NYC culture",
        "Dirt media internet culture NYC",
    ],
    "city_pulse_london": [
        "Time Out London things to do this week",
        "ES Magazine London going out scene",
        "Vittles London food culture",
        "Ganymede magazine London",
        "Secret London what's on",
    ],
    "specials": [
        "NYC chef collaboration limited edition dish restaurant June 2026",
        "London chef collab limited menu pop-up residency June 2026",
        "NYC restaurant special seasonal menu item this week",
        "London restaurant special collab dish this week",
    ],
    "ai_tech": [
        "TLDR newsletter AI this week",
        "The Rundown AI latest",
        "TechCrunch AI agents news",
        "Ben's Bites AI tools this week",
        "Hacker News top AI stories",
    ],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text())
    return default


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def call_anthropic(messages: list, system: str, max_tokens: int = 4096) -> str:
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


def verify_url(url: str) -> bool:
    """Return True only if the URL returns a 200-range status."""
    if not url or not url.startswith("http"):
        return False
    try:
        req = urllib.request.Request(url, method="HEAD")
        req.add_header("User-Agent", "Mozilla/5.0")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status < 400
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Git + run-claim helpers
#
# The daily/scheduled-run guard only protects against duplicate posts if its
# state survives a `git push` — and pushes race whenever two runs (a delayed
# native schedule, a watchdog retrigger, a manual click) land close together.
# So instead of "post, then commit at the very end", we CLAIM the run slot
# with a commit+push BEFORE doing any expensive research, using git's
# fast-forward-only push as the atomic arbiter of who wins a race. The loser
# re-fetches, sees the winner's claim, and backs off before ever calling the
# Anthropic API or Slack.
# ---------------------------------------------------------------------------

def _git(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True)


def git_configure_identity():
    if not IN_CI:
        return
    _git("config", "user.name", "resx-digest-bot")
    _git("config", "user.email", "bot@resx.app")


def git_sync_to_remote_main():
    """Discard any local commit and reset to whatever origin/main actually has —
    safe here because this is an ephemeral CI checkout, never a local working tree."""
    _git("fetch", "origin", "main")
    _git("reset", "--hard", "origin/main")


def git_commit_and_push(paths: list, message: str) -> bool:
    """Commit the given paths and push, retrying on a losing race by resetting
    to origin and letting the caller recompute + retry. Returns True iff the
    push (or an empty no-op commit) landed."""
    if not IN_CI:
        return True  # local/manual runs never write to git; treat as a no-op success

    for attempt in range(1, MAX_GIT_RETRIES + 1):
        _git("add", *paths)
        diff = _git("diff", "--cached", "--quiet")
        if diff.returncode == 0:
            return True  # nothing changed — already up to date, not a failure
        commit = _git("commit", "-m", message)
        if commit.returncode != 0:
            print(f"git commit failed (attempt {attempt}): {commit.stderr.strip()}")
            return False
        push = _git("push", "origin", "HEAD:main")
        if push.returncode == 0:
            return True
        print(f"git push lost the race (attempt {attempt}/{MAX_GIT_RETRIES}): {push.stderr.strip()}")
        git_sync_to_remote_main()
        time.sleep(1.5 * attempt)
    return False


def _now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _parse_iso(ts: str):
    try:
        return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def claim_todays_run(today_iso: str, trigger: str, run_id: str, force: bool) -> tuple[bool, str]:
    """
    Attempts to atomically claim today's run slot. Returns (claimed, reason).
    reason is only meaningful when claimed is False, for logging.
    """
    for attempt in range(1, MAX_GIT_RETRIES + 1):
        state = load_json(LAST_POST_FILE, {})
        if state.get("date") == today_iso:
            status = state.get("status", "completed")  # older files predate "status"; treat as completed
            if status == "completed" and not force:
                return False, f"already completed today at {state.get('posted_at', 'unknown time')}"
            if status == "running":
                started = _parse_iso(state.get("started_at", ""))
                stale = started is None or (_now_utc() - started) > datetime.timedelta(minutes=CLAIM_STALE_MINUTES)
                if not stale:
                    return False, f"another run claimed this slot at {state.get('started_at')} (trigger={state.get('trigger')}) and hasn't finished or timed out yet"
                print(f"Found a stale 'running' claim from {state.get('started_at')} — treating as abandoned and reclaiming")

        save_json(LAST_POST_FILE, {
            "date": today_iso, "status": "running", "run_id": run_id,
            "trigger": trigger, "started_at": _now_utc().isoformat(),
        })
        if git_commit_and_push([str(LAST_POST_FILE)], f"bot: claim {today_iso} run ({trigger}) [skip ci]"):
            return True, ""
        print(f"Lost the claim race, retrying ({attempt}/{MAX_GIT_RETRIES})...")

    return False, "could not win the claim race after retries"


def log_run_event(today_iso: str, trigger: str, outcome: str, detail: str = ""):
    """Best-effort structured history: data/run_log.json, 30-day rolling window."""
    log = load_json(RUN_LOG_FILE, [])
    log.append({
        "date": today_iso, "timestamp": _now_utc().isoformat(),
        "trigger": trigger, "outcome": outcome, "detail": detail,
    })
    cutoff = (datetime.date.today() - datetime.timedelta(days=30)).isoformat()
    log = [e for e in log if e.get("date", "") >= cutoff]
    save_json(RUN_LOG_FILE, log)
    git_commit_and_push([str(RUN_LOG_FILE)], f"bot: log {outcome} run [skip ci]")


# ---------------------------------------------------------------------------
# Holiday & food-day calendar
# (month, day, name, special_header_template, prompt_hint, is_food_day)
# special_header_template=None for food days; July 4 header is computed dynamically.
# ---------------------------------------------------------------------------

CALENDAR = [
    (1,  1,  "New Year's Day",          "🥂 New Year's Edition",    "New Year's is tomorrow — look for NYE dining, top tables for the year, and resolution menus",                       False),
    (2,  9,  "National Pizza Day",       None,                       "It's National Pizza Day — surface pizza collabs, legendary slices, or pizza cultural moments in NYC/London",         True),
    (2, 14,  "Valentine's Day",          "❤️ Valentine's Edition",   "Valentine's Day is coming — look for romantic dining, prix-fixe specials, and date-night spots",                     False),
    (2, 22,  "National Margarita Day",   None,                       "National Margarita Day is this week — margarita specials, tequila bars having a moment",                             True),
    (5,  5,  "Cinco de Mayo",            None,                       "Cinco de Mayo is days away — Mexican restaurant collabs, mezcal/tequila moments worth noting",                       True),
    (5, 28,  "National Burger Day (US)", None,                       "National Burger Day (US) is this week — burger collabs, smash burger moments, limited edition patties",              True),
    (6, 19,  "National Martini Day",     None,                       "National Martini Day is this week — martini specials, dirty martini trends, cocktail bar news",                      True),
    (7,  4,  "Fourth of July",           "🇺🇸 Special Edition",      "Fourth of July is days away — look for patriotic dining content, summer entertaining, and July 4th specials in NYC", False),
    (7, 17,  "National Hot Dog Day",     None,                       "National Hot Dog Day is this week — hot dog collabs, NYC cart culture moments, limited-run dogs",                    True),
    (8, 25,  "UK Summer Bank Holiday",   None,                       "UK Bank Holiday weekend — London pop-ups, long-weekend dining, and things to do",                                    False),
    (10,  4, "National Taco Day",        None,                       "National Taco Day is days away — taco collabs, creative fillings, taco pop-ups worth noting",                        True),
    (10, 31, "Halloween",                "🎃 Halloween Edition",     "Halloween is days away — spooky dining events, themed menus, Halloween pop-ups and collabs",                          False),
    (11,  5, "Guy Fawkes Night",         None,                       "Guy Fawkes Night is days away — London fireworks dining, bonfire night restaurant events",                            False),
    (12, 25, "Christmas",                "🎄 Holiday Edition",       "Christmas is approaching — festive menus, holiday dining, Christmas party venues in NYC and London",                  False),
    (12, 26, "Boxing Day",               None,                       "Boxing Day is coming — post-Christmas London dining, Boxing Day brunch spots",                                        False),
]


def get_holiday_context(today: datetime.date) -> dict:
    """
    Returns {"special_header": str|None, "prompt_hints": [str]} for any holiday or food day
    within 10 days (or 7 days for food days). special_header is only set when a major holiday
    is within 3 days.
    """
    hints = []
    special_header = None

    for month, day, name, header_tpl, hint, is_food_day in CALENDAR:
        try:
            holiday = datetime.date(today.year, month, day)
        except ValueError:
            continue
        days_until = (holiday - today).days
        window = 7 if is_food_day else 10
        if 0 <= days_until <= window:
            hints.append(hint)
            if not is_food_day and days_until <= 3 and header_tpl and not special_header:
                if month == 7 and day == 4:
                    years = today.year - 1776
                    special_header = f"🇺🇸 Special Edition — America's {years}th Birthday"
                else:
                    special_header = header_tpl

    # Month-long observances
    if today.month == 6:
        hints.append("It's Pride Month — look for LGBTQ+ dining moments, pride events at restaurants, rainbow menus and collabs")
    if today.month == 1:
        hints.append("It's Dry January and Veganuary — look for 0% cocktails, mocktail menus, and vegan restaurant moments")

    return {"special_header": special_header, "prompt_hints": hints}


# ---------------------------------------------------------------------------
# Step 1 — Refresh competitor list
# ---------------------------------------------------------------------------

def refresh_competitors() -> tuple[list, list]:
    existing = load_json(COMPETITORS_FILE, SEED_COMPETITORS)

    result = call_anthropic(
        messages=[{
            "role": "user",
            "content": (
                "Search for any NEW restaurant reservation apps, last-minute dining platforms, "
                "or reservation marketplace startups that have launched or received significant press "
                "in the past 2 weeks. Focus on competitors to a last-minute restaurant reservation "
                "marketplace operating in NYC and London. "
                "Return ONLY a JSON array of company/product name strings. "
                "If nothing new, return []."
            ),
        }],
        system="You are a competitive intelligence researcher. Return only valid JSON arrays, no markdown.",
        max_tokens=500,
    )

    try:
        clean = re.sub(r"```[a-z]*", "", result).strip().strip("`").strip()
        start = clean.index("[")
        new_entries, _ = json.JSONDecoder().raw_decode(clean, start)
        if not isinstance(new_entries, list):
            new_entries = []
    except Exception:
        new_entries = []

    existing_lower = {e.lower() for e in existing}
    truly_new = [e for e in new_entries if e.lower() not in existing_lower]
    full_list = existing + truly_new

    save_json(COMPETITORS_FILE, full_list)
    return full_list, truly_new


# ---------------------------------------------------------------------------
# Stable identity — fixes restaurants silently duplicating across New Openings / Watching
# ---------------------------------------------------------------------------

def normalize_identity(name: str, city: str) -> str:
    """Stable dedup key for a restaurant/venue: lowercase, strip punctuation, drop common
    city/article suffixes (the exact drift pattern that caused "Dishoom" vs "Dishoom NYC" to
    silently fail to match). Deliberately does NOT strip parenthetical qualifiers like
    "(Williamsburg)" — that's left to the edit_and_rank LLM pass, which can tell a dropped
    disambiguator from two genuinely distinct locations of an expanding chain; a plain string
    function can't safely make that call."""
    n = re.sub(r"[''`\".,!]", "", (name or "").lower())
    n = re.sub(r"\b(nyc|ny|london|the)\b", "", n)
    n = re.sub(r"\s+", " ", n).strip()
    city_key = (city or "").strip().upper()
    if city_key not in ("NYC", "LDN"):
        city_key = "BOTH"
    return f"{n}::{city_key}"


# ---------------------------------------------------------------------------
# Step 2 — Research openings
# ---------------------------------------------------------------------------

def research_openings(city: str, seen: set, watching: list) -> dict:
    """
    Returns:
      {
        "just_opened": {"items": [...]},
        "coming_soon": [...]
      }
    Every item is tagged with "category" ("new_opening" or "watching") and a stable "id"
    (see normalize_identity) at ingestion, so downstream code never has to re-derive identity
    from a possibly-drifted name string.
    """
    seen_str = ", ".join(seen) if seen else "none yet"
    city_label = "NYC" if city == "nyc" else "London"
    sources = SOURCES[f"openings_{city}"]
    signal_accounts = NYC_SIGNAL_ACCOUNTS if city == "nyc" else LONDON_SIGNAL_ACCOUNTS

    city_key = "NYC" if city == "nyc" else "LDN"
    city_watching = [w for w in watching if w.get("city", "").upper() in (city_key, "BOTH")]
    watching_str = ", ".join(w["name"] for w in city_watching) if city_watching else "none"

    prompt = f"""
You are researching restaurant openings in {city_label} for a weekly digest aimed at the team
at ResX — a last-minute restaurant reservation marketplace for 25-35 year olds in NYC and London.

Use these sources: {', '.join(sources)}

As a vibe calibration, the target audience is similar to followers of these accounts
(use as signal only, do NOT cite them): {', '.join(signal_accounts)}

Currently watching (announced but not yet open as of last run): {watching_str}
If any of these have now opened, include them in JUST OPENED.

Return TWO lists:

1. JUST OPENED: Up to 3 restaurants that actually opened THIS WEEK (verifiably open, taking reservations or walk-ins).
   EXCLUDE already seen: {seen_str}
   Also exclude these restaurants which are tracked separately on the watching list — do NOT independently discover them as new openings. They will only appear if you are graduating them from the watching list above: {watching_str}

2. COMING SOON: Up to 3 noteworthy restaurants announced for an upcoming opening (not yet open).
   These will be tracked week-to-week until they open.

For each restaurant in BOTH lists return:
- name: restaurant name
- date: opening date (e.g. "June 18") or "opens [date]" for coming soon
- blurb: 1 punchy sentence, max 12 words — vibe, concept, what makes it notable
- city: "{city_key}"
- website: search for the official website — try "[name].com", "[name].co.uk", and "site:[name] official". Only include if the URL resolves. Leave blank if nothing confirmed.
- instagram_handle: official Instagram handle e.g. @restaurantname — search for it, required
- instagram_url: full official Instagram profile URL — required, search for it (e.g. https://www.instagram.com/restaurantname)
- cover_image_post: Required for every item. Must be a post showing the FOOD at THIS specific restaurant — from a food blogger, food creator, or regular diner only. NOT the restaurant's own account, and NOT from editorial outlets like Eater, The Infatuation, Time Out, or Hot Dinners. Best sources: the restaurant's tagged photos on Instagram, or the restaurant's geotag. Must show actual food/dishes — not exteriors, not graphic cards. Each restaurant must have a DISTINCT cover URL — never reuse the same URL across two restaurants. Only include if you've confirmed the URL resolves.

For COMING SOON items also return:
- source_url: URL to the article or announcement that confirms this opening and its date — required. Only include if you've confirmed it resolves.

Return ONLY valid JSON:
{{
  "just_opened": {{
    "items": [
      {{
        "name": "...", "date": "...", "blurb": "...", "city": "...",
        "website": "...", "instagram_handle": "...", "instagram_url": "...", "cover_image_post": "..."
      }}
    ]
  }},
  "coming_soon": [
    {{
      "name": "...", "date": "...", "blurb": "...", "city": "...",
      "website": "...", "instagram_handle": "...", "instagram_url": "...", "cover_image_post": "...", "source_url": "..."
    }}
  ]
}}
"""

    result = call_anthropic(
        messages=[{"role": "user", "content": prompt}],
        system="You are a food media researcher. Only include URLs you have actually verified exist. Return only valid JSON, no markdown.",
        max_tokens=2500,
    )

    try:
        clean = re.sub(r"```[a-z]*", "", result).strip().strip("`").strip()
        # Find the start of the JSON object and use raw_decode to stop at its end
        start = clean.index("{")
        data, _ = json.JSONDecoder().raw_decode(clean, start)
        just_opened_items = data.get("just_opened", {}).get("items", [])
        coming_soon_items = data.get("coming_soon", [])
        for item in just_opened_items + coming_soon_items:
            if item.get("website") and not verify_url(item["website"]):
                item["website"] = ""
            if item.get("instagram_url") and not verify_url(item["instagram_url"]):
                item["instagram_url"] = ""
                item["instagram_handle"] = ""
            if item.get("cover_image_post") and not verify_url(item["cover_image_post"]):
                item["cover_image_post"] = ""
            if item.get("source_url") and not verify_url(item["source_url"]):
                item["source_url"] = ""
        for item in just_opened_items:
            item["category"] = "new_opening"
            item["id"] = normalize_identity(item.get("name", ""), item.get("city", ""))
        for item in coming_soon_items:
            item["category"] = "watching"
            item["id"] = normalize_identity(item.get("name", ""), item.get("city", ""))
        return {"just_opened": {"items": just_opened_items}, "coming_soon": coming_soon_items}
    except Exception as e:
        print(f"Error parsing openings for {city}: {e}")

    return {"just_opened": {"items": []}, "coming_soon": []}


# ---------------------------------------------------------------------------
# Step 3 — Research news sections
# ---------------------------------------------------------------------------

def _city_label_instruction() -> str:
    return "For each item, also include a 'city' field: either 'NYC', 'LDN', or 'BOTH'."


def _build_exclude_instruction(exclude: list) -> str:
    """Stories already surfaced by another research call THIS run — sequential, same-run dedup."""
    if not exclude:
        return ""
    already_used = "\n".join(
        f"- {item.get('headline', '')} ({item.get('url', 'no url')})"
        for item in exclude
        if item.get("headline")
    )
    return (
        f"\n\nIMPORTANT: The following stories have already appeared earlier in this digest — "
        f"do NOT include them or any article covering the same news:\n{already_used}\n"
        f"Find different stories that do not duplicate any of the above."
    )


def _build_seen_instruction(seen_stories: list) -> str:
    """Stories covered in recent past digests — cross-run dedup, allows a materially new
    development to resurface a subject rather than blocking it outright."""
    if not seen_stories:
        return ""
    covered_str = "\n".join(
        f"- {s.get('headline', '')} — {s.get('detail', '')} ({s.get('so_what', '')})"
        for s in seen_stories[:60]
    )
    return (
        f"\n\nIMPORTANT — already covered in recent digests, do not just re-report these:\n"
        f"{covered_str}\n\n"
        f"For each one: only cover the same underlying subject again if there is a genuinely "
        f"MATERIAL new development since — e.g. a restaurant that was 'coming soon' has now "
        f"officially opened or confirmed its date, a funding rumor is now an official confirmed "
        f"round, a launch expands to a new city, a new partnership is announced, or a teased "
        f"date is now confirmed. Do NOT re-cover the same announcement reworded, the same menu "
        f"item, or the same opening with no new facts — that is a duplicate even if the headline "
        f"or source differs. If you do include a follow-up to something above, make the new "
        f"development explicit in 'detail' or 'so_what' (e.g. 'now confirmed for Sept 12', not "
        f"a repeat of the original teaser)."
    )


def _run_news_research(prompt: str, label: str, exclude: list, seen_stories: list,
                        holiday_hint: str, max_tokens: int = 1600) -> list:
    """Shared plumbing for the news-style research calls (industry/culture/ai_product):
    assembles the holiday/exclude/seen-story instructions, calls Claude, and parses the
    returned JSON array."""
    if holiday_hint:
        prompt = f"CONTEXT FOR THIS RUN: {holiday_hint}\n\n" + prompt.lstrip()
    exclude_instruction = _build_exclude_instruction(exclude)
    if exclude_instruction:
        prompt = prompt.rstrip() + exclude_instruction
    seen_instruction = _build_seen_instruction(seen_stories)
    if seen_instruction:
        prompt = prompt.rstrip() + seen_instruction

    result = call_anthropic(
        messages=[{"role": "user", "content": prompt}],
        system=(
            "You are a sharp editor writing for a small startup team. "
            "Be factual and specific — no hype, no marketing language, no AI-sounding phrases. "
            "Only surface articles published within the last 7 days; do not include content "
            "from previous months or years. "
            "Return only a valid JSON array of objects. No markdown fences."
        ),
        max_tokens=max_tokens,
    )

    try:
        clean = re.sub(r"```[a-z]*", "", result).strip().strip("`").strip()
        start = clean.index("[")
        data, _ = json.JSONDecoder().raw_decode(clean, start)
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"Error parsing {label}: {e}")
        return []


def research_industry(competitors: list = None, exclude: list = None,
                       seen_stories: list = None, holiday_hint: str = None) -> list:
    """Industry & Competitor Watch. Returns list of dicts: {headline, detail, so_what, url, city}"""
    comp_str = ", ".join(competitors or SEED_COMPETITORS)
    sources_str = ", ".join(SOURCES["industry"])

    prompt = f"""
Search for news from the past week about these restaurant reservation competitors
and the broader reservation/dining landscape: {comp_str}

Also search these industry sources: {sources_str}

Look for:
- M&A in hospitality tech, platform updates (OpenTable, Resy, SevenRooms, DoorDash, Uber Eats)
- Restaurant industry business news, funding rounds, policy changes affecting restaurants
- Restaurant reservation regulation news, reservation bot crackdowns, new reservation-adjacent
  features from Google/Apple Maps, dining trend shifts
- Notable restaurant/chef business moves — closures, chef departures/hires, brand
  partnerships, acquisitions (the business angle, not the cultural gossip angle — that
  belongs in City & Culture)

Find 3-5 most relevant items. {_city_label_instruction()}
For each return: headline (max 8 words), detail (max 12 words), so_what (max 10 words —
factual and direct, no hype), url (direct article link if available), city.
"""
    return _run_news_research(prompt, "industry", exclude, seen_stories, holiday_hint)


def research_culture(exclude: list = None, seen_stories: list = None, holiday_hint: str = None) -> list:
    """City & Culture. Returns list of dicts: {headline, detail, so_what, url, city}"""
    nyc_sources = ", ".join(SOURCES["city_pulse_nyc"])
    ldn_sources = ", ".join(SOURCES["city_pulse_london"])
    specials_sources = ", ".join(SOURCES["specials"])
    hospitality_sources = ", ".join(SOURCES["hospitality"])
    signal = ", ".join(NYC_SIGNAL_ACCOUNTS + LONDON_SIGNAL_ACCOUNTS)

    prompt = f"""
You are finding city culture moments from the past week for a 25-35 going-out audience
in NYC and London.

NYC sources: {nyc_sources}
London sources: {ldn_sources}
Specials/collab sources: {specials_sources}
Insider hospitality sources: {hospitality_sources}

Signal accounts (use as vibe calibration, do NOT cite directly): {signal}

Look for:
- Cultural trends: what the city is obsessed with, experiences people are seeking out
- Social moments driving people to make plans
- Celebrity or cultural figure spotted at a restaurant — the gossip-meets-dining crossover
  (e.g. "Sabrina Carpenter caught at Emmets on Grove" — this kind of micro-moment is gold)
- Brand × food collabs going viral on social media (e.g. a yogurt brand doing a froyo pop-up)
- Insider hospitality gossip and cultural moments — chef moves as a CULTURAL story (not a
  business one — that belongs in Industry & Competitor Watch), food-media buzz, brand x
  restaurant crossover moments
- Named chef x restaurant collabs with a specific dish (e.g. "Chef X x Restaurant Y = The
  [Dish Name]"), limited-time/seasonal menu items with a story behind them, pop-up residencies
  with a clear end date and specific menu
- NOT generic events listings, NOT general prix-fixe deals/restaurant week/generic seasonal
  menus without a story

Be specific — name the place, the person, the detail. Think insider knowledge, not trend
think-pieces. Think: "Vesper is averaging 1.5 martinis per guest since opening" or "Waiters at
Osteria Vibrato carry Tide Pens in their pockets." Real facts and named details beat vague
observations every time.

Find 4-5 items across NYC and London. {_city_label_instruction()}
For each return: headline (punchy, max 8 words), detail (max 12 words), so_what (max 10 words —
factual and direct, no hype), url (direct link if available), city.
"""
    return _run_news_research(prompt, "culture", exclude, seen_stories, holiday_hint)


def research_ai_product(exclude: list = None, seen_stories: list = None, holiday_hint: str = None) -> list:
    """AI & Product. Returns list of dicts: {headline, detail, why_it_matters, url, city}"""
    sources_str = ", ".join(SOURCES["ai_tech"])

    prompt = f"""
Search these sources for the past week: {sources_str}

Look for big AI news AND practical implications for a small software startup — this must NOT
become a generic AI newsletter roundup. Candidates: model releases from Anthropic/OpenAI/Google,
new agent/automation tooling, developer tools relevant to a React Native + Node/TypeScript +
Firebase stack, AI features shipping in tools we already use.

For EVERY item, before including it, answer explicitly: does this matter to ResX because it
could (a) lower our costs, (b) be worth experimenting with, or (c) improve how the team already
works? If you can't answer at least one of those concretely, do NOT include the item — generic
"AI is advancing" news with no team-relevant angle does not belong here.

Find 2-3 items — quality and relevance over volume. City field should always be 'BOTH'.
For each return: headline (max 8 words), detail (max 12 words, what happened), why_it_matters
(REQUIRED, max 15 words — one concrete, actionable takeaway for ResX: what to try, what it
saves, or what to change; not "this could be useful" — name the specific action or saving),
url (direct link if available), city.
"""
    return _run_news_research(prompt, "ai_product", exclude, seen_stories, holiday_hint)


# ---------------------------------------------------------------------------
# Step 3.5 — Flatten, merge/dedup, categorize, and rank into one story pool
#
# This is the piece that actually fixes stories duplicating across sections: no individual
# research call above has visibility into what any other call found, so a restaurant opening
# could surface independently via research_openings AND research_industry with zero
# cross-awareness. edit_and_rank sees the whole pool at once and is the single place a
# duplicate gets caught and collapsed.
# ---------------------------------------------------------------------------

def normalize_stories(new_opening_items: list, watching_candidate_items: list,
                      industry_items: list, culture_items: list, ai_items: list) -> list:
    """Flattens every research call's output into one pool. Openings/watching items already
    carry category+id from research_openings; this fills in the same fields for the three
    news-style lists, whose id is the source url (or a normalized-title fallback, mirroring
    _story_entries' existing key logic) since they have no other stable identity."""
    pool = list(new_opening_items) + list(watching_candidate_items)

    for items, category in (
        (industry_items, "industry"),
        (culture_items, "culture"),
        (ai_items, "ai_product"),
    ):
        for raw in items:
            item = dict(raw)
            item["category"] = category
            item["id"] = item.get("url") or f"{category}::{item.get('headline', '')}"
            pool.append(item)

    return pool


def _parse_editor_response(text: str, pool: list) -> list:
    """Parses edit_and_rank's JSON response. On failure, falls back to a deterministic
    pass-through — keep each item's research-assigned category, rank by original order —
    so a bad editor-pass response never silently empties the whole digest."""
    try:
        clean = re.sub(r"```[a-z]*", "", text).strip().strip("`").strip()
        start = clean.index("[")
        data, _ = json.JSONDecoder().raw_decode(clean, start)
        if isinstance(data, list) and data:
            return data
    except Exception as e:
        print(f"Error parsing editor response: {e}")

    print("Falling back to pass-through categorization (no merge/rerank this run)")
    fallback = []
    rank_counters = {}
    for raw in pool:
        item = dict(raw)
        cat = item.get("category", "culture")
        rank_counters[cat] = rank_counters.get(cat, 0) + 1
        item["importance_rank"] = rank_counters[cat]
        item.setdefault("merged_from", [])
        fallback.append(item)
    return fallback


def edit_and_rank(pool: list, watching_context: list, holiday_hint: str = None) -> list:
    """The single consolidation pass: merges duplicate entities researched independently
    across calls, confirms/reassigns each story's final category, and ranks importance
    within category."""
    if not pool:
        return []

    pool_json = json.dumps(pool, indent=2)
    watching_json = json.dumps(watching_context, indent=2) if watching_context else "[]"

    prompt = f"""
You are the final editor for the ResX restaurant-industry Slack digest. Below is the full pool
of candidate stories researched for this issue — some may describe the same underlying
restaurant, event, or entity from different angles (e.g. a chef opening a restaurant surfaced
once as a "new_opening" and again as an "industry" story; a celebrity-at-a-restaurant story
that also mentions the restaurant just opened).

CANDIDATE STORIES (JSON):
{pool_json}

ALREADY TRACKED AS "WATCHING" from prior issues (context only — do not re-emit these, they
are carried forward automatically; only use this to recognize when a NEW candidate above is
actually the same restaurant, even if renamed or a qualifier like "(Williamsburg)" was dropped
now that it's open):
{watching_json}

Do three things:

1. MERGE DUPLICATES. If two or more candidates are fundamentally about the same underlying
   restaurant/entity/event, merge them into ONE story — keep the single best title/summary
   (prefer the more specific, more factual version; combine distinct facts from both if each
   adds something the other lacks). A story must exist exactly once in your output. When
   merging, prefer the MORE ACTIONABLE framing — e.g. if a "chef opening a new restaurant"
   story appears both as an opening-style item and an industry-style item, merge into one
   new_opening (or watching) entry, not an industry entry.

2. ASSIGN FINAL CATEGORY — exactly one of: new_opening, watching, industry, culture, ai_product.
   Rules, in priority order:
   a. If a restaurant/hotel/bakery/bar/members-club etc. has OFFICIALLY OPENED (verifiably
      taking reservations/walk-ins) -> new_opening.
   b. Else if it's an ANNOUNCED-BUT-NOT-YET-OPEN restaurant (teaser, soft opening, confirmed
      future launch) -> watching.
   c. Items whose "origin" field is "pinned" keep their given category exactly as provided —
      do not recategorize pinned items, only rank them.
   d. For everything else, assign whichever of industry / culture / ai_product is the SINGLE
      most actionable section for a ResX team member — i.e. which section someone would most
      usefully look under. If a story plausibly fits two, pick the one where its PRIMARY
      newsworthy angle lives (a chef's new restaurant business deal -> industry; the same
      chef's restaurant being an Instagram-viral celebrity hangout -> culture). Never assign
      the same story to two categories.

3. RANK BY IMPORTANCE within each of industry/culture/ai_product: assign importance_rank 1..N
   per category (1 = most important), based on specificity, relevance to a last-minute dining
   reservation marketplace in NYC/London for 25-35 year olds, and whether the story has a
   genuinely notable, concrete hook (not generic trend commentary). new_opening/watching items
   don't need ranking — set importance_rank to 0 for those.

Do not invent facts. Do not soften or genericize any story's summary/so_what/detail/why_it_matters
— preserve the existing specificity and insider detail exactly as written.

Return ONLY a valid JSON array, no markdown: every story object with all of its original fields
preserved, plus "category" (final), "importance_rank" (int), and "merged_from" (list of the
input ids that were merged into this story, [] if it wasn't a merge).
"""

    if holiday_hint:
        prompt = f"CONTEXT FOR THIS RUN: {holiday_hint}\n\n" + prompt.lstrip()

    result = call_anthropic(
        messages=[{"role": "user", "content": prompt}],
        system=(
            "You are a meticulous editor. You never invent facts, never soften specific "
            "details into generic ones, and never let the same story appear twice. "
            "Return only a valid JSON array, no markdown."
        ),
        max_tokens=6000,
    )

    return _parse_editor_response(result, pool)


# ---------------------------------------------------------------------------
# Step 4 — Format Slack blocks
# ---------------------------------------------------------------------------

CITY_TAG = {
    "NYC": "NYC",
    "LDN": "LDN",
    "BOTH": "",
}

def city_tag(item: dict) -> str:
    return CITY_TAG.get(item.get("city", "BOTH"), "")


def safe_link(url: str, label: str) -> str:
    """Return a Slack mrkdwn link, encoding chars that break the <url|label> format."""
    url = url.replace("&", "&amp;").replace("<", "").replace(">", "").replace("|", "%7C")
    label = label.replace("<", "").replace(">", "").replace("|", "-").replace("&", "&amp;")
    return f"<{url}|{label}>"


def safe_text(text: str, limit: int = 2950) -> str:
    """Truncate block text to Slack's 3000-char section limit."""
    return text[:limit] if len(text) > limit else text


def format_opening_item(item: dict) -> str:
    name = item.get("name", "")
    date = item.get("date", "")
    blurb = item.get("blurb", "")
    website = item.get("website", "")
    ig_handle = item.get("instagram_handle", "")
    ig_url = item.get("instagram_url", "")
    cover = item.get("cover_image_post", "")
    source_url = item.get("source_url", "")

    name_str = f"*{safe_link(website, name)}*" if website else f"*{name}*"
    if date:
        date_str = safe_link(source_url, date) if source_url else date
        name_str += f"  _{date_str}_"
    if ig_handle and ig_url:
        name_str += f"  ·  {safe_link(ig_url, ig_handle)}"
    elif ig_handle:
        name_str += f"  ·  {ig_handle}"
    if cover:
        name_str += f"  ·  {safe_link(cover, 'ugc cover')}"

    lines = [name_str]
    if blurb:
        lines.append(blurb)

    return "\n".join(lines)


def format_news_items(items: list) -> str:
    lines = []
    for item in items:
        tag = city_tag(item)
        headline = item.get("headline", "")
        detail = item.get("detail", "")
        so_what = item.get("so_what", "")
        url = item.get("url", "")
        headline_str = f"*{safe_link(url, headline)}*" if url else f"*{headline}*"
        tag_str = f"  _{tag}_" if tag else ""
        lines.append(f"• {headline_str}{tag_str}\n  {detail} _{so_what}_")
    return "\n\n".join(lines)


def format_ai_item(item: dict) -> str:
    """Unlike format_news_items' compact inline so_what, AI & Product items render an
    explicit, own-line 'Why it matters:' — a literal requirement for this section."""
    headline = item.get("headline", "")
    detail = item.get("detail", "")
    why = item.get("why_it_matters") or item.get("so_what", "")
    url = item.get("url", "")
    headline_str = f"*{safe_link(url, headline)}*" if url else f"*{headline}*"
    lines = [f"• {headline_str}"]
    if detail:
        lines.append(f"  {detail}")
    if why:
        lines.append(f"  *Why it matters:* {why}")
    return "\n".join(lines)


def format_ai_items(items: list) -> str:
    return "\n\n".join(format_ai_item(item) for item in items)


def build_slack_blocks(
    date_str: str,
    stories: list,
    special_header: str = None,
    new_competitors: list = None,
) -> list:
    """Renders the 5-category digest from one unified, already-categorized/ranked story list
    (see edit_and_rank) instead of 7 independently-researched parameter lists."""

    new_openings = [s for s in stories if s.get("category") == "new_opening"]
    watching     = [s for s in stories if s.get("category") == "watching"]
    rank_key     = lambda s: s.get("importance_rank") or 999
    industry     = sorted((s for s in stories if s.get("category") == "industry"), key=rank_key)
    culture      = sorted((s for s in stories if s.get("category") == "culture"), key=rank_key)
    ai_product   = sorted((s for s in stories if s.get("category") == "ai_product"), key=rank_key)

    blocks = []

    # Optional special edition one-liner (holidays, birthdays, etc.)
    if special_header:
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"*{special_header}*"}],
        })

    # Header
    blocks.append({
        "type": "header",
        "text": {"type": "plain_text", "text": f"ResX Digest  ·  {date_str}"},
    })
    blocks.append({"type": "divider"})

    # ── 1. New Openings ─────────────────────────────────────────────────────
    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": "*📍  NEW OPENINGS*"},
    })
    nyc_new = [s for s in new_openings if s.get("city", "").upper() in ("NYC", "BOTH")]
    ldn_new = [s for s in new_openings if s.get("city", "").upper() in ("LDN", "BOTH")]
    if nyc_new:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "*🗽  NYC*"}})
        for item in nyc_new:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": format_opening_item(item)}})
    if ldn_new:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "*🇬🇧  London*"}})
        for item in ldn_new:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": format_opening_item(item)}})
    blocks.append({"type": "divider"})

    # ── 🔭 Watching ─────────────────────────────────────────────────────────
    if watching:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*🔭  WATCHING*"},
        })
        nyc_watch = [s for s in watching if s.get("city", "").upper() in ("NYC", "BOTH")]
        ldn_watch = [s for s in watching if s.get("city", "").upper() in ("LDN", "BOTH")]
        if nyc_watch:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "*🗽  NYC*"}})
            for item in nyc_watch:
                blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": format_opening_item(item)}})
        if ldn_watch:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "*🇬🇧  London*"}})
            for item in ldn_watch:
                blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": format_opening_item(item)}})
        blocks.append({"type": "divider"})

    # ── 2. Industry & Competitor Watch ──────────────────────────────────────
    if industry or new_competitors:
        industry_text = format_news_items(industry) if industry else ""
        if new_competitors:
            comp_str = ", ".join(new_competitors)
            new_comp_block = f"\n\n*New competitor spotted:* {comp_str}"
            industry_text = (industry_text + new_comp_block).strip()
        if industry_text:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": safe_text(f"*🏢  INDUSTRY & COMPETITOR WATCH*\n\n{industry_text}")},
            })
            blocks.append({"type": "divider"})

    # ── 3. City & Culture ────────────────────────────────────────────────────
    if culture:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": safe_text(f"*🏙️  CITY & CULTURE*\n\n{format_news_items(culture)}")},
        })
        blocks.append({"type": "divider"})

    # ── 4. AI & Product ──────────────────────────────────────────────────────
    if ai_product:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": safe_text(f"*🤖  AI & PRODUCT*\n\n{format_ai_items(ai_product)}")},
        })

    blocks.append({"type": "divider"})
    blocks.append({
        "type": "context",
        "elements": [{
            "type": "mrkdwn",
            "text": "ResX News Bot  ·  Powered by Claude  ·  Mon / Fri",
        }],
    })

    return blocks


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _story_entries(items: list, today_iso: str) -> list:
    """Build dedup entries (key + the actual coverage text) from a list of story dicts,
    so a later run can judge whether a repeat has a materially new development."""
    entries = []
    for item in items:
        key = item.get("url") or item.get("headline", "")
        if not key:
            continue
        entries.append({
            "key": key, "date": today_iso,
            "headline": item.get("headline", ""),
            "detail": item.get("detail", ""),
            "so_what": item.get("so_what") or item.get("why_it_matters", ""),
        })
    return entries


def _pinned_to_story(pin: dict) -> dict:
    """Converts a manually pinned story into the common pool shape. Category is treated as
    authoritative by edit_and_rank's prompt (see the "origin" == "pinned" rule) — pins get
    ranked alongside everything else researched this run, never recategorized."""
    story = {k: v for k, v in pin.items() if k != "section"}
    story["category"] = pin.get("section", "culture")
    story["origin"] = "pinned"
    story["id"] = story.get("url") or f"pinned::{story.get('headline', '')}"
    return story


def main():
    today = datetime.date.today()
    today_str = today.strftime("%B %d, %Y")
    today_iso = today.isoformat()

    force_post = os.environ.get("FORCE_POST") == "1"
    trigger = "forced" if force_post else os.environ.get("TRIGGER_TYPE", "manual")
    run_id = os.environ.get("GITHUB_RUN_ID", f"local-{int(time.time())}")

    print(f"Running ResX News Bot — {today_str} [{trigger.upper()}]")

    git_configure_identity()

    claimed, skip_reason = claim_todays_run(today_iso, trigger, run_id, force=force_post)
    if not claimed:
        print(f"[SKIPPED] {skip_reason}")
        log_run_event(today_iso, trigger, "skipped", skip_reason)
        return

    print("[STARTED] Claimed today's run slot")

    try:
        seen_openings = set(load_json(SEEN_OPENINGS_FILE, []))
        watching = load_json(WATCHING_FILE, [])

        # Load pinned stories (manually added between runs)
        pinned = load_json(PINNED_STORIES_FILE, [])

        # Load cross-run story history (last 14 days = ~4 runs)
        seen_stories_raw = load_json(SEEN_STORIES_FILE, [])
        cutoff = (today - datetime.timedelta(days=14)).isoformat()
        recent_stories = [e for e in seen_stories_raw if e.get("date", "") >= cutoff]
        print(f"Loaded {len(recent_stories)} recent stories for dedup")

        # Compute holiday context
        holiday_ctx = get_holiday_context(today)
        special_header = holiday_ctx["special_header"]
        holiday_hint = " ".join(holiday_ctx["prompt_hints"]) if holiday_ctx["prompt_hints"] else None
        if special_header:
            print(f"Holiday special edition: {special_header}")
        if holiday_hint:
            print(f"Holiday hint injected: {holiday_hint[:80]}...")

        print("Refreshing competitor list...")
        competitors, new_competitors = refresh_competitors()

        print("Researching NYC openings...")
        nyc_result = research_openings("nyc", seen_openings, watching)

        print("Researching London openings...")
        london_result = research_openings("london", seen_openings, watching)

        nyc_data    = nyc_result.get("just_opened", {"items": []})
        london_data = london_result.get("just_opened", {"items": []})

        # Deterministic graduation — carried forward untouched, guarantees the hard
        # "must disappear from Watching" requirement regardless of the editor pass below.
        opened_ids = {i["id"] for i in nyc_data.get("items", []) + london_data.get("items", [])}
        watching_carried = [
            w for w in watching
            if w.get("id", normalize_identity(w.get("name", ""), w.get("city", ""))) not in opened_ids
        ]
        carried_ids = {
            w.get("id", normalize_identity(w.get("name", ""), w.get("city", "")))
            for w in watching_carried
        }

        # Only genuinely NEW coming_soon candidates enter the pool for editing/dedup — everything
        # already tracked is carried forward untouched, never re-derived from what the editor
        # pass happens to re-emit this run (which would silently drop untouched entries).
        new_watching_candidates = [
            item for item in (nyc_result.get("coming_soon", []) + london_result.get("coming_soon", []))
            if item["id"] not in carried_ids
        ]

        print("Researching industry & competitor watch...")
        industry_items = research_industry(competitors, seen_stories=recent_stories, holiday_hint=holiday_hint)

        print("Researching city & culture...")
        culture_items = research_culture(exclude=industry_items, seen_stories=recent_stories, holiday_hint=holiday_hint)

        print("Researching AI & product...")
        ai_items = research_ai_product(
            exclude=industry_items + culture_items, seen_stories=recent_stories, holiday_hint=holiday_hint
        )

        pool = normalize_stories(
            nyc_data.get("items", []) + london_data.get("items", []),
            new_watching_candidates, industry_items, culture_items, ai_items,
        )
        for pin in pinned:
            print(f"Pinned story queued for {pin.get('section', 'culture')!r}: {pin.get('headline', '')}")
        pool += [_pinned_to_story(p) for p in pinned]

        print(f"Editing & ranking {len(pool)} candidate stories...")
        final_stories = edit_and_rank(pool, watching_context=watching_carried, holiday_hint=holiday_hint)

        new_opening_stories = [s for s in final_stories if s.get("category") == "new_opening"]
        watching = watching_carried + [s for s in final_stories if s.get("category") == "watching"]
        seen_openings.update(s["name"] for s in new_opening_stories if s.get("name"))

        # Save cross-run story history (keep last 30 days to cap file size)
        new_story_entries = _story_entries(
            [s for s in final_stories if s.get("category") in ("industry", "culture", "ai_product")],
            today_iso,
        )
        keep_cutoff = (today - datetime.timedelta(days=30)).isoformat()
        all_stories = [e for e in seen_stories_raw if e.get("date", "") >= keep_cutoff] + new_story_entries

        print("Building Slack blocks...")
        blocks = build_slack_blocks(
            date_str=today_str,
            stories=final_stories,
            special_header=special_header,
            new_competitors=new_competitors,
        )

        print(f"Posting to Slack... ({len(blocks)} blocks)")
        for i, b in enumerate(blocks):
            txt = b.get("text", {}).get("text", "")
            if txt:
                print(f"  block[{i}] len={len(txt)}: {txt[:80]!r}")
        post_to_slack(blocks)

        # Persist everything from this run in one shot, now that the post landed
        save_json(WATCHING_FILE, watching)
        save_json(SEEN_OPENINGS_FILE, list(seen_openings))
        save_json(SEEN_STORIES_FILE, all_stories)
        if pinned:
            save_json(PINNED_STORIES_FILE, [])
        save_json(LAST_POST_FILE, {
            "date": today_iso, "status": "completed", "run_id": run_id,
            "trigger": trigger, "posted_at": _now_utc().isoformat(),
        })

        state_paths = [
            str(p) for p in (
                WATCHING_FILE, SEEN_OPENINGS_FILE, SEEN_STORIES_FILE,
                PINNED_STORIES_FILE, COMPETITORS_FILE, LAST_POST_FILE,
            )
        ]
        if not git_commit_and_push(state_paths, f"bot: update data files [skip ci]"):
            print("WARNING: final state commit did not land after retries — next run may not see today's post")

        log_run_event(today_iso, trigger, "completed", f"{len(blocks)} blocks posted")
        print("[COMPLETED] Done ✓")

    except Exception as e:
        print(f"[FAILED] {e}")
        # Best-effort: mark the claim as failed so a legitimate retry doesn't have to
        # wait out the full staleness window.
        save_json(LAST_POST_FILE, {
            "date": today_iso, "status": "failed", "run_id": run_id,
            "trigger": trigger, "failed_at": _now_utc().isoformat(), "error": str(e),
        })
        git_commit_and_push([str(LAST_POST_FILE)], f"bot: mark {today_iso} run failed [skip ci]")
        log_run_event(today_iso, trigger, "failed", str(e))
        raise


if __name__ == "__main__":
    main()

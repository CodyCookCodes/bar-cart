"""
IBA cocktail scraper (v3) - maintains ONE master file: iba_cocktails.json

Run it weekly. Each run:
  1. Reads the directory pages (a few requests) to get the current list of cocktails.
  2. Compares that list against iba_cocktails.json:
       - already in the file   -> quick check only (name + category from the list)
       - new on the site       -> opens its recipe page and adds it (status = "active")
       - gone from the site    -> flagged status = "removed", removed_at = now
       - removed but back      -> re-activated
  3. Writes the file back (safely) and prints a summary.

Recipe edits on existing drinks are only caught by a full run, which re-opens
every recipe page. Do that occasionally (e.g. monthly):
  python scraper.py --full

The file is a JSON array (one object per cocktail), so Snowflake can load it
with FILE_FORMAT = (TYPE = 'JSON' STRIP_OUTER_ARRAY = TRUE): one row per drink.

Run:
  pip install requests beautifulsoup4
  python scraper.py          # weekly quick check
  python scraper.py --full   # occasional full re-scrape
"""

import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup, Tag

BASE_URL = "https://iba-world.com/cocktails/all-cocktails/"
OUTPUT_FILE = Path("iba_cocktails.json")
HEADERS = {"User-Agent": "bar-cart-portfolio-scraper/3.0 (personal learning project)"}
TIMEOUT = 30
PAUSE_SECONDS = 1.0

# Safety guard: if the directory suddenly lists far fewer drinks than we know
# about (site down, layout change), stop instead of flagging everything removed.
MIN_LISTING_RATIO = 0.5

# Fields that define "the recipe": a change to any of these sets updated_at.
RECIPE_FIELDS = ("name", "iba_category", "ingredients", "method", "garnish")

SECTION_TITLES = {"ingredients", "method", "garnish"}
STOP_TITLES = SECTION_TITLES | {"most viewed cocktails"}
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "strong", "b", "p", "div", "span"}


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean(text: str) -> str:
    """Collapse all whitespace (incl. \\n\\n\\n and double spaces) to single spaces."""
    return re.sub(r"\s+", " ", text or "").strip()


def slug_from_url(url: str) -> str:
    return url.rstrip("/").split("/")[-1]


def get(session: requests.Session, url: str, retries: int = 3) -> requests.Response:
    """GET with timeout, polite pause, and backoff on 429/5xx."""
    for attempt in range(1, retries + 1):
        resp = session.get(url, timeout=TIMEOUT)
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries:
            time.sleep(2**attempt)
            continue
        time.sleep(PAUSE_SECONDS)
        return resp
    return resp


# --------------------------------------------------------------------------
# Scrape: directory pages -> list of cards
# --------------------------------------------------------------------------
def parse_directory_cards(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    cards = []
    for a in soup.select('a[href*="/iba-cocktail/"]'):
        href = a.get("href")
        name_el = a.select_one(".cocktail-content h2")
        if not href or not name_el:
            continue  # nav/sidebar links, not cocktail cards
        category_el = a.select_one(".cocktail-category")
        cards.append(
            {
                "url": href,
                "slug": slug_from_url(href),
                "name": clean(name_el.get_text()),
                "iba_category": clean(category_el.get_text()) if category_el else None,
            }
        )
    return cards


def fetch_all_cards(session: requests.Session) -> dict:
    """Walk directory pages until a 404 or a page with no new cards. Returns {slug: card}."""
    cards = {}
    page = 1
    while True:
        page_url = BASE_URL if page == 1 else f"{BASE_URL.rstrip('/')}/page/{page}/"
        print(f"Directory page {page}: {page_url}")
        resp = get(session, page_url)
        if resp.status_code == 404:
            print(f"  end of pagination (404 at page {page})")
            break
        resp.raise_for_status()
        new = 0
        for card in parse_directory_cards(resp.text):
            if card["slug"] not in cards:
                cards[card["slug"]] = card
                new += 1
        if new == 0:
            print(f"  no new cards on page {page}, stopping")
            break
        print(f"  found {new} cocktails (total {len(cards)})")
        page += 1
    return cards


# --------------------------------------------------------------------------
# Scrape: one recipe page -> ingredients / method / garnish
# --------------------------------------------------------------------------
def find_section_heading(content: Tag, title: str):
    """Innermost element whose text is exactly the title (headings sit inside wrapper divs)."""
    matches = [
        el
        for el in content.find_all(HEADING_TAGS)
        if clean(el.get_text()).lower() == title
    ]
    for el in matches:
        if not any(other in el.descendants for other in matches if other is not el):
            return el
    return None


def section_elements(heading: Tag):
    """Yield <li> and <p> elements after a heading, until the next section heading."""
    for el in heading.find_all_next():
        if el is heading:
            continue
        if el.name in HEADING_TAGS and clean(el.get_text()).lower() in STOP_TITLES:
            return
        if el.name in ("li", "p"):
            yield el


def parse_recipe_html(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    content = soup.select_one(".entry-content") or soup.find("article") or soup.body
    result = {"ingredients": [], "method": None, "garnish": None, "parse_warnings": []}
    if content is None:
        result["parse_warnings"].append("no page content found")
        return result

    for title in ("ingredients", "method", "garnish"):
        heading = find_section_heading(content, title)
        if heading is None:
            result["parse_warnings"].append(f"no '{title}' heading found")
            continue

        elements = list(section_elements(heading))
        if title == "ingredients":
            # One <li> = one ingredient; get_text(" ") keeps "Maraschino <strong>Luxardo</strong>" together.
            items = [clean(el.get_text(" ")) for el in elements if el.name == "li"]
            if not items:
                items = [clean(el.get_text(" ")) for el in elements if el.name == "p"]
                result["parse_warnings"].append(
                    "ingredients had no <li>; used <p> fallback"
                )
            result["ingredients"] = [i for i in items if i]
            if not result["ingredients"]:
                result["parse_warnings"].append(
                    "'ingredients' heading found but section is empty"
                )
        else:
            # Method/garnish can be <p> paragraphs OR <li> bullets (e.g. Dry Martini).
            lines = [clean(el.get_text(" ")) for el in elements]
            text = "\n".join(line for line in lines if line)
            result[title] = text or None
            if not text:
                result["parse_warnings"].append(
                    f"'{title}' heading found but section is empty"
                )
    return result


# --------------------------------------------------------------------------
# Master file: load, merge, save
# --------------------------------------------------------------------------
def load_master() -> dict:
    if not OUTPUT_FILE.exists():
        print(f"No {OUTPUT_FILE} yet - this run creates it.")
        return {}
    records = json.loads(OUTPUT_FILE.read_text(encoding="utf-8"))
    if not isinstance(records, list) or (records and "slug" not in records[0]):
        raise SystemExit(
            f"{OUTPUT_FILE} is in the old format (an object keyed by URL, from scraper v1). "
            f"Rename or delete it, then run again to build a fresh master file."
        )
    return {r["slug"]: r for r in records}


def save_master(master: dict) -> None:
    """Write to a temp file, then swap it in, so a crash never leaves a half-written file."""
    records = sorted(master.values(), key=lambda r: r["slug"])
    fd, tmp = tempfile.mkstemp(dir=OUTPUT_FILE.parent or ".", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, ensure_ascii=False)
    os.replace(tmp, OUTPUT_FILE)


def merge(master: dict, cards: dict, scraped: dict, run_at: str) -> dict:
    """Apply this week's scrape to the master records. Returns a change summary."""
    summary = {
        "added": [],
        "changed": [],
        "returned": [],
        "removed": [],
        "unchanged": 0,
    }

    for slug, card in cards.items():
        parsed = scraped.get(slug)  # None if this page failed to scrape
        existing = master.get(slug)

        if existing is None:
            if parsed is None:
                continue  # new drink but its page failed; pick it up next week
            master[slug] = {
                "slug": slug,
                "url": card["url"],
                "name": card["name"],
                "iba_category": card["iba_category"],
                "ingredients": parsed["ingredients"],
                "method": parsed["method"],
                "garnish": parsed["garnish"],
                "status": "active",
                "first_seen_at": run_at,
                "last_seen_at": run_at,
                "updated_at": run_at,
                "removed_at": None,
            }
            summary["added"].append(slug)
            continue

        # Still listed on the site -> it's active and was seen this run.
        if existing["status"] == "removed":
            summary["returned"].append(slug)
        existing["status"] = "active"
        existing["removed_at"] = None
        existing["last_seen_at"] = run_at

        if parsed is None:
            # Not scraped (already in the file) or its page failed. The directory
            # card still gives us name + category for free, so check just those.
            card_values = {"name": card["name"], "iba_category": card["iba_category"]}
            if any(existing.get(f) != v for f, v in card_values.items()):
                existing.update(card_values)
                existing["updated_at"] = run_at
                summary["changed"].append(slug)
            else:
                summary["unchanged"] += 1
            continue

        new_values = {
            "name": card["name"],
            "iba_category": card["iba_category"],
            "ingredients": parsed["ingredients"],
            "method": parsed["method"],
            "garnish": parsed["garnish"],
        }
        if any(existing.get(f) != new_values[f] for f in RECIPE_FIELDS):
            existing.update(new_values)
            existing["updated_at"] = run_at
            summary["changed"].append(slug)
        else:
            summary["unchanged"] += 1

    # Anything active in the master that the directory no longer lists -> removed.
    for slug, record in master.items():
        if slug not in cards and record["status"] == "active":
            record["status"] = "removed"
            record["removed_at"] = run_at
            summary["removed"].append(slug)

    return summary


# --------------------------------------------------------------------------
# Run
# --------------------------------------------------------------------------
def run_pipeline(full: bool = False):
    run_at = now_utc()
    master = load_master()
    active_before = sum(1 for r in master.values() if r["status"] == "active")

    with requests.Session() as session:
        session.headers.update(HEADERS)
        cards = fetch_all_cards(session)

        if active_before and len(cards) < active_before * MIN_LISTING_RATIO:
            print(
                f"\nABORTED: directory listed {len(cards)} drinks but the master has "
                f"{active_before} active. Site down or layout changed? Nothing was written."
            )
            return

        # Quick check: only open recipe pages for drinks NOT already in the file.
        # --full re-opens every page to catch recipe edits (run it occasionally).
        if full:
            to_scrape = cards
            print(
                f"\n{len(cards)} cocktails listed. --full: scraping every recipe page...\n"
            )
        else:
            to_scrape = {slug: c for slug, c in cards.items() if slug not in master}
            print(
                f"\n{len(cards)} cocktails listed. {len(cards) - len(to_scrape)} already in "
                f"{OUTPUT_FILE}, skipped. Scraping {len(to_scrape)} new...\n"
            )

        scraped, failures = {}, []
        for i, (slug, card) in enumerate(to_scrape.items(), 1):
            print(f"[{i}/{len(to_scrape)}] {card['name']}")
            try:
                resp = get(session, card["url"])
                resp.raise_for_status()
                scraped[slug] = parse_recipe_html(resp.text)
            except requests.RequestException as exc:
                failures.append((slug, str(exc)))
                print(f"  FAILED: {exc}")

    summary = merge(master, cards, scraped, run_at)
    save_master(master)

    warned = [
        (slug, p["parse_warnings"])
        for slug, p in scraped.items()
        if p["parse_warnings"]
    ]
    print(f"\n=== Run {run_at} -> {OUTPUT_FILE} ===")
    print(
        f"Active: {sum(1 for r in master.values() if r['status'] == 'active')}   "
        f"Removed (all time): {sum(1 for r in master.values() if r['status'] == 'removed')}"
    )
    print(f"Added:     {len(summary['added'])} {summary['added']}")
    print(f"Changed:   {len(summary['changed'])} {summary['changed']}")
    print(f"Returned:  {len(summary['returned'])} {summary['returned']}")
    print(f"Removed:   {len(summary['removed'])} {summary['removed']}")
    print(f"Unchanged: {summary['unchanged']}")
    print(f"Parse warnings: {len(warned)} {warned}")
    print(f"Page failures:  {len(failures)} {failures}")


if __name__ == "__main__":
    run_pipeline(full="--full" in sys.argv)

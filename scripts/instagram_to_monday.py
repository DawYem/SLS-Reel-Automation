#!/usr/bin/env python3
"""
Servant Leader Scholars — Instagram Reel -> monday.com automation

What this does, every time it runs:
  1. Asks Apify to scrape the latest reels from @servantleaderscholars
     (via the apify/instagram-reel-scraper Actor's run-sync-get-dataset-items
     endpoint, which runs the Actor and hands back the results in one call).
  2. Skips any reel we've already filed (tracked in state/processed_reels.json).
  3. For each new reel:
       - Checks the caption/hashtags for "day of service" / "community service".
       - Looks for a matching item on the monday.com board by name.
       - If found, drops the reel link into that item's Link column.
       - If not found, creates a new item in the current month's group and
         sets the Link column there.
  4. Saves the updated "already processed" list back to disk.

This script is meant to be run on a schedule by GitHub Actions (see
.github/workflows/instagram-to-monday.yml), which also commits the updated
state file back to the repo so the next run knows what's already been done.

Required environment variables (set as GitHub Actions secrets):
  APIFY_TOKEN   - Apify personal API token
  MONDAY_TOKEN  - monday.com personal API token
"""

import json
import os
import sys
import re
from datetime import datetime, timezone
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Configuration - adjust these if your board/columns/keywords change
# ---------------------------------------------------------------------------

INSTAGRAM_USERNAME = "servantleaderscholars"
APIFY_ACTOR_ID = "apify~instagram-reel-scraper"  # apify/instagram-reel-scraper
BOARD_ID = 18348530348
LINK_COLUMN_ID = "link_mkkw6x1d"

# Only scrape reels newer than this (keep in sync with how often the Action runs).
# "2 days" gives a safety margin if a run is ever delayed or skipped.
ONLY_NEWER_THAN = "2 days"
RESULTS_LIMIT = 10

# Caption/hashtag keywords that mean "this reel belongs with our recurring
# Day of Service content" - matched case-insensitively as substrings.
MATCH_KEYWORDS = ["day of service", "community service"]

# Month name -> monday.com group ID, from the board's existing group structure.
MONTH_GROUPS = {
    "January": "new_group71",
    "February": "new_group_mkkw81sf",
    "March": "new_group_mkkwdfvq",
    "April": "new_group_mkmr55yq",
    "May": "group_mknmdmwx",
    "June": "group_mkqtdfs4",
    "July": "group_mkqtkwmz",
    "August": "group_mkqtqatm",
    "September": "group_mktqyrf7",
    "October": "group_mktq3swr",
    "November": "group_mktqv5nr",
    "December": "group_mktq79rx",
}

STATE_FILE = Path(__file__).resolve().parent.parent / "state" / "processed_reels.json"

APIFY_TOKEN = os.environ.get("APIFY_TOKEN")
MONDAY_TOKEN = os.environ.get("MONDAY_TOKEN")

MONDAY_API_URL = "https://api.monday.com/v2"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def log(msg):
    print(f"[{datetime.now(timezone.utc).isoformat()}] {msg}", flush=True)


def load_processed_ids():
    if STATE_FILE.exists():
        with open(STATE_FILE, "r") as f:
            return set(json.load(f))
    return set()


def save_processed_ids(ids):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(sorted(ids), f, indent=2)


def fetch_new_reels():
    """Run the Apify actor and return its dataset items directly."""
    if not APIFY_TOKEN:
        sys.exit("Missing APIFY_TOKEN environment variable.")

    url = f"https://api.apify.com/v2/acts/{APIFY_ACTOR_ID}/run-sync-get-dataset-items"
    params = {"token": APIFY_TOKEN, "timeout": 120}
    payload = {
        "username": [INSTAGRAM_USERNAME],
        "resultsLimit": RESULTS_LIMIT,
        "onlyPostsNewerThan": ONLY_NEWER_THAN,
        "skipPinnedPosts": True,
    }

    log(f"Requesting reels for @{INSTAGRAM_USERNAME} from Apify...")
    resp = requests.post(url, params=params, json=payload, timeout=150)
    resp.raise_for_status()
    items = resp.json()
    log(f"Apify returned {len(items)} reel(s).")
    return items


def reel_id(item):
    """Best-effort unique identifier for a reel across possible field names."""
    for key in ("id", "shortCode", "pk", "code"):
        if item.get(key):
            return str(item[key])
    # Fall back to the URL itself if nothing else is present.
    return item.get("url") or item.get("reelUrl") or json.dumps(item, sort_keys=True)


def reel_url(item):
    for key in ("url", "reelUrl", "postUrl", "link"):
        if item.get(key):
            return item[key]
    return None


def reel_caption(item):
    for key in ("caption", "text", "description"):
        if item.get(key):
            return item[key]
    return ""


def matches_keywords(caption):
    caption_lower = (caption or "").lower()
    return any(kw in caption_lower for kw in MATCH_KEYWORDS)


def monday_request(query, variables=None):
    if not MONDAY_TOKEN:
        sys.exit("Missing MONDAY_TOKEN environment variable.")
    headers = {
        "Authorization": MONDAY_TOKEN,
        "Content-Type": "application/json",
    }
    resp = requests.post(
        MONDAY_API_URL,
        headers=headers,
        json={"query": query, "variables": variables or {}},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"monday.com API error: {data['errors']}")
    return data["data"]


def find_matching_item():
    """Look for an existing board item whose name mentions our keywords."""
    query = """
    query ($boardId: [ID!]) {
      boards(ids: $boardId) {
        items_page(limit: 500) {
          items {
            id
            name
          }
        }
      }
    }
    """
    data = monday_request(query, {"boardId": [BOARD_ID]})
    items = data["boards"][0]["items_page"]["items"]
    for item in items:
        name_lower = item["name"].lower()
        if any(kw in name_lower for kw in MATCH_KEYWORDS):
            return item
    return None


def set_link_column(item_id, url):
    query = """
    mutation ($boardId: ID!, $itemId: ID!, $columnValues: JSON!) {
      change_multiple_column_values(
        board_id: $boardId,
        item_id: $itemId,
        column_values: $columnValues
      ) {
        id
      }
    }
    """
    column_values = json.dumps({LINK_COLUMN_ID: {"url": url, "text": "View Reel"}})
    monday_request(
        query,
        {"boardId": BOARD_ID, "itemId": item_id, "columnValues": column_values},
    )


def create_item_with_link(item_name, url):
    month_name = datetime.now(timezone.utc).strftime("%B")
    group_id = MONTH_GROUPS.get(month_name)
    if not group_id:
        sys.exit(f"No group mapping found for month '{month_name}'.")

    query = """
    mutation ($boardId: ID!, $groupId: String!, $itemName: String!, $columnValues: JSON!) {
      create_item(
        board_id: $boardId,
        group_id: $groupId,
        item_name: $itemName,
        column_values: $columnValues
      ) {
        id
      }
    }
    """
    column_values = json.dumps({LINK_COLUMN_ID: {"url": url, "text": "View Reel"}})
    data = monday_request(
        query,
        {
            "boardId": BOARD_ID,
            "groupId": group_id,
            "itemName": item_name,
            "columnValues": column_values,
        },
    )
    return data["create_item"]["id"]


def default_item_name(caption):
    """Build a short, readable item name if we have to create a new item."""
    snippet = re.sub(r"\s+", " ", (caption or "")).strip()[:60]
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if snippet:
        return f"{snippet}... ({date_str})"
    return f"New Reel ({date_str})"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    processed = load_processed_ids()
    reels = fetch_new_reels()

    new_count = 0
    for item in reels:
        if item.get("error"):
            log(
                "Actor returned an error/placeholder item, skipping "
                f"(not a real reel): {item.get('errorDescription', item.get('error'))}"
            )
            continue

        rid = reel_id(item)
        if rid in processed:
            continue

        url = reel_url(item)
        caption = reel_caption(item)
        if not url:
            log(f"Skipping reel with no URL found: {item}")
            processed.add(rid)
            continue

        log(f"New reel: {url}")
        log(f"Caption/hashtags: {caption[:120]!r}")

        if matches_keywords(caption):
            matched_item = find_matching_item()
        else:
            matched_item = None

        if matched_item:
            log(f"Matched existing item '{matched_item['name']}' (id={matched_item['id']}) - adding link.")
            set_link_column(matched_item["id"], url)
        else:
            name = default_item_name(caption)
            log(f"No match found - creating new item '{name}'.")
            new_id = create_item_with_link(name, url)
            log(f"Created item id={new_id}.")

        processed.add(rid)
        new_count += 1

    save_processed_ids(processed)
    log(f"Done. Processed {new_count} new reel(s) this run.")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Servant Leader Scholars — Instagram Reel -> monday.com automation

What this does, every time it runs:
  1. Asks Apify to scrape the latest reels from @servantleaderscholars
     (via the apify/instagram-reel-scraper Actor's run-sync-get-dataset-items
     endpoint, which runs the Actor and hands back the results in one call).
  2. Skips any reel we've already filed (tracked in state/processed_reels.json).
  3. For each new reel:
       - Pulls every existing item name from the monday.com board.
       - Asks Claude to compare the reel's caption/content against those
         item names and decide - confidently - whether this reel belongs
         with an existing item (same event/campaign/topic), or is something new.
       - If a confident match is found, drops the reel link into that item's
         Link column.
       - If not, creates a new item in the current month's group and sets
         the Link column there.
  4. Saves the updated "already processed" list back to disk.

This script is meant to be run on a schedule by GitHub Actions (see
.github/workflows/instagram-to-monday.yml), which also commits the updated
state file back to the repo so the next run knows what's already been done.

Required environment variables (set as GitHub Actions secrets):
  APIFY_TOKEN       - Apify personal API token
  MONDAY_TOKEN      - monday.com personal API token
  ANTHROPIC_API_KEY - Anthropic API key, used to intelligently match a reel's
                      content against existing calendar item names
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

# Model used to compare a reel's caption against existing calendar item names.
# Haiku is plenty for this simple matching task and keeps cost negligible.
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"

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
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

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


def ai_match_item(caption, board_items):
    """
    Ask Claude whether this reel's caption clearly corresponds to an existing
    monday.com calendar item (same event, campaign, or topic), by comparing
    the reel's content against every item name on the board. Returns the
    matching item dict, or None if there's no confident match.
    """
    if not ANTHROPIC_API_KEY:
        sys.exit("Missing ANTHROPIC_API_KEY environment variable.")
    if not board_items:
        return None

    item_list = "\n".join(f"- {item['name']}" for item in board_items)
    prompt = f"""You're matching a new Instagram reel to an existing marketing content calendar for a nonprofit.

Reel caption:
\"\"\"
{caption or "(no caption)"}
\"\"\"

Existing calendar item names:
{item_list}

Does this reel clearly correspond to one of these existing items (the same event, campaign, or topic - not just a vague thematic similarity)? Only answer yes if you're confident. A generic or unrelated reel should not be forced into a match - it's completely fine, and expected, to say there's no match.

Reply with ONLY a JSON object and nothing else, in exactly this shape:
{{"match": "<exact item name from the list above>", "confident": true}}
or
{{"match": null, "confident": false}}"""

    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 200,
        "messages": [{"role": "user", "content": prompt}],
    }
    resp = requests.post(ANTHROPIC_API_URL, headers=headers, json=body, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    text = "".join(
        block.get("text", "")
        for block in data.get("content", [])
        if block.get("type") == "text"
    ).strip()
    # Be forgiving of stray code fences, just in case.
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()

    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        log(f"Could not parse Claude's matching response as JSON, treating as no match: {text!r}")
        return None

    if not result.get("confident") or not result.get("match"):
        return None

    match_name = result["match"]
    for item in board_items:
        if item["name"] == match_name:
            return item

    log(f"Claude returned a match name not found on the board, treating as no match: {match_name!r}")
    return None


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


def get_all_board_items():
    """Fetch every item currently on the board (id + name) for AI matching."""
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
    return data["boards"][0]["items_page"]["items"]


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
    board_items = get_all_board_items()

    new_count = 0
    for item in reels:
        if item.get("error"):
            log(
                "Actor returned an error/placeholder item, skipping "
                f"(not a real reel): {item.get('errorDescription', item.get('error'))}"
            )
            continue

        # The actor's own "skip pinned reels" input isn't reliable - it can
        # still return an account's pinned reels regardless of that setting
        # or the date filter. Filter them out here instead.
        if item.get("isPinned"):
            log(f"Skipping pinned reel (not a new post): {item.get('url')}")
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

        matched_item = ai_match_item(caption, board_items)

        if matched_item:
            log(f"Claude matched this to existing item '{matched_item['name']}' (id={matched_item['id']}) - adding link.")
            set_link_column(matched_item["id"], url)
        else:
            name = default_item_name(caption)
            log(f"No confident match found - creating new item '{name}'.")
            new_id = create_item_with_link(name, url)
            log(f"Created item id={new_id}.")
            # So a second new reel in this same run could still match this
            # brand-new item if it turns out to be about the same thing.
            board_items.append({"id": new_id, "name": name})

        processed.add(rid)
        new_count += 1

    save_processed_ids(processed)
    log(f"Done. Processed {new_count} new reel(s) this run.")


if __name__ == "__main__":
    main()

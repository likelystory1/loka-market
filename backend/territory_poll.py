"""
Standalone territory poll script — run via cron.

Fetches territories + alliance data, snapshots to territories.db,
and detects battles. Safe to run repeatedly (INSERT OR IGNORE).

Usage:
    python territory_poll.py
"""

import os
import json
import requests
from territory_poller import poll_cycle

BASE_DIR             = os.path.dirname(os.path.abspath(__file__))
LOKA_CACHE_ALLIANCES = os.path.join(BASE_DIR, "cache", "alliances.json")
API_BASE             = "https://api.lokamc.com"


def get_alliances():
    """Return alliances list from cache file, or fetch from API if missing."""
    if os.path.exists(LOKA_CACHE_ALLIANCES):
        try:
            with open(LOKA_CACHE_ALLIANCES) as f:
                data = json.load(f)
            alliances = data.get("_embedded", {}).get("alliances", [])
            if alliances:
                return alliances
        except Exception:
            pass

    # Cache missing or empty — fetch directly
    try:
        r = requests.get(f"{API_BASE}/alliances", timeout=15)
        r.raise_for_status()
        data = r.json()
        os.makedirs(os.path.dirname(LOKA_CACHE_ALLIANCES), exist_ok=True)
        with open(LOKA_CACHE_ALLIANCES, "w") as f:
            json.dump(data, f)
        return data.get("_embedded", {}).get("alliances", [])
    except Exception as e:
        print(f"[warn] Could not fetch alliances: {e}")
        return []


if __name__ == "__main__":
    poll_cycle(get_alliances)

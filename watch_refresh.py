#!/data/data/com.termux/files/usr/bin/env python
"""
Watches for a live-refresh request from the dashboard's "Refresh" button and
runs run_daily.sh when one is pending.

The button (via a Cloudflare Worker - see cloudflare-worker/README/worker.js)
writes a timestamp to docs/data/refresh_request.json in the repo. This script
polls the live GitHub Pages site (same place the dashboard itself reads from,
so no GitHub API rate limits) and compares that timestamp against
docs/data/index.json's own generated_at to decide whether a scrape is still
owed. If it is, it runs run_daily.sh, which pulls, scrapes, and pushes if the
data actually changed - after that, index.json's generated_at is naturally
newer than the request, so the next poll sees nothing pending.

Meant to run continuously in the background on the phone (e.g. started at
boot via Termux:Boot, wrapped in `termux-wake-lock` so Android doesn't
suspend it). Ctrl+C to stop when running in the foreground.

Usage: python watch_refresh.py
"""
import subprocess
import sys
import time
from datetime import datetime

import requests

PAGES_BASE = "https://abhi24me.github.io/koshvani-dashboard"
REQUEST_URL = f"{PAGES_BASE}/data/refresh_request.json"
INDEX_URL = f"{PAGES_BASE}/data/index.json"
POLL_SECONDS = 120
REPO_DIR = None  # None = current directory; set to an absolute path if run from elsewhere


def parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def fetch_json(url):
    resp = requests.get(url, timeout=20, params={"_": int(time.time())})
    resp.raise_for_status()
    return resp.json()


def check_once():
    try:
        requested_at = parse_iso(fetch_json(REQUEST_URL).get("requested_at"))
        generated_at = parse_iso(fetch_json(INDEX_URL).get("generated_at"))
    except Exception as exc:
        print(f"[watch] could not check for a refresh request: {exc}", file=sys.stderr)
        return

    if requested_at and (not generated_at or requested_at > generated_at):
        print(f"[watch] refresh requested at {requested_at} (last data from {generated_at}) - running now")
        subprocess.run(["bash", "run_daily.sh"], cwd=REPO_DIR)
    else:
        print(f"[watch] nothing pending (requested_at={requested_at}, generated_at={generated_at})")


def main():
    print(f"[watch] polling every {POLL_SECONDS}s - press Ctrl+C to stop")
    while True:
        check_once()
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[watch] stopped")

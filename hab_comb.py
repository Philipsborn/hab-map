#!/usr/bin/env python3
"""
HAB Comb Agent  -  Hydralife Solutions
--------------------------------------
Runs daily on GitHub Actions. Finds NEW U.S. (lower-48) harmful algae bloom
advisories reported recently, verifies + geocodes them, dedupes against the
existing events.json, and appends the new ones. The live map (index.html)
reads events.json, so anything added here shows up on the map automatically.

Safety:
- Append-only. It never deletes or rewrites existing events.
- Validates every new event (required fields, lower-48 coords, real URL, ISO date).
- If the model returns nothing or errors, events.json is left untouched.
- Writes LATEST_RUN.md each run so you have a plain-English review trail.
"""

import json, os, re, datetime
from anthropic import Anthropic

EVENTS_FILE  = "events.json"
SUMMARY_FILE = "LATEST_RUN.md"
MODEL          = os.environ.get("HAB_MODEL", "claude-sonnet-4-6")
LOOKBACK_DAYS  = int(os.environ.get("HAB_LOOKBACK_DAYS", "10"))
MAX_SEARCHES   = int(os.environ.get("HAB_MAX_SEARCHES", "12"))  # caps API cost per run


def load_events():
    with open(EVENTS_FILE, encoding="utf-8") as f:
        return json.load(f)


def norm_key(e):
    name = re.sub(r"[^a-z0-9]", "", str(e.get("name", "")).lower())
    return f"{e.get('state','')}|{name}|{e.get('date','')}"


PROMPT = """You are the HAB Discovery Agent for Hydralife Solutions. Your job is to
find NEW harmful algae bloom (HAB) events reported in the United States lower 48
states in roughly the last {days} days, and return them as clean JSON.

A HAB is any reported bloom of cyanobacteria / blue-green algae, red tide

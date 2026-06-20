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
(Karenia brevis), golden alga (Prymnesium parvum), or any algae bloom that has
triggered a public advisory, warning, closure, illness, or death. Lower 48 only.
Exclude Alaska, Hawaii, territories, and anything outside the United States.

Use web search across: state environmental/health agency advisory pages, county
health departments, EPA/NOAA, and credible local news. Search broadly across many
states, not just Florida.

For EACH new event, output a JSON object with EXACTLY these fields:
  "id"          : "YYYYMMDD-XX-NNN"  (date + 2-letter state + a number)
  "date"        : "YYYY-MM-DD"       (date bloom was observed/sampled/reported)
  "state"       : two-letter state code
  "county"      : county name
  "name"        : water body name
  "type"        : lake / reservoir / pond / river / stream / canal / coastal / other
  "sev"         : advisory / warning / danger / closure
  "algae"       : e.g. "cyanobacteria", "Karenia brevis", "Prymnesium parvum"
  "lat"         : decimal latitude (geocode the water body; centroid is fine)
  "lng"         : decimal longitude
  "url"         : the primary source URL
  "source_type" : state_agency / federal_agency / county_health / local_news / citizen_report
  "quote"       : a short factual summary drawn from the source (1-3 sentences)

RULES:
- Do NOT fabricate. If you cannot find a real source URL, do not include the event.
- INCLUDE the event even if the advisory was later LIFTED, downgraded, rescinded, or the bloom has since dissipated. If it was a real 2026 HAB advisory, warning, caution, closure, illness, or death at any point during 2026, it belongs on the map. Use the original issue or sample date. Never exclude an event solely because it is no longer active.
- Coordinates must fall inside the lower 48 (lat 24-50, lng -125 to -66).
- Do NOT include any event already in the EXISTING list below (match by water body
  + state + nearby date). Only return events that are genuinely new.
- Output ONLY a single JSON array of the new event objects, nothing else.
  If there are no new events, output exactly: []

EXISTING events already on the map (state | name | date):
{existing}
"""


def call_claude(prompt):
    client = Anthropic()
    resp = client.messages.create(
        model=MODEL,
        max_tokens=8000,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": MAX_SEARCHES}],
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", None) == "text")


def extract_json_array(text):
    m = re.search(r"\[\s*(?:\{.*\}\s*)?\]", text, re.S)
    if not m:
        return []
    return json.loads(m.group(0))


REQUIRED = ["id","date","state","county","name","type","sev","algae","lat","lng","url","source_type","quote"]


def valid_event(e):
    if not all(k in e and e[k] not in (None, "") for k in REQUIRED):
        return False
    try:
        lat, lng = float(e["lat"]), float(e["lng"])
    except (TypeError, ValueError):
        return False
    if not (24.0 <= lat <= 50.0 and -125.5 <= lng <= -66.0):
        return False
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", str(e["date"])):
        return False
    if not str(e["url"]).startswith("http"):
        return False
    return True


def main():
    events = load_events()
    ids  = {e.get("id", "") for e in events}
    keys = {norm_key(e) for e in events}

    existing_lines = "\n".join(
        f"{e.get('state')} | {e.get('name')} | {e.get('date')}" for e in events
    )
    prompt = PROMPT.format(days=LOOKBACK_DAYS, existing=existing_lines)

    try:
        text = call_claude(prompt)
        found = extract_json_array(text)
    except Exception as ex:
        print("Run error (events.json left unchanged):", ex)
        found = []

    added, skipped = [], []
    for e in found:
        if not valid_event(e):
            skipped.append((e.get("name", "?"), "failed validation"))
            continue
        if e["id"] in ids or norm_key(e) in keys:
            skipped.append((e.get("name", "?"), "duplicate"))
            continue
        events.append(e)
        ids.add(e["id"]); keys.add(norm_key(e))
        added.append(e)

    if added:
        with open(EVENTS_FILE, "w", encoding="utf-8") as f:
            json.dump(events, f, ensure_ascii=False, indent=1)

    with open(SUMMARY_FILE, "w", encoding="utf-8") as f:
        f.write(f"# HAB comb run {datetime.datetime.utcnow():%Y-%m-%d %H:%M} UTC\n\n")
        f.write(f"Total events on the map: {len(events)}\n\n")
        f.write(f"## Added {len(added)}\n")
        for e in added:
            f.write(f"- {e['state']} - {e['name']} ({e['date']}, {e['sev']}) - {e['url']}\n")
        f.write(f"\n## Skipped {len(skipped)}\n")
        for n, why in skipped:
            f.write(f"- {n}: {why}\n")

    print(f"Added {len(added)}, skipped {len(skipped)}, total now {len(events)}")


if __name__ == "__main__":
    main()

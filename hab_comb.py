#!/usr/bin/env python3
"""
HAB Comb Agent  -  Hydralife Solutions
--------------------------------------
Runs weekly on GitHub Actions. Adds NEW U.S. (lower-48) harmful algae bloom
events from two sources, dedupes them against the existing events.json, and
appends the new ones. The live map (index.html) reads events.json, so anything
added here shows up on the map automatically.

Sources:
1. California direct API - data.ca.gov Freshwater HAB dataset (a stable public
   API). Pulls this year's cases where California actually recommended an
   advisory (Caution / Warning / Danger / posted algal-mat alert). Reliable and
   not model-dependent.
2. The comb - Claude with web search, for every other state, using the confirmed
   -only policy below.

Policy:
- CONFIRMED HABs only. An event is added only when an agency has confirmed a
  bloom (visual confirmation, lab toxin/cell-count results, or an official
  recreational advisory / warning / closure). Unconfirmed "possible",
  "suspected", "under investigation", "pending", "at risk", and citizen-only
  reports are excluded.

Safety:
- Append-only. It never deletes or rewrites existing events.
- Validates every new event (required fields, canonical type, lower-48 coords,
  real URL, ISO date, confirmation language).
- If the California API fails, it is skipped with a ::warning:: and the run
  continues. If the model errors (e.g. API credit exhausted), events.json is
  left unchanged and the job FAILS LOUDLY (red run + ::error:: annotation).
- Writes LATEST_RUN.md on each successful run for a plain-English review trail.
"""

import json, os, re, sys, datetime, urllib.request, urllib.parse
from anthropic import Anthropic

EVENTS_FILE  = "events.json"
SUMMARY_FILE = "LATEST_RUN.md"
MODEL          = os.environ.get("HAB_MODEL", "claude-sonnet-4-5")
LOOKBACK_DAYS  = int(os.environ.get("HAB_LOOKBACK_DAYS", "21"))
MAX_SEARCHES   = int(os.environ.get("HAB_MAX_SEARCHES", "25"))  # caps API cost per run

CANONICAL_TYPES = {"lake","reservoir","pond","river","canal","coastal","other"}


def load_events():
    with open(EVENTS_FILE, encoding="utf-8") as f:
        return json.load(f)


def norm_key(e):
    name = re.sub(r"[^a-z0-9]", "", str(e.get("name", "")).lower())
    return f"{e.get('state','')}|{name}|{e.get('date','')}"


def wb_type(name):
    u = str(name).upper()
    if "CANAL" in u: return "canal"
    if "RIVER" in u or "CREEK" in u: return "river"
    if "POND" in u: return "pond"
    if "MARSH" in u: return "other"
    if "RESERVOIR" in u or "DAM" in u: return "reservoir"
    return "lake"


# ----------------------------------------------------------------------------
# Source 1: California direct API (data.ca.gov Freshwater HAB dataset)
# ----------------------------------------------------------------------------
CA_RESOURCE = "67648948-034f-4882-bbc0-c07c7d38daf9"
CA_URL      = "https://mywaterquality.ca.gov/habs/resources/reports-map/"
# Advisory_Recommended value -> map severity. These are the confirmed tiers;
# "None", "General awareness", "Visual observation" etc. are intentionally excluded.
CA_ADVISORY_SEV = {
    "Caution": "advisory",
    "Warning": "warning",
    "Danger":  "danger",
    "Algal mat alert sign": "advisory",
}


def pull_california():
    """Return this year's confirmed California advisories from the state API."""
    year = datetime.datetime.utcnow().year
    levels = "','".join(CA_ADVISORY_SEV.keys())
    sql = (f'SELECT "Case_Water_Body_Name","County","Bloom_Latitude","Bloom_Longitude",'
           f'"Advisory_Recommended","Case_Start_Date" FROM "{CA_RESOURCE}" '
           f'WHERE "Case_Year" = \'{year}\' AND "Advisory_Recommended" IN (\'{levels}\')')
    url = "https://data.ca.gov/api/3/action/datastore_search_sql?sql=" + urllib.parse.quote(sql)
    req = urllib.request.Request(url, headers={"User-Agent": "hydralife-hab-comb"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.load(resp)
    recs = (data.get("result") or {}).get("records") or []
    out = []
    for r in recs:
        wb   = (r.get("Case_Water_Body_Name") or "").strip()
        lat  = r.get("Bloom_Latitude"); lng = r.get("Bloom_Longitude")
        date = (r.get("Case_Start_Date") or "")[:10]
        adv  = r.get("Advisory_Recommended")
        if not (wb and lat and lng and date.startswith(str(year))):
            continue
        try:
            lat = float(lat); lng = float(lng)
        except (TypeError, ValueError):
            continue
        county = (r.get("County") or "").strip()
        sev = CA_ADVISORY_SEV.get(adv, "advisory")
        if adv == "Algal mat alert sign":
            quote = (f"California's HAB program posted an algal mat alert at {wb} ({county} County) on {date}; "
                     f"avoid contact with visible cyanobacteria mats and scum, and keep children and pets away.")
        else:
            quote = (f"California's State Water Board HAB program posted a {adv} advisory at {wb} ({county} County) "
                     f"based on a bloom reported {date}; avoid contact with the water where scum is present.")
        out.append({"date": date, "state": "CA", "county": county, "name": wb,
                    "type": wb_type(wb), "sev": sev, "algae": "cyanobacteria",
                    "lat": round(lat, 4), "lng": round(lng, 4),
                    "url": CA_URL, "source_type": "state_agency", "quote": quote})
    return out


# ----------------------------------------------------------------------------
# Source 2: the comb (Claude + web search) for every other state
# ----------------------------------------------------------------------------
PROMPT = """You are the HAB Discovery Agent for Hydralife Solutions. Your job is to
find NEW, AGENCY-CONFIRMED harmful algae bloom (HAB) events in the United States
lower 48 states from roughly the last {days} days, and return them as clean JSON.
California is already covered by a separate data feed, so you may skip California.

WHAT COUNTS (confirmed only):
A HAB for this map is a bloom of cyanobacteria / blue-green algae, red tide
(Karenia brevis), or golden alga (Prymnesium parvum) that an agency has CONFIRMED
in one of these ways:
  - a state, federal, or county agency visually confirmed the bloom, OR
  - laboratory results confirmed cyanotoxins or a cell-count exceedance, OR
  - an agency issued an official recreational advisory, warning, or closure.
Lower 48 only. Exclude Alaska, Hawaii, territories, and anything outside the U.S.

WHAT TO EXCLUDE (do NOT add these):
  - Unconfirmed or citizen-only reports an agency has not confirmed.
  - Anything described as "possible", "suspected", "under investigation",
    "awaiting results", "pending", or a water body merely "at risk".
  - Pure bacteria / E. coli beach advisories with no algal-toxin component.
  When in doubt, leave it out.

Use web search aggressively across MANY states every run, not just Florida. Check these
authoritative state HAB dashboards and advisory pages by name, plus their host agencies:
- Nevada: Nevada Office of State Epidemiology HAB page (nvose.org)
- Kansas: KDHE / KDWP blue-green algae public health advisory list
- Florida: county Department of Health blue-green algae alerts; FWC red tide status
- New York: DEC NYHABS notifications list
- Ohio, Michigan, Wisconsin, Minnesota, Indiana: state EPA / DNR / health beach and HAB dashboards
- Oregon, Utah, Washington, Arizona, Colorado, Nebraska: state HAB advisory pages
- Federal / multistate: EPA CyAN, NOAA, USACE lake and swim-beach closures, USGS
In summer (June through September) prioritize the Great Lakes, Upper Midwest, and Northeast,
where bloom volume peaks, in addition to the year-round Sunbelt sources. Run enough distinct
searches to cover at least 8 to 10 different states each run. Map source severity language to
the schema: a state "watch" maps to advisory, "warning" maps to warning, a beach or lake
"closure" maps to closure, and life-threatening or extreme levels map to danger.

For EACH new confirmed event, output a JSON object with EXACTLY these fields:
  "id"          : "YYYYMMDD-XX-NNN"  (date + 2-letter state + a number)
  "date"        : "YYYY-MM-DD"       (date bloom was observed/sampled/reported)
  "state"       : two-letter state code
  "county"      : county name
  "name"        : water body name
  "type"        : lake / reservoir / pond / river / canal / coastal / other
                  (use "river" for streams and creeks)
  "sev"         : advisory / warning / danger / closure
  "algae"       : e.g. "cyanobacteria", "Karenia brevis", "Prymnesium parvum"
  "lat"         : decimal latitude (geocode the water body; centroid is fine)
  "lng"         : decimal longitude
  "url"         : the primary source URL
  "source_type" : state_agency / federal_agency / county_health / local_news
  "quote"       : a short factual summary from the source that shows the bloom was
                  confirmed (name the confirming agency, toxin/cell result, or the
                  advisory/closure that was issued). 1-3 sentences.

RULES:
- Do NOT fabricate. If you cannot find a real source URL, do not include the event.
- Include an event even if the advisory was later LIFTED, downgraded, or the bloom
  has dissipated, AS LONG AS it was an agency-confirmed HAB at some point in {year}.
  Use the original issue or sample date. But NEVER include an event that was only
  ever a possible / suspected / unconfirmed report.
- Coordinates must fall inside the lower 48 (lat 24-50, lng -125 to -66).
- Do NOT include any event already in the EXISTING list below (match by water body
  + state + nearby date). Only return events that are genuinely new.
- Output ONLY a single JSON array of the new event objects, nothing else.
  If there are no new confirmed events, output exactly: []

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

CONFIRMED_RE = re.compile(
    r"confirm|µg/?l|ug/?l|ppb|cells?/ml|cell count|microcystin|anatoxin|"
    r"cylindrospermopsin|saxitoxin|toxin.{0,20}(detect|exceed|measur)|"
    r"(detect|exceed|measur).{0,20}(microcyst|anatox|cylindro|saxitox|toxin)|"
    r"do not (swim|contact|use)|avoid (all )?(water )?contact|"
    r"health (alert|advisory) (issued|posted)|alert (posted|sign)|closure|beach closed", re.I)

UNCONFIRMED_RE = re.compile(
    r"\bpossible\b|report of possible|public (bloom )?report|reported by the public|"
    r"\bsuspected\b|under investigation|\bpending\b|awaiting results|\bat risk\b|"
    r"photos? (show|indicate|appear)", re.I)


def looks_confirmed(e):
    q  = str(e.get("quote", ""))
    st = str(e.get("source_type", ""))
    strong = bool(CONFIRMED_RE.search(q))
    if UNCONFIRMED_RE.search(q) and not strong:
        return False
    if st == "citizen_report" and not strong:
        return False
    return True


def valid_event(e):
    if not all(k in e and e[k] not in (None, "") for k in REQUIRED):
        return False
    if str(e.get("type")).lower() not in CANONICAL_TYPES:
        return False
    if str(e.get("sev")).lower() not in {"advisory","warning","danger","closure"}:
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
    if not looks_confirmed(e):
        return False
    return True


def main():
    events = load_events()
    ids  = {e.get("id", "") for e in events}
    keys = {norm_key(e) for e in events}

    # Source 1: California API (best-effort; never blocks the run).
    try:
        california = pull_california()
        for i, e in enumerate(california):
            e["id"] = f"{e['date'].replace('-','')}-CA-{900 + i:03d}"
        print(f"California API returned {len(california)} confirmed advisories")
    except Exception as ex:
        print(f"::warning::California API pull failed (skipped this run): {ex}")
        california = []

    # Source 2: the comb (model + web search). A hard failure fails the whole run.
    year = datetime.datetime.utcnow().year
    existing_lines = "\n".join(
        f"{e.get('state')} | {e.get('name')} | {e.get('date')}" for e in events
    )
    prompt = PROMPT.format(days=LOOKBACK_DAYS, year=year, existing=existing_lines)
    try:
        text  = call_claude(prompt)
        found = extract_json_array(text)
    except Exception as ex:
        print(f"::error::HAB comb failed, events.json left unchanged: {ex}")
        sys.exit(1)

    added, skipped, ca_added = [], [], 0
    for e in california + found:
        if not valid_event(e):
            skipped.append((e.get("name", "?"), "failed validation / not confirmed"))
            continue
        if e["id"] in ids or norm_key(e) in keys:
            skipped.append((e.get("name", "?"), "duplicate"))
            continue
        events.append(e)
        ids.add(e["id"]); keys.add(norm_key(e))
        added.append(e)
        if e.get("state") == "CA":
            ca_added += 1

    if added:
        with open(EVENTS_FILE, "w", encoding="utf-8") as f:
            json.dump(events, f, ensure_ascii=False, indent=1)

    with open(SUMMARY_FILE, "w", encoding="utf-8") as f:
        f.write(f"# HAB comb run {datetime.datetime.utcnow():%Y-%m-%d %H:%M} UTC\n\n")
        f.write(f"Total events on the map: {len(events)}\n\n")
        f.write(f"## Added {len(added)} (confirmed only; {ca_added} from the California API)\n")
        for e in added:
            f.write(f"- {e['state']} - {e['name']} ({e['date']}, {e['sev']}) - {e['url']}\n")
        f.write(f"\n## Skipped {len(skipped)}\n")
        for n, why in skipped:
            f.write(f"- {n}: {why}\n")

    print(f"Added {len(added)} ({ca_added} CA), skipped {len(skipped)}, total now {len(events)}")


if __name__ == "__main__":
    main()

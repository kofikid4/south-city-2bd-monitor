#!/usr/bin/env python3
"""
Equity Apartments availability monitor.

Scrapes a community page, tracks target-bedroom units across runs,
opens a GitHub issue (and optionally emails) when a unit is listed,
and logs final pricing when a unit leaves the market.

Runs in GitHub Actions on a schedule. State and logs are plain files
committed back to the repo:

    data/state.json            current snapshot (bookkeeping)
    data/availability_log.csv  one row per listed / price_change event
    data/offline_log.csv       one row per unit that left the market

Configuration is via environment variables (see README).
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ----------------------------------------------------------------- config

PROPERTY_URL = os.environ.get(
    "PROPERTY_URL",
    "https://www.equityapartments.com/san-francisco-bay/"
    "south-san-francisco/south-city-station-apartments",
)
PROPERTY_NAME = os.environ.get("PROPERTY_NAME", "South City Station")

# Unit-level application portal (EliseAI). Client rendered, so it is scraped
# with headless Chromium; exposes real unit numbers and a per-term pricing
# matrix. Primary source when SOURCE=auto.
UNITS_APP_URL = os.environ.get(
    "UNITS_APP_URL",
    "https://eqr-applications.com/building/south-city-station-2/units",
)
# auto  = try the application portal, fall back to equityapartments.com
# eqr   = portal only (fail loudly if it breaks)
# eqweb = equityapartments.com only (no browser needed)
SOURCE = os.environ.get("SOURCE", "auto").lower()
# Lease term, in months, whose rate gets recorded. If a unit does not offer
# exactly this term, the closest available term is used and logged as such.
TARGET_TERM = int(os.environ.get("TARGET_TERM", "12"))

# Bedroom counts to track. Comma separated, 0 = studio. Default "2".
TARGET_BEDS = {
    int(b) for b in os.environ.get("TARGET_BEDS", "2").replace(" ", "").split(",") if b
} or {2}

# Which events open a GitHub issue: listed, price_change, delisted
NOTIFY_EVENTS = {
    e.strip() for e in os.environ.get("NOTIFY_EVENTS", "listed").split(",") if e.strip()
}

# Consecutive runs a unit must be absent before it is logged as offline.
# Protects against a single flaky page render creating a false delisting.
OFFLINE_CONFIRM_RUNS = int(os.environ.get("OFFLINE_CONFIRM_RUNS", "2"))

DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
DEBUG_DIR = Path("debug")
STATE_FILE = DATA_DIR / "state.json"
AVAIL_LOG = DATA_DIR / "availability_log.csv"
OFFLINE_LOG = DATA_DIR / "offline_log.csv"
ALERT_FILE = Path("alert.md")  # written only when a notification fires

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
}

# ----------------------------------------------------------------- regexes

PRICE_RE = re.compile(r"\$([\d,]{3,})")
BEDS_RE = re.compile(r"(\d+)\s*Bed\b|\bStudio\b", re.I)
BATHS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*Bath", re.I)
SQFT_RE = re.compile(r"([\d,]+)\s*sq\W{0,3}ft", re.I)
FLOOR_RE = re.compile(r"Floor\s*(\d+)", re.I)
AVAIL_RE = re.compile(r"Available\s+(Now|\d{1,2}/\d{1,2}/\d{2,4})", re.I)
TERM_RE = re.compile(r"(\d+)\s*mo\b", re.I)
FP_IMG_RE = re.compile(r"-FP-(\d+)-")
TAB_RE = re.compile(r"(All|Studio|\d\s*Bed)\s*\((\d+)\)")
UNITID_PATTERNS = [
    re.compile(
        r'unit[\s_-]?(?:id|number)["\']?\s*[:=]\s*["\']?([A-Za-z]{0,3}\d[\w.-]{0,10})',
        re.I,
    ),
    re.compile(r"[?&/]unit(?:id)?=([A-Za-z]{0,3}\d[\w.-]{0,10})", re.I),
]
APT_RE = re.compile(r"\bApt\.?\s*#?\s*([A-Za-z]{0,3}\d[\w.-]{0,8})", re.I)

# ----------------------------------------------------------------- helpers


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def money(v) -> str:
    try:
        return f"${int(v):,}"
    except (TypeError, ValueError):
        return str(v)


def norm_date(raw: str) -> str:
    raw = raw.strip()
    if raw.lower() == "now":
        return "now"
    m, d, y = raw.split("/")
    y = int(y)
    if y < 100:
        y += 2000
    return f"{y:04d}-{int(m):02d}-{int(d):02d}"


def gh_output(**kv):
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a") as f:
        for k, v in kv.items():
            f.write(f"{k}={str(v).replace(chr(10), ' ')}\n")


def step_summary(md: str):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as f:
            f.write(md + "\n")
    else:
        print(md)


# ----------------------------------------------------------------- fetch


def fetch_html() -> str:
    test = os.environ.get("TEST_HTML")
    if test:
        return Path(test).read_text()
    last = ""
    for attempt in range(3):
        try:
            r = requests.get(PROPERTY_URL, headers=HEADERS, timeout=30)
            if r.status_code == 200 and len(r.text) > 20000:
                return r.text
            last = f"HTTP {r.status_code}, {len(r.text)} bytes"
        except requests.RequestException as exc:
            last = str(exc)
        time.sleep(10 * (attempt + 1))
    raise RuntimeError(
        f"Could not fetch {PROPERTY_URL} ({last}). "
        "If this persists, the site may be blocking GitHub runner IPs."
    )


# ------------------------------------------- source: application portal


def _capture_portal_payloads() -> tuple[list, str]:
    """Render the units page headlessly and capture every JSON response the
    app fetches. Returns (payloads, rendered_html)."""
    from playwright.sync_api import sync_playwright  # lazy: eqweb needs no browser

    payloads: list = []
    html = ""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=HEADERS["User-Agent"],
            viewport={"width": 1280, "height": 2000},
        )
        page = ctx.new_page()

        def on_response(resp):
            try:
                if "json" not in resp.headers.get("content-type", ""):
                    return
                body = resp.text()
                if 2 < len(body) <= 3_000_000:
                    payloads.append(json.loads(body))
            except Exception:
                pass

        page.on("response", on_response)
        page.goto(UNITS_APP_URL, wait_until="domcontentloaded", timeout=45000)
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        for _ in range(4):  # trigger any lazy loading
            page.mouse.wheel(0, 2400)
            page.wait_for_timeout(700)
        page.wait_for_timeout(1500)
        html = page.content()
        browser.close()
    return payloads, html


def _walk_dicts(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk_dicts(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_dicts(v)


def _kget(d: dict, pattern: str):
    """First value whose key matches pattern, skipping min/max/avg keys."""
    rx = re.compile(pattern, re.I)
    for k, v in d.items():
        k = str(k)
        if re.match(r"(min|max|avg|total)", k, re.I):
            continue
        if rx.search(k):
            return v
    return None


def _to_int(v):
    try:
        return int(round(float(str(v).replace(",", "").replace("$", ""))))
    except (TypeError, ValueError):
        return None


def _norm_baths(v) -> str:
    if v is None:
        return ""
    try:
        f = float(str(v))
        return str(int(f)) if f.is_integer() else str(f)
    except ValueError:
        return str(v)


def _norm_json_date(v) -> str:
    if v in (None, ""):
        return ""
    if isinstance(v, (int, float)) and v > 1e12:  # epoch millis
        return datetime.fromtimestamp(v / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    s = str(v)
    m = re.match(r"\d{4}-\d{2}-\d{2}", s)
    if m:
        return m.group(0)
    m = re.match(r"\d{1,2}/\d{1,2}/\d{2,4}", s)
    if m:
        return norm_date(m.group(0))
    return s[:10]


def _json_unit(d: dict) -> dict | None:
    """Normalize one JSON object into a unit record, or None. Key names are
    matched fuzzily so minor API schema differences do not break parsing."""
    beds = _to_int(_kget(d, r"bed(room)?s?(count)?$"))
    if beds is None or not 0 <= beds <= 5:
        return None

    # Prefer a per-term pricing matrix, e.g. [{term: 12, rent: 4589}, ...]
    price, term = None, ""
    for k, v in d.items():
        if not re.search(r"term|pricing|rates?$|rents?$|prices?$", str(k), re.I):
            continue
        if isinstance(v, list) and v and isinstance(v[0], dict):
            matrix = {}
            for row in v:
                months = _to_int(_kget(row, r"term|month|length|duration"))
                rent = _to_int(_kget(row, r"rent|price|rate|amount"))
                if months and rent and 300 <= rent <= 30000:
                    matrix[months] = rent
            if matrix:
                term = min(matrix, key=lambda m: (abs(m - TARGET_TERM), m))
                price = matrix[term]
                break
    if price is None:
        cands = [n for k, v in d.items()
                 if re.search(r"rent|price", str(k), re.I)
                 and (n := _to_int(v)) and 300 <= n <= 30000]
        if cands:
            price = min(cands)
    if price is None:
        return None

    unit_no = ""
    for pat in (r"unit.*(number|name|code)", r"^(unit|name|number)$"):
        v = _kget(d, pat)
        if v is not None and re.search(r"\d", str(v)) and len(str(v)) <= 10:
            unit_no = str(v)
            break

    sqft = _to_int(_kget(d, r"sq(uare)?_?f(oo|ee)?t|area$|size$"))
    if sqft is not None and not 100 <= sqft <= 5000:
        sqft = None
    avail = _norm_json_date(_kget(d, r"avail\w*(date)?$|move.?in"))
    # A record with neither a unit number nor (sqft + date) is probably a
    # building/floorplan summary, not a unit. Skip it.
    if not unit_no and not (sqft and avail):
        return None

    floor = _kget(d, r"^floor(number)?$")
    plan = _kget(d, r"floor.?plan(name)?$|^plan(name)?$")
    return {
        "unit_number": unit_no,
        "floorplan": str(plan) if plan not in (None, "") else "",
        "fp_id": "",
        "beds": beds,
        "baths": _norm_baths(_kget(d, r"bath")),
        "sqft": sqft if sqft is not None else "",
        "floor": "" if floor is None else str(floor),
        "price": price,
        "lease_term_months": str(term),
        "available": avail,
        "sig": "",
    }


def _units_from_payloads(payloads: list) -> list[dict]:
    best: list[dict] = []
    for pl in payloads:
        found, seen = [], set()
        for d in _walk_dicts(pl):
            u = _json_unit(d)
            if not u:
                continue
            fp = (u["unit_number"], u["beds"], u["sqft"], u["price"])
            if fp in seen:
                continue
            seen.add(fp)
            found.append(u)
        if len(found) > len(best):
            best = found
    return best


def portal_units() -> list[dict]:
    payloads, html = _capture_portal_payloads()
    units = _units_from_payloads(payloads)
    if not units:
        DEBUG_DIR.mkdir(exist_ok=True)
        (DEBUG_DIR / "portal_page.html").write_text(html or "")
        (DEBUG_DIR / "portal_payloads.json").write_text(
            json.dumps(payloads[:20], indent=2, default=str)[:2_000_000])
        raise RuntimeError(
            f"no units recognized in {len(payloads)} JSON payloads; "
            "snapshots saved to debug/")
    return units


def eqweb_units() -> tuple[list[dict], dict]:
    html = fetch_html()
    units, expected = parse_page(html)
    exp_all = expected.get("All")
    if not units and (exp_all is None or exp_all > 0):
        DEBUG_DIR.mkdir(exist_ok=True)
        (DEBUG_DIR / "last_page.html").write_text(html)
        raise SystemExit(
            "PARSE FAILURE: no unit cards found on equityapartments.com; "
            "page saved to debug/. The site markup may have changed.")
    return units, expected


def acquire_units() -> tuple[list[dict], dict, str, list[str]]:
    """Returns (units, expected_tab_counts, source_name, warnings)."""
    warns: list[str] = []
    test_json = os.environ.get("TEST_JSON")
    if test_json:
        return (_units_from_payloads([json.loads(Path(test_json).read_text())]),
                {}, "portal", warns)
    if os.environ.get("TEST_HTML"):
        return (*parse_page(fetch_html()), "eqweb", warns)
    if SOURCE in ("auto", "eqr"):
        try:
            return portal_units(), {}, "portal", warns
        except SystemExit:
            raise
        except Exception as exc:
            if SOURCE == "eqr":
                raise
            warns.append(f"Application portal scrape failed ({exc}); "
                         "fell back to equityapartments.com.")
    units, expected = eqweb_units()
    return units, expected, "eqweb", warns


def remap_after_source_switch(known: dict, units: list[dict]) -> dict:
    """Carry first_seen / initial_price across a source change by matching
    old records to new keys on progressively looser criteria."""
    new_known: dict[str, dict] = {}
    used: set[str] = set()
    for u in units:
        match = None
        for crit in (("beds", "sqft", "floor", "available"),
                     ("beds", "sqft", "floor", "price"),
                     ("beds", "sqft", "price")):
            cands = [k for k, su in known.items()
                     if k not in used
                     and all(str(su.get(c, "")) == str(u.get(c, ""))
                             for c in crit)]
            if len(cands) == 1:
                match = cands[0]
                break
        if match:
            used.add(match)
            su = dict(known[match])
            su["key"] = u["key"]
            new_known[u["key"]] = su
    for k, su in known.items():
        if k not in used:
            new_known.setdefault(k, su)
    return new_known


# ----------------------------------------------------------------- parsing


def _enclosing_card(node):
    """Climb from a $price text node to the smallest element that looks
    like one complete unit card (exactly one availability date, plus
    price, beds, and square footage)."""
    best = None
    cur = node.parent
    for _ in range(12):
        if cur is None or cur.name in ("body", "html", "[document]"):
            break
        text = " ".join(cur.stripped_strings)
        if len(AVAIL_RE.findall(text)) > 1:
            break
        if (
            AVAIL_RE.search(text)
            and PRICE_RE.search(text)
            and SQFT_RE.search(text)
            and BEDS_RE.search(text)
        ):
            best = cur
        cur = cur.parent
    return best


def _stable_sig(text: str) -> str:
    """Hash of the card text with price, dates, and lease term stripped.
    Survives price changes and availability-date shifts, and captures
    the unit-specific feature chips (exposure, balcony, etc.)."""
    t = PRICE_RE.sub("", text)
    t = AVAIL_RE.sub("", t)
    t = TERM_RE.sub("", t)
    t = re.sub(r"\d{1,2}/\d{1,2}/\d{2,4}", "", t)
    t = re.sub(r"\s+", " ", t).strip().lower()
    return hashlib.md5(t.encode()).hexdigest()[:8]


def _card_to_unit(card) -> dict | None:
    text = " ".join(card.stripped_strings)
    beds_m = BEDS_RE.search(text)
    price_m = PRICE_RE.search(text)
    avail_m = AVAIL_RE.search(text)
    if not (beds_m and price_m and avail_m):
        return None
    beds = int(beds_m.group(1)) if beds_m.group(1) else 0
    sqft_m = SQFT_RE.search(text)
    baths_m = BATHS_RE.search(text)
    floor_m = FLOOR_RE.search(text)
    term_m = TERM_RE.search(text)

    fp_name, fp_id = "", ""
    for im in card.find_all("img"):
        src = im.get("src") or ""
        m = FP_IMG_RE.search(src)
        if m:
            fp_id = m.group(1)
            fp_name = (im.get("alt") or "").strip()
            break
    if not fp_name:
        first_img = card.find("img")
        if first_img is not None:
            fp_name = (first_img.get("alt") or "").strip()

    unit_number = ""
    raw = str(card)
    for pat in UNITID_PATTERNS:
        m = pat.search(raw)
        if m:
            unit_number = m.group(1)
            break
    if not unit_number:
        m = APT_RE.search(text)
        if m:
            unit_number = m.group(1)

    return {
        "unit_number": unit_number,
        "floorplan": fp_name,
        "fp_id": fp_id,
        "beds": beds,
        "baths": baths_m.group(1) if baths_m else "",
        "sqft": int(sqft_m.group(1).replace(",", "")) if sqft_m else "",
        "floor": floor_m.group(1) if floor_m else "",
        "price": int(price_m.group(1).replace(",", "")),
        "lease_term_months": term_m.group(1) if term_m else "",
        "available": norm_date(avail_m.group(1)),
        "sig": _stable_sig(text),
    }


def assign_keys(units: list[dict]):
    """Stable identity per unit. Prefer a real unit number if the markup
    exposes one; otherwise floorplan + sqft + floor + feature-chip hash,
    disambiguated by availability date only when two units collide."""
    groups: dict[str, list[dict]] = {}
    for u in units:
        base = u["unit_number"] or (
            f"fp{u['fp_id'] or u['floorplan'] or 'x'}-{u['sqft']}sf-fl{u['floor']}-{u['sig']}"
        )
        groups.setdefault(base, []).append(u)
    for base, grp in groups.items():
        if len(grp) == 1:
            grp[0]["key"] = base
            continue
        grp.sort(key=lambda x: (str(x["available"]), x["price"]))
        used = set()
        for i, u in enumerate(grp):
            key = f"{base}-{u['available']}"
            if key in used:
                key = f"{key}-{i}"
            used.add(key)
            u["key"] = key


def parse_page(html: str):
    soup = BeautifulSoup(html, "html.parser")
    seen, cards = set(), []
    for node in soup.find_all(string=PRICE_RE):
        card = _enclosing_card(node)
        if card is not None and id(card) not in seen:
            seen.add(id(card))
            cards.append(card)
    units = [u for u in (_card_to_unit(c) for c in cards) if u]

    # Expected counts from the availability filter tabs, e.g. "2 Bed (3)"
    page_text = " ".join(soup.stripped_strings)
    expected: dict[str, int] = {}
    for label, n in TAB_RE.findall(page_text):
        expected.setdefault(re.sub(r"\s+", " ", label), int(n))
    return units, expected


# ----------------------------------------------------------------- logging


AVAIL_COLS = [
    "logged_utc", "event", "unit_key", "unit_number", "floorplan", "beds",
    "baths", "sqft", "floor", "price", "prev_price", "lease_term_months",
    "available_date", "first_seen_utc", "source", "url",
]
OFFLINE_COLS = [
    "delisted_utc", "unit_key", "unit_number", "floorplan", "beds", "baths",
    "sqft", "floor", "last_price", "initial_price", "price_change_while_listed",
    "last_available_date", "first_seen_utc", "last_seen_utc", "days_listed",
    "source", "url",
]


def append_csv(path: Path, cols: list[str], row: dict):
    new = not path.exists()
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        if new:
            w.writeheader()
        w.writerow({c: row.get(c, "") for c in cols})


def log_availability(event: str, u: dict, ts: str, prev_price=""):
    append_csv(AVAIL_LOG, AVAIL_COLS, {
        "logged_utc": ts, "event": event, "unit_key": u["key"],
        "unit_number": u["unit_number"], "floorplan": u["floorplan"],
        "beds": u["beds"], "baths": u["baths"], "sqft": u["sqft"],
        "floor": u["floor"], "price": u["price"], "prev_price": prev_price,
        "lease_term_months": u["lease_term_months"],
        "available_date": u["available"],
        "first_seen_utc": u.get("first_seen_utc", ts),
        "source": u.get("source", ""), "url": PROPERTY_URL,
    })


def log_offline(u: dict, ts: str):
    days = ""
    try:
        delta = parse_iso(u["last_seen_utc"]) - parse_iso(u["first_seen_utc"])
        days = round(delta.total_seconds() / 86400, 1)
    except Exception:
        pass
    append_csv(OFFLINE_LOG, OFFLINE_COLS, {
        "delisted_utc": ts, "unit_key": u["key"],
        "unit_number": u["unit_number"], "floorplan": u["floorplan"],
        "beds": u["beds"], "baths": u["baths"], "sqft": u["sqft"],
        "floor": u["floor"], "last_price": u["price"],
        "initial_price": u.get("initial_price", ""),
        "price_change_while_listed": (
            u["price"] - u["initial_price"] if u.get("initial_price") else ""
        ),
        "last_available_date": u["available"],
        "first_seen_utc": u.get("first_seen_utc", ""),
        "last_seen_utc": u.get("last_seen_utc", ""),
        "days_listed": days, "source": u.get("source", ""), "url": PROPERTY_URL,
    })


# ----------------------------------------------------------------- alerts


def units_table(units: list[dict]) -> str:
    if not units:
        return "_None_\n"
    lines = [
        "| Price | Plan | Sq Ft | Floor | Baths | Available | Unit |",
        "|---|---|---|---|---|---|---|",
    ]
    for u in sorted(units, key=lambda x: x["price"]):
        lines.append(
            f"| {money(u['price'])} | {u['floorplan'] or u['fp_id']} | {u['sqft']} "
            f"| {u['floor']} | {u['baths']} | {u['available']} "
            f"| {u['unit_number'] or ''} |"
        )
    return "\n".join(lines) + "\n"


def create_github_issue(title: str, body: str) -> bool:
    repo = os.environ.get("GITHUB_REPOSITORY")
    token = os.environ.get("GITHUB_TOKEN")
    if not (repo and token):
        print("No GITHUB_TOKEN/GITHUB_REPOSITORY; printing alert instead.\n")
        print(title, "\n", body)
        return False
    api = f"https://api.github.com/repos/{repo}/issues"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    payload = {"title": title, "body": body, "labels": ["apartment-alert"]}
    r = requests.post(api, headers=headers, json=payload, timeout=30)
    if r.status_code == 422:  # label may not exist yet on some setups
        payload.pop("labels")
        r = requests.post(api, headers=headers, json=payload, timeout=30)
    ok = r.status_code == 201
    print(f"GitHub issue: {'created' if ok else 'failed ' + str(r.status_code)}")
    return ok


def build_alert(events: list[dict], current: list[dict]) -> tuple[str, str]:
    labels = {"listed": "new", "price_change": "repriced",
              "delisted": "delisted"}
    beds_desc = "/".join(str(b) for b in sorted(TARGET_BEDS)) + "BR"
    counts: dict[str, int] = {}
    for e in events:
        counts[e["type"]] = counts.get(e["type"], 0) + 1
    parts = [f"{n} {labels[t]} {beds_desc} unit{'s' if n > 1 else ''}"
             for t, n in counts.items()]
    listed = [e["unit"] for e in events if e["type"] == "listed"]
    if len(listed) == 1 and len(events) == 1:
        u = listed[0]
        sf = f"{u['sqft']:,}" if u["sqft"] else "?"
        title = (f"{PROPERTY_NAME}: {beds_desc} listed at {money(u['price'])}, "
                 f"{sf} sf, available {u['available']}")
    else:
        title = f"{PROPERTY_NAME}: " + ", ".join(parts)

    body = [f"## {PROPERTY_NAME} update", ""]
    for e in events:
        u = e["unit"]
        line = (f"- **{labels[e['type']].capitalize()}**: {u['beds']}bd/{u['baths']}ba, "
                f"{u['sqft']} sf, floor {u['floor']}, {money(u['price'])}, "
                f"available {u['available']}")
        if e["type"] == "price_change":
            line += f" (was {money(e['prev_price'])})"
        body.append(line)
    body += ["", f"### All {beds_desc} units currently listed", "",
             units_table(current), "",
             f"[Application portal]({UNITS_APP_URL}) | [Listing page]({PROPERTY_URL})"]
    repo = os.environ.get("GITHUB_REPOSITORY")
    branch = os.environ.get("GITHUB_REF_NAME", "main")
    if repo:
        base = f"https://github.com/{repo}/blob/{branch}/data"
        body.append(f" | [Availability log]({base}/availability_log.csv)"
                    f" | [Offline log]({base}/offline_log.csv)")
    return title, "\n".join(body) + "\n"


# ----------------------------------------------------------------- main


def main() -> int:
    ts = iso(now_utc())
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    all_units, expected, source, warnings = acquire_units()
    assign_keys(all_units)
    target = [u for u in all_units if u["beds"] in TARGET_BEDS]
    for u in target:
        u["source"] = source
    current = {u["key"]: u for u in target}

    # Can we trust that a unit missing from this parse is really gone?
    exp_target = 0
    have_expectation = False
    for b in TARGET_BEDS:
        label = "Studio" if b == 0 else f"{b} Bed"
        if label in expected:
            have_expectation = True
            exp_target += expected[label]
    trust_absence = True
    if have_expectation and len(target) < exp_target:
        trust_absence = False
        warnings.append(
            f"Parsed {len(target)} target units but page reports {exp_target}; "
            "skipping offline detection this run."
        )

    state = (json.loads(STATE_FILE.read_text())
             if STATE_FILE.exists() else {"version": 1, "units": {}})
    known: dict[str, dict] = state["units"]
    prev_source = state.get("source")
    if prev_source and prev_source != source and known:
        known = state["units"] = remap_after_source_switch(known, target)
        warnings.append(f"Data source switched {prev_source} -> {source}; "
                        "unit identities remapped where possible.")
    state["source"] = source
    events: list[dict] = []

    for key, u in current.items():
        if key in known:
            su = known[key]
            su["missing_count"] = 0
            if u["price"] != su["price"]:
                events.append({"type": "price_change", "unit": u,
                               "prev_price": su["price"]})
                u["first_seen_utc"] = su.get("first_seen_utc", ts)
                log_availability("price_change", u, ts, prev_price=su["price"])
            su.update({k: u[k] for k in
                       ("price", "available", "floor", "sqft", "baths",
                        "floorplan", "fp_id", "unit_number",
                        "lease_term_months", "source")})
            su["last_seen_utc"] = ts
        else:
            su = {**u, "first_seen_utc": ts, "last_seen_utc": ts,
                  "initial_price": u["price"], "missing_count": 0}
            known[key] = su
            events.append({"type": "listed", "unit": su})
            log_availability("listed", su, ts)

    if trust_absence:
        for key in list(known):
            if key in current:
                continue
            su = known[key]
            su["missing_count"] = su.get("missing_count", 0) + 1
            if su["missing_count"] >= OFFLINE_CONFIRM_RUNS:
                events.append({"type": "delisted", "unit": su})
                log_offline(su, ts)
                del known[key]

    state["last_run_utc"] = ts
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")

    # Notifications
    notify_events = [e for e in events if e["type"] in NOTIFY_EVENTS]
    if notify_events:
        title, body = build_alert(notify_events, list(current.values()))
        ALERT_FILE.write_text(f"# {title}\n\n{body}")
        create_github_issue(title, body)
        gh_output(notify="true", subject=title)
    else:
        gh_output(notify="false")

    # Run summary (visible on the Actions run page)
    beds_desc = "/".join(str(b) for b in sorted(TARGET_BEDS))
    md = [f"### {PROPERTY_NAME} check at {ts} (source: {source})", ""]
    for w in warnings:
        md.append(f"> Warning: {w}")
    if events:
        md.append(f"Events this run: " + ", ".join(
            f"{e['type']} ({money(e['unit']['price'])})" for e in events))
    else:
        md.append("No changes this run.")
    md += ["", f"Currently listed {beds_desc}-bed units:", "",
           units_table(list(current.values()))]
    step_summary("\n".join(md))

    print(f"OK [{source}]: parsed {len(all_units)} units total, "
          f"{len(target)} target, {len(events)} event(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
Equity Apartments availability monitor.

Primary source is the EliseAI application portal (real unit numbers, unit
detail pages with move-in dates and per-term pricing). Each run also reads
the equityapartments.com marketing page and cross-matches units by beds,
square footage, move-in date, and rent to enrich records with floorplan
name/image and facing (exposure). Falls back to the marketing page alone
if the portal is unreachable.

State and logs are plain files committed back to the repo:

    data/state.json            current snapshot (bookkeeping)
    data/availability_log.csv  one row per listed / price_change event
    data/offline_log.csv       one row per unit that left the market
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

UNITS_APP_URL = os.environ.get(
    "UNITS_APP_URL",
    "https://eqr-applications.com/building/south-city-station-2/units",
)
# auto  = portal primary + marketing-page enrichment, full fallback
# eqr   = portal only (fail loudly if it breaks)
# eqweb = marketing page only (no browser needed)
SOURCE = os.environ.get("SOURCE", "auto").lower()
# Lease term, in months, whose rate gets recorded. If a unit does not offer
# exactly this term, the closest available term is used and logged as such.
TARGET_TERM = int(os.environ.get("TARGET_TERM", "12"))
# Community site map image; auto-discovered from the marketing page when
# blank, with the media gallery as the fallback link.
SITE_MAP_URL = os.environ.get("SITE_MAP_URL", "")
GALLERY_URL = PROPERTY_URL + "#/mediaGallery"

TARGET_BEDS = {
    int(b) for b in os.environ.get("TARGET_BEDS", "2").replace(" ", "").split(",") if b
} or {2}
NOTIFY_EVENTS = {
    e.strip() for e in os.environ.get("NOTIFY_EVENTS", "listed").split(",") if e.strip()
}
OFFLINE_CONFIRM_RUNS = int(os.environ.get("OFFLINE_CONFIRM_RUNS", "2"))
MAX_DETAIL_PAGES = int(os.environ.get("MAX_DETAIL_PAGES", "40"))

DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
DEBUG_DIR = Path("debug")
STATE_FILE = DATA_DIR / "state.json"
AVAIL_LOG = DATA_DIR / "availability_log.csv"
OFFLINE_LOG = DATA_DIR / "offline_log.csv"
ALERT_FILE = Path("alert.md")

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
EXPOSURE_RE = re.compile(r"(\w+)\s+Exposure", re.I)
UNIT_IN_TEXT_RE = re.compile(r"\b(\d{1,3}-\d{3,5})\b")
MOVEIN_TEXT_RE = re.compile(
    r"(?:move[\s-]?in|available)\D{0,30}(\d{1,2}/\d{1,2}/\d{4})", re.I)
SITEMAP_URL_RE = re.compile(
    r"https://media\.equityapartments\.com/[^\"'\\)\s]*"
    r"(?:site.?map|siteplan|community.?map)[^\"'\\)\s]*", re.I)

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


def rent_psf(u: dict) -> str:
    try:
        return f"{u['price'] / u['sqft']:.2f}"
    except (TypeError, ValueError, ZeroDivisionError, KeyError):
        return ""


def norm_date(raw: str) -> str:
    raw = raw.strip()
    if raw.lower() == "now":
        return "now"
    m, d, y = raw.split("/")
    y = int(y)
    if y < 100:
        y += 2000
    return f"{y:04d}-{int(m):02d}-{int(d):02d}"


def try_parse_date(v) -> str:
    """Return YYYY-MM-DD (or 'now') if v is recognizably a date, else ''."""
    if v in (None, "", True, False):
        return ""
    if isinstance(v, (int, float)):
        try:
            if v > 1e12:
                return datetime.fromtimestamp(v / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
            if v > 1e9:
                return datetime.fromtimestamp(v, tz=timezone.utc).strftime("%Y-%m-%d")
        except (OverflowError, OSError, ValueError):
            return ""
        return ""
    s = str(v).strip()
    if s.lower() in ("now", "today", "immediately"):
        return "now"
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return m.group(0)
    m = re.match(r"\d{1,2}/\d{1,2}/\d{2,4}$", s)
    if m:
        return norm_date(s)
    return ""


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


# ------------------------------------------------- source: marketing page


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


def _facing_from_text(text: str) -> str:
    dirs = []
    for w in EXPOSURE_RE.findall(text):
        w = w.capitalize()
        if w.lower().endswith("ern"):
            w = w[:-3]
        if w not in dirs:
            dirs.append(w)
    return "/".join(dirs)


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

    fp_name, fp_id, fp_image = "", "", ""
    for im in card.find_all("img"):
        src = im.get("src") or ""
        m = FP_IMG_RE.search(src)
        if m:
            fp_id = m.group(1)
            fp_name = (im.get("alt") or "").strip()
            fp_image = src
            break
    if not fp_name:
        first_img = card.find("img")
        if first_img is not None:
            fp_name = (first_img.get("alt") or "").strip()
            fp_image = first_img.get("src") or ""

    return {
        "unit_number": "",
        "building": "",
        "floorplan": fp_name,
        "fp_id": fp_id,
        "fp_image": fp_image,
        "beds": beds,
        "baths": baths_m.group(1) if baths_m else "",
        "sqft": int(sqft_m.group(1).replace(",", "")) if sqft_m else "",
        "floor": floor_m.group(1) if floor_m else "",
        "facing": _facing_from_text(text),
        "price": int(price_m.group(1).replace(",", "")),
        "lease_term_months": term_m.group(1) if term_m else "",
        "status": "",
        "available": norm_date(avail_m.group(1)),
        "sig": _stable_sig(text),
    }


def parse_page(html: str):
    soup = BeautifulSoup(html, "html.parser")
    seen, cards = set(), []
    for node in soup.find_all(string=PRICE_RE):
        card = _enclosing_card(node)
        if card is not None and id(card) not in seen:
            seen.add(id(card))
            cards.append(card)
    units = [u for u in (_card_to_unit(c) for c in cards) if u]

    page_text = " ".join(soup.stripped_strings)
    expected: dict[str, int] = {}
    for label, n in TAB_RE.findall(page_text):
        expected.setdefault(re.sub(r"\s+", " ", label), int(n))
    return units, expected


def discover_site_map(html: str) -> str:
    m = SITEMAP_URL_RE.search(html)
    return m.group(0) if m else ""


def eqweb_units() -> tuple[list[dict], dict, str]:
    html = fetch_html()
    units, expected = parse_page(html)
    exp_all = expected.get("All")
    if not units and (exp_all is None or exp_all > 0):
        DEBUG_DIR.mkdir(exist_ok=True)
        (DEBUG_DIR / "last_page.html").write_text(html)
        raise SystemExit(
            "PARSE FAILURE: no unit cards found on equityapartments.com; "
            "page saved to debug/. The site markup may have changed.")
    return units, expected, discover_site_map(html)


# ------------------------------------------- source: application portal


def _capture_portal() -> tuple[list, list[str], str]:
    """Render the units list, then every unit detail page, capturing all
    JSON responses plus the rendered text of each detail page (for the
    move-in-date fallback). Returns (payloads, detail_texts, list_html)."""
    from playwright.sync_api import sync_playwright  # lazy import

    payloads: list = []
    detail_texts: list[str] = []
    list_html = ""
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
        for _ in range(4):
            page.mouse.wheel(0, 2400)
            page.wait_for_timeout(600)
        page.wait_for_timeout(1200)
        list_html = page.content()

        hrefs = page.eval_on_selector_all(
            "a", "els => els.map(e => e.href)")
        base = UNITS_APP_URL.rstrip("/")
        detail_urls = []
        for h in hrefs or []:
            if not h:
                continue
            h = h.split("#")[0]
            if h.startswith(base + "/") and h != base and h not in detail_urls:
                detail_urls.append(h)
        for url in detail_urls[:MAX_DETAIL_PAGES]:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                try:
                    page.wait_for_load_state("networkidle", timeout=12000)
                except Exception:
                    pass
                page.wait_for_timeout(600)
                detail_texts.append(page.inner_text("body"))
            except Exception:
                continue
        browser.close()
    return payloads, detail_texts, list_html


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


def _term_matrix(d: dict) -> dict[int, int]:
    """Find a per-term pricing matrix anywhere one level down, e.g.
    leaseTerms: [{term: 12, rent: 4589}, ...] or pricing: {terms: [...]}."""
    def rows_to_matrix(rows) -> dict[int, int]:
        matrix: dict[int, int] = {}
        if not (isinstance(rows, list) and rows and isinstance(rows[0], dict)):
            return matrix
        for row in rows:
            months = _to_int(_kget(row, r"term|month|length|duration"))
            rent = _to_int(_kget(row, r"rent|price|rate|amount"))
            if months and rent and 300 <= rent <= 30000 and 1 <= months <= 36:
                matrix[months] = rent
        return matrix

    for k, v in d.items():
        if not re.search(r"term|pricing|rates?$|rents?$|prices?$", str(k), re.I):
            continue
        m = rows_to_matrix(v)
        if m:
            return m
        if isinstance(v, dict):
            for vv in v.values():
                m = rows_to_matrix(vv)
                if m:
                    return m
    return {}


def _avail_and_status(d: dict) -> tuple[str, str]:
    """Move-in date and status from a unit record. Date-bearing keys are
    preferred over status-bearing ones; nested dicts one level down are
    searched too. Never returns truncated junk as a date."""
    date_first: list = []
    avail_keys: list = []
    for k, v in d.items():
        ks = str(k)
        if isinstance(v, dict):
            for kk, vv in v.items():
                if re.search(r"date|move|avail", str(kk) + ks, re.I):
                    date_first.append(vv)
            continue
        if re.search(r"date|move.?in", ks, re.I):
            date_first.append(v)
        elif re.search(r"avail", ks, re.I):
            avail_keys.append(v)

    available = ""
    for v in date_first + avail_keys:
        available = try_parse_date(v)
        if available:
            break

    status = ""
    for k, v in d.items():
        if isinstance(v, str) and 0 < len(v) < 40 and not try_parse_date(v) \
                and re.search(r"status|avail", str(k), re.I) \
                and not re.search(r"^\d", v):
            status = v.strip()
            break
    if not available and status and re.search(r"now|immediate", status, re.I):
        available = "now"
    return available, status


def _json_unit(d: dict) -> dict | None:
    """Normalize one JSON object into a unit record, or None. Key names are
    matched fuzzily so minor API schema differences do not break parsing."""
    beds = _to_int(_kget(d, r"bed(room)?s?(count)?$"))
    if beds is None or not 0 <= beds <= 5:
        return None

    matrix = _term_matrix(d)
    price, term = None, ""
    if matrix:
        term = min(matrix, key=lambda m: (abs(m - TARGET_TERM), m))
        price = matrix[term]
    else:
        cands = [n for k, v in d.items()
                 if re.search(r"rent|price", str(k), re.I)
                 and not isinstance(v, (list, dict))
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
    available, status = _avail_and_status(d)
    # Records with neither a unit number nor (sqft + some availability
    # signal) are probably building/floorplan summaries, not units.
    if not unit_no and not (sqft and (available or status)):
        return None

    floor = _kget(d, r"^floor(number)?$")
    plan = _kget(d, r"floor.?plan(name)?$|^plan(name)?$")
    facing = _kget(d, r"facing|orientation|exposure")
    return {
        "unit_number": unit_no,
        "building": "",
        "floorplan": str(plan) if plan not in (None, "") else "",
        "fp_id": "",
        "fp_image": "",
        "beds": beds,
        "baths": _norm_baths(_kget(d, r"bath")),
        "sqft": sqft if sqft is not None else "",
        "floor": "" if floor is None else str(floor),
        "facing": "" if facing in (None, "") else str(facing),
        "price": price,
        "lease_term_months": str(term),
        "status": status,
        "available": available,
        "sig": "",
    }


def _merge_unit(base: dict, new: dict) -> dict:
    """Field-wise merge of two records for the same unit. Detail-page data
    (which carries a term matrix) wins on price."""
    out = dict(base)
    for k, v in new.items():
        if out.get(k) in ("", None) and v not in ("", None):
            out[k] = v
    if out.get("available") in ("", "now") and \
            new.get("available") not in ("", None, "now"):
        out["available"] = new["available"]
    base_has_term = bool(base.get("lease_term_months"))
    new_has_term = bool(new.get("lease_term_months"))
    if new_has_term and (not base_has_term or
                         str(new["lease_term_months"]) == str(TARGET_TERM)):
        out["price"] = new["price"]
        out["lease_term_months"] = new["lease_term_months"]
    return out


def _units_from_payloads(payloads: list) -> list[dict]:
    merged: dict[str, dict] = {}
    anon: list[dict] = []
    for pl in payloads:
        for d in _walk_dicts(pl):
            u = _json_unit(d)
            if not u:
                continue
            if u["unit_number"]:
                cur = merged.get(u["unit_number"])
                merged[u["unit_number"]] = _merge_unit(cur, u) if cur else u
            else:
                anon.append(u)
    units = list(merged.values())
    for a in anon:
        if not any(m["beds"] == a["beds"] and m["sqft"] == a["sqft"]
                   and abs(m["price"] - a["price"]) <= 5 for m in units):
            units.append(a)
    return units


def _dates_from_detail_texts(units: list[dict], texts: list[str]):
    """Fallback: pull 'Move-in 10/6/2026'-style dates from rendered detail
    pages and attach them to the unit whose number appears on that page."""
    for t in texts:
        nums = set(UNIT_IN_TEXT_RE.findall(t))
        m = MOVEIN_TEXT_RE.search(t)
        if not m:
            continue
        date = norm_date(m.group(1))
        cands = [u for u in units if u["unit_number"] in nums]
        if len(cands) == 1 and not cands[0]["available"]:
            cands[0]["available"] = date


def portal_units() -> list[dict]:
    payloads, detail_texts, list_html = _capture_portal()
    units = _units_from_payloads(payloads)
    if not units:
        DEBUG_DIR.mkdir(exist_ok=True)
        (DEBUG_DIR / "portal_page.html").write_text(list_html or "")
        (DEBUG_DIR / "portal_payloads.json").write_text(
            json.dumps(payloads[:20], indent=2, default=str)[:2_000_000])
        raise RuntimeError(
            f"no units recognized in {len(payloads)} JSON payloads; "
            "snapshots saved to debug/")
    _dates_from_detail_texts(units, detail_texts)
    return units


# -------------------------------------------------- cross-source matching


def enrich_from_eqweb(portal: list[dict], marketing: list[dict]):
    """Match portal units to marketing-page cards and copy over floorplan
    name/image and facing. Match on beds + sqft, scored by move-in date,
    rent, and floor agreement."""
    taken: set[int] = set()
    for u in portal:
        best, best_score = None, 0
        for i, w in enumerate(marketing):
            if i in taken or w["beds"] != u["beds"] or w["sqft"] != u["sqft"]:
                continue
            score = 1
            if u["available"] and w["available"] == u["available"]:
                score += 3
            if w["price"] == u["price"]:
                score += 2
            if u["floor"] and w["floor"] == u["floor"]:
                score += 1
            if score > best_score:
                best, best_score = i, score
        if best is not None and best_score >= 3:
            taken.add(best)
            w = marketing[best]
            for f in ("floorplan", "fp_id", "fp_image", "facing"):
                if not u.get(f):
                    u[f] = w[f]
            if not u["available"]:
                u["available"] = w["available"]
            if not u["floor"]:
                u["floor"] = w["floor"]


def derive_building_floor(u: dict):
    """EQR unit numbers look like 01-2049: building 01, floor = first digit
    of the unit part. Used to fill blanks; never overrides source data."""
    m = re.match(r"^([A-Za-z0-9]{1,3})-(\d{3,5})$", u.get("unit_number") or "")
    if not m:
        return
    if not u.get("building"):
        u["building"] = m.group(1)
    if not u.get("floor"):
        u["floor"] = m.group(2)[0]


# ----------------------------------------------------------- keys / diff


def assign_keys(units: list[dict]):
    """Stable identity per unit. Prefer a real unit number; otherwise
    floorplan + sqft + floor + feature-chip hash, disambiguated by
    availability date only when two units collide."""
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


# ------------------------------------------------------------ acquisition


def acquire_units() -> tuple[list[dict], dict, str, str, list[str]]:
    """Returns (units, expected_tab_counts, source, site_map_url, warnings)."""
    warns: list[str] = []
    site_map = SITE_MAP_URL
    test_json = os.environ.get("TEST_JSON")
    test_html = os.environ.get("TEST_HTML")

    if test_json:
        raw = json.loads(Path(test_json).read_text())
        payloads = raw["__payloads__"] if isinstance(raw, dict) \
            and "__payloads__" in raw else [raw]
        units = _units_from_payloads(payloads)
        if test_html:
            marketing, _, found = eqweb_units()
            enrich_from_eqweb(units, marketing)
            site_map = site_map or found
        return units, {}, "portal", site_map, warns

    if test_html:
        units, expected, found = eqweb_units()
        return units, expected, "eqweb", site_map or found, warns

    if SOURCE in ("auto", "eqr"):
        try:
            units = portal_units()
            if SOURCE == "auto":
                try:
                    marketing, _, found = eqweb_units()
                    enrich_from_eqweb(units, marketing)
                    site_map = site_map or found
                except Exception as exc:
                    warns.append(f"Marketing-page enrichment failed ({exc}); "
                                 "portal data only this run.")
            return units, {}, "portal", site_map, warns
        except SystemExit:
            raise
        except Exception as exc:
            if SOURCE == "eqr":
                raise
            warns.append(f"Application portal scrape failed ({exc}); "
                         "fell back to equityapartments.com.")

    units, expected, found = eqweb_units()
    return units, expected, "eqweb", site_map or found, warns


# ----------------------------------------------------------------- logging


AVAIL_COLS = [
    "logged_utc", "event", "unit_key", "unit_number", "building", "floorplan",
    "beds", "baths", "sqft", "floor", "facing", "price", "prev_price",
    "rent_psf", "lease_term_months", "status", "available_date",
    "first_seen_utc", "source", "url",
]
OFFLINE_COLS = [
    "delisted_utc", "unit_key", "unit_number", "building", "floorplan",
    "beds", "baths", "sqft", "floor", "facing", "last_price", "initial_price",
    "price_change_while_listed", "last_rent_psf", "last_available_date",
    "first_seen_utc", "last_seen_utc", "days_listed", "source", "url",
]


def _ensure_csv_schema(path: Path, cols: list[str]):
    """Upgrade an existing CSV in place when columns are added: old rows are
    kept, new columns filled blank."""
    if not path.exists():
        return
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames == cols:
            return
        rows = list(reader)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})


def append_csv(path: Path, cols: list[str], row: dict):
    _ensure_csv_schema(path, cols)
    new = not path.exists()
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        if new:
            w.writeheader()
        w.writerow({c: row.get(c, "") for c in cols})


def log_availability(event: str, u: dict, ts: str, prev_price=""):
    append_csv(AVAIL_LOG, AVAIL_COLS, {
        "logged_utc": ts, "event": event, "unit_key": u["key"],
        "unit_number": u["unit_number"], "building": u.get("building", ""),
        "floorplan": u["floorplan"], "beds": u["beds"], "baths": u["baths"],
        "sqft": u["sqft"], "floor": u["floor"], "facing": u.get("facing", ""),
        "price": u["price"], "prev_price": prev_price,
        "rent_psf": rent_psf(u),
        "lease_term_months": u["lease_term_months"],
        "status": u.get("status", ""), "available_date": u["available"],
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
        "unit_number": u["unit_number"], "building": u.get("building", ""),
        "floorplan": u["floorplan"], "beds": u["beds"], "baths": u["baths"],
        "sqft": u["sqft"], "floor": u["floor"], "facing": u.get("facing", ""),
        "last_price": u["price"],
        "initial_price": u.get("initial_price", ""),
        "price_change_while_listed": (
            u["price"] - u["initial_price"] if u.get("initial_price") else ""
        ),
        "last_rent_psf": rent_psf(u),
        "last_available_date": u["available"],
        "first_seen_utc": u.get("first_seen_utc", ""),
        "last_seen_utc": u.get("last_seen_utc", ""),
        "days_listed": days, "source": u.get("source", ""),
        "url": PROPERTY_URL,
    })


# ----------------------------------------------------------------- alerts


def _avail_disp(u: dict) -> str:
    return u["available"] or (u.get("status") or "?")


def units_table(units: list[dict]) -> str:
    if not units:
        return "_None_\n"
    lines = [
        "| Unit | Bldg | Floor | Faces | Price | $/SF | Bd/Ba | Sq Ft | Move-in | Plan |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for u in sorted(units, key=lambda x: x["price"]):
        psf = rent_psf(u)
        plan = u["floorplan"] or u["fp_id"] or ""
        if u.get("fp_image"):
            plan = f"[{plan or 'floorplan'}]({u['fp_image']})"
        lines.append(
            f"| {u['unit_number'] or u.get('key', '')} | {u.get('building', '')} "
            f"| {u['floor']} | {u.get('facing', '')} | {money(u['price'])} "
            f"| {'$' + psf if psf else ''} | {u['beds']}/{u['baths']} "
            f"| {u['sqft']:,} | {_avail_disp(u)} | {plan} |"
            if isinstance(u['sqft'], int) else
            f"| {u['unit_number'] or u.get('key', '')} | {u.get('building', '')} "
            f"| {u['floor']} | {u.get('facing', '')} | {money(u['price'])} "
            f"| {'$' + psf if psf else ''} | {u['beds']}/{u['baths']} "
            f"| {u['sqft']} | {_avail_disp(u)} | {plan} |"
        )
    return "\n".join(lines) + "\n"


def _unit_line(u: dict) -> str:
    bits = [u["unit_number"] or u.get("key", "?")]
    if u.get("building"):
        bits.append(f"Bldg {u['building']}")
    if u["floor"]:
        bits.append(f"Fl {u['floor']}")
    if u.get("facing"):
        bits.append(f"faces {u['facing']}")
    sq = f"{u['sqft']:,}" if isinstance(u["sqft"], int) else str(u["sqft"])
    bits.append(f"{u['beds']}bd/{u['baths']}ba {sq} sf")
    psf = rent_psf(u)
    bits.append(money(u["price"]) + (f" (${psf}/sf/mo)" if psf else ""))
    bits.append(f"move-in {_avail_disp(u)}")
    line = " · ".join(bits)
    if u.get("fp_image"):
        line += f" · [floorplan]({u['fp_image']})"
    return line


def map_footer(site_map: str) -> str:
    if site_map:
        return (f"**Community site map** (locate by building/floor/facing):\n\n"
                f"![Site map]({site_map})\n\n[Full gallery]({GALLERY_URL})")
    return f"[Community map and gallery]({GALLERY_URL})"


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
    if r.status_code == 422:
        payload.pop("labels")
        r = requests.post(api, headers=headers, json=payload, timeout=30)
    ok = r.status_code == 201
    print(f"GitHub issue: {'created' if ok else 'failed ' + str(r.status_code)}")
    return ok


def build_alert(events: list[dict], current: list[dict],
                site_map: str) -> tuple[str, str]:
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
        sf = f"{u['sqft']:,}" if isinstance(u["sqft"], int) else "?"
        title = (f"{PROPERTY_NAME}: {u['unit_number'] or beds_desc} listed at "
                 f"{money(u['price'])}, {sf} sf, move-in {_avail_disp(u)}")
    else:
        title = f"{PROPERTY_NAME}: " + ", ".join(parts)

    body = [f"## {PROPERTY_NAME} update", ""]
    for e in events:
        u = e["unit"]
        line = f"- **{labels[e['type']].capitalize()}**: " + _unit_line(u)
        if e["type"] == "price_change":
            line += f" (was {money(e['prev_price'])})"
        body.append(line)
    body += ["", f"### All {beds_desc} units currently listed", "",
             units_table(current), "", map_footer(site_map), "",
             f"[Application portal]({UNITS_APP_URL}) | "
             f"[Listing page]({PROPERTY_URL})"]
    repo = os.environ.get("GITHUB_REPOSITORY")
    branch = os.environ.get("GITHUB_REF_NAME", "main")
    if repo:
        base = f"https://github.com/{repo}/blob/{branch}/data"
        body.append(f" | [Availability log]({base}/availability_log.csv)"
                    f" | [Offline log]({base}/offline_log.csv)")
    return title, "\n".join(body) + "\n"


# ------------------------------------------------------------------- main


def main() -> int:
    ts = iso(now_utc())
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    all_units, expected, source, site_map, warnings = acquire_units()
    for u in all_units:
        derive_building_floor(u)
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
    if site_map:
        state["site_map_url"] = site_map
    else:
        site_map = state.get("site_map_url", "")
    events: list[dict] = []

    carry = ("price", "available", "floor", "sqft", "baths", "floorplan",
             "fp_id", "fp_image", "unit_number", "building", "facing",
             "status", "lease_term_months", "source")
    for key, u in current.items():
        if key in known:
            su = known[key]
            su["missing_count"] = 0
            if u["price"] != su["price"]:
                events.append({"type": "price_change", "unit": u,
                               "prev_price": su["price"]})
                u["first_seen_utc"] = su.get("first_seen_utc", ts)
                log_availability("price_change", u, ts, prev_price=su["price"])
            su.update({k: u.get(k, "") for k in carry})
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

    notify_events = [e for e in events if e["type"] in NOTIFY_EVENTS]
    if notify_events:
        title, body = build_alert(notify_events, list(current.values()),
                                  site_map)
        ALERT_FILE.write_text(f"# {title}\n\n{body}")
        create_github_issue(title, body)
        gh_output(notify="true", subject=title)
    else:
        gh_output(notify="false")

    beds_desc = "/".join(str(b) for b in sorted(TARGET_BEDS))
    md = [f"### {PROPERTY_NAME} check at {ts} (source: {source})", ""]
    for w in warnings:
        md.append(f"> Warning: {w}")
    if events:
        md.append("Events this run: " + ", ".join(
            f"{e['type']} ({e['unit'].get('unit_number') or ''} "
            f"{money(e['unit']['price'])})" for e in events))
    else:
        md.append("No changes this run.")
    md += ["", f"Currently listed {beds_desc}-bed units:", "",
           units_table(list(current.values())), "", map_footer(site_map)]
    step_summary("\n".join(md))

    print(f"OK [{source}]: parsed {len(all_units)} units total, "
          f"{len(target)} target, {len(events)} event(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

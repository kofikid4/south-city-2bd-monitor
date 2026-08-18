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
from html import escape as hesc
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
# The portal can split one property into several building slugs; list them
# all comma-separated in UNITS_APP_URL and each is scraped every run.
PORTAL_URLS = [u.strip() for u in UNITS_APP_URL.split(",") if u.strip()]
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
ALERT_HTML = Path("alert.html")  # bordered-table version for direct email

EVENT_LABELS = {"listed": "New", "price_change": "Repriced",
                "delisted": "Delisted"}

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


def _img_url(im) -> str:
    """Real image URL from an <img>, tolerating lazy-load attributes."""
    for attr in ("src", "data-src", "data-original", "data-lazy",
                 "data-lazy-src"):
        v = im.get(attr) or ""
        if v and not v.startswith("data:"):
            return v
    ss = im.get("srcset") or im.get("data-srcset") or ""
    if ss:
        return ss.split(",")[0].strip().split(" ")[0]
    return ""


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
        u = _img_url(im)
        m = FP_IMG_RE.search(u)
        if m:
            fp_id = m.group(1)
            fp_name = (im.get("alt") or "").strip()
            fp_image = u
            break
    if not fp_name:
        first_img = card.find("img")
        if first_img is not None:
            fp_name = (first_img.get("alt") or "").strip()
            fp_image = _img_url(first_img)
    if fp_image.startswith("//"):
        fp_image = "https:" + fp_image

    return {
        "unit_number": "",
        "building": "",
        "wing": "",
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


def fetch_html_browser() -> str:
    """Marketing page via headless Chromium, for when requests gets 403'd."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = _launch_browser(p)
        page = browser.new_context(
            user_agent=HEADERS["User-Agent"], locale="en-US").new_page()
        page.goto(PROPERTY_URL, wait_until="domcontentloaded", timeout=45000)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        page.wait_for_timeout(2500)
        html = page.content()
        browser.close()
    return html


def eqweb_units() -> tuple[list[dict], dict, str]:
    try:
        html = fetch_html()
    except RuntimeError as exc:
        try:
            html = fetch_html_browser()
        except Exception:
            raise exc
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


def _launch_browser(p):
    """Launch Chrome/Chromium. BROWSER_CHANNEL=chrome (default) uses the
    system Chrome preinstalled on GitHub runners, skipping the multi-minute
    playwright browser download. Falls back to the bundled Chromium for
    local machines without Chrome. BROWSER_HEADED=1 runs headed (needs
    xvfb on a runner), which defeats stricter bot checks."""
    headless = os.environ.get("BROWSER_HEADED") != "1"
    args = ["--disable-blink-features=AutomationControlled"]
    channel = os.environ.get("BROWSER_CHANNEL", "chrome").strip()
    if channel:
        try:
            return p.chromium.launch(channel=channel, headless=headless,
                                     args=args)
        except Exception as exc:
            print(f"Channel '{channel}' unavailable ({exc}); "
                  "using bundled Chromium.")
    return p.chromium.launch(headless=headless, args=args)


def _capture_portal() -> tuple[list, list[str], str, str]:
    """Render the units list, then every unit detail page, capturing all
    JSON responses plus the rendered text of each detail page. Finally
    load the marketing page in the same browser (a real Chromium session
    passes bot checks that plain requests fails). Returns
    (payloads, detail_texts, list_html, marketing_html)."""
    from playwright.sync_api import sync_playwright  # lazy import

    payloads: list = []
    detail_texts: list[str] = []
    list_html = ""
    marketing_html = ""
    with sync_playwright() as p:
        browser = _launch_browser(p)
        ctx = browser.new_context(
            user_agent=HEADERS["User-Agent"],
            viewport={"width": 1280, "height": 2000},
            locale="en-US",
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
        for purl in PORTAL_URLS:
            page.goto(purl, wait_until="domcontentloaded", timeout=45000)
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
            base = purl.rstrip("/")
            detail_urls = []
            for h in hrefs or []:
                if not h:
                    continue
                h = h.split("#")[0]
                if h.startswith(base + "/") and h != base                         and h not in detail_urls:
                    detail_urls.append(h)
            for url in detail_urls[:MAX_DETAIL_PAGES]:
                try:
                    page.goto(url, wait_until="domcontentloaded",
                              timeout=30000)
                    try:
                        page.wait_for_load_state("networkidle", timeout=12000)
                    except Exception:
                        pass
                    page.wait_for_timeout(600)
                    detail_texts.append(page.inner_text("body"))
                except Exception:
                    continue

        try:
            mpage = ctx.new_page()
            mpage.goto(PROPERTY_URL, wait_until="domcontentloaded",
                       timeout=45000)
            try:
                mpage.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            mpage.wait_for_timeout(2500)  # let any bot-check sensor settle
            marketing_html = mpage.content()
        except Exception:
            marketing_html = ""
        browser.close()
    return payloads, detail_texts, list_html, marketing_html


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
        "wing": "",
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


TERM_PRICE_TEXT_RE = re.compile(
    r"(\d{1,2})\s*(?:months?|mo\.?)\b\D{0,15}\$([\d,]{3,})", re.I)


def _enrich_from_detail_texts(units: list[dict], texts: list[str]):
    """Fallback extraction from rendered detail pages: move-in dates and
    per-term pricing shown in the UI, attached to the unit whose number
    appears on that page."""
    for t in texts:
        nums = set(UNIT_IN_TEXT_RE.findall(t))
        cands = [u for u in units if u["unit_number"] in nums]
        if len(cands) != 1:
            continue
        u = cands[0]
        m = MOVEIN_TEXT_RE.search(t)
        if m and not u["available"]:
            u["available"] = norm_date(m.group(1))
        if not u["lease_term_months"]:
            matrix = {}
            for mo, pr in TERM_PRICE_TEXT_RE.findall(t):
                mo, pr = int(mo), int(pr.replace(",", ""))
                if 1 <= mo <= 24 and 300 <= pr <= 30000:
                    matrix[mo] = pr
            plausible = len(matrix) >= 2 or any(
                abs(pr - u["price"]) <= 0.25 * u["price"]
                for pr in matrix.values())
            if matrix and plausible:
                term = min(matrix, key=lambda m_: (abs(m_ - TARGET_TERM), m_))
                u["lease_term_months"] = str(term)
                u["price"] = matrix[term]


def portal_units() -> tuple[list[dict], str]:
    payloads, detail_texts, list_html, marketing_html = _capture_portal()
    units = _units_from_payloads(payloads)
    if os.environ.get("DEBUG_PAYLOADS") or not units:
        DEBUG_DIR.mkdir(exist_ok=True)
        (DEBUG_DIR / "portal_payloads.json").write_text(
            json.dumps(payloads[:40], indent=2, default=str)[:4_000_000])
        (DEBUG_DIR / "portal_detail_texts.txt").write_text(
            "\n\n===== PAGE =====\n\n".join(detail_texts)[:2_000_000])
    if not units:
        DEBUG_DIR.mkdir(exist_ok=True)
        (DEBUG_DIR / "portal_page.html").write_text(list_html or "")
        raise RuntimeError(
            f"no units recognized in {len(payloads)} JSON payloads; "
            "snapshots saved to debug/")
    _enrich_from_detail_texts(units, detail_texts)
    return units, marketing_html


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


# Read off the South City Station community site plan. Unit numbers are
# floor-first (2049 = floor 2, stack 49); the stack number determines the
# physical building and wing. The 01- prefix is an EQR property code, not
# a building. Stacks 14-67 are the west building (Costco Entry Dr /
# McLellan Dr / El Camino Real block, leasing center), 68-121 the east
# building (BART Station Access Rd block), 1-13 the standalone garages.
STACK_WINGS = [
    (1, 13, "Garages", "standalone garage row north of the west building"),
    (14, 19, "West", "north side, by the leasing center and garage row"),
    (20, 25, "West", "inner east wing, garage-adjacent"),
    (26, 30, "West", "inner south wing, garage-adjacent"),
    (31, 31, "West", "northwest corner on Costco Entry Dr"),
    (32, 35, "West", "northeast corner on McLellan Dr, by fitness"),
    (36, 41, "West", "east edge along McLellan Dr"),
    (42, 47, "West", "southeast edge along El Camino Real"),
    (48, 56, "West", "south courtyard cluster by the spa"),
    (57, 67, "West", "southwest edge along Costco Entry Dr"),
    (68, 74, "East", "west edge along McLellan Dr"),
    (75, 83, "East", "north edge along BART Station Access Rd"),
    (84, 88, "East", "inner east wing, garage-adjacent"),
    (89, 93, "East", "inner south wing, by mail and fitness"),
    (94, 99, "East", "northeast outer corner"),
    (100, 107, "East", "east outer edge"),
    (108, 110, "East", "southeast corner"),
    (111, 121, "East", "south courtyard cluster by the spa"),
]


def derive_location(u: dict):
    """Fill building, wing, and floor from the unit number using the site
    plan mapping. Building/wing are deterministic derived data and always
    recomputed; floor only fills a blank (portal floor wins)."""
    m = re.match(r"^(?:[A-Za-z0-9]{1,3}-)?([1-4])(\d{2,3})$",
                 str(u.get("unit_number") or ""))
    if not m:
        return
    floor, stack = m.group(1), int(m.group(2))
    for lo, hi, bldg, wing in STACK_WINGS:
        if lo <= stack <= hi:
            u["building"] = bldg
            u["wing"] = wing
            break
    if not u.get("floor"):
        u["floor"] = floor


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


def acquire_units() -> tuple[list[dict], dict, str, str, list[str], int | None]:
    """Returns (units, expected_tab_counts, source, site_map_url, warnings,
    marketing_target_count). The last item drives the coverage guard: when
    the marketing page shows more target-bed units than the portal did, the
    portal URL(s) may cover only part of the property."""
    warns: list[str] = []
    site_map = SITE_MAP_URL
    mkt_target: int | None = None
    test_json = os.environ.get("TEST_JSON")
    test_html = os.environ.get("TEST_HTML")

    if test_json:
        raw = json.loads(Path(test_json).read_text())
        payloads = raw["__payloads__"] if isinstance(raw, dict) \
            and "__payloads__" in raw else [raw]
        units = _units_from_payloads(payloads)
        test_texts = os.environ.get("TEST_DETAIL_TEXTS")
        if test_texts:
            _enrich_from_detail_texts(
                units, Path(test_texts).read_text().split("\n=====\n"))
        if test_html:
            html = Path(test_html).read_text()
            marketing, _ = parse_page(html)
            mkt_target = sum(1 for m in marketing if m["beds"] in TARGET_BEDS)
            enrich_from_eqweb(units, marketing)
            site_map = site_map or discover_site_map(html)
        return units, {}, "portal", site_map, warns, mkt_target

    if test_html:
        units, expected, found = eqweb_units()
        return units, expected, "eqweb", site_map or found, warns, None

    if SOURCE in ("auto", "eqr"):
        try:
            units, marketing_html = portal_units()
        except SystemExit:
            raise
        except Exception as exc:
            if SOURCE == "eqr":
                raise
            warns.append(f"Application portal scrape failed ({exc}); "
                         "fell back to equityapartments.com.")
        else:
            if SOURCE == "auto":
                marketing, _ = parse_page(marketing_html or "")
                if marketing:
                    mkt_target = sum(
                        1 for m in marketing if m["beds"] in TARGET_BEDS)
                    enrich_from_eqweb(units, marketing)
                    site_map = site_map or discover_site_map(marketing_html)
                else:
                    warns.append(
                        "Marketing page yielded no unit cards in-browser "
                        "(likely a bot check); enrichment skipped this run.")
            return units, {}, "portal", site_map, warns, mkt_target

    units, expected, found = eqweb_units()
    return units, expected, "eqweb", site_map or found, warns, None


# ----------------------------------------------------------------- logging


AVAIL_COLS = [
    "logged_utc", "event", "unit_key", "unit_number", "building", "wing", "floorplan",
    "beds", "baths", "sqft", "floor", "facing", "price", "prev_price",
    "rent_psf", "lease_term_months", "status", "available_date",
    "first_seen_utc", "source", "url",
]
OFFLINE_COLS = [
    "delisted_utc", "unit_key", "unit_number", "building", "wing", "floorplan",
    "beds", "baths", "sqft", "floor", "facing", "last_price", "initial_price",
    "price_change_while_listed", "last_rent_psf", "last_available_date",
    "first_seen_utc", "last_seen_utc", "days_listed", "source", "url",
]


def _repair_row(r: dict) -> dict:
    """One-time cleanup applied during schema migration: junk status strings
    that landed in available_date move to status, and rent_psf/building are
    backfilled where computable."""
    ad = r.get("available_date", "") or r.get("last_available_date", "")
    key = "available_date" if "available_date" in r else "last_available_date"
    if ad and not try_parse_date(ad):
        if not r.get("status"):
            r["status"] = ad
        r[key] = ""
    for psf_col, price_col in (("rent_psf", "price"),
                               ("last_rent_psf", "last_price")):
        if price_col in r and not r.get(psf_col):
            try:
                r[psf_col] = f"{int(r[price_col]) / int(r['sqft']):.2f}"
            except (KeyError, TypeError, ValueError, ZeroDivisionError):
                pass
    if not r.get("building"):
        m = re.match(r"^([A-Za-z0-9]{1,3})-\d{3,5}$",
                     r.get("unit_number", "") or "")
        if m:
            r["building"] = m.group(1)
    return r


def _ensure_csv_schema(path: Path, cols: list[str]):
    """Upgrade an existing CSV in place when columns are added: old rows are
    kept (repaired where possible), new columns filled blank."""
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
            r = _repair_row(dict(r))
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
        "wing": u.get("wing", ""),
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
        "wing": u.get("wing", ""),
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


def locations_block(units: list[dict]) -> str:
    lines = []
    for u in sorted(units, key=lambda x: x["price"]):
        if u.get("wing"):
            lines.append(f"- **{u['unit_number'] or u.get('key','?')}**: "
                         f"{u.get('building','')} building, floor {u['floor']}"
                         + (f", faces {u['facing']}" if u.get("facing") else "")
                         + f" — {u['wing']}")
    return ("**Where they are** (see site map below):\n"
            + "\n".join(lines) + "\n") if lines else ""


def _unit_line(u: dict) -> str:
    bits = [u["unit_number"] or u.get("key", "?")]
    if u.get("building"):
        bits.append(f"{u['building']} bldg"
                    + (f" ({u['wing']})" if u.get("wing") else ""))
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


def load_legend() -> dict:
    p = DATA_DIR / "building_legend.json"
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except Exception:
        return {}


def map_footer(site_map: str) -> str:
    lines = []
    legend = load_legend()
    if legend:
        lines.append("**Building legend**:")
        for k in sorted(legend):
            if k.lower() == "note":
                lines.append(f"- {legend[k]}")
            else:
                lines.append(f"- {k} building: {legend[k]}")
        lines.append("")
    repo = os.environ.get("GITHUB_REPOSITORY")
    branch = os.environ.get("GITHUB_REF_NAME", "main")
    if repo and (DATA_DIR / "community_map.png").exists():
        site_map = (f"https://raw.githubusercontent.com/{repo}/{branch}"
                    f"/data/community_map.png")
    if site_map:
        lines.append("**Community site map** (unit numbers are floor + "
                     "stack; find the stack on the plan):")
        lines.append("")
        lines.append(f"![Site map]({site_map})")
        lines.append("")
        lines.append(f"[Full gallery]({GALLERY_URL})")
    else:
        lines.append(f"[Community map and gallery]({GALLERY_URL})")
    return "\n".join(lines)


def build_alert_html(title: str, intro_html: str, current: list[dict],
                     site_map: str) -> str:
    """Self-contained HTML email body. Inline styles only (Gmail strips
    <style> blocks); solid black cell borders for mobile readability."""
    td = "border:1px solid #000;padding:6px 8px;font-size:14px;"
    th = td + "background:#f2f2f2;font-weight:bold;"
    head = "".join(f'<th style="{th}">{h}</th>' for h in
                   ("Unit", "Price", "$/SF", "Sq Ft", "Fl", "Faces",
                    "Move-in"))
    trs = []
    for u in sorted(current, key=lambda x: x["price"]):
        sq = f"{u['sqft']:,}" if isinstance(u["sqft"], int) else hesc(str(u["sqft"]))
        unit = hesc(u["unit_number"] or u.get("key", ""))
        if u.get("fp_image"):
            unit = f'<a href="{hesc(u["fp_image"])}">{unit}</a>'
        psf = rent_psf(u)
        cells = (unit, money(u["price"]), f"${psf}" if psf else "", sq,
                 hesc(str(u["floor"])), hesc(u.get("facing", "")),
                 hesc(_avail_disp(u)))
        trs.append("<tr>" + "".join(f'<td style="{td}">{c}</td>'
                                    for c in cells) + "</tr>")
    locs = "".join(
        f"<li style='margin-bottom:4px'><b>{hesc(u['unit_number'] or '')}"
        f"</b>: {hesc(u.get('building', ''))} building, floor "
        f"{hesc(str(u['floor']))}"
        + (f", faces {hesc(u['facing'])}" if u.get("facing") else "")
        + (f" — {hesc(u['wing'])}" if u.get("wing") else "") + "</li>"
        for u in sorted(current, key=lambda x: x["price"]))
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    branch = os.environ.get("GITHUB_REF_NAME", "main")
    chart = map_link = links = ""
    if repo:
        chart = (f'<p><img src="https://raw.githubusercontent.com/{repo}/'
                 f'{branch}/data/price_history.png" '
                 f'style="max-width:100%;height:auto" alt="Price history"/>'
                 f"</p>")
        links = (f'<p style="font-size:14px">'
                 f'<a href="https://github.com/{repo}/issues">Alert history'
                 f"</a> &middot; "
                 f'<a href="{UNITS_APP_URL.split(",")[0]}">Apply portal</a>'
                 f' &middot; <a href="{PROPERTY_URL}">Listing page</a></p>')
        if (DATA_DIR / "community_map.png").exists():
            map_link = (f'<p style="font-size:14px"><a href='
                        f'"https://raw.githubusercontent.com/{repo}/{branch}'
                        f'/data/community_map.png">Community site map</a> '
                        f"(unit numbers are floor + stack)</p>")
    font = ("font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,"
            "sans-serif;color:#111;")
    return (f'<div style="{font}">'
            f'<h2 style="font-size:17px;margin:0 0 10px">{hesc(title)}</h2>'
            f"{intro_html}"
            f'<table style="border-collapse:collapse;border:1px solid #000">'
            f"<tr>{head}</tr>{''.join(trs)}</table>"
            f'<p style="font-size:14px;margin:12px 0 4px"><b>Where they are'
            f"</b></p>"
            f'<ul style="font-size:14px;margin:0;padding-left:18px">{locs}'
            f"</ul>{links}{chart}{map_link}</div>")


def events_intro_html(events: list[dict]) -> str:
    items = []
    for e in events:
        u = e["unit"]
        line = (f"<li style='margin-bottom:4px'><b>"
                f"{EVENT_LABELS[e['type']]}</b>: "
                f"{hesc(u['unit_number'] or u.get('key', ''))} at "
                f"{money(u['price'])}, move-in {hesc(_avail_disp(u))}")
        if e["type"] == "price_change":
            line += f" (was {money(e['prev_price'])})"
        items.append(line + "</li>")
    return ('<ul style="font-size:14px;padding-left:18px">'
            + "".join(items) + "</ul>")


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
    if ok:
        print("GitHub issue: created")
    else:
        print(f"GitHub issue: FAILED {r.status_code}: {r.text[:300]}")
    return ok


def _with_mention(body: str) -> str:
    user = os.environ.get("MENTION_USER", "").strip().lstrip("@")
    return body + (f"\n\ncc @{user}" if user else "")


def build_alert(events: list[dict], current: list[dict],
                site_map: str) -> tuple[str, str]:
    labels = {k: v.lower() for k, v in EVENT_LABELS.items()}
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
             units_table(current), "", locations_block(current), "",
             map_footer(site_map), "",
             f"[Application portal]({UNITS_APP_URL}) | "
             f"[Listing page]({PROPERTY_URL})"]
    repo = os.environ.get("GITHUB_REPOSITORY")
    branch = os.environ.get("GITHUB_REF_NAME", "main")
    if repo:
        base = f"https://github.com/{repo}/blob/{branch}/data"
        body.append(f" | [Availability log]({base}/availability_log.csv)"
                    f" | [Offline log]({base}/offline_log.csv)")
        body.append("\n### Price history\n")
        body.append(f"![Price history](https://raw.githubusercontent.com/"
                    f"{repo}/{branch}/data/price_history.png)")
    return title, "\n".join(body) + "\n"


def backfill_csv_from_state(state: dict):
    """One-time-ish repair: fill blanks in historical availability rows
    (facing, floorplan, move-in date, building) from the current state,
    keyed by unit. Idempotent; rewrites only when something changed."""
    if not AVAIL_LOG.exists():
        return
    units = state.get("units", {})
    with open(AVAIL_LOG, newline="") as f:
        reader = csv.DictReader(f)
        cols = reader.fieldnames or []
        rows = list(reader)
    changed = False
    for r in rows:
        loc = {"unit_number": r.get("unit_number", ""), "floor": r.get("floor")}
        derive_location(loc)
        for f in ("building", "wing"):
            if f in r and loc.get(f) and r.get(f) != loc[f]:
                r[f] = loc[f]
                changed = True
        su = units.get(r.get("unit_key", ""))
        if not su:
            continue
        for src_f, dst_f in (("facing", "facing"), ("floorplan", "floorplan"),
                             ("available", "available_date")):
            if dst_f in r and not r.get(dst_f) and su.get(src_f):
                r[dst_f] = str(su[src_f])
                changed = True
    if changed:
        with open(AVAIL_LOG, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            w.writerows(rows)
        print("Backfilled historical rows from state.")


def generate_chart(state: dict) -> str:
    """Step chart of asking rent and $/SF over time for target-bed units,
    built from the event logs. X marks a delisting. Saved as
    data/price_history.png; returns the path or '' if skipped."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt
        from matplotlib.ticker import StrMethodFormatter
    except Exception as exc:
        print(f"Chart skipped (matplotlib unavailable: {exc})")
        return ""

    series: dict[str, dict] = {}

    def bucket(key, sqft):
        s = series.setdefault(key, {"pts": [], "sqft": None, "end": None})
        if sqft:
            s["sqft"] = sqft
        return s

    if AVAIL_LOG.exists():
        with open(AVAIL_LOG, newline="") as f:
            for r in csv.DictReader(f):
                try:
                    if int(r["beds"]) not in TARGET_BEDS:
                        continue
                    s = bucket(r["unit_key"], int(r["sqft"] or 0))
                    s["pts"].append((parse_iso(r["logged_utc"]),
                                     int(r["price"])))
                except (KeyError, TypeError, ValueError):
                    continue
    if OFFLINE_LOG.exists():
        with open(OFFLINE_LOG, newline="") as f:
            for r in csv.DictReader(f):
                try:
                    if int(r["beds"]) not in TARGET_BEDS:
                        continue
                    if r["unit_key"] in series:
                        series[r["unit_key"]]["end"] = (
                            parse_iso(r["delisted_utc"]),
                            int(r["last_price"]))
                except (KeyError, TypeError, ValueError):
                    continue

    nowdt = now_utc()
    for key, s in series.items():
        if s["end"] is not None:
            s["pts"].append(s["end"])
        else:
            su = state.get("units", {}).get(key)
            if su:
                s["pts"].append((nowdt, su["price"]))
    series = {k: s for k, s in series.items() if s["pts"]}
    if not series:
        return ""

    fig, (ax1, ax2) = plt.subplots(2, 1, sharex=True, figsize=(10, 7))
    for key in sorted(series):
        s = series[key]
        pts = sorted(s["pts"], key=lambda p: p[0])
        ts, ps = [p[0] for p in pts], [p[1] for p in pts]
        line, = ax1.step(ts, ps, where="post", marker="o",
                         markersize=4, label=key)
        color = line.get_color()
        if s["sqft"]:
            ax2.step(ts, [p / s["sqft"] for p in ps], where="post",
                     marker="o", markersize=4, color=color, label=key)
        if s["end"] is not None:
            ax1.plot([s["end"][0]], [s["end"][1]], marker="X",
                     markersize=11, color=color)
            if s["sqft"]:
                ax2.plot([s["end"][0]], [s["end"][1] / s["sqft"]],
                         marker="X", markersize=11, color=color)

    beds_desc = "/".join(str(b) for b in sorted(TARGET_BEDS))
    ax1.set_title(f"{PROPERTY_NAME} {beds_desc}BR asking rents "
                  f"(X = delisted) — updated {nowdt:%Y-%m-%d %H:%M} UTC")
    ax1.set_ylabel("Asking rent ($/mo)")
    ax1.yaxis.set_major_formatter(StrMethodFormatter("${x:,.0f}"))
    ax1.grid(alpha=0.3)
    ax1.legend(fontsize=8)
    ax2.set_ylabel("$/SF/mo")
    ax2.yaxis.set_major_formatter(StrMethodFormatter("${x:,.2f}"))
    ax2.grid(alpha=0.3)
    loc = mdates.AutoDateLocator()
    ax2.xaxis.set_major_locator(loc)
    ax2.xaxis.set_major_formatter(mdates.ConciseDateFormatter(loc))
    fig.tight_layout()
    out = DATA_DIR / "price_history.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    return str(out)


# ------------------------------------------------------------------- main


def main() -> int:
    ts = iso(now_utc())
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _ensure_csv_schema(AVAIL_LOG, AVAIL_COLS)
    _ensure_csv_schema(OFFLINE_LOG, OFFLINE_COLS)

    all_units, expected, source, site_map, warnings, mkt_target = acquire_units()
    for u in all_units:
        derive_location(u)
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
    if mkt_target is not None and mkt_target > len(target):
        trust_absence = False
        warnings.append(
            f"Marketing page shows {mkt_target} target-bed units but the "
            f"portal returned {len(target)}. The portal URL(s) may cover only "
            "part of the property; check eqr-applications.com for sibling "
            "building slugs and add them comma-separated to UNITS_APP_URL. "
            "Offline detection skipped this run.")

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
             "fp_id", "fp_image", "unit_number", "building", "wing",
             "facing", "status", "lease_term_months", "source")
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
    backfill_csv_from_state(state)
    chart_path = generate_chart(state)

    test_alert = os.environ.get("TEST_ALERT", "").lower() in ("1", "true")
    notify_events = [e for e in events if e["type"] in NOTIFY_EVENTS]
    if test_alert:
        beds_lbl = "/".join(str(b) for b in sorted(TARGET_BEDS))
        title = (f"[TEST] {PROPERTY_NAME} alert channel check: "
                 f"{len(current)} {beds_lbl}BR currently listed")
        body = _with_mention(
            "Manually triggered test alert. If this reached you as an email "
            "or notification, the channel works; real alerts will look the "
            "same and fire only on actual events.\n\n"
            "### Currently listed\n\n"
            + units_table(list(current.values())) + "\n"
            + locations_block(list(current.values())) + "\n"
            + map_footer(site_map))
        ALERT_FILE.write_text(f"# {title}\n\n{body}")
        intro = ('<p style="font-size:14px">Manually triggered test alert. '
                 "Real alerts look the same and fire only on actual "
                 "events.</p>")
        ALERT_HTML.write_text(build_alert_html(
            title, intro, list(current.values()), site_map))
        create_github_issue(title, body)
        gh_output(notify="true", subject=title)
    elif notify_events:
        title, body = build_alert(notify_events, list(current.values()),
                                  site_map)
        body = _with_mention(body)
        ALERT_FILE.write_text(f"# {title}\n\n{body}")
        ALERT_HTML.write_text(build_alert_html(
            title, events_intro_html(notify_events),
            list(current.values()), site_map))
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
           units_table(list(current.values())), "",
           locations_block(list(current.values())), "", map_footer(site_map)]
    repo = os.environ.get("GITHUB_REPOSITORY")
    if chart_path and repo:
        branch = os.environ.get("GITHUB_REF_NAME", "main")
        md.append(f"\n[Price history chart](https://github.com/{repo}/"
                  f"blob/{branch}/data/price_history.png)")
    step_summary("\n".join(md))

    print(f"OK [{source}]: parsed {len(all_units)} units total, "
          f"{len(target)} target, {len(events)} event(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

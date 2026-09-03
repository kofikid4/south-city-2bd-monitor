<!-- STATUS:START -->
## Latest check: 2026-09-03 4:17 AM PDT (source: portal)

### Currently available 2-bed units

| Unit | Bldg | Floor | Faces | Price | $/SF | Bd/Ba | Sq Ft | Move-in | Plan |
|---|---|---|---|---|---|---|---|---|---|
| 01-2049 | West | 2 | East/North | $4,874 | $4.62 | 2/2 | 1,054 | 2026-10-05 | [2 Bedrooms A](https://media.equityapartments.com/image/upload/f_auto,q_auto,b_white/4062-FP-5167-2-2-1054) |


### Leased or delisted since tracking began

| Unit | Last price | $/SF | Sq Ft | First seen | Off market | Days listed | Chg while listed |
|---|---|---|---|---|---|---|---|
| 01-2104 | $4,790 | $4.54 | 1054 | 2026-08-30 | 2026-08-30 | 0.0 | +0 |
| 01-3063 | $5,093 | $4.02 | 1266 | 2026-08-17 | 2026-08-26 | 8.7 | -20 |
| 01-2088 | $4,534 | $4.30 | 1054 | 2026-08-17 | 2026-08-26 | 8.2 | -55 |


**Where they are** (see site map below):
- **01-2049**: West building, floor 2, faces East/North — south courtyard cluster by the spa


**Building legend**:
- East building: Stacks 68-121. Block on BART Station Access Rd across McLellan Dr; mail and second fitness center on the inner south side, spa in the SE courtyard.
- West building: Stacks 14-67. Block bounded by Costco Entry Dr, McLellan Dr, and El Camino Real; leasing center and fitness at the NE corner, spa in the south courtyard, standalone garages 1001-1013 across the north drive.
- Unit numbers are floor-first: 2049 = floor 2, stack 49. The 01- prefix is an EQR property code, not a building.

### Price history

![Price history](data/price_history.png)

### Community site map
(unit numbers are floor + stack)

![Community site map](data/community_map_email.jpg)

[Full resolution](data/community_map.png)

<!-- STATUS:END -->

# South City Station 2BR monitor

Checks South City Station availability once an hour via GitHub Actions.
When a 2 bedroom unit shows up, it opens an issue in this repo, which
GitHub emails to you automatically. When a 2 bedroom disappears, it writes
the unit's last asking rent and time on market to a CSV. All history lives
in the `data/` folder as CSVs that render as sortable tables on github.com.

## Data sources

Primary source is the application portal at eqr-applications.com, rendered
with headless Chromium. The script loads the unit list, then visits every
unit detail page, because the list API only carries a status field
(renovating, available-now) while the exact move-in date and the per-term
pricing matrix live on the detail endpoints. The 12 month rate is selected
from that matrix; if a unit does not offer exactly 12 months, the closest
term is used and the actual term is written to lease_term_months.

Each run also cross-matches portal units to the marketing page cards on
beds, square footage, move-in date, rent, and floor, and copies over the
floorplan name, the floorplan image link, and the unit facing (parsed from
the exposure chips). Building is derived from the unit number (01-2049 is
building 01) and floor comes from the portal, so every unit carries
building, floor, facing, move-in date, and rent per square foot per month.
Alerts and run summaries show all of it, link each unit floorplan image,
and embed the community site map (auto-discovered from the marketing page,
overridable via SITE_MAP_URL, with the media gallery as fallback) so you
can place a unit by building, floor, and facing.

If the portal scrape fails for any reason, the run falls back automatically
to the equityapartments.com marketing page, which is server rendered and
scrapes reliably with plain requests, also at the 12 month rate. On a
source switch the script remaps unit identities by matching bed count,
square footage, floor, availability date, and price, so first-seen dates
and initial prices carry across and you do not get a wave of false
new/delisted events. Every log row records which source it came from.

## Setup

1. Create a GitHub repo (private is fine) and push these files, keeping the
   structure intact, including `.github/workflows/monitor.yml`.
2. Go to the Actions tab and enable workflows if prompted.
3. Run it once manually: Actions, Apartment monitor, Run workflow. The first
   run treats every currently listed 2BR as new, so you should get an issue
   listing current units within a couple of minutes. That confirms the
   pipeline end to end.
4. Verify the notification channel end to end: Actions, run the workflow
   with the "Send a test alert" box checked. Within a couple of minutes an
   issue titled [TEST] should appear in the Issues tab and land in your
   email. Alerts @mention you (MENTION_USER in the workflow env), which
   GitHub emails under Participating notifications, the most reliable
   category. If the test issue appears but no email arrives, check
   github.com/settings/notifications and enable email for Participating;
   the GitHub Mobile app also pushes these instantly. If no issue appears
   at all, open the run's "Check availability" step log; the line starting
   "GitHub issue:" prints the exact API error.
5. Optional extra channels, each auto-skipped when its secrets are absent.
   Email: secrets `MAIL_USERNAME`, `MAIL_PASSWORD` (Gmail app password),
   `MAIL_TO` (comma-separated for multiple recipients). Phone push via the
   free ntfy app: both people install ntfy, subscribe to a made-up
   unguessable topic, and store it as secret `NTFY_TOPIC`. Real SMS via
   Textbelt: buy a prepaid key at textbelt.com, store it as `TEXTBELT_KEY`,
   and put the destination number(s), comma-separated, in `SMS_TO`. Note
   carrier email-to-text gateways (vtext, txt.att.net, tmomail) are shut
   down or dying and are deliberately not used here. Textbelt filters
   messages containing URLs unless whitelisted, so the SMS body is the
   subject line only.

## Where the data lives

`data/availability_log.csv` gets a row every time a 2BR is listed or its
price changes: timestamp, 12 month rate, previous rate, rent per sf, unit
number, building, floorplan, square footage, floor, facing, status,
move-in date, source. When columns are added in an update, existing CSVs
are migrated in place with old rows preserved.

`data/offline_log.csv` gets a row when a 2BR leaves the market: last asking
rent, initial asking rent, price movement while listed, days on market,
first and last seen timestamps. The closest public proxy for what the unit
leased at.

`data/price_history.png` is a chart regenerated every run: asking rent on
top, rent per sf below, one step line per unit, a dot at listing and each
price change, an X where a unit was delisted. It renders at the bottom of
every alert issue and in this README below.

Unit locations are derived from the community site plan, which is committed
at data/community_map.png and embedded in every alert. Unit numbers are
floor-first (2049 = floor 2, stack 49; the 01- prefix is an EQR property
code, not a building). The stack number maps to a physical building and
wing: stacks 14-67 are the west building on the Costco Entry Dr / McLellan /
El Camino block, stacks 68-121 the east building on the BART Station Access
Rd block, 1001-1013 the standalone garages. The script carries a wing-level
map of both buildings (STACK_WINGS in monitor.py), so every alert states
building, wing, floor, and facing per unit, e.g. "West building, floor 2,
faces East/North — south courtyard cluster by the spa".
data/building_legend.json holds the building descriptions shown under
alerts and is editable.

`data/state.json` is bookkeeping for the run-to-run diff. All three are
committed back after each run, so the commit history doubles as an audit
trail. Every run also writes a table of currently listed units to the
Actions run summary page, whether or not anything changed.

## Configuration

All knobs are env vars at the top of `.github/workflows/monitor.yml`:

1. `UNITS_APP_URL` and `PROPERTY_URL`: the portal and marketing pages.
   UNITS_APP_URL accepts a comma-separated list because EliseAI sometimes
   splits one property into several building slugs (this one is
   south-city-station-2, implying a sibling may exist). Every run compares
   the marketing page's unit count against the portal's; if marketing shows
   more, the run warns that portal coverage is partial and suspends offline
   detection until the missing slug is added.
2. `SOURCE`: `auto` (portal with fallback, default), `eqr` (portal only,
   fail loudly), or `eqweb` (marketing page only, no browser needed).
3. `TARGET_TERM`: lease months whose rate is recorded. Default 12.
4. `TARGET_BEDS`: comma separated, 0 means studio. `"1,2"` tracks both.
5. `NOTIFY_EVENTS`: which events open an issue. Default `listed` only;
   add `price_change` or `delisted` for issues on those too. Everything is
   logged to CSV regardless.
6. `OFFLINE_CONFIRM_RUNS`: consecutive absent runs before a unit is
   declared offline. Default 2, so one flaky render cannot fake a lease-up.
7. The cron line is UTC. GitHub can delay scheduled runs by 10 to 30
   minutes at busy times; normal.

## Failure modes worth knowing

If the portal scrape yields nothing, the run saves the rendered page and
the captured API payloads to a downloadable artifact before falling back,
so you can inspect exactly what the app returned. If both sources fail, the
workflow fails loudly and GitHub emails you. Silent staleness is the thing
this is designed to avoid.

equityapartments.com blocks plain-requests traffic from GitHub runner IPs
(HTTP 403), so both enrichment and the fallback fetch the marketing page
through the Chromium session instead, which passes the bot check. If either
site ever starts blocking headless Chromium too, set BROWSER_HEADED to 1 in
the workflow env; the run already executes under xvfb so a headed browser
works on the runner. Setting DEBUG_PAYLOADS to 1 uploads the captured
portal JSON and rendered detail-page text as a run artifact, useful if a
field ever comes through blank and the parser needs adjusting. The source
column in the CSVs records where each row came from. The whole thing also runs on any machine with
Python if you ever want a residential IP: `pip install -r requirements.txt`,
`playwright install chromium`, `python scraper/monitor.py`.

Runs use the Chrome preinstalled on GitHub runners (BROWSER_CHANNEL env)
plus a pip cache, so setup takes seconds instead of the minutes a Chromium
download would; set BROWSER_CHANNEL to "" to fall back to downloading the
bundled browser.

GitHub disables cron schedules in repos with no commits for 60 days. Data
commits normally keep this alive; if the market goes completely quiet for
two months, GitHub emails first and re-enabling is one click.

## Local testing

`TEST_JSON=payload.json python scraper/monitor.py` parses a saved API
payload, `TEST_HTML=page.html python scraper/monitor.py` parses a saved
marketing page, and without a `GITHUB_TOKEN` alerts print to stdout instead
of opening issues. Useful for debugging parser changes against snapshots.

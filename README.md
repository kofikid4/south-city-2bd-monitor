# South City Station 2BR monitor

Checks South City Station availability once an hour via GitHub Actions.
When a 2 bedroom unit shows up, it opens an issue in this repo, which
GitHub emails to you automatically. When a 2 bedroom disappears, it writes
the unit's last asking rent and time on market to a CSV. All history lives
in the `data/` folder as CSVs that render as sortable tables on github.com.

## Data sources

Primary source is the application portal at eqr-applications.com, which is
rendered with headless Chromium so the script can capture the JSON the app
loads. That JSON carries real unit numbers (perfect unit identity) and a
per-term pricing matrix, from which the 12 month rate is recorded. If a
unit does not offer exactly 12 months, the closest term is used and the
actual term is written to the lease_term_months column so you can see it.

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
4. Make sure you are watching your own repo (default) and that your GitHub
   notification settings deliver issue notifications by email. That is the
   alert channel; nothing else to configure.
5. Optional direct email on top of the issues: add repo secrets
   `MAIL_USERNAME`, `MAIL_PASSWORD` (a Gmail app password, not your login
   password), and `MAIL_TO`. The email step skips itself when these are
   absent.

## Where the data lives

`data/availability_log.csv` gets a row every time a 2BR is listed or its
price changes: timestamp, 12 month rate, previous rate, unit number,
floorplan, square footage, floor, availability date, source.

`data/offline_log.csv` gets a row when a 2BR leaves the market: last asking
rent, initial asking rent, price movement while listed, days on market,
first and last seen timestamps. The closest public proxy for what the unit
leased at.

`data/state.json` is bookkeeping for the run-to-run diff. All three are
committed back after each run, so the commit history doubles as an audit
trail. Every run also writes a table of currently listed units to the
Actions run summary page, whether or not anything changed.

## Configuration

All knobs are env vars at the top of `.github/workflows/monitor.yml`:

1. `UNITS_APP_URL` and `PROPERTY_URL`: the portal and marketing pages. Both
   are parameterized, so cloning this for another EQR building means
   swapping two URLs.
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

The portal sits behind EliseAI and could bot-block GitHub runner IPs; that
is precisely what the fallback is for, and the source column in the CSVs
tells you when it happened. The whole thing also runs on any machine with
Python if you ever want a residential IP: `pip install -r requirements.txt`,
`playwright install chromium`, `python scraper/monitor.py`.

GitHub disables cron schedules in repos with no commits for 60 days. Data
commits normally keep this alive; if the market goes completely quiet for
two months, GitHub emails first and re-enabling is one click.

## Local testing

`TEST_JSON=payload.json python scraper/monitor.py` parses a saved API
payload, `TEST_HTML=page.html python scraper/monitor.py` parses a saved
marketing page, and without a `GITHUB_TOKEN` alerts print to stdout instead
of opening issues. Useful for debugging parser changes against snapshots.

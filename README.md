# dailyrankmon

Daily hotel-rank monitoring for MakeMyTrip. It drives the real desktop site
through [browser-harness](https://github.com/browser-use/browser-harness) and
records the top-N search-result cards exactly as a user sees them, sponsored
slots included. Every city × date × guest combination you configure becomes
one CSV.

```
setup.sh          one-time machine setup
mmt_batch.py      the thing you run
config/           what to search: cities.csv, dxrn.csv, pax.csv
core/mmt_srp.py   single-search scraper (called by mmt_batch.py)
output_sample/    what a result and a manifest look like
runs/             where batch output lands (gitignored)
```

## 1. Set up (once per machine, macOS)

```bash
./setup.sh
```

Checks Chrome, python3 and git, installs `uv` if missing, clones browser-harness
to `~/Developer/browser-harness` (override with `HARNESS_DIR=...`), registers it
with `uv tool install -e .`, and verifies the scraper can reach it. Safe to re-run.

## 2. Configure

Three comma-separated files in `config/`, each with a header row:

| file | columns | notes |
|---|---|---|
| `cities.csv` | `city_name` | one city per line, as you would type it into MMT's search box |
| `dxrn.csv` | `dx,rn` | `dx` = days from today to check-in (0 today, 1 tomorrow …), `rn` = nights |
| `pax.csv` | `adult,children,ages` | `children` optional (blank = 0); `ages` space-separated, one per child; missing ages default to 1 |

Rooms are always 1.

## 3. Run

```bash
python3 mmt_batch.py          # top 50 cards per search
python3 mmt_batch.py 20       # top 20
```

Every city × (dx, rn) × pax row is searched, one after another (they share one
Chrome and one harness daemon). The first run launches a dedicated Chrome on
port 9333 with a throwaway profile in `/tmp/mmt-chrome`; your everyday Chrome
is never touched. Expect roughly 20–30 s per search.

Optional flags: `--cities`, `--dxrn`, `--pax` (alternative input files),
`--runs-dir` (output parent), `--rooms`.

## 4. Read the output

Each run creates `runs/mmt_srp_run_<YYYYmmdd_HHMMSS>/` containing:

- one CSV per combination, named so nothing collides:
  `mmt_srp_<city>_dx<DX>_rn<RN>_<A>a<C>c[_ages<a-b>]_top<N>.csv`,
  e.g. `mmt_srp_goa_dx15_rn2_2a2c_ages5-9_top50.csv`
- `manifest.csv`: one line per search with the resolved check-in/check-out
  dates, status (`ok`, `partial` = fewer than N cards available, `FAILED`),
  row count, seconds and file name
- `run.log`: the scraper's own output for every search
- `summary.txt`: start/finish time, total elapsed, seconds per search,
  ok/partial/failed counts and total rows (also printed when the batch ends)

Result CSV columns:

| column | content |
|---|---|
| `Rank` | position on the page, 1 = top |
| `Hotel_Name` | as shown (MMT sometimes appends a descriptor after a `\|`) |
| `Location` | locality line; blank when the card has none |
| `Price` | MMT's displayed per-night price before taxes |
| `HDP_Url` | the hotel's product page, e.g. `https://www.makemytrip.com/hotels/ginger_goa_candolim-details-goa.html` |
| `HDP_Deeplink` | the link the card carries: same page with dates, city and occupancy in the query |
| `Hotel_Id` | MMT hotel id |
| `Sponsored` | `SPONSORED` (icon tag), `SPOTLIGHT` (Spotlight program badge), or blank for organic |

Sponsored listings stay at their on-page rank. Fields containing commas (many
hotel names, prices like `₹4,049`) are double-quoted per the CSV standard, so
use a real CSV parser rather than splitting on commas.

## One-off search

`core/mmt_srp.py` runs standalone for a single query:

```bash
python3 core/mmt_srp.py --city Goa --dx 3 --rn 1 --adults 2 --children 5 --top 20 --out goa.csv
```

Explicit `--checkin` / `--checkout` dates override `--dx` / `--rn`.
Exit code 2 means fewer than `--top` cards were available.

## How it works, and what to know when it breaks

- It is a UI scrape on purpose: the point is what a user sees (ranking, ad
  slots, display price), not MMT's search API.
- Flow per search: MMT home → Hotels tab → city autosuggest → calendar →
  rooms & guests → SEARCH → scroll the infinite list until N cards are loaded
  → read each card. Popups (login sheet, promos) are dismissed at every step
  and logged.
- `HDP_Url` comes from the listing page's live Redux store (`seoUrl`), read
  through the React fiber tree; the card's own anchor only has the dated
  deeplink.
- A "Recently Viewed" card appears above the results once the Chrome profile
  has opened any hotel page; it is not part of the ranking and is skipped.
- The scraper works with both browser-harness layouts (the older top-level
  `admin.py` checkout and the current `browser_harness` package). When the
  package is only installed in the harness's own venv it re-executes itself
  under that interpreter, so plain `python3` keeps working.
  `BROWSER_HARNESS_DIR` overrides the lookup.
- Site mechanics (selectors, traps) are documented for the next agent in
  browser-harness's `domain-skills/makemytrip/hotels.md`.

#!/usr/bin/env python3
"""MakeMyTrip hotel SRP scraper — drives the real UI through browser-harness (CDP).

Flow (mirrors what a person does): home → Hotels tab → city autosuggest →
check-in / check-out on the calendar → rooms & guests → SEARCH → scroll the
listing until N cards are loaded → read each card → CSV.

Why the UI and not MMT's private search API (see ../parallax): the point of
this tool is to see the SRP exactly as a user does — the same ranking,
the same sponsored slots, the same display price — so it deliberately never
calls the API directly.

Usually driven by ../mmt_batch.py; for a one-off search run it directly:

  python3 core/mmt_srp.py                               # Goa, DX=15 RN=2, 1 room, 2 adults
  python3 core/mmt_srp.py --city Mumbai --dx 0 --rn 1   # tonight, one night
  python3 core/mmt_srp.py --city Mumbai --dx 30 --rn 3 \
                          --rooms 1 --adults 2 --children 5 9 --top 50 --out mumbai.csv

Dates: DX = days from today to check-in (0 today, 1 tomorrow …), RN = nights.
--checkin/--checkout accept explicit dates and override them.

Chrome: connects to a Chrome with remote debugging on --cdp-port (default 9333).
If none is listening it launches one on a throwaway profile dir, so the user's
own Chrome (which needs a manual "Allow" on chrome://inspect) is never touched.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
HOME = "https://www.makemytrip.com/"

# ---------------------------------------------------------------- chrome / harness

def chrome_ws(port: int) -> str | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as r:
            return json.load(r)["webSocketDebuggerUrl"]
    except Exception:
        return None


def ensure_chrome(port: int, profile_dir: str) -> str:
    """Return the browser websocket URL, launching a dedicated Chrome if needed."""
    ws = chrome_ws(port)
    if ws:
        return ws
    if not os.path.exists(CHROME):
        sys.exit(f"Chrome not found at {CHROME}; start one with --remote-debugging-port={port} and retry")
    Path(profile_dir).mkdir(parents=True, exist_ok=True)
    subprocess.Popen(
        [CHROME, f"--remote-debugging-port={port}", f"--user-data-dir={profile_dir}",
         "--no-first-run", "--no-default-browser-check", "--window-size=1400,1000", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    deadline = time.time() + 30
    while time.time() < deadline:
        ws = chrome_ws(port)
        if ws:
            return ws
        time.sleep(0.5)
    sys.exit(f"Chrome did not expose DevTools on port {port} within 30s")


HARNESS_INSTALL = """browser-harness not found. Install it once (or run ./setup.sh):

  git clone https://github.com/browser-use/browser-harness ~/Developer/browser-harness
  cd ~/Developer/browser-harness && uv tool install -e .

or point BROWSER_HARNESS_DIR at an existing checkout."""


def _tool_python() -> str | None:
    """Interpreter of the installed `browser-harness` command (its shebang names
    the uv tool venv, where the package and its deps live)."""
    exe = shutil.which("browser-harness")
    if not exe:
        return None
    try:
        first = open(exe).readline().strip()
    except OSError:
        return None
    py = first[2:] if first.startswith("#!") else None
    return py if py and os.path.exists(py) else None


def _legacy_checkout() -> Path | None:
    """Pre-0.1.x layout: admin.py / helpers.py at the checkout root, daemon via `uv run`."""
    for c in (os.environ.get("BROWSER_HARNESS_DIR"), "~/Developer/browser-harness", "~/src/browser-harness"):
        if c and (Path(c).expanduser() / "admin.py").is_file():
            return Path(c).expanduser()
    return None


def load_harness():
    """Return (admin, helpers) for whichever browser-harness is available.

    Upstream moved from top-level modules to a `browser_harness` package whose
    daemon is spawned with `sys.executable -m browser_harness.daemon`, so the
    package (and its deps) must be importable *by this interpreter*. Order:
      1. `browser_harness` already importable here (venv / pip install).
      2. legacy checkout: admin.py at the root — add it to sys.path.
      3. re-exec this script under the `browser-harness` command's own Python,
         where 1 (or 2, for an old editable install) will succeed.
    """
    try:
        from browser_harness import admin, helpers  # type: ignore
        return admin, helpers
    except ImportError:
        pass
    legacy = _legacy_checkout()
    if legacy:
        sys.path.insert(0, str(legacy))
    try:
        # legacy checkout on sys.path, or an old editable install that exposes
        # admin/helpers as top-level modules in this interpreter
        import admin, helpers  # type: ignore
    except ImportError:
        admin = helpers = None
    if admin is not None:
        if not shutil.which("uv"):
            sys.exit("`uv` not on PATH — this browser-harness starts its daemon with `uv run`; install from https://docs.astral.sh/uv/")
        return admin, helpers
    py = _tool_python()
    # Compare venv prefixes, not realpaths: the tool venv's python3 is a symlink
    # to the same framework binary as /usr/local/bin/python3, so realpath()
    # calls them equal and the re-exec would never happen.
    if py and os.path.dirname(os.path.dirname(py)) != sys.prefix and not os.environ.get("MMT_REEXEC"):
        os.environ["MMT_REEXEC"] = "1"
        sys.stdout.flush()  # execv drops unflushed buffers (the banner, when piped)
        os.execv(py, [py, os.path.abspath(__file__)] + sys.argv[1:])
    sys.exit(HARNESS_INSTALL)


def harness_status() -> str:
    """What `connect()` would do — used by setup.sh's verify step."""
    try:
        import browser_harness  # type: ignore
        return f"package browser_harness at {os.path.dirname(browser_harness.__file__)}"
    except ImportError:
        pass
    legacy = _legacy_checkout()
    if legacy:
        return f"legacy checkout at {legacy}"
    py = _tool_python()
    if py:
        return f"will re-exec under {py}"
    return "NOT FOUND"


def connect(port: int, profile_dir: str, name: str):
    """Point the harness daemon at our Chrome and return its helpers module.

    The daemon reads BU_CDP_WS / BU_NAME from the environment *when it starts*,
    so both are set before ensure_daemon(). A daemon for `name` that is already
    alive is reused as-is (ensure_daemon is idempotent).
    """
    # helpers computes its daemon socket path from BU_NAME at import time, so
    # the environment has to be in place before load_harness().
    os.environ["BU_NAME"] = name
    os.environ["BU_CDP_WS"] = ensure_chrome(port, profile_dir).replace("localhost", "127.0.0.1")
    admin, helpers = load_harness()
    admin.ensure_daemon(name=name, env={"BU_NAME": name, "BU_CDP_WS": os.environ["BU_CDP_WS"]})
    return helpers


# ---------------------------------------------------------------- page mechanics

class MMT:
    def __init__(self, h, log=print):
        self.h = h
        self.log = log

    def js(self, expr):
        return self.h.js(expr)

    def wait_for(self, expr: str, timeout: float = 15, what: str = "") -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.js(expr):
                return True
            time.sleep(0.3)
        self.log(f"  ! timed out waiting for {what or expr[:60]}")
        return False

    def click_el(self, selector_js: str, what: str = "") -> bool:
        """Compositor-level click on the centre of the element `selector_js` evaluates to.

        Used for react-autosuggest options and react-day-picker cells: both
        listen for real mouse events and ignore a synthetic element.click().
        """
        xy = self.js(f"""
          const el = (() => {{ {selector_js} }})();
          if (!el) return null;
          el.scrollIntoView({{block: 'center'}});
          const b = el.getBoundingClientRect();
          return [b.x + b.width / 2, b.y + b.height / 2];
        """)
        if not xy:
            self.log(f"  ! not found: {what or selector_js[:60]}")
            return False
        self.h.click_at_xy(*xy)
        return True

    # -- popups ------------------------------------------------------------
    def dismiss_popups(self, label: str = "") -> None:
        """Close the login modal / promo overlays / chatbot nudge if any is showing.

        MMT shows the login sheet lazily (a few seconds after load, and again
        on the SRP), so this is called at every step boundary, not once.
        Escape first — every MMT modal honours it — then any visible close
        control, then nudge the backdrop. Everything is logged so a new popup
        variant is visible in the run output rather than silently ignored.
        """
        closed = []
        for sel in (
            ".loginModal .close, .loginModal span[class*=close], [class*=loginModal] [class*=cross]",
            "[data-cy*='close' i], [data-cy*='Close']",
            ".commonModal__close, .modalClose, .close-btn, [class*='closeBtn' i], [class*='crossIcon' i]",
            "[class*='popup' i] [class*='close' i], [class*='Popup'] [class*='Close']",
            "[role=dialog] button[aria-label*='close' i]",
            ".chatbot__close, [class*='chatBot' i] [class*='close' i]",
        ):
            hit = self.js(f"""
              const els = [...document.querySelectorAll({json.dumps(sel)})]
                .filter(e => e.offsetParent !== null || getComputedStyle(e).position === 'fixed');
              if (!els.length) return null;
              els[0].click();
              return (els[0].className || els[0].tagName).toString().slice(0, 60);
            """)
            if hit:
                closed.append(hit)
                time.sleep(0.5)
        # Escape is harmless when nothing is open, and closes the login sheet,
        # the calendar and the guest panel alike.
        self.h.press_key("Escape")
        backdrop = self.js("""
          const b = document.querySelector('.hsBackDrop, .overlay, [class*=backdrop i]');
          return b && b.offsetParent !== null ? b.className : null;
        """)
        if backdrop and "hsBackDrop" not in backdrop:
            closed.append(f"backdrop:{backdrop}")
        if closed:
            self.log(f"  popups dismissed{(' (' + label + ')') if label else ''}: {closed}")

    # -- steps -------------------------------------------------------------
    def open_hotels(self) -> None:
        self.h.new_tab(HOME)
        self.h.wait_for_load(30)
        time.sleep(3)
        self.dismiss_popups("home")
        # The top LOB strip: `li.menu_Hotels > a` (href /hotels/). Not the
        # "Hotels" tab inside the Offers carousel further down, which has the
        # same visible text (id superOffersTab_HOTELS).
        if not self.js("const a=document.querySelector('li.menu_Hotels a'); if(!a) return false; a.click(); return true"):
            self.h.goto_url(HOME + "hotels/")
        self.h.wait_for_load(30)
        self.wait_for("!!document.querySelector('label[for=city]') && !!document.querySelector('#hsw_search_button')", 20, "hotel search widget")
        time.sleep(1.5)
        self.dismiss_popups("hotels")

    def set_city(self, city: str) -> str:
        self.js("document.querySelector('label[for=city]').click()")
        # The visible #city input is readonly; typing goes into the
        # react-autosuggest input that appears on top of it.
        self.wait_for("document.activeElement && document.activeElement.classList.contains('react-autosuggest__input')", 5, "city autosuggest input")
        self.h.type_text(city)
        ok = self.wait_for("document.querySelectorAll('ul.react-autosuggest__suggestions-list li[role=option] .clickable').length > 0", 10, "city suggestions")
        if not ok:
            raise RuntimeError(f"no autosuggest results for {city!r}")
        time.sleep(0.8)  # list re-renders once more as later suggestions arrive
        # Prefer the option whose bold headline equals the query and whose
        # subtitle says "City in …"; else the first clickable option.
        picked = self.click_el(f"""
          const want = {json.dumps(city.strip().lower())};
          const opts = [...document.querySelectorAll('ul.react-autosuggest__suggestions-list li[role=option]')]
            .filter(li => li.querySelector('.clickable'));
          const exact = opts.find(li => {{
            const b = li.querySelector('.sr_city b, b');
            const sub = (li.querySelector('.sr_city .font14') || {{}}).textContent || '';
            return b && b.textContent.trim().toLowerCase() === want && /city/i.test(sub);
          }});
          return exact || opts[0];
        """, "city suggestion")
        if not picked:
            raise RuntimeError("could not click a city suggestion")
        self.wait_for(f"(document.querySelector('#city').value || '').toLowerCase().includes({json.dumps(city.strip().lower().split(',')[0])})", 5, "city value")
        chosen = self.js("return document.querySelector('#city').value")
        self.log(f"  city → {chosen}")
        return chosen

    def _pick_day(self, d: date) -> None:
        """Click a react-day-picker cell by its aria-label ('Fri Sep 25 2026').

        Two months are visible at a time. The picker opens on whatever month
        the profile last searched (it remembers across sessions), which can be
        *after* the wanted month — so page backwards as well as forwards,
        choosing the direction from the first visible caption ("October2026").
        """
        label = d.strftime("%a %b %d %Y")
        finder = f"""
          return [...document.querySelectorAll('.DayPicker-Day')]
            .find(e => e.getAttribute('aria-label') === {json.dumps(label)} && !e.classList.contains('DayPicker-Day--outside'));
        """
        for _ in range(24):
            if self.js("(() => {" + finder + "})() != null"):
                break
            cap = self.js("const c=document.querySelector('.DayPicker-Caption'); return c ? c.textContent.trim() : null")
            if not cap:
                raise RuntimeError("calendar not open")
            mo = re.match(r"([A-Za-z]+)\s*(\d{4})", cap)
            first = datetime.strptime(f"{mo.group(1)[:3]} {mo.group(2)}", "%b %Y").date() if mo else None
            direction = "prev" if first and (d.year, d.month) < (first.year, first.month) else "next"
            if not self.js(f"const n=document.querySelector('.DayPicker-NavButton--{direction}'); if(!n||n.classList.contains('DayPicker-NavButton--interactionDisabled')) return false; n.click(); return true"):
                raise RuntimeError(f"cannot page {direction} to {label} (calendar limit)")
            time.sleep(0.4)
        else:
            raise RuntimeError(f"{label} never appeared in the calendar")
        if self.js("(() => {" + finder + "})().classList.contains('DayPicker-Day--disabled')"):
            raise RuntimeError(f"{label} is not selectable (past date?)")
        if not self.click_el(finder, label):
            raise RuntimeError(f"could not click {label}")

    def set_dates(self, checkin: date, checkout: date) -> None:
        if not self.js("return !!document.querySelector('.DayPicker')"):
            self.js("document.querySelector('label[for=checkin]').click()")
            self.wait_for("!!document.querySelector('.DayPicker')", 5, "calendar")
        self._pick_day(checkin)
        time.sleep(0.6)
        # After the check-in click the same picker stays open for check-out.
        if not self.js("return !!document.querySelector('.DayPicker')"):
            self.js("document.querySelector('label[for=checkout]').click()")
            self.wait_for("!!document.querySelector('.DayPicker')", 5, "calendar (checkout)")
        self._pick_day(checkout)
        time.sleep(0.6)
        self.log("  dates → " + self.js(
            "return [...document.querySelectorAll('label[for=checkin],label[for=checkout]')].map(l=>l.textContent.trim().replace(/\\s+/g,' ')).join(' / ')"))

    def _set_counter(self, name: str, value: int) -> None:
        for _ in range(30):
            cur = int(self.js(f"return document.querySelector('.counter[aria-label={json.dumps(name)}] .counter__value').textContent") or 0)
            if cur == value:
                return
            btn = "increment" if value > cur else "decrement"
            if not self.js(f"const b=document.querySelector('.counter[aria-label={json.dumps(name)}] .counter__button--{btn}'); if(!b||b.disabled) return false; b.click(); return true"):
                raise RuntimeError(f"{name}: cannot reach {value} (limit hit at {cur})")
            time.sleep(0.25)

    def set_guests(self, rooms: int, adults: int, children: list[int]) -> None:
        if not self.js("return !!document.querySelector('.rmsGst')"):
            self.js("document.querySelector('label[for=guest]').click()")
            self.wait_for("!!document.querySelector('.rmsGst')", 5, "guest panel")
        self._set_counter("Rooms counter", rooms)
        self._set_counter("Adults counter", adults)
        self._set_counter("Children counter", len(children))
        for i, age in enumerate(children):
            # One custom dropdown (.gstSlct) per child appears in .rmsGst__slctAge;
            # options are two-digit strings ('05'), hence the parseInt compare.
            self.js(f"document.querySelectorAll('.rmsGst__slctAge .gstSlct')[{i}].click()")
            self.wait_for(f"document.querySelectorAll('.rmsGst__slctAge .gstDrpDown__cont')[{i}].querySelectorAll('.gstSlct__list li').length > 0", 5, "child age list")
            ok = self.click_el(f"""
              const cont = document.querySelectorAll('.rmsGst__slctAge .gstDrpDown__cont')[{i}];
              return [...cont.querySelectorAll('.gstSlct__list li')].find(li => parseInt(li.textContent) === {int(age)});
            """, f"child {i + 1} age {age}")
            if not ok:
                raise RuntimeError(f"age {age} not offered for child {i + 1}")
            time.sleep(0.4)
        self.js("document.querySelector('.rmsGst .btnApplyNew').click()")
        time.sleep(0.8)
        self.log("  guests → " + (self.js("return document.querySelector('[data-cy=roomGuestCount]').textContent.trim().replace(/\\s+/g,' ')") or "?"))

    def search(self) -> str:
        self.dismiss_popups("pre-search")
        self.js("document.querySelector('#hsw_search_button').click()")
        # SEARCH navigates the same tab to /hotels/hotel-listing/…
        self.wait_for("location.pathname.includes('hotel-listing')", 30, "listing URL")
        self.h.wait_for_load(30)
        self.wait_for("document.querySelectorAll('[id^=Listing_hotel_]').length >= 1", 30, "first hotel cards")
        time.sleep(2)
        self.dismiss_popups("srp")
        url = self.js("return location.href")
        self.log(f"  SRP → {url}")
        return url

    def load_cards(self, top: int) -> int:
        """Scroll until `top` main cards exist. The list is an infinite-scroll
        component that appends 10 cards per batch as the bottom comes into view."""
        count_js = "return document.querySelectorAll('.listingRowOuter [id^=Listing_hotel_]').length"
        # The infinite scroll is driven by an IntersectionObserver, which Chrome
        # throttles in background tabs: scrolling then loads nothing. Bring the
        # tab to the front first (newer harness versions open tabs in the background).
        try:
            self.h.cdp("Target.activateTarget", targetId=self.h.current_tab()["targetId"])
        except Exception:
            pass
        stale = 0
        n = self.js(count_js)
        # "Recently Viewed" cards render above the results and are dropped by
        # read_cards(), so they must not count towards the target.
        rv_count = lambda: sum(1 for v in self.store_index().values() if v.get("section") == "RECENTLY_VIEWED_HOTELS")
        target = top + rv_count()
        while n < target and stale < 4:
            self.js("window.scrollTo(0, document.documentElement.scrollHeight)")
            deadline = time.time() + 8
            grew = False
            while time.time() < deadline:
                time.sleep(0.5)
                m = self.js(count_js)
                if m > n:
                    n, grew = m, True
                    break
            stale = 0 if grew else stale + 1
            if grew:
                target = top + rv_count()   # store may not have been populated before the first batch
            if not grew:
                # A nudge up and back down re-triggers the intersection observer.
                self.js("window.scrollBy(0, -600)"); time.sleep(0.4)
            self.dismiss_popups("scroll")
        self.log(f"  cards loaded: {n}")
        return n

    def store_index(self) -> dict:
        """{hotelId: {seoUrl, sponsored, spotlight, section}} from the page's live Redux store.

        `window.__INITIAL_STATE__` is a static snapshot of the SSR'd first batch;
        the cards added on scroll live only in the store. The store is not on
        `window`, but the Provider is two fibers below the React root, so read
        it off `memoizedProps.store`. `seoUrl` is the canonical product page
        (`/hotels/<slug>-details-<city>.html`) — the card's own anchor only
        carries the dated search deeplink.
        """
        return self.js("""
          const root = document.querySelector('#root') || document.body.firstElementChild;
          const key = Object.keys(root).find(k => k.startsWith('__reactContainer') || k.startsWith('_reactRootContainer'));
          let fiber = key ? (root[key].current || (root[key]._internalRoot && root[key]._internalRoot.current) || root[key]) : null;
          let store = null;
          for (let i = 0; fiber && i < 60 && !store; i++) {
            const p = fiber.memoizedProps || {};
            if (p.store && p.store.getState) store = p.store;
            fiber = fiber.child;
          }
          if (!store) return {};
          const st = store.getState(), out = {};
          for (const k of ['searchHotels', 'hotelListing']) {
            const v = st[k];
            for (const sec of (v && v.personalizedSections) || []) {
              for (const h of sec.hotels || []) {
                if (!out[h.id]) out[h.id] = {seoUrl: h.seoUrl || '', sponsored: !!h.sponsored,
                                             spotlight: !!h.spotlightApplicable, section: sec.name || ''};
              }
            }
          }
          return out;
        """) or {}

    def read_cards(self, top: int) -> list[dict]:
        rows = self.js("""
          const abs = u => u ? (u.startsWith('//') ? 'https:' + u : u) : '';
          const txt = el => el ? el.textContent.replace(/\\s+/g, ' ').trim() : '';
          // Only the main SRP list: cards are `#Listing_hotel_<n>` inside
          // `.listingRowOuter`. Rails such as "similar properties" don't use it.
          return [...document.querySelectorAll('.listingRowOuter [id^=Listing_hotel_]')]
            .filter(c => /^Listing_hotel_\\d+$/.test(c.id))
            .map(c => {
              const a = c.querySelector('#hlistpg_hotel_name a, [itemprop=name] a, a[href*="hotel-details"]');
              // The name <p> also holds the Spotlight (paid placement) tooltip
              // and the "Like a 3" alt-acco star hint, so read only the
              // span inside the anchor (`#htl_id_seo_<hotelId>`).
              const nameEl = c.querySelector('span[id^=htl_id_seo_]') || (a && a.querySelector('span')) || a;
              // Some cards carry no locality line at all; leave Location blank then.
              const loc = c.querySelector('.pc__locationPerNew .pc__html span, [itemprop=address] span')
                       || c.querySelector('[itemprop=address]');
              const href = abs(a ? a.getAttribute('href') : '');
              return {
                id: (href.match(/hotelId=(\\d+)/) || [])[1] || '',
                Hotel_Name: txt(nameEl),
                Location: txt(loc).split('|')[0].trim(),
                Price: txt(c.querySelector('#hlistpg_hotel_shown_price, .priceText')).replace(/\\s+/g, ''),
                deeplink: href,
                sold_out: !!c.querySelector('[class*=soldOut i]'),
                // Two paid-placement formats, both kept in the list and flagged:
                //  - "Sponsored" tag: an icon-only sprite (.icSponsored) in the
                //    persuasion strip, no text anywhere in the DOM.
                //  - Spotlight program: badge + tooltip (.spotlightWrap) next to the name.
                sponsored: c.querySelector('.pc__sponsored, .icSponsored') ? 'SPONSORED'
                         : c.querySelector('.spotlightWrap') ? 'SPOTLIGHT' : '',
              };
            });
        """) or []
        store = self.store_index()
        # A "Recently Viewed" section (present only when this Chrome profile has
        # opened a hotel page before) is rendered *above* the results with the
        # same card markup and even reuses id Listing_hotel_0. It is not part of
        # the ranking, so drop those leading cards; rank is then DOM order.
        rv = {hid for hid, v in store.items() if v.get("section") == "RECENTLY_VIEWED_HOTELS"}
        while rows and rows[0]["id"] in rv:
            self.log(f"  skipping 'Recently Viewed' card: {rows[0]['Hotel_Name']}")
            rows.pop(0)
        out = []
        for i, r in enumerate(rows[:top], 1):
            seo = (store.get(r["id"]) or {}).get("seoUrl") or ""
            if not seo:
                self.log(f"  ! no seoUrl in store for {r['Hotel_Name']}; using search deeplink")
            out.append({
                "Rank": i,
                "Hotel_Name": r["Hotel_Name"],
                "Location": r["Location"],
                "Price": r["Price"] or ("SOLD OUT" if r["sold_out"] else ""),
                "HDP_Url": seo or r["deeplink"],
                "HDP_Deeplink": r["deeplink"],
                "Hotel_Id": r["id"],
                "Sponsored": r["sponsored"],
            })
        return out


# ---------------------------------------------------------------- main

def parse_args():
    today = date.today()
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--city", default="Goa", help="city / area / property name typed into the search box")
    p.add_argument("--dx", type=int, default=15, metavar="DAYS",
                   help="days from today to check-in: 0 = today, 1 = tomorrow … (default 15)")
    p.add_argument("--rn", type=int, default=2, metavar="NIGHTS", help="number of nights (default 2)")
    p.add_argument("--checkin", type=date.fromisoformat, default=None,
                   help="explicit YYYY-MM-DD; overrides --dx")
    p.add_argument("--checkout", type=date.fromisoformat, default=None,
                   help="explicit YYYY-MM-DD; overrides --rn (needs --checkin)")
    p.add_argument("--rooms", type=int, default=1, help="number of rooms (PAX config)")
    p.add_argument("--adults", type=int, default=2, help="number of adults")
    p.add_argument("--children", type=int, nargs="*", default=[], metavar="AGE",
                   help="one age (0-17) per child, e.g. --children 5 9; count = number of ages given")
    p.add_argument("--top", type=int, default=50, help="how many main SRP cards to capture")
    p.add_argument("--out", default=None, help="CSV path (default mmt_<city>_<checkin>.csv)")
    p.add_argument("--cdp-port", type=int, default=int(os.environ.get("MMT_CDP_PORT", 9333)))
    p.add_argument("--profile-dir", default=os.environ.get("MMT_CHROME_PROFILE", "/tmp/mmt-chrome"))
    p.add_argument("--daemon-name", default="mmt", help="browser-harness BU_NAME for this Chrome")
    p.add_argument("--keep-tab", action="store_true", help="leave the SRP tab open after scraping")
    a = p.parse_args()
    if a.dx < 0:
        p.error("--dx must be >= 0 (0 = today)")
    if a.rn < 1:
        p.error("--rn must be >= 1")
    if a.checkout and not a.checkin:
        p.error("--checkout needs --checkin")
    if a.checkin is None:
        a.checkin = today + timedelta(days=a.dx)
    if a.checkout is None:
        a.checkout = a.checkin + timedelta(days=a.rn)
    if a.checkout <= a.checkin:
        p.error("--checkout must be after --checkin")
    if a.checkin < today:
        p.error("--checkin is in the past")
    a.dx, a.rn = (a.checkin - today).days, (a.checkout - a.checkin).days
    if any(not 0 <= c <= 17 for c in a.children):
        p.error("child ages must be 0-17")
    if a.out is None:
        a.out = f"mmt_{re.sub(r'[^A-Za-z0-9]+', '_', a.city).strip('_').lower()}_{a.checkin:%Y%m%d}.csv"
    return a


def main():
    a = parse_args()
    print(f"MakeMyTrip SRP: {a.city}, {a.checkin} → {a.checkout} (DX={a.dx}, RN={a.rn}), {a.rooms} room(s), "
          f"{a.adults} adult(s), children ages {a.children or 'none'}, top {a.top}")
    h = connect(a.cdp_port, a.profile_dir, a.daemon_name)
    m = MMT(h)
    print("→ opening Hotels")
    m.open_hotels()
    print("→ city")
    m.set_city(a.city)
    print("→ dates")
    m.set_dates(a.checkin, a.checkout)
    print("→ guests")
    m.set_guests(a.rooms, a.adults, a.children)
    print("→ search")
    srp_url = m.search()
    print("→ loading cards")
    m.load_cards(a.top)
    rows = m.read_cards(a.top)

    fields = ["Rank", "Hotel_Name", "Location", "Price", "HDP_Url", "HDP_Deeplink", "Hotel_Id", "Sponsored"]
    with open(a.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    print(f"✓ {len(rows)} hotels → {a.out}")
    print(f"  captured {datetime.now():%Y-%m-%d %H:%M} from {srp_url}")
    if not a.keep_tab:
        try:
            h.cdp("Target.closeTarget", targetId=h.current_tab()["targetId"])
        except Exception:
            pass
    if len(rows) < a.top:
        print(f"! only {len(rows)} of {a.top} cards were available", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()

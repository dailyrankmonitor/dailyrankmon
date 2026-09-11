#!/usr/bin/env python3
"""Batch driver for mmt_srp.py: every city × every (DX, RN) × every PAX row.

Inputs (CSV with a header row, in config/ unless overridden):

  config/cities.csv   city_name             one city per line
  config/dxrn.csv     dx,rn                 days-ahead and nights per line
  config/pax.csv      adult,children,ages   ages = space-separated ints, one per
                                            child; missing ages default to 1.
                                            children may be 0 or blank.

Usage:
  python3 mmt_batch.py            # top 50
  python3 mmt_batch.py 20         # top 20
  python3 mmt_batch.py 20 --cities my_cities.csv --pax my_pax.csv

Output: runs/mmt_srp_run_<YYYYmmdd_HHMMSS>/ containing one CSV per combination,
named  mmt_srp_<city>_dx<DX>_rn<RN>_<A>a<C>c[_ages<a-b-c>]_top<N>.csv
plus manifest.csv (one line per combination: status, rows, dates, file),
run.log (the scraper's own output) and summary.txt (timing and counts, also
printed at the end). Rooms are always 1.

Runs are sequential on purpose: every run shares one Chrome and one harness
daemon, and the daemon holds a single attached tab.
"""
from __future__ import annotations

import argparse
import csv
import re
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRAPER = HERE / "core" / "mmt_srp.py"
CONFIG = HERE / "config"
RUNS = HERE / "runs"


def read_rows(path: Path) -> list[list[str]]:
    """CSV rows minus the header, blank lines dropped, cells stripped."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = [[c.strip() for c in r] for r in csv.reader(f)]
    rows = [r for r in rows if any(r)]
    if len(rows) < 2:
        sys.exit(f"{path}: no data rows (header only?)")
    return rows[1:]  # header


def load_cities(path: Path) -> list[str]:
    cities = [r[0] for r in read_rows(path) if r[0]]
    if not cities:
        sys.exit(f"{path}: no cities")
    return cities


def load_dxrn(path: Path) -> list[tuple[int, int]]:
    out = []
    for r in read_rows(path):
        try:
            dx, rn = int(r[0]), int(r[1])
        except (IndexError, ValueError):
            sys.exit(f"{path}: expected 'dx,rn' integers, got {r}")
        if dx < 0 or rn < 1:
            sys.exit(f"{path}: dx must be >= 0 and rn >= 1, got {r}")
        out.append((dx, rn))
    return out


def load_pax(path: Path) -> list[tuple[int, list[int]]]:
    """Each row → (adults, [child ages]). `children` may be blank/0; the ages
    column may be blank or shorter than `children` — missing ages are 1."""
    out = []
    for r in read_rows(path):
        r = r + ["", ""]  # tolerate rows with fewer columns
        try:
            adults = int(r[0])
            children = int(r[1]) if r[1] else 0
        except ValueError:
            sys.exit(f"{path}: expected 'adult,children,ages', got {r}")
        if adults < 1 or children < 0:
            sys.exit(f"{path}: adult must be >= 1 and children >= 0, got {r}")
        ages = [int(a) for a in re.split(r"[\s;|]+", r[2]) if a] if children else []
        if any(not 0 <= a <= 17 for a in ages):
            sys.exit(f"{path}: child ages must be 0-17, got {r}")
        ages = (ages + [1] * children)[:children]   # pad with 1, truncate extras
        out.append((adults, ages))
    return out


def slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_").lower()


def out_name(city: str, dx: int, rn: int, adults: int, ages: list[int], top: int) -> str:
    pax = f"{adults}a{len(ages)}c" + (f"_ages{'-'.join(map(str, ages))}" if ages else "")
    return f"mmt_srp_{slug(city)}_dx{dx}_rn{rn}_{pax}_top{top}.csv"


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("top", nargs="?", type=int, default=50, help="results per search (--top for mmt_srp.py), default 50")
    p.add_argument("--cities", default=str(CONFIG / "cities.csv"))
    p.add_argument("--dxrn", default=str(CONFIG / "dxrn.csv"))
    p.add_argument("--pax", default=str(CONFIG / "pax.csv"))
    p.add_argument("--runs-dir", default=str(RUNS), help="parent directory for run output (default runs/)")
    p.add_argument("--rooms", type=int, default=1, help="rooms per search (default 1)")
    a = p.parse_args()
    if a.top < 1:
        p.error("top must be >= 1")

    cities = load_cities(Path(a.cities))
    dxrn = load_dxrn(Path(a.dxrn))
    pax = load_pax(Path(a.pax))

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = Path(a.runs_dir) / f"mmt_srp_run_{stamp}"
    run_dir.mkdir(parents=True)
    log = open(run_dir / "run.log", "a", encoding="utf-8")
    combos = [(c, dx, rn, ad, ages) for c in cities for dx, rn in dxrn for ad, ages in pax]
    print(f"{len(cities)} cities × {len(dxrn)} dx/rn × {len(pax)} pax = {len(combos)} searches, top {a.top} → {run_dir}/")

    manifest = open(run_dir / "manifest.csv", "w", newline="", encoding="utf-8")
    mw = csv.writer(manifest)
    mw.writerow(["seq", "city", "dx", "rn", "checkin", "checkout", "rooms", "adults", "children", "ages",
                 "top", "status", "rows", "seconds", "file"])
    today = date.today()
    started = datetime.now()
    counts = {"ok": 0, "partial": 0, "FAILED": 0}
    total_rows = 0
    for i, (city, dx, rn, adults, ages) in enumerate(combos, 1):
        name = out_name(city, dx, rn, adults, ages, a.top)
        checkin, checkout = today + timedelta(days=dx), today + timedelta(days=dx + rn)
        cmd = [sys.executable, str(SCRAPER), "--city", city, "--dx", str(dx), "--rn", str(rn),
               "--rooms", str(a.rooms), "--adults", str(adults), "--top", str(a.top), "--out", str(run_dir / name)]
        if ages:
            cmd += ["--children", *map(str, ages)]
        print(f"[{i}/{len(combos)}] {city} dx={dx} rn={rn} {adults}a{len(ages)}c{('/' + ' '.join(map(str, ages))) if ages else ''} … ", end="", flush=True)
        log.write(f"\n===== [{i}/{len(combos)}] {' '.join(cmd)}\n"); log.flush()
        t0 = time.time()
        r = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, text=True)
        secs = round(time.time() - t0, 1)
        rows = 0
        f = run_dir / name
        if f.exists():
            with open(f, newline="", encoding="utf-8") as fh:
                rows = max(0, sum(1 for _ in fh) - 1)
        # mmt_srp exits 2 when it got fewer than --top; that's a partial, not a failure
        status = "ok" if r.returncode == 0 else ("partial" if r.returncode == 2 and rows else "FAILED")
        counts[status] += 1
        total_rows += rows
        print(f"{status} ({rows} rows, {secs}s)")
        mw.writerow([i, city, dx, rn, checkin, checkout, a.rooms, adults, len(ages), " ".join(map(str, ages)),
                     a.top, status, rows, secs, name if f.exists() else ""])
        manifest.flush()
    manifest.close(); log.close()

    finished = datetime.now()
    elapsed = finished - started
    n = len(combos)
    summary = "\n".join([
        "Run summary",
        f"  started    {started:%Y-%m-%d %H:%M:%S}",
        f"  finished   {finished:%Y-%m-%d %H:%M:%S}",
        f"  elapsed    {_hms(elapsed.total_seconds())}  ({elapsed.total_seconds() / n:.1f} s per search)",
        f"  searches   {n}  ({len(cities)} cities × {len(dxrn)} dx/rn × {len(pax)} pax, top {a.top})",
        f"  ok         {counts['ok']}",
        f"  partial    {counts['partial']}",
        f"  failed     {counts['FAILED']}",
        f"  rows       {total_rows}",
        f"  output     {run_dir}/",
    ])
    (run_dir / "summary.txt").write_text(summary + "\n", encoding="utf-8")
    print("\n" + summary)
    sys.exit(1 if counts["FAILED"] else 0)


def _hms(seconds: float) -> str:
    s = int(round(seconds))
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h}h {m:02d}m {s:02d}s" if h else f"{m}m {s:02d}s"


if __name__ == "__main__":
    main()

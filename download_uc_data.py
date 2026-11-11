"""
Download UC admissions (freshman/high school only) data from UCOP Tableau dashboards.
Downloads per-year, per-campus TSV files, then builds a SQLite database.

Views: FREthbyYr (race/ethnicity), FRGndbyYr (gender), FRGPAbyYr (GPA)
Campuses: Universitywide + 9 individual UC campuses
School types: CA public HS (default, already downloaded) + CA private HS
Years: 1994–2025

File naming:
  Public HS (existing):  {view_key}_{campus_slug}_{year}.tsv
  Private HS (new):      {view_key}_private_{campus_slug}_{year}.tsv

Usage:
  python3 download_uc_data.py                  # download private HS + build DB
  python3 download_uc_data.py --db-only        # rebuild DB from already-downloaded files
  python3 download_uc_data.py --public-only    # re-download public HS only
"""

import asyncio
import csv
import io
import sqlite3
import sys
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright

BASE_URL = "https://visualizedata.ucop.edu/t/Public/views/AdmissionsDataTable"
OUT_DIR = Path(__file__).parent / "raw_downloads"
DB_PATH = Path(__file__).parent / "uc_admissions.db"

YEARS = list(range(2025, 1993, -1))  # 2025 → 1994

VIEWS = {
    "race_ethnicity": "FREthbyYr",
    "gender":         "FRGndbyYr",
    "gpa":            "FRGPAbyYr",
}

# School type radio label → file slug
# Public HS was already downloaded (files: {view_key}_{campus_slug}_{year}.tsv)
# Private HS is new (files: {view_key}_private_{campus_slug}_{year}.tsv)
SCHOOL_TYPES = {
    "ca_public":  "California public high school",
    "ca_private": "California private high school",
}

# Campus display name → URL-safe slug
CAMPUSES = {
    "Universitywide": "universitywide",
    "Berkeley":       "berkeley",
    "Davis":          "davis",
    "Irvine":         "irvine",
    "Los Angeles":    "los_angeles",
    "Merced":         "merced",
    "Riverside":      "riverside",
    "San Diego":      "san_diego",
    "Santa Barbara":  "santa_barbara",
    "Santa Cruz":     "santa_cruz",
}


# ---------------------------------------------------------------------------
# Playwright helpers
# ---------------------------------------------------------------------------

async def load_view(page, view_name: str):
    await page.goto(f"{BASE_URL}/{view_name}", wait_until="domcontentloaded", timeout=30000)
    await asyncio.sleep(8)


async def select_school_type(page, label: str) -> bool:
    """Click a school-type radio button by its visible label text."""
    radio = await page.query_selector(f'[role="radio"]:has-text("{label}")')
    if radio:
        await radio.click()
        await asyncio.sleep(3)
        return True
    return False


async def select_campus(page, campus: str) -> bool:
    """Select a campus from the Campus combo box (index 1)."""
    combos = await page.query_selector_all('.tabComboBox')
    if len(combos) < 2:
        return False
    current = (await combos[1].inner_text()).strip()
    if current == campus:
        return True
    await combos[1].click()
    await asyncio.sleep(1)
    item = await page.query_selector(f'a:has-text("{campus}")')
    if item:
        await item.click()
        await asyncio.sleep(3)
        return True
    await page.keyboard.press("Escape")
    return False


async def get_current_year(page) -> str:
    combos = await page.query_selector_all('.tabComboBox')
    return (await combos[0].inner_text()).strip() if combos else ""


async def select_year(page, year: int) -> bool:
    if await get_current_year(page) == str(year):
        return True
    combos = await page.query_selector_all('.tabComboBox')
    if not combos:
        return False
    await combos[0].click()
    await asyncio.sleep(1)
    for sel in [f'a:has-text("{year}")', f'[role="option"]:has-text("{year}")']:
        item = await page.query_selector(sel)
        if item:
            await item.click()
            await asyncio.sleep(3)
            return True
    await page.keyboard.press("Escape")
    return False


async def download_crosstab_csv(page) -> Optional[bytes]:
    dl_btn = await page.query_selector('button:has-text("Download")')
    if not dl_btn:
        return None
    await dl_btn.click()
    await asyncio.sleep(1)
    crosstab = await page.query_selector('text="Crosstab"')
    if not crosstab:
        return None
    await crosstab.click()
    await asyncio.sleep(2)
    csv_label = await page.query_selector('label:has-text("CSV")')
    if csv_label:
        await csv_label.click()
        await asyncio.sleep(0.5)
    async with page.expect_download(timeout=30000) as dl_info:
        await page.evaluate("""
            () => {
                const btns = Array.from(document.querySelectorAll('button'));
                const dlBtns = btns.filter(b => b.textContent.trim() === 'Download');
                if (dlBtns.length) dlBtns[dlBtns.length - 1].click();
            }
        """)
        download = await dl_info.value
    with open(await download.path(), "rb") as f:
        return f.read()


async def close_dialog(page):
    for sel in ['[aria-label="Close"]', 'button:has-text("×")', 'button:has-text("Cancel")']:
        try:
            btn = await page.query_selector(sel)
            if btn:
                await btn.click()
                await asyncio.sleep(0.5)
                return
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Download one view × one campus (iterates over all years)
# ---------------------------------------------------------------------------

async def download_view_campus(playwright, view_key: str, view_name: str,
                               campus: str, school_type_key: str = "ca_public",
                               school_type_label: str = "California public high school"):
    slug = CAMPUSES[campus]
    # Public HS uses legacy naming; private uses _private_ prefix
    file_prefix = "" if school_type_key == "ca_public" else f"{school_type_key}_"
    browser = await playwright.chromium.launch(headless=True)
    context = await browser.new_context(accept_downloads=True)
    page = await context.new_page()

    downloaded = skipped = 0
    errors = []

    try:
        await load_view(page, view_name)
        if not await select_school_type(page, school_type_label):
            print(f"  [{view_key}/{slug}/{school_type_key}] Could not select school type")
        if not await select_campus(page, campus):
            print(f"  [{view_key}/{slug}/{school_type_key}] Could not select campus '{campus}'")

        for year in YEARS:
            out_path = OUT_DIR / f"{view_key}_{file_prefix}{slug}_{year}.tsv"
            if out_path.exists() and out_path.stat().st_size > 50:
                skipped += 1
                continue

            if not await select_year(page, year):
                errors.append(year)
                continue

            try:
                raw = await download_crosstab_csv(page)
            except Exception as e:
                errors.append(year)
                print(f"  [{view_key}/{slug}/{school_type_key}] {year}: ERROR {e}")
                await load_view(page, view_name)
                await select_school_type(page, school_type_label)
                await select_campus(page, campus)
                continue

            if raw and len(raw) > 50:
                out_path.write_bytes(raw)
                downloaded += 1
                rows = raw.decode("utf-16", errors="replace").count("\n")
                print(f"  [{view_key}/{slug}/{school_type_key}] {year}: {rows} rows")
            else:
                errors.append(year)

            await close_dialog(page)

    finally:
        await browser.close()

    return downloaded, skipped, errors


# ---------------------------------------------------------------------------
# Main download orchestrator — runs campuses in parallel batches
# ---------------------------------------------------------------------------

PARALLEL = 4  # concurrent browser instances per view

async def download_all(public: bool = True, private: bool = True):
    OUT_DIR.mkdir(exist_ok=True)

    campus_list = list(CAMPUSES.keys())
    types_to_run = []
    if public:
        types_to_run.append(("ca_public", SCHOOL_TYPES["ca_public"]))
    if private:
        types_to_run.append(("ca_private", SCHOOL_TYPES["ca_private"]))

    async with async_playwright() as p:
        for st_key, st_label in types_to_run:
            for view_key, view_name in VIEWS.items():
                print(f"\n{'='*60}")
                print(f"School type: {st_key}  |  View: {view_key} ({view_name})")

                for i in range(0, len(campus_list), PARALLEL):
                    batch = campus_list[i:i + PARALLEL]
                    results = await asyncio.gather(*[
                        download_view_campus(p, view_key, view_name, c, st_key, st_label)
                        for c in batch
                    ])
                    for campus, (dl, sk, err) in zip(batch, results):
                        slug = CAMPUSES[campus]
                        print(f"  {slug}: downloaded={dl} skipped={sk} errors={len(err)}")
                        if err:
                            print(f"    error years: {err}")

    print("\nAll downloads complete.")


# ---------------------------------------------------------------------------
# Database builder
# ---------------------------------------------------------------------------

def decode_tsv(raw: bytes) -> list:
    try:
        text = raw.decode("utf-16")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
    return list(csv.DictReader(io.StringIO(text), delimiter="\t"))


def parse_int(v) -> Optional[int]:
    s = str(v).replace(",", "").strip() if v else ""
    try:
        return int(s) if s else None
    except ValueError:
        return None


def parse_float(v) -> Optional[float]:
    s = str(v).replace(",", "").strip() if v else ""
    try:
        return float(s) if s else None
    except ValueError:
        return None


SLUG_TO_CAMPUS = {v: k for k, v in CAMPUSES.items()}


def build_database():
    print(f"\nBuilding SQLite database: {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.executescript("""
        DROP TABLE IF EXISTS race_ethnicity;
        DROP TABLE IF EXISTS gender;
        DROP TABLE IF EXISTS gpa;

        CREATE TABLE race_ethnicity (
            id                   INTEGER PRIMARY KEY,
            school               TEXT NOT NULL,
            school_id            TEXT,
            city                 TEXT,
            county_state_country TEXT,
            campus               TEXT,
            year                 INTEGER,
            count_type           TEXT,
            all_count            INTEGER,
            african_american     INTEGER,
            american_indian      INTEGER,
            hispanic_latinx      INTEGER,
            pacific_islander     INTEGER,
            asian                INTEGER,
            white                INTEGER,
            domestic_unknown     INTEGER,
            intl                 INTEGER
        );

        CREATE TABLE gender (
            id                   INTEGER PRIMARY KEY,
            school               TEXT NOT NULL,
            school_id            TEXT,
            city                 TEXT,
            county_state_country TEXT,
            campus               TEXT,
            year                 INTEGER,
            count_type           TEXT,
            all_count            INTEGER,
            female               INTEGER,
            male                 INTEGER,
            other_gender         INTEGER,
            unknown_gender       INTEGER
        );

        CREATE TABLE gpa (
            id                   INTEGER PRIMARY KEY,
            school               TEXT NOT NULL,
            school_id            TEXT,
            city                 TEXT,
            county_state_country TEXT,
            campus               TEXT,
            year                 INTEGER,
            app_gpa              REAL,
            adm_gpa              REAL,
            enr_gpa              REAL
        );

        CREATE INDEX idx_re_school_year   ON race_ethnicity(school, year);
        CREATE INDEX idx_re_year_campus   ON race_ethnicity(year, campus);
        CREATE INDEX idx_re_campus        ON race_ethnicity(campus);
        CREATE INDEX idx_gnd_school_year  ON gender(school, year);
        CREATE INDEX idx_gpa_school_year  ON gpa(school, year);
    """)

    re_rows, gnd_rows, gpa_rows = [], [], []
    tsv_files = sorted(OUT_DIR.glob("*.tsv"))
    campus_slugs = set(CAMPUSES.values())

    # Accepted school-type prefixes (empty string = legacy public HS files)
    # New private files: {view_key}_ca_private_{campus_slug}_{year}.tsv
    KNOWN_TYPE_PREFIXES = {"", "ca_private_"}

    def parse_filename(f):
        """Return (view_key, campus_slug, year) or None if not a recognised file."""
        stem = f.stem
        for vk in ("race_ethnicity", "gender", "gpa"):
            if not stem.startswith(vk):
                continue
            rest = stem[len(vk) + 1:]          # strip "race_ethnicity_" etc.
            parts = rest.split("_")
            year_str = parts[-1]
            if not year_str.isdigit():
                return None
            middle = "_".join(parts[:-1])       # e.g. "ca_private_universitywide"
            # Try each known type prefix
            for tp in KNOWN_TYPE_PREFIXES:
                if middle.startswith(tp):
                    slug = middle[len(tp):]
                    if slug in campus_slugs:
                        return vk, slug, int(year_str)
        return None

    seen = set()
    final_files = []
    for f in tsv_files:
        parsed = parse_filename(f)
        if parsed is None:
            continue
        vk, slug, year = parsed
        key = (vk, slug, year, f.stem)       # include stem so both public+private load
        if key not in seen:
            seen.add(key)
            final_files.append((f, vk, slug, year))

    print(f"Processing {len(final_files)} TSV files...")

    for f, view_key, slug, year in final_files:
        campus = SLUG_TO_CAMPUS.get(slug, slug)
        raw = f.read_bytes()
        try:
            rows = decode_tsv(raw)
        except Exception as e:
            print(f"  Decode error {f.name}: {e}")
            continue

        for row in rows:
            school = row.get("School", "").strip()
            if not school:
                continue
            school_id = row.get("Calculation1", "").strip()
            city = row.get("City", "").strip()
            county = (row.get("County/State/ Country") or
                      row.get("County/State/Country") or "").strip()

            if view_key == "race_ethnicity":
                re_rows.append((
                    school, school_id, city, county, campus, year,
                    row.get("Count", "").strip(),
                    parse_int(row.get("All")),
                    parse_int(row.get("African American")),
                    parse_int(row.get("American Indian")),
                    parse_int(row.get("Hispanic/ Latinx") or row.get("Hispanic/Latinx")),
                    parse_int(row.get("Pacific Islander")),
                    parse_int(row.get("Asian")),
                    parse_int(row.get("White")),
                    parse_int(row.get("Domestic Unknown")),
                    parse_int(row.get("Int'l")),
                ))
            elif view_key == "gender":
                gnd_rows.append((
                    school, school_id, city, county, campus, year,
                    row.get("Count", "").strip(),
                    parse_int(row.get("All")),
                    parse_int(row.get("Female")),
                    parse_int(row.get("Male")),
                    parse_int(row.get("Other")),
                    parse_int(row.get("Unknown")),
                ))
            elif view_key == "gpa":
                gpa_rows.append((
                    school, school_id, city, county, campus, year,
                    parse_float(row.get("App GPA")),
                    parse_float(row.get("Adm GPA")),
                    parse_float(row.get("Enrl GPA")),
                ))

    print(f"Inserting {len(re_rows):,} race/ethnicity rows...")
    cur.executemany("""
        INSERT INTO race_ethnicity
        (school, school_id, city, county_state_country, campus, year, count_type,
         all_count, african_american, american_indian, hispanic_latinx, pacific_islander,
         asian, white, domestic_unknown, intl)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, re_rows)

    print(f"Inserting {len(gnd_rows):,} gender rows...")
    cur.executemany("""
        INSERT INTO gender
        (school, school_id, city, county_state_country, campus, year, count_type,
         all_count, female, male, other_gender, unknown_gender)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    """, gnd_rows)

    print(f"Inserting {len(gpa_rows):,} GPA rows...")
    cur.executemany("""
        INSERT INTO gpa
        (school, school_id, city, county_state_country, campus, year,
         app_gpa, adm_gpa, enr_gpa)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, gpa_rows)

    conn.commit()
    conn.close()

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    print("\nDatabase summary:")
    for tbl in ("race_ethnicity", "gender", "gpa"):
        cur.execute(f"SELECT COUNT(*), MIN(year), MAX(year), COUNT(DISTINCT school), COUNT(DISTINCT campus) FROM {tbl}")
        n, miny, maxy, schools, campuses = cur.fetchone()
        print(f"  {tbl:20s}: {n:>10,} rows | {schools:>5,} schools | {campuses} campuses | years {miny}–{maxy}")
    conn.close()
    print(f"\nDatabase saved to: {DB_PATH}")


if __name__ == "__main__":
    if "--db-only" in sys.argv:
        build_database()
    elif "--public-only" in sys.argv:
        asyncio.run(download_all(public=True, private=False))
        build_database()
    else:
        # Default: skip public (already downloaded), only fetch private
        asyncio.run(download_all(public=False, private=True))
        build_database()

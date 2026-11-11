"""
Download CA private/parochial high school data from the UCOP Tableau
single-school lookup view (FREthbyHS / FRGndbyHS / FRGPAbyHS).

The bulk Crosstab only contains the ~1600 highest-volume schools (mostly public).
Private schools are accessible by name search in these three views.

Strategy:
  1. For each search pattern, open FREthbyHS (or gender/GPA variant), type the
     pattern, press Enter, download the Crosstab (which includes ALL schools
     matching the pattern, all years, all campuses).
  2. Parse downloaded TSV, skip schools already in DB.
  3. Insert new rows directly into uc_admissions.db.

Usage:
  python3 download_private_schools.py              # full run
  python3 download_private_schools.py --test       # 3 schools only
  python3 download_private_schools.py --db-only    # re-insert from saved TSVs
"""

import asyncio
import csv
import io
import re
import sqlite3
import sys
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright, Page, BrowserContext

OUT_DIR  = Path(__file__).parent / "raw_private"
DB_PATH  = Path(__file__).parent / "uc_admissions.db"

TABLEAU_BASE = "https://visualizedata.ucop.edu"
VIEW_BASE    = f"{TABLEAU_BASE}/t/Public/views/AdmissionsDataTable"

HS_VIEWS = {
    "race_ethnicity": "FREthbyHS",
    "gender":         "FRGndbyHS",
    "gpa":            "FRGPAbyHS",
}

# ─────────────────────────────────────────────────────────────────────────────
# Search patterns — each downloads a crosstab with ALL matching schools.
# Prefer patterns that predominantly match private/parochial schools.
# Patterns that match schools already in the DB are harmless (skipped on insert).
# ─────────────────────────────────────────────────────────────────────────────

SEARCH_PATTERNS = [
    # Religious / Catholic / parochial (mostly private)
    "SAINT IGNATIUS",
    "SACRED HEART",
    "BISHOP",
    "ARCHBISHOP",
    "CARDINAL",
    "NOTRE DAME",
    "DE LA SALLE",
    "LOYOLA",
    "SALESIAN",
    "FRANCISCAN",
    "DOMINICAN",
    "JESUIT",
    "CHRISTIAN BROTHERS",
    "MERCY HIGH",
    "IMMACULATE",
    "HOLY NAMES",
    "HOLY TRINITY",
    "MARIN CATHOLIC",
    "CARONDELET",
    "SAINT FRANCIS",
    "SAINT MARY",
    "SAINT JOSEPH",
    "SAINT PATRICK",
    "SAINT ELIZABETH",
    "SAINT CATHERINE",
    "SAINT JOHN",
    "SAINT ANDREW",
    "SAINT THOMAS",
    "SAINT PETER",
    "SAINT PAUL",
    "SAINT MARGARET",
    "SAINT AGNES",
    "SAINT MONICA",
    "SETON",
    "VERBUM DEI",
    "OUR LADY",
    "ASSUMPTION",
    "INCARNATION",
    "CALVARY",
    "COVENANT CHRISTIAN",
    "TRINITY CHRISTIAN",
    "VALLEY CHRISTIAN",
    "FAITH CHRISTIAN",
    "CHRISTIAN HIGH",
    "FLINTRIDGE SACRED",
    "FLINTRIDGE PREP",
    "JUNIPERO SERRA",
    "SERRA HIGH",
    "RIORDAN",
    "BELLARMINE",
    "MITTY",
    # Well-known independent/secular private schools
    "LICK-WILMERDING",
    "HARKER",
    "MENLO SCHOOL",
    "CASTILLEJA",
    "CRYSTAL SPRINGS",
    "WOODSIDE PRIORY",
    "MARIN ACADEMY",
    "URBAN SCHOOL",
    "HEAD-ROYCE",
    "COLLEGE PREP SCHOOL",
    "COLLEGE PREPARATORY SCHOOL",
    "BRANSON",
    "CHADWICK",
    "CAMPBELL HALL",
    "BRENTWOOD SCHOOL",
    "CROSSROADS SCHOOL",
    "MARYMOUNT",
    "VIEWPOINT SCHOOL",
    "VILLANOVA PREP",
    "OAK PARK SCHOOL",
    "DREW SCHOOL",
    "NUEVA SCHOOL",
    "ATHENIAN SCHOOL",
    "PINEWOOD SCHOOL",
    "STRATFORD",
    "QUARRY LANE",
    "BENTLEY SCHOOL",
    "COLLEGE PREPARATORY",
    "PACIFIC RIDGE",
    "BISHOPS SCHOOL",
    "LA JOLLA COUNTRY DAY",
    "FRANCIS PARKER",
    "TORREY PINES CHRISTIAN",
    "WEBB SCHOOL",
    "THACHER SCHOOL",
    "CATE SCHOOL",
    "DUNN SCHOOL",
    "OAKS CHRISTIAN",
    "WINDWARD SCHOOL",
    "MILKEN COMMUNITY",
    "CHAMINADE COLLEGE PREP",
    "LOYOLA HIGH",
    "LOYOLA HS",
    "MARYMOUNT HIGH",
    "WESTRIDGE SCHOOL",
    "POLY PREP",
    "POLYTECHNIC SCHOOL",
    "FLINTRIDGE PREP",
    "CALTECH PREP",
    "PALM VALLEY",
    "FAITH LUTHERAN",
    "CALVARY CHAPEL",
    "LIBERTY CHRISTIAN",
    "HORIZON CHRISTIAN",
    "GRACE CHRISTIAN",
    "NORTHERN CALIFORNIA CHRISTIAN",
    "SAN DIEGO JEWISH",
    "JEWISH COMMUNITY",
    "JEWISH HIGH",
    "HILLEL",
    "KEHILLAH",
    "YESHIVA",
    "YAVNEH",
    "BRANDEIS",
    "JEWISH COMMUNITY HIGH",
    "MILTON HIGH",
    "INTERNATIONAL SCHOOL",
    "FRENCH AMERICAN",
    "GERMAN AMERICAN",
    "CHINESE AMERICAN",
    "CHINESE CHRISTIAN",
    "KOREAN CHRISTIAN",
    "WESTWOOD HIGH",
    "SAGE HILL",
    "TESORO HIGH",
    "JSerra",
    "JSERRA",
    "CAPISTRANO VALLEY CHRISTIAN",
    "SAN JUAN HILLS",
    "OCEAN VIEW CHRISTIAN",
    "SANTA MARGARITA CATHOLIC",
    "SERVITE",
    "MATER DEI",
    "ROSARY",
    "CORNELIA CONNELLY",
    # More LA/SoCal private schools
    "HARVARD-WESTLAKE",
    "LOYOLA HIGH SCHOOL LOS ANGELES",
    "ALEMANY",
    "CRESPI CARMELITE",
    "PARACLETE",
    "BISHOP GARCIA DIEGO",
    "GRACE BRETHREN",
    "VERITAS PREP",
    "SONOMA ACADEMY",
    # More NorCal private schools
    "JUSTIN-SIENA",
    "CARDINAL NEWMAN",
    "PRESENTATION HIGH",
    "MERCY HIGH SCHOOL",
    "SACRED HEART PREP",
    "NOTRE DAME HIGH SAN JOSE",
    "VALLEY CHRISTIAN HIGH",
    "MITTY HIGH",
    "O'DOWD",
    "RIORDAN HIGH",
]

# Deduplicate while preserving order
seen_pats = set()
SEARCH_PATTERNS = [
    p for p in SEARCH_PATTERNS
    if not (p.lower() in seen_pats or seen_pats.add(p.lower()))
]


# ─────────────────────────────────────────────────────────────────────────────
# Playwright helpers
# ─────────────────────────────────────────────────────────────────────────────

async def load_and_search(ctx: BrowserContext, view_name: str, pattern: str) -> Optional[bytes]:
    """
    Open view_name, type pattern, press Enter, download the Crosstab CSV.
    Returns raw bytes or None on failure.
    """
    page = await ctx.new_page()
    try:
        url = f"{VIEW_BASE}/{view_name}?:embed=y&:showVizHome=no&:toolbar=top"
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass
        await asyncio.sleep(10)

        # Find visible QueryBox
        visible_box = await _find_search_box(page)
        if not visible_box:
            return None

        # Clear, type, enter
        await visible_box.click(click_count=3, timeout=10000)
        await asyncio.sleep(0.3)
        await page.keyboard.type(pattern)
        await asyncio.sleep(0.5)
        await page.keyboard.press("Enter")
        await asyncio.sleep(6)

        # Confirm data appeared
        content = await page.content()
        if "App" not in content and "Adm" not in content and "GPA" not in content:
            return None

        return await _download_crosstab(page)
    finally:
        await page.close()


async def _find_search_box(page: Page):
    boxes = await page.query_selector_all('.QueryBox')
    for b in boxes:
        vis = await b.is_visible()
        bb  = await b.bounding_box()
        if vis and bb and bb['width'] > 50:
            return b
    return None


async def _download_crosstab(page: Page) -> Optional[bytes]:
    # Open Download menu
    btn = await page.query_selector('button:has-text("Download")')
    if not btn:
        return None
    await btn.click()
    await asyncio.sleep(1.5)

    # Click Crosstab
    ct = await page.query_selector('text="Crosstab"')
    if not ct:
        return None
    await ct.click()
    await asyncio.sleep(2)

    # Select CSV
    csv_lbl = await page.query_selector('label:has-text("CSV")')
    if csv_lbl:
        await csv_lbl.click()
        await asyncio.sleep(0.5)

    # JS-click the final Download button (bypasses glass overlay)
    try:
        async with page.expect_download(timeout=30000) as dl_info:
            await page.evaluate("""() => {
                const btns = Array.from(document.querySelectorAll('button'));
                const dl = btns.filter(b => b.textContent.trim() === 'Download');
                if (dl.length) dl[dl.length - 1].click();
            }""")
        download = await dl_info.value
        with open(await download.path(), "rb") as f:
            return f.read()
    except Exception as e:
        print(f"    download error: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# TSV parsing
# ─────────────────────────────────────────────────────────────────────────────

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


# ─────────────────────────────────────────────────────────────────────────────
# Database insertion — skips rows already present
# ─────────────────────────────────────────────────────────────────────────────

def get_existing_schools(conn) -> set:
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT school FROM race_ethnicity")
    return {r[0] for r in cur.fetchall()}


def insert_race_ethnicity(conn, rows: list, existing: set) -> int:
    cur = conn.cursor()
    inserted = 0
    for row in rows:
        school = row.get("School", "").strip()
        if not school or school in existing:
            continue
        year = row.get("Fall Term", "").strip()
        campus = row.get("Campus", "").strip()
        count_type = row.get("Count", "").strip()
        if not year or not count_type:
            continue
        try:
            cur.execute("""
                INSERT OR IGNORE INTO race_ethnicity
                (school, school_id, city, county_state_country, campus, year,
                 count_type, all_count, african_american, american_indian,
                 hispanic_latinx, pacific_islander, asian, white,
                 domestic_unknown, intl)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                school,
                row.get("Calculation1", "").strip(),
                row.get("City", "").strip(),
                (row.get("County/State/ Country") or row.get("County/State/Country") or "").strip(),
                campus,
                parse_int(year),
                count_type,
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
            if cur.rowcount:
                inserted += 1
        except Exception:
            pass
    conn.commit()
    return inserted


def insert_gender(conn, rows: list, existing: set) -> int:
    cur = conn.cursor()
    inserted = 0
    for row in rows:
        school = row.get("School", "").strip()
        if not school or school in existing:
            continue
        year = row.get("Fall Term", "").strip()
        campus = row.get("Campus", "").strip()
        count_type = row.get("Count", "").strip()
        if not year or not count_type:
            continue
        try:
            cur.execute("""
                INSERT OR IGNORE INTO gender
                (school, school_id, city, county_state_country, campus, year,
                 count_type, all_count, female, male, other_gender, unknown_gender)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                school,
                row.get("Calculation1", "").strip(),
                row.get("City", "").strip(),
                (row.get("County/State/Country") or row.get("County/State/ Country") or "").strip(),
                campus,
                parse_int(year),
                count_type,
                parse_int(row.get("All")),
                parse_int(row.get("Female")),
                parse_int(row.get("Male")),
                parse_int(row.get("Other")),
                parse_int(row.get("Unknown")),
            ))
            if cur.rowcount:
                inserted += 1
        except Exception:
            pass
    conn.commit()
    return inserted


def insert_gpa(conn, rows: list, existing_gpa: set) -> int:
    cur = conn.cursor()
    inserted = 0
    for row in rows:
        school = row.get("School", "").strip()
        if not school or school in existing_gpa:
            continue
        year = (row.get("Fall term") or row.get("Fall Term") or "").strip()
        campus = row.get("Campus", "").strip()
        if not year:
            continue
        try:
            cur.execute("""
                INSERT OR IGNORE INTO gpa
                (school, school_id, city, county_state_country, campus, year,
                 app_gpa, adm_gpa, enr_gpa)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (
                school,
                row.get("Calculation1", "").strip(),
                row.get("City", "").strip(),
                (row.get("County/State/ Country") or row.get("County/State/Country") or "").strip(),
                campus,
                parse_int(year),
                parse_float(row.get("App GPA")),
                parse_float(row.get("Adm GPA")),
                parse_float(row.get("Enrl GPA")),
            ))
            if cur.rowcount:
                inserted += 1
        except Exception:
            pass
    conn.commit()
    return inserted


# ─────────────────────────────────────────────────────────────────────────────
# Main download loop — parallel pattern downloads, sequential DB inserts
# ─────────────────────────────────────────────────────────────────────────────

PARALLEL = 4  # concurrent browser contexts


async def download_one_pattern(pw, sem: asyncio.Semaphore, pattern: str, idx: int, total: int):
    """Download all 3 views for one pattern, respecting the concurrency semaphore."""
    slug = re.sub(r'[^A-Z0-9]', '_', pattern.upper())[:40]

    async with sem:
        results = {}  # view_key -> bytes|None
        for view_key, view_name in HS_VIEWS.items():
            out_file = OUT_DIR / f"{view_key}_{slug}.tsv"
            if out_file.exists() and out_file.stat().st_size > 200:
                results[view_key] = out_file.read_bytes()
                print(f"  [{idx}/{total} {pattern[:30]}] {view_key}: cached")
                continue

            browser = await pw.chromium.launch(headless=True)
            ctx = await browser.new_context(accept_downloads=True,
                                            viewport={"width": 1400, "height": 900})
            raw = await load_and_search(ctx, view_name, pattern)
            await ctx.close()
            await browser.close()

            if raw:
                out_file.write_bytes(raw)
                results[view_key] = raw
                print(f"  [{idx}/{total} {pattern[:30]}] {view_key}: {len(raw):,} bytes")
            else:
                results[view_key] = None
                print(f"  [{idx}/{total} {pattern[:30]}] {view_key}: no data")

        return pattern, results


async def run_downloads(patterns):
    OUT_DIR.mkdir(exist_ok=True)

    # Phase 1: Download all TSV files in parallel
    sem = asyncio.Semaphore(PARALLEL)
    async with async_playwright() as pw:
        tasks = [
            download_one_pattern(pw, sem, p, i + 1, len(patterns))
            for i, p in enumerate(patterns)
        ]
        all_results = await asyncio.gather(*tasks, return_exceptions=True)

    # Phase 2: Insert into DB sequentially
    conn = sqlite3.connect(DB_PATH)
    existing_re  = get_existing_schools(conn)
    existing_gnd = existing_re.copy()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT school FROM gpa")
    existing_gpa = {r[0] for r in cur.fetchall()}

    print(f"\nDB has {len(existing_re)} schools already. Inserting new data...")

    total_re = total_gnd = total_gpa = 0
    new_schools: set = set()

    for result in all_results:
        if isinstance(result, Exception):
            print(f"  Error: {result}")
            continue
        pattern, view_data = result
        for view_key, raw in view_data.items():
            if not raw:
                continue
            rows = decode_tsv(raw)
            schools_in_file = {r.get("School", "").strip() for r in rows if r.get("School", "").strip()}
            new_in_file = schools_in_file - existing_re

            if view_key == "race_ethnicity":
                n = insert_race_ethnicity(conn, rows, existing_re)
                total_re += n
                if n:
                    existing_re.update(new_in_file)
                    new_schools.update(new_in_file)
            elif view_key == "gender":
                n = insert_gender(conn, rows, existing_gnd)
                total_gnd += n
                if n:
                    existing_gnd.update(new_in_file)
            elif view_key == "gpa":
                n = insert_gpa(conn, rows, existing_gpa)
                total_gpa += n
                if n:
                    existing_gpa.update(new_in_file)

    conn.close()

    print(f"\n{'='*60}")
    print(f"DONE — {len(new_schools)} new schools added:")
    for s in sorted(new_schools)[:50]:
        print(f"  {s}")
    print(f"\nRows inserted:")
    print(f"  race_ethnicity: {total_re:,}")
    print(f"  gender:         {total_gnd:,}")
    print(f"  gpa:            {total_gpa:,}")


def db_only():
    """Re-insert from any TSV files already in OUT_DIR."""
    OUT_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    existing_re  = get_existing_schools(conn)
    existing_gnd = existing_re.copy()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT school FROM gpa")
    existing_gpa = {r[0] for r in cur.fetchall()}

    for f in sorted(OUT_DIR.glob("*.tsv")):
        rows = decode_tsv(f.read_bytes())
        if not rows:
            continue
        if f.stem.startswith("race_ethnicity"):
            n = insert_race_ethnicity(conn, rows, existing_re)
        elif f.stem.startswith("gender"):
            n = insert_gender(conn, rows, existing_gnd)
        elif f.stem.startswith("gpa"):
            n = insert_gpa(conn, rows, existing_gpa)
        else:
            continue
        if n:
            print(f"  {f.name}: +{n} rows")
    conn.close()


if __name__ == "__main__":
    if "--db-only" in sys.argv:
        db_only()
    elif "--test" in sys.argv:
        # Quick test with 3 specific schools
        asyncio.run(run_downloads([
            "SAINT IGNATIUS COLLEGE PREP",
            "SACRED HEART CATHEDRAL",
            "LICK-WILMERDING",
        ]))
    else:
        asyncio.run(run_downloads(SEARCH_PATTERNS))

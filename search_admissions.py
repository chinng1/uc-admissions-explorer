"""
Search the UC admissions SQLite database.

Usage examples:
  python3 search_admissions.py --school "Lowell"
  python3 search_admissions.py --school "Mission San Jose" --year 2023
  python3 search_admissions.py --school "Palo Alto" --type race
  python3 search_admissions.py --county "Santa Clara" --year 2024
  python3 search_admissions.py --list-schools "Berkeley"
  python3 search_admissions.py --info
"""

import argparse
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).parent / "uc_admissions.db"


def get_db():
    if not DB_PATH.exists():
        print(f"Database not found: {DB_PATH}")
        print("Run download_uc_data.py first.")
        sys.exit(1)
    return sqlite3.connect(DB_PATH)


def list_schools(pattern: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT school, city, county_state_country,
               MIN(year), MAX(year)
        FROM race_ethnicity
        WHERE school LIKE ?
        GROUP BY school, city, county_state_country
        ORDER BY school
        LIMIT 50
    """, (f"%{pattern.upper()}%",))
    rows = cur.fetchall()
    conn.close()
    if not rows:
        print(f"No schools found matching '{pattern}'")
        return
    print(f"\n{'School':<50} {'City':<20} {'County':<20} {'First Yr'} {'Last Yr'}")
    print("-" * 115)
    for school, city, county, miny, maxy in rows:
        print(f"{school:<50} {city:<20} {county:<20} {miny}     {maxy}")
    print(f"\n{len(rows)} school(s) found.")


def show_race_ethnicity(school: str, year: int = None):
    conn = get_db()
    cur = conn.cursor()
    conditions = ["school LIKE ?"]
    params = [f"%{school.upper()}%"]
    if year:
        conditions.append("year = ?")
        params.append(year)
    where = " AND ".join(conditions)
    cur.execute(f"""
        SELECT school, city, county_state_country, year, count_type,
               all_count, african_american, american_indian, hispanic_latinx,
               pacific_islander, asian, white, domestic_unknown, intl
        FROM race_ethnicity
        WHERE {where}
        ORDER BY school, year DESC, count_type
    """, params)
    rows = cur.fetchall()
    conn.close()
    if not rows:
        print(f"No race/ethnicity data for '{school}'")
        return
    print(f"\n{'School':<35} {'County':<18} {'Year'} {'Ct':<5} {'All':>5} {'AfAm':>5} {'AmInd':>6} {'Hisp':>6} {'PacIs':>6} {'Asian':>6} {'White':>6} {'DomUnk':>7} \"Int'l\"")
    print("-" * 130)
    for r in rows:
        school_s, city, county, yr, ct, total, aa, ai, hl, pi, asian, white, du, intl = r
        vals = [total, aa, ai, hl, pi, asian, white, du, intl]
        vals_s = [str(v) if v is not None else "-" for v in vals]
        print(f"{school_s[:35]:<35} {county[:18]:<18} {yr} {ct:<5} {vals_s[0]:>5} {vals_s[1]:>5} {vals_s[2]:>6} {vals_s[3]:>6} {vals_s[4]:>6} {vals_s[5]:>6} {vals_s[6]:>6} {vals_s[7]:>7} {vals_s[8]:>6}")
    print(f"\n{len(rows)} rows.")


def show_gender(school: str, year: int = None):
    conn = get_db()
    cur = conn.cursor()
    conditions = ["school LIKE ?"]
    params = [f"%{school.upper()}%"]
    if year:
        conditions.append("year = ?")
        params.append(year)
    where = " AND ".join(conditions)
    cur.execute(f"""
        SELECT school, city, year, count_type,
               all_count, female, male, other_gender, unknown_gender
        FROM gender
        WHERE {where}
        ORDER BY school, year DESC, count_type
    """, params)
    rows = cur.fetchall()
    conn.close()
    if not rows:
        print(f"No gender data for '{school}'")
        return
    print(f"\n{'School':<40} {'City':<18} {'Year'} {'Count':<6} {'All':>6} {'Female':>8} {'Male':>6} {'Other':>6} {'Unknown':>8}")
    print("-" * 115)
    for r in rows:
        school_s, city, yr, ct, total, f, m, o, u = r
        print(f"{school_s[:40]:<40} {city[:18]:<18} {yr} {ct:<6} {total or '-':>6} {f or '-':>8} {m or '-':>6} {o or '-':>6} {u or '-':>8}")
    print(f"\n{len(rows)} rows.")


def show_gpa(school: str, year: int = None):
    conn = get_db()
    cur = conn.cursor()
    conditions = ["school LIKE ?"]
    params = [f"%{school.upper()}%"]
    if year:
        conditions.append("year = ?")
        params.append(year)
    where = " AND ".join(conditions)
    cur.execute(f"""
        SELECT school, city, county_state_country, year,
               app_gpa, adm_gpa, enr_gpa
        FROM gpa
        WHERE {where}
        ORDER BY school, year DESC
    """, params)
    rows = cur.fetchall()
    conn.close()
    if not rows:
        print(f"No GPA data for '{school}'")
        return
    print(f"\n{'School':<45} {'City':<18} {'County':<18} {'Year'} {'App GPA':>8} {'Adm GPA':>8} {'Enr GPA':>8}")
    print("-" * 120)
    for r in rows:
        school_s, city, county, yr, app, adm, enr = r
        app_s = f"{app:.2f}" if app else "-"
        adm_s = f"{adm:.2f}" if adm else "-"
        enr_s = f"{enr:.2f}" if enr else "-"
        print(f"{school_s[:45]:<45} {city[:18]:<18} {county[:18]:<18} {yr} {app_s:>8} {adm_s:>8} {enr_s:>8}")
    print(f"\n{len(rows)} rows.")


def show_summary(school: str, year: int = None):
    conn = get_db()
    cur = conn.cursor()
    year_clause = f"AND g.year = {year}" if year else ""
    cur.execute(f"""
        SELECT g.year, g.count_type, g.all_count,
               g.female, g.male,
               r.african_american, r.american_indian, r.hispanic_latinx,
               r.pacific_islander, r.asian, r.white, r.intl,
               gp.app_gpa, gp.adm_gpa, gp.enr_gpa
        FROM gender g
        LEFT JOIN race_ethnicity r
            ON r.school = g.school AND r.year = g.year AND r.count_type = g.count_type
        LEFT JOIN gpa gp
            ON gp.school = g.school AND gp.year = g.year
        WHERE g.school LIKE ?
        {year_clause}
        ORDER BY g.year DESC, g.count_type
    """, (f"%{school.upper()}%",))
    rows = cur.fetchall()
    conn.close()
    if not rows:
        print(f"No data found for '{school}'")
        return

    current_year = None
    for r in rows:
        yr, ct, total, female, male, aa, ai, hl, pi, asian, white, intl, app_gpa, adm_gpa, enr_gpa = r
        if yr != current_year:
            print(f"\n{'='*70}")
            print(f"Year: {yr}")
            print(f"{'='*70}")
            current_year = yr
        gpa_str = ""
        if ct == "App" and app_gpa:
            gpa_str = f"  App GPA: {app_gpa:.2f}"
        elif ct == "Adm" and adm_gpa:
            gpa_str = f"  Adm GPA: {adm_gpa:.2f}"
        elif ct == "Enr" and enr_gpa:
            gpa_str = f"  Enr GPA: {enr_gpa:.2f}"
        print(f"  {ct}: n={total or '-':<6}  F={female or '-'}  M={male or '-'}  |  "
              f"Asian={asian or '-'}  Hisp={hl or '-'}  White={white or '-'}  "
              f"AfAm={aa or '-'}  Int'l={intl or '-'}{gpa_str}")


def show_county(county: str, year: int, count_type: str = "App"):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT r.school, r.city, r.all_count,
               r.hispanic_latinx, r.asian, r.white, r.african_american,
               g.app_gpa
        FROM race_ethnicity r
        LEFT JOIN gpa g ON g.school = r.school AND g.year = r.year
        WHERE r.county_state_country LIKE ?
          AND r.year = ?
          AND r.count_type = ?
        ORDER BY r.all_count DESC
        LIMIT 40
    """, (f"%{county}%", year, count_type))
    rows = cur.fetchall()
    conn.close()
    if not rows:
        print(f"No data for county '{county}', year {year}")
        return
    print(f"\n{count_type} data — {county} county, {year} (top 40 by volume):")
    print(f"\n{'School':<45} {'City':<18} {'All':>5} {'Hisp':>6} {'Asian':>6} {'White':>6} {'AfAm':>6} {'App GPA':>8}")
    print("-" * 105)
    for r in rows:
        school, city, total, hisp, asian, white, aa, gpa = r
        gpa_s = f"{gpa:.2f}" if gpa else "-"
        print(f"{school[:45]:<45} {city[:18]:<18} {total or '-':>5} {hisp or '-':>6} {asian or '-':>6} {white or '-':>6} {aa or '-':>6} {gpa_s:>8}")


def db_info():
    conn = get_db()
    cur = conn.cursor()
    size_mb = DB_PATH.stat().st_size / 1e6
    print(f"\nDatabase: {DB_PATH}  ({size_mb:.1f} MB)\n")
    for tbl in ("race_ethnicity", "gender", "gpa"):
        cur.execute(f"SELECT COUNT(*), MIN(year), MAX(year), COUNT(DISTINCT school) FROM {tbl}")
        n, miny, maxy, schools = cur.fetchone()
        print(f"  {tbl:20s}: {n:>8,} rows | {schools:>5,} schools | years {miny}–{maxy}")
    conn.close()


def main():
    parser = argparse.ArgumentParser(description="Search UC admissions database")
    parser.add_argument("--school", help="School name (partial match, case-insensitive)")
    parser.add_argument("--year", type=int, help="Filter to specific year")
    parser.add_argument("--type", choices=["race", "gender", "gpa", "summary", "all"],
                        default="summary", help="Data type to show (default: summary)")
    parser.add_argument("--county", help="Show top schools in a county for a given year")
    parser.add_argument("--list-schools", metavar="PATTERN", help="List schools matching pattern")
    parser.add_argument("--info", action="store_true", help="Show database statistics")
    args = parser.parse_args()

    if args.info:
        db_info()
        return

    if args.list_schools:
        list_schools(args.list_schools)
        return

    if args.county:
        year = args.year or 2024
        show_county(args.county, year)
        return

    if not args.school:
        parser.print_help()
        return

    dtype = args.type
    if dtype in ("summary", "all"):
        show_summary(args.school, args.year)
    if dtype in ("race", "all"):
        show_race_ethnicity(args.school, args.year)
    if dtype in ("gender", "all"):
        show_gender(args.school, args.year)
    if dtype in ("gpa", "all"):
        show_gpa(args.school, args.year)


if __name__ == "__main__":
    main()

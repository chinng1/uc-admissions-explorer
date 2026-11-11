"""
UC Admissions Explorer — Streamlit app.
Run: streamlit run explorer.py
"""

import gzip
import shutil
import sqlite3
import urllib.request
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

DB_PATH = Path(__file__).parent / "uc_admissions.db"
DB_URL = ("https://github.com/chinng1/uc-admissions-explorer/releases/download/"
          "data-v1/uc_admissions.db.gz")

# California counties have Title Case names; out-of-state entries are 2-letter
# state codes (IL, WA) or ALL-CAPS country names (THAILAND, JAPAN).
CA_FILTER = "county_state_country GLOB '[A-Z][a-z]*'"

# ---------------------------------------------------------------------------
# Data helpers (cached)
# ---------------------------------------------------------------------------

def ensure_db():
    if DB_PATH.exists():
        return
    tmp = DB_PATH.with_suffix(".db.part")
    with st.spinner("First start: downloading admissions database (~100 MB)…"):
        with urllib.request.urlopen(DB_URL) as resp, gzip.GzipFile(fileobj=resp) as gz, \
                open(tmp, "wb") as out:
            shutil.copyfileobj(gz, out, length=1 << 20)
        conn = sqlite3.connect(tmp)
        conn.executescript("""
            CREATE INDEX idx_re_school_year ON race_ethnicity(school, year);
            CREATE INDEX idx_re_year_campus ON race_ethnicity(year, campus);
            CREATE INDEX idx_re_campus      ON race_ethnicity(campus);
            CREATE INDEX idx_gnd_school_year ON gender(school, year);
            CREATE INDEX idx_gpa_school_year ON gpa(school, year);
        """)
        conn.close()
        tmp.rename(DB_PATH)


@st.cache_resource
def get_conn():
    ensure_db()
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    return conn


@st.cache_data
def load_campuses():
    conn = get_conn()
    try:
        df = pd.read_sql("""
            SELECT DISTINCT campus FROM race_ethnicity
            WHERE campus IS NOT NULL AND campus != ''
            ORDER BY campus
        """, conn)
        campuses = df["campus"].tolist()
        # Put Universitywide first if present
        if "Universitywide" in campuses:
            campuses = ["Universitywide"] + [c for c in campuses if c != "Universitywide"]
        return campuses
    except Exception:
        return []


@st.cache_data
def load_counties(campus: str = "Universitywide"):
    conn = get_conn()
    has_campus = _has_campus_col()
    campus_clause = "AND campus = ?" if has_campus and campus else ""
    params = [campus] if (has_campus and campus) else []
    df = pd.read_sql(f"""
        SELECT DISTINCT county_state_country AS county
        FROM race_ethnicity
        WHERE {CA_FILTER}
        {campus_clause}
        ORDER BY county_state_country
    """, conn, params=params if params else None)
    return df["county"].tolist()


@st.cache_data
def load_cities(county: str, campus: str = "Universitywide"):
    conn = get_conn()
    has_campus = _has_campus_col()
    conditions = ["city != ''", CA_FILTER]
    params = []
    if county and county != "All counties":
        conditions.append("county_state_country = ?")
        params.append(county)
    if has_campus and campus:
        conditions.append("campus = ?")
        params.append(campus)
    where = "WHERE " + " AND ".join(conditions)
    df = pd.read_sql(f"""
        SELECT DISTINCT city FROM race_ethnicity {where} ORDER BY city
    """, conn, params=params if params else None)
    return df["city"].tolist()


def _has_campus_col():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(race_ethnicity)")
    cols = {row[1] for row in cur.fetchall()}
    return "campus" in cols


@st.cache_data
def search_schools(query: str = "", county: str = "", city: str = "",
                   campus: str = "Universitywide") -> pd.DataFrame:
    conn = get_conn()
    has_campus = _has_campus_col()
    conditions = ["r.count_type = 'App'", CA_FILTER.replace("county_state_country", "r.county_state_country")]
    params = []
    if query:
        conditions.append("r.school LIKE ?")
        params.append(f"%{query.upper()}%")
    if county and county != "All counties":
        conditions.append("r.county_state_country = ?")
        params.append(county)
    if city and city != "All cities":
        conditions.append("r.city = ?")
        params.append(city)
    if has_campus and campus:
        conditions.append("r.campus = ?")
        params.append(campus)
    where = "WHERE " + " AND ".join(conditions)

    campus_join_clause = "AND g.campus = r.campus" if has_campus else ""
    campus_subq_clause = f"AND r2.campus = '{campus}'" if (has_campus and campus) else ""

    df = pd.read_sql(f"""
        SELECT r.school, r.city, r.county_state_country AS county,
               MAX(r.year) AS latest_year,
               MAX(CASE WHEN r.year = (SELECT MAX(year) FROM race_ethnicity r2
                                       WHERE r2.school = r.school {campus_subq_clause})
                        THEN r.all_count END) AS applicants_latest,
               g.app_gpa AS latest_gpa
        FROM race_ethnicity r
        LEFT JOIN gpa g ON g.school = r.school
            AND g.year = (SELECT MAX(year) FROM race_ethnicity r2
                          WHERE r2.school = r.school {campus_subq_clause})
            {campus_join_clause}
        {where}
        GROUP BY r.school, r.city, r.county_state_country
        ORDER BY applicants_latest DESC NULLS LAST
        LIMIT 200
    """, conn, params=params if params else None)
    return df


@st.cache_data
def load_school_data(school: str, city: str, campus: str = "Universitywide") -> dict:
    conn = get_conn()
    has_campus = _has_campus_col()
    campus_clause = "AND campus = ?" if has_campus else ""
    base_params = [school, city]
    params_re = base_params + ([campus] if has_campus else [])

    re = pd.read_sql(f"""
        SELECT year, count_type, all_count,
               african_american, american_indian, hispanic_latinx,
               pacific_islander, asian, white, domestic_unknown, intl
        FROM race_ethnicity WHERE school = ? AND city = ? {campus_clause}
        ORDER BY year, count_type
    """, conn, params=params_re)
    gnd = pd.read_sql(f"""
        SELECT year, count_type, all_count, female, male
        FROM gender WHERE school = ? AND city = ? {campus_clause}
        ORDER BY year, count_type
    """, conn, params=params_re)
    gpa = pd.read_sql(f"""
        SELECT year, app_gpa, adm_gpa, enr_gpa
        FROM gpa WHERE school = ? AND city = ? {campus_clause}
        ORDER BY year
    """, conn, params=params_re)
    return {"re": re, "gnd": gnd, "gpa": gpa}


@st.cache_data
def load_school_by_campus(school: str, city: str) -> pd.DataFrame:
    conn = get_conn()
    df = pd.read_sql("""
        SELECT r.campus, r.year,
               MAX(CASE WHEN r.count_type='App' THEN r.all_count END) AS applicants,
               MAX(CASE WHEN r.count_type='Adm' THEN r.all_count END) AS admits,
               MAX(CASE WHEN r.count_type='Enr' THEN r.all_count END) AS enrollees,
               MAX(g.app_gpa) AS app_gpa, MAX(g.adm_gpa) AS adm_gpa, MAX(g.enr_gpa) AS enr_gpa
        FROM race_ethnicity r
        LEFT JOIN gpa g ON g.school = r.school AND g.city = r.city
                       AND g.year = r.year AND g.campus = r.campus
        WHERE r.school = ? AND r.city = ? AND r.campus != 'Universitywide'
        GROUP BY r.campus, r.year
        ORDER BY r.campus, r.year
    """, conn, params=[school, city])
    df["admit_rate"] = (df["admits"] / df["applicants"] * 100).round(1)
    df["yield_rate"] = (df["enrollees"] / df["admits"] * 100).round(1)
    return df


@st.cache_data
def load_compare_data(school_city_pairs: list, campus: str = "Universitywide") -> pd.DataFrame:
    """school_city_pairs: list of (school, city, display_label) tuples"""
    if not school_city_pairs:
        return pd.DataFrame()
    conn = get_conn()
    has_campus = _has_campus_col()
    campus_join = "AND g.campus = r.campus" if has_campus else ""
    campus_clause = "AND r.campus = ?" if has_campus else ""

    rows = []
    for school, city, label in school_city_pairs:
        params = [school, city] + ([campus] if has_campus else [])
        df = pd.read_sql(f"""
            SELECT r.year,
                   MAX(CASE WHEN r.count_type='App' THEN r.all_count END) AS applicants,
                   MAX(CASE WHEN r.count_type='Adm' THEN r.all_count END) AS admits,
                   MAX(CASE WHEN r.count_type='Enr' THEN r.all_count END) AS enrollees,
                   g.app_gpa, g.adm_gpa, g.enr_gpa
            FROM race_ethnicity r
            LEFT JOIN gpa g ON g.school = r.school AND g.city = r.city AND g.year = r.year {campus_join}
            WHERE r.school = ? AND r.city = ? {campus_clause}
            GROUP BY r.year
            ORDER BY r.year
        """, conn, params=params)
        df["admit_rate"] = (df["admits"] / df["applicants"] * 100).round(1)
        df["yield_rate"] = (df["enrollees"] / df["admits"] * 100).round(1)
        df["enroll_rate"] = (df["enrollees"] / df["applicants"] * 100).round(1)
        df.insert(0, "school", label)
        rows.append(df)
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()


@st.cache_data
def load_region_data(county: str, city: str = "", campus: str = "Universitywide") -> pd.DataFrame:
    conn = get_conn()
    has_campus = _has_campus_col()
    conditions = [CA_FILTER]
    params = []
    if county and county != "All counties":
        conditions.append("county_state_country = ?")
        params.append(county)
    if city and city != "All cities":
        conditions.append("city = ?")
        params.append(city)
    if has_campus and campus:
        conditions.append("campus = ?")
        params.append(campus)
    where = "WHERE " + " AND ".join(conditions)
    df = pd.read_sql(f"""
        SELECT year, count_type,
               SUM(all_count) AS all_count,
               SUM(african_american) AS african_american,
               SUM(american_indian) AS american_indian,
               SUM(hispanic_latinx) AS hispanic_latinx,
               SUM(pacific_islander) AS pacific_islander,
               SUM(asian) AS asian,
               SUM(white) AS white,
               SUM(domestic_unknown) AS domestic_unknown,
               SUM(intl) AS intl
        FROM (
            -- The table holds exact duplicate rows; count each once.
            SELECT DISTINCT school, school_id, city, county_state_country, campus,
                   year, count_type, all_count, african_american, american_indian,
                   hispanic_latinx, pacific_islander, asian, white, domestic_unknown, intl
            FROM race_ethnicity {where}
        )
        GROUP BY year, count_type ORDER BY year, count_type
    """, conn, params=params if params else None)
    return df


# ---------------------------------------------------------------------------
# Chart helpers
# ---------------------------------------------------------------------------

ETHNICITY_COLS = ["hispanic_latinx", "asian", "white", "african_american",
                  "american_indian", "pacific_islander", "intl", "domestic_unknown"]
ETHNICITY_LABELS = {
    "hispanic_latinx": "Hispanic/Latinx",
    "asian": "Asian",
    "white": "White",
    "african_american": "African American",
    "american_indian": "American Indian",
    "pacific_islander": "Pacific Islander",
    "intl": "International",
    "domestic_unknown": "Unknown",
}
COUNT_COLORS = {"App": "#4C8BF5", "Adm": "#34A853", "Enr": "#FBBC05"}
ETHNICITY_COLORS = {
    "Hispanic/Latinx": "#E67E22",
    "Asian": "#2980B9",
    "White": "#7F8C8D",
    "African American": "#8E44AD",
    "American Indian": "#27AE60",
    "Pacific Islander": "#16A085",
    "International": "#C0392B",
    "Unknown": "#BDC3C7",
}


def fig_gpa_trend(gpa_df: pd.DataFrame, title: str = "Average GPA by Year"):
    fig = go.Figure()
    styles = {"app_gpa": ("Applicants", "#4C8BF5", "solid"),
              "adm_gpa": ("Admits", "#34A853", "dot"),
              "enr_gpa": ("Enrollees", "#FBBC05", "dash")}
    for col, (label, color, dash) in styles.items():
        valid = gpa_df[["year", col]].dropna()
        if valid.empty:
            continue
        fig.add_trace(go.Scatter(
            x=valid["year"], y=valid[col], name=label,
            mode="lines+markers",
            line=dict(color=color, dash=dash, width=2),
            marker=dict(size=5),
        ))
    fig.update_layout(title=title, xaxis_title="Year", yaxis_title="GPA",
                      legend=dict(orientation="h", y=-0.2), height=350,
                      margin=dict(t=40, b=10))
    return fig


def fig_volume_trend(re_df: pd.DataFrame, title: str = "Applicants / Admits / Enrollees by Year"):
    app = re_df[re_df["count_type"] == "App"][["year", "all_count"]].rename(columns={"all_count": "Applicants"})
    adm = re_df[re_df["count_type"] == "Adm"][["year", "all_count"]].rename(columns={"all_count": "Admits"})
    enr = re_df[re_df["count_type"] == "Enr"][["year", "all_count"]].rename(columns={"all_count": "Enrollees"})
    merged = app.merge(adm, on="year", how="outer").merge(enr, on="year", how="outer").sort_values("year")
    fig = go.Figure()
    for col, color in [("Applicants", "#4C8BF5"), ("Admits", "#34A853"), ("Enrollees", "#FBBC05")]:
        if col in merged.columns:
            fig.add_trace(go.Scatter(x=merged["year"], y=merged[col], name=col,
                                     mode="lines+markers",
                                     line=dict(color=color, width=2), marker=dict(size=5)))
    fig.update_layout(title=title, xaxis_title="Year", yaxis_title="Count",
                      legend=dict(orientation="h", y=-0.2), height=350,
                      margin=dict(t=40, b=10))
    return fig


def fig_ethnicity_bar(re_df: pd.DataFrame, year: int, count_type: str = "App"):
    row = re_df[(re_df["year"] == year) & (re_df["count_type"] == count_type)]
    if row.empty:
        return None
    row = row.iloc[0]
    data = {ETHNICITY_LABELS[c]: (row[c] or 0) for c in ETHNICITY_COLS if (row[c] or 0) > 0}
    if not data:
        return None
    df = pd.DataFrame({"Ethnicity": list(data.keys()), "Count": list(data.values())})
    df = df.sort_values("Count", ascending=True)
    fig = px.bar(df, x="Count", y="Ethnicity", orientation="h",
                 color="Ethnicity", color_discrete_map=ETHNICITY_COLORS,
                 title=f"Race/Ethnicity — {count_type} {year}")
    fig.update_layout(showlegend=False, height=300, margin=dict(t=40, b=10))
    return fig


def fig_ethnicity_trend(re_df: pd.DataFrame, count_type: str = "App"):
    df = re_df[re_df["count_type"] == count_type].copy()
    if df.empty:
        return None
    eth_cols = [c for c in ETHNICITY_COLS if df[c].notna().any()]
    melted = df.melt(id_vars="year", value_vars=eth_cols,
                     var_name="ethnicity", value_name="count")
    melted["ethnicity"] = melted["ethnicity"].map(ETHNICITY_LABELS)
    melted = melted.dropna(subset=["count"])
    fig = px.area(melted, x="year", y="count", color="ethnicity",
                  color_discrete_map=ETHNICITY_COLORS,
                  title=f"Race/Ethnicity Over Time ({count_type})",
                  labels={"count": "Count", "year": "Year", "ethnicity": "Group"})
    fig.update_layout(legend=dict(orientation="h", y=-0.3), height=380,
                      margin=dict(t=40, b=10))
    return fig


def fig_admit_rate(re_df: pd.DataFrame, title: str = "Admission & Yield Rate"):
    app = re_df[re_df["count_type"] == "App"][["year", "all_count"]].rename(columns={"all_count": "app"})
    adm = re_df[re_df["count_type"] == "Adm"][["year", "all_count"]].rename(columns={"all_count": "adm"})
    enr = re_df[re_df["count_type"] == "Enr"][["year", "all_count"]].rename(columns={"all_count": "enr"})
    df = app.merge(adm, on="year").merge(enr, on="year").sort_values("year")
    df["admit_rate"] = (df["adm"] / df["app"] * 100).round(1)
    df["yield_rate"] = (df["enr"] / df["adm"] * 100).round(1)
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=df["year"], y=df["admit_rate"], name="Admit rate %",
                             mode="lines+markers", line=dict(color="#4C8BF5", width=2)))
    fig.add_trace(go.Scatter(x=df["year"], y=df["yield_rate"], name="Yield rate %",
                             mode="lines+markers", line=dict(color="#34A853", width=2, dash="dot")))
    fig.update_layout(title=title, xaxis_title="Year", yaxis_title="%",
                      yaxis=dict(range=[0, 105]),
                      legend=dict(orientation="h", y=-0.2), height=320,
                      margin=dict(t=40, b=10))
    return fig


def fig_compare_metric(compare_df: pd.DataFrame, metric: str, title: str):
    rate_metrics = {"admit_rate", "yield_rate", "enroll_rate"}
    y_label = "%" if metric in rate_metrics else title
    fig = px.line(compare_df, x="year", y=metric, color="school",
                  markers=True, title=title,
                  labels={"year": "Year", metric: y_label, "school": "School"})
    if metric in rate_metrics:
        fig.update_layout(yaxis=dict(range=[0, 105]))
    fig.update_layout(legend=dict(orientation="h", y=-0.3), height=380,
                      margin=dict(t=40, b=10))
    return fig


# ---------------------------------------------------------------------------
# App layout
# ---------------------------------------------------------------------------

st.set_page_config(page_title="UC Admissions Explorer", layout="wide", page_icon="🎓")

st.markdown("""
<style>
    .school-card {
        border: 1px solid #e0e0e0;
        border-radius: 8px;
        padding: 10px 14px;
        margin: 4px 0;
        cursor: pointer;
        background: white;
    }
    .school-card:hover { background: #f0f4ff; }
    div[data-testid="stMetricValue"] { font-size: 1.6rem; }
</style>
""", unsafe_allow_html=True)

st.title("🎓 UC Admissions Explorer")
st.caption("Freshman admissions data 1994–2025 · Source: UC Information Center")

# ---------------------------------------------------------------------------
# Sidebar — global filters
# ---------------------------------------------------------------------------

with st.sidebar:
    st.header("Filters")

    # Campus selector
    campuses = load_campuses()
    if campuses:
        selected_campus = st.selectbox(
            "UC Campus",
            campuses,
            index=0,
            help="Filter admissions by destination campus"
        )
    else:
        # Old DB without campus column — fall back gracefully
        selected_campus = ""
        st.info("Campus filter available after re-downloading data.")

    st.divider()

    counties = ["All counties"] + load_counties(selected_campus)
    selected_county = st.selectbox("County", counties)

    cities = ["All cities"] + load_cities(
        "" if selected_county == "All counties" else selected_county,
        selected_campus,
    )
    selected_city = st.selectbox("City", cities)

    school_query = st.text_input("Search school name", placeholder="e.g. Lowell, Mission San Jose")

    st.divider()
    view_mode = st.radio("View", ["🔍 School lookup", "📊 Compare schools", "🗺️ Regional overview"])

# ---------------------------------------------------------------------------
# Regional overview
# ---------------------------------------------------------------------------

if view_mode == "🗺️ Regional overview":
    county_label = selected_county if selected_county != "All counties" else "All California"
    city_label = f" · {selected_city}" if selected_city != "All cities" else ""
    campus_label = f" → {selected_campus}" if selected_campus else ""
    st.subheader(f"Regional Overview — {county_label}{city_label}{campus_label}")

    reg = load_region_data(
        "" if selected_county == "All counties" else selected_county,
        "" if selected_city == "All cities" else selected_city,
        selected_campus,
    )

    if reg.empty:
        st.info("No data for this selection.")
    else:
        col1, col2 = st.columns(2)
        with col1:
            st.plotly_chart(fig_volume_trend(reg, "Total Applicants / Admits / Enrollees"), use_container_width=True)
        with col2:
            fig_eth = fig_ethnicity_trend(reg, "App")
            if fig_eth:
                st.plotly_chart(fig_eth, use_container_width=True)

        # Snapshot table for a selected year
        st.divider()
        year_range = sorted(reg["year"].unique())
        snap_year = st.select_slider("Year snapshot", options=year_range, value=year_range[-1])

        has_campus = _has_campus_col()
        snap_conn = get_conn()
        snap_clauses = ["r.year = ?", CA_FILTER.replace("county_state_country", "r.county_state_country")]
        snap_params = [int(snap_year)]
        if has_campus and selected_campus:
            snap_clauses.append("r.campus = ?")
            snap_params.append(selected_campus)
        if selected_county != "All counties":
            snap_clauses.append("r.county_state_country = ?")
            snap_params.append(selected_county)
        if selected_city != "All cities":
            snap_clauses.append("r.city = ?")
            snap_params.append(selected_city)
        snap_where = "WHERE " + " AND ".join(snap_clauses)

        snap_df = pd.read_sql(f"""
            SELECT r.school, r.city,
                   MAX(CASE WHEN r.count_type='App' THEN r.all_count END) AS applicants,
                   MAX(CASE WHEN r.count_type='Adm' THEN r.all_count END) AS admits,
                   MAX(CASE WHEN r.count_type='Enr' THEN r.all_count END) AS enrollees,
                   g.app_gpa
            FROM race_ethnicity r
            LEFT JOIN gpa g ON g.school = r.school AND g.city = r.city AND g.year = r.year
                {('AND g.campus = r.campus' if has_campus else '')}
            {snap_where}
            GROUP BY r.school, r.city
            ORDER BY applicants DESC NULLS LAST
            LIMIT 50
        """, snap_conn, params=snap_params)

        if not snap_df.empty:
            snap_df["admit_rate"] = (snap_df["admits"] / snap_df["applicants"] * 100).round(1)
            snap_df["yield_rate"] = (snap_df["enrollees"] / snap_df["admits"] * 100).round(1)
            mc1, mc2, mc3 = st.columns(3)
            mc1.metric("Year", snap_year)
            mc2.metric("Total applicants (top 50)", f"{int(snap_df['applicants'].sum()):,}")
            mc3.metric("Avg admit rate", f"{snap_df['admit_rate'].mean():.1f}%")
            st.dataframe(snap_df.rename(columns={
                "school": "School", "city": "City",
                "applicants": "Applicants", "admits": "Admits", "enrollees": "Enrollees",
                "admit_rate": "Admit %", "yield_rate": "Yield %", "app_gpa": "App GPA",
            }), use_container_width=True, hide_index=True)

# ---------------------------------------------------------------------------
# Compare schools
# ---------------------------------------------------------------------------

elif view_mode == "📊 Compare schools":
    st.subheader(f"Compare Schools — {selected_campus or 'All campuses'}")

    # Session state: list of (school, city, label) tuples persists across searches
    if "compare_list" not in st.session_state:
        st.session_state.compare_list = []

    results = search_schools(
        school_query,
        "" if selected_county == "All counties" else selected_county,
        "" if selected_city == "All cities" else selected_city,
        selected_campus,
    )

    # Build search result options as "Name (City)" for disambiguation
    search_options = {}  # label -> (school, city)
    if not results.empty:
        for _, row in results.iterrows():
            label = f"{row['school'].title()} ({row['city'].title()})"
            search_options[label] = (row["school"], row["city"])

    # Add schools from search results to comparison
    already_labels = {t[2] for t in st.session_state.compare_list}
    new_options = [lbl for lbl in search_options if lbl not in already_labels]

    col_add, col_clear = st.columns([3, 1])
    with col_add:
        to_add = st.multiselect(
            "Search & add schools (use sidebar filters to find schools)",
            options=new_options,
            placeholder="Type to search, then select schools to add…",
        )
    with col_clear:
        st.write("")
        if st.button("Clear all", use_container_width=True):
            st.session_state.compare_list = []
            st.rerun()

    if to_add:
        for lbl in to_add:
            school, city = search_options[lbl]
            st.session_state.compare_list.append((school, city, lbl))
        st.rerun()

    # Show selected schools with remove buttons
    if st.session_state.compare_list:
        st.markdown("**Selected schools:**")
        cols = st.columns(min(len(st.session_state.compare_list), 4))
        to_remove = []
        for i, (school, city, label) in enumerate(st.session_state.compare_list):
            with cols[i % 4]:
                if st.button(f"✕ {label}", key=f"rm_{i}", use_container_width=True):
                    to_remove.append(i)
        if to_remove:
            st.session_state.compare_list = [
                t for i, t in enumerate(st.session_state.compare_list) if i not in to_remove
            ]
            st.rerun()

    if not st.session_state.compare_list:
        st.info("Use the sidebar filters and search box above to find schools, then add them to compare.")
    else:
        cdf = load_compare_data(st.session_state.compare_list[:8], selected_campus)
        if cdf.empty:
            st.warning("No data found for selected schools.")
        else:
            metric_choice = st.radio(
                "Metric",
                ["applicants", "admits", "enrollees",
                 "admit_rate", "yield_rate", "enroll_rate",
                 "app_gpa", "adm_gpa", "enr_gpa"],
                horizontal=True,
                format_func=lambda x: {
                    "applicants": "Applicants", "admits": "Admits", "enrollees": "Enrollees",
                    "admit_rate": "Admit %", "yield_rate": "Yield %", "enroll_rate": "Enroll %",
                    "app_gpa": "App GPA", "adm_gpa": "Adm GPA", "enr_gpa": "Enr GPA",
                }[x]
            )
            title_map = {
                "applicants": "Applicants by Year", "admits": "Admits by Year",
                "enrollees": "Enrollees by Year",
                "admit_rate": "Admit Rate % by Year",
                "yield_rate": "Yield Rate % by Year",
                "enroll_rate": "Overall Enrollment Rate % by Year",
                "app_gpa": "Applicant GPA by Year",
                "adm_gpa": "Admit GPA by Year", "enr_gpa": "Enrollee GPA by Year",
            }
            st.plotly_chart(
                fig_compare_metric(cdf, metric_choice, title_map[metric_choice]),
                use_container_width=True
            )

            latest_year = int(cdf["year"].max())
            snap = cdf[cdf["year"] == latest_year].copy()
            snap["admit_rate"] = (snap["admits"] / snap["applicants"] * 100).round(1)
            snap["yield_rate"] = (snap["enrollees"] / snap["admits"] * 100).round(1)
            st.caption(f"Latest year ({latest_year}) snapshot")
            st.dataframe(snap[["school", "applicants", "admits", "enrollees",
                                "admit_rate", "yield_rate", "app_gpa", "adm_gpa", "enr_gpa"]]
                         .rename(columns={
                             "school": "School", "applicants": "Applicants",
                             "admits": "Admits", "enrollees": "Enrollees",
                             "admit_rate": "Admit %", "yield_rate": "Yield %",
                             "app_gpa": "App GPA", "adm_gpa": "Adm GPA", "enr_gpa": "Enr GPA"
                         }),
                         use_container_width=True, hide_index=True)

# ---------------------------------------------------------------------------
# School lookup
# ---------------------------------------------------------------------------

else:  # School lookup
    col_left, col_right = st.columns([1, 2.5])

    with col_left:
        st.subheader("Schools")
        results = search_schools(
            school_query,
            "" if selected_county == "All counties" else selected_county,
            "" if selected_city == "All cities" else selected_city,
            selected_campus,
        )

        if results.empty:
            st.info("No schools match.")
        else:
            st.caption(f"{len(results)} schools found")
            # Show "School (City)" when same name appears multiple times
            name_counts = results["school"].value_counts()
            def school_label(row):
                name = row["school"].title()[:35]
                if name_counts[row["school"]] > 1:
                    return f"{name} ({row['city'].title()})"
                return name
            labels = results.apply(school_label, axis=1).tolist()
            keys = list(zip(results["school"], results["city"]))
            # Keep the chosen school selected when the campus/filters change the list.
            remembered = st.session_state.get("lookup_school")
            idx = keys.index(remembered) if remembered in keys else 0
            selected_label = st.radio(
                "Select a school",
                labels,
                index=idx,
                label_visibility="collapsed",
            )
            sel_pos = labels.index(selected_label)
            if sel_pos != idx or remembered in keys:
                st.session_state.lookup_school = keys[sel_pos]
            selected_school = keys[sel_pos][0]
            lookup_city = keys[sel_pos][1]

    with col_right:
        if results.empty:
            st.info("Adjust filters to find schools.")
        else:
            info = results[(results["school"] == selected_school)
                           & (results["city"] == lookup_city)].iloc[0]
            data = load_school_data(selected_school, info["city"], selected_campus)
            re_df = data["re"]
            gnd_df = data["gnd"]
            gpa_df = data["gpa"]

            # School header
            st.subheader(selected_school.title())
            campus_disp = f" · {selected_campus}" if selected_campus else ""
            st.caption(f"{info['city']} · {info['county']}{campus_disp}")

            if re_df.empty:
                st.warning("No data for this school.")
            else:
                # Metric cards — latest year
                latest = int(re_df["year"].max())
                app_row = re_df[(re_df["year"] == latest) & (re_df["count_type"] == "App")]
                adm_row = re_df[(re_df["year"] == latest) & (re_df["count_type"] == "Adm")]
                enr_row = re_df[(re_df["year"] == latest) & (re_df["count_type"] == "Enr")]
                gpa_row = gpa_df[gpa_df["year"] == latest]

                m1, m2, m3, m4, m5 = st.columns(5)
                m1.metric("Year", latest)
                m2.metric("Applicants", int(app_row["all_count"].iloc[0]) if not app_row.empty else "—")
                m3.metric("Admits", int(adm_row["all_count"].iloc[0]) if not adm_row.empty else "—")
                m4.metric("Enrolled", int(enr_row["all_count"].iloc[0]) if not enr_row.empty else "—")
                m5.metric("App GPA", f"{gpa_row['app_gpa'].iloc[0]:.2f}" if not gpa_row.empty and pd.notna(gpa_row['app_gpa'].iloc[0]) else "—")

                tab1, tab2, tab3, tab4, tab5 = st.tabs(["📈 Volume & Rates", "🎓 GPA Trends", "👥 Race/Ethnicity", "⚧ Gender", "🏫 By Campus"])

                with tab5:
                    bc = load_school_by_campus(selected_school, info["city"])
                    if bc.empty:
                        st.info("No per-campus data for this school.")
                    else:
                        bc_metrics = {
                            "admit_rate": "Admit %", "yield_rate": "Yield %",
                            "applicants": "Applicants", "admits": "Admits",
                            "enrollees": "Enrollees", "adm_gpa": "Admit GPA",
                        }
                        bc_metric = st.radio("Metric", list(bc_metrics), horizontal=True,
                                             format_func=bc_metrics.get, key="bc_metric")
                        fig_bc = px.line(bc.dropna(subset=[bc_metric]), x="year", y=bc_metric,
                                         color="campus", markers=True,
                                         title=f"{bc_metrics[bc_metric]} by Campus",
                                         labels={"year": "Year", bc_metric: bc_metrics[bc_metric],
                                                 "campus": "Campus"})
                        if bc_metric in ("admit_rate", "yield_rate"):
                            fig_bc.update_layout(yaxis=dict(range=[0, 105]))
                        fig_bc.update_layout(legend=dict(orientation="h", y=-0.3), height=420,
                                             margin=dict(t=40, b=10))
                        st.plotly_chart(fig_bc, use_container_width=True)
                        st.caption("Click campus names in the legend to hide or show them.")

                        bc_year = int(bc["year"].max())
                        snap_bc = (bc[bc["year"] == bc_year]
                                   .sort_values("admit_rate", ascending=False)
                                   [["campus", "applicants", "admits", "enrollees",
                                     "admit_rate", "yield_rate", "app_gpa", "adm_gpa"]]
                                   .rename(columns={
                                       "campus": "Campus", "applicants": "Applicants",
                                       "admits": "Admits", "enrollees": "Enrollees",
                                       "admit_rate": "Admit %", "yield_rate": "Yield %",
                                       "app_gpa": "App GPA", "adm_gpa": "Admit GPA"}))
                        st.caption(f"{bc_year} by campus (highest admit rate first)")
                        st.dataframe(snap_bc, use_container_width=True, hide_index=True)

                with tab1:
                    c1, c2 = st.columns(2)
                    c1.plotly_chart(fig_volume_trend(re_df), use_container_width=True)
                    c2.plotly_chart(fig_admit_rate(re_df), use_container_width=True)

                with tab2:
                    if gpa_df.empty:
                        st.info("No GPA data.")
                    else:
                        st.plotly_chart(fig_gpa_trend(gpa_df), use_container_width=True)
                        st.caption("Admit GPA consistently above applicant GPA reflects selectivity; enrollee GPA above admit GPA reflects yield selectivity.")

                with tab3:
                    year_options = sorted(re_df["year"].unique(), reverse=True)
                    selected_year = st.select_slider("Year", options=year_options, value=year_options[0], key="eth_year")
                    count_type = st.radio("Count type", ["App", "Adm", "Enr"], horizontal=True, key="eth_ct")
                    c1, c2 = st.columns(2)
                    bar = fig_ethnicity_bar(re_df, selected_year, count_type)
                    if bar:
                        c1.plotly_chart(bar, use_container_width=True)
                    trend = fig_ethnicity_trend(re_df, count_type)
                    if trend:
                        c2.plotly_chart(trend, use_container_width=True)

                with tab4:
                    ct = st.radio("Count type", ["App", "Adm", "Enr"], horizontal=True, key="gnd_ct")
                    gnd_sub = gnd_df[gnd_df["count_type"] == ct].copy()
                    if gnd_sub.empty:
                        st.info("No gender data.")
                    else:
                        fig_g = go.Figure()
                        for col, color, label in [("female", "#E91E8C", "Female"),
                                                   ("male", "#1E88E5", "Male")]:
                            valid = gnd_sub[["year", col]].dropna()
                            fig_g.add_trace(go.Scatter(x=valid["year"], y=valid[col],
                                                        name=label, mode="lines+markers",
                                                        line=dict(color=color, width=2)))
                        fig_g.update_layout(title=f"Gender — {ct}", xaxis_title="Year",
                                             yaxis_title="Count",
                                             legend=dict(orientation="h", y=-0.2),
                                             height=350, margin=dict(t=40, b=10))
                        st.plotly_chart(fig_g, use_container_width=True)

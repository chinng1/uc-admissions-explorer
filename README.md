# UC Admissions Explorer

Streamlit app for exploring University of California freshman admissions by high school, 1994–2025 (applicants, admits, enrollees, GPA, race/ethnicity, gender), by campus. Data comes from the UC Information Center.

```bash
pip install -r requirements.txt
streamlit run explorer.py
```

The SQLite database is too large for the repo. It's published as a release asset, and the app downloads it automatically on first start. To rebuild it from source, use `download_uc_data.py`.

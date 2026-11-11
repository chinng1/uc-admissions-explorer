"""
Test downloading crosstab from FREthbyHS after searching for a school name.
"""
import asyncio
import csv
import io
import json
import re
from pathlib import Path

from playwright.async_api import async_playwright, Request, Response

TABLEAU_BASE = "https://visualizedata.ucop.edu"
VIEW_BASE    = f"{TABLEAU_BASE}/t/Public/views/AdmissionsDataTable"
LOG_DIR      = Path(__file__).parent / "private_school_logs"


async def download_school_crosstab(school_name: str, view_short: str = "FREthbyHS") -> object:
    """
    Open the single-school lookup view, search for a school, download Crosstab as CSV.
    Returns raw bytes of the downloaded file, or None on failure.
    """
    LOG_DIR.mkdir(exist_ok=True)
    slug = re.sub(r'[^A-Z0-9]', '_', school_name.upper())[:30]

    captured_session = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            viewport={"width": 1400, "height": 900},
            accept_downloads=True,
        )
        page = await ctx.new_page()

        def on_req(req: Request):
            if "vizql" in req.url:
                m = re.search(r"/sessions/([^/]+)/", req.url)
                if m and not captured_session:
                    captured_session.append(m.group(1))

        page.on("request", on_req)

        # Load view
        url = f"{VIEW_BASE}/{view_short}?:embed=y&:showVizHome=no&:toolbar=top"
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass
        await asyncio.sleep(10)

        # Find visible search box
        boxes = await page.query_selector_all('.QueryBox')
        visible_box = None
        for b in boxes:
            vis = await b.is_visible()
            bb  = await b.bounding_box()
            if vis and bb and bb['width'] > 50:
                visible_box = b
                break

        if not visible_box:
            print(f"  [{view_short}] No search box")
            await browser.close()
            return None

        # Clear, type school name, press Enter
        await visible_box.click(click_count=3, timeout=10000)
        await asyncio.sleep(0.3)
        await page.keyboard.type(school_name)
        await asyncio.sleep(0.5)
        await page.keyboard.press("Enter")
        await asyncio.sleep(6)

        # Take screenshot to verify data loaded
        ss = LOG_DIR / f"dl_{slug}_{view_short}.png"
        await page.screenshot(path=str(ss))

        # Check data appeared
        content = await page.content()
        if "App" not in content and "Adm" not in content:
            print(f"  [{view_short}] No data for '{school_name}' (content length: {len(content)})")
            await browser.close()
            return None

        # Download Crosstab
        print(f"  [{view_short}] Data found for '{school_name}', downloading crosstab...")
        raw = await _download_crosstab(page)
        await browser.close()

        if raw:
            print(f"  [{view_short}] Downloaded {len(raw)} bytes")
            # Save raw file for debugging
            out = LOG_DIR / f"raw_{slug}_{view_short}.tsv"
            out.write_bytes(raw)
            print(f"  [{view_short}] Saved to {out}")
        else:
            print(f"  [{view_short}] Download failed")

        return raw


async def _download_crosstab(page) -> object:
    """Click Download → Crosstab → CSV → Download and return the file bytes."""
    # Click the Download button
    for sel in ['button:has-text("Download")', '.tabToolbarButtonFace:has-text("Download")',
                '[title="Download"]', '.download-button']:
        btn = await page.query_selector(sel)
        if btn:
            await btn.click()
            await asyncio.sleep(1.5)
            break
    else:
        # Try toolbar buttons
        btns = await page.query_selector_all('button')
        for btn in btns:
            text = await btn.inner_text()
            if "Download" in text or "download" in text:
                await btn.click()
                await asyncio.sleep(1.5)
                break
        else:
            print("    No Download button found")
            return None

    # Screenshot after clicking download
    ss = LOG_DIR / "dl_menu.png"
    await page.screenshot(path=str(ss))

    # Click Crosstab option
    for sel in ['text="Crosstab"', 'text="Cross-tab"', '[data-value="crosstab"]',
                'a:has-text("Crosstab")', 'button:has-text("Crosstab")']:
        el = await page.query_selector(sel)
        if el:
            await el.click()
            await asyncio.sleep(2)
            break
    else:
        print("    No Crosstab option found")
        return None

    # Select CSV format if available
    for sel in ['label:has-text("CSV")', 'input[value="csv"]', 'text="CSV"']:
        el = await page.query_selector(sel)
        if el:
            await el.click()
            await asyncio.sleep(0.5)
            break

    # Click the final download button using JS (bypasses glass overlay)
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
        print(f"    Download error: {e}")
        return None


def parse_crosstab(raw: bytes) -> list[dict]:
    """Parse the UTF-16 TSV crosstab into a list of row dicts."""
    try:
        text = raw.decode("utf-16")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
    return list(csv.DictReader(io.StringIO(text), delimiter="\t"))


async def main():
    # Test downloading Saint Ignatius - all three views
    for view in ["FREthbyHS", "FRGndbyHS", "FRGPAbyHS"]:
        raw = await download_school_crosstab("SAINT IGNATIUS COLLEGE PREP", view)
        if raw:
            rows = parse_crosstab(raw)
            print(f"\n  Parsed {len(rows)} rows from {view}")
            if rows:
                print(f"  Columns: {list(rows[0].keys())}")
                print(f"  First row: {rows[0]}")
                print(f"  Sample rows:")
                for r in rows[:5]:
                    print(f"    {r}")


if __name__ == "__main__":
    asyncio.run(main())

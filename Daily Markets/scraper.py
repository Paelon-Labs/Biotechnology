"""
Daily biotech market pull.

1. Fetches the full Nasdaq stock screener (JSON API, no browser) and keeps the
   three target Biotechnology industries.
2. Upserts every row into the opportunity Supabase table `biotech_market`
   (point in time: one row per ticker), then deletes rows not seen today.
3. Writes today's "Biotechnology MM-DD-YYYY.csv" here, replacing the previous
   day's file. Archiving is monthly: when the file being replaced is from an
   earlier month, it is the month-end snapshot, so it is gzipped into History/
   first. Same-month files are simply overwritten.

Env: OPPORTUNITY_SUPABASE_URL, OPPORTUNITY_SUPABASE_SERVICE_KEY
Flags: --force   run even if today is not an NYSE session
       --no-db   skip the Supabase write (CSV only)
"""
import csv
import glob
import gzip
import os
import re
import shutil
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
HISTORY_DIR = os.path.join(HERE, "..", "History")

SCREENER_URL = "https://api.nasdaq.com/api/screener/stocks?tableonly=true&download=true"
SCREENER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/",
}

TARGET_INDUSTRIES = {
    "Biotechnology: Pharmaceutical Preparations",
    "Biotechnology: Biological Products (No Diagnostic Substances)",
    "Biotechnology: In Vitro & In Vivo Diagnostic Substances",
}

# ~705 rows in Oct 2026. Far fewer means a bad pull; fail rather than
# overwrite the table and CSV with a partial universe.
MIN_ROWS = 500

CSV_COLUMNS = ["Symbol", "Name", "Last Sale", "Net Change", "% Change", "Market Cap",
               "Country", "IPO Year", "Volume", "Sector", "Industry"]


def num(value):
    """'$948.45' / '-2.719%' / '1,234' / '' -> float or None."""
    s = str(value or "").replace("$", "").replace(",", "").replace("%", "").strip()
    if s in ("", "NA", "N/A", "--"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def as_int(value):
    n = num(value)
    return int(n) if n is not None else None


def is_trading_day(day):
    import exchange_calendars as xcals
    return xcals.get_calendar("XNYS").is_session(day.isoformat())


def fetch_screener():
    last_err = None
    for attempt in range(1, 4):
        try:
            r = requests.get(SCREENER_URL, headers=SCREENER_HEADERS, timeout=60)
            r.raise_for_status()
            rows = (r.json().get("data") or {}).get("rows") or []
            if rows:
                return rows
            last_err = "empty rows"
        except Exception as e:
            last_err = e
        print(f"⚠️ Screener attempt {attempt} failed: {last_err}")
        time.sleep(5 * attempt)
    raise RuntimeError(f"Nasdaq screener fetch failed: {last_err}")


def to_records(raw_rows, as_of):
    records = []
    for r in raw_rows:
        if r.get("industry") not in TARGET_INDUSTRIES:
            continue
        records.append({
            "symbol": r["symbol"].strip(),
            "name": r.get("name") or None,
            "last_sale": num(r.get("lastsale")),
            "net_change": num(r.get("netchange")),
            "pct_change": num(r.get("pctchange")),
            "market_cap": as_int(r.get("marketCap")),
            "country": r.get("country") or None,
            "ipo_year": as_int(r.get("ipoyear")),
            "volume": as_int(r.get("volume")),
            "sector": r.get("sector") or None,
            "industry": r.get("industry"),
            "as_of": as_of.isoformat(),
        })
    records.sort(key=lambda x: x["market_cap"] or 0, reverse=True)
    return records


def write_supabase(records, as_of):
    url = os.environ.get("OPPORTUNITY_SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("OPPORTUNITY_SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        raise RuntimeError("Missing OPPORTUNITY_SUPABASE_URL / OPPORTUNITY_SUPABASE_SERVICE_KEY")

    rest = f"{url}/rest/v1/biotech_market"
    headers = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    now = datetime.now(ZoneInfo("UTC")).isoformat()

    for i in range(0, len(records), 500):
        batch = [{**rec, "updated_at": now} for rec in records[i:i + 500]]
        r = requests.post(
            rest, params={"on_conflict": "symbol"}, json=batch, timeout=60,
            headers={**headers, "Prefer": "resolution=merge-duplicates,return=minimal"},
        )
        if r.status_code >= 300:
            raise RuntimeError(f"Upsert failed ({r.status_code}): {r.text}")

    # Point in time: drop tickers that weren't in today's pull.
    r = requests.delete(rest, params={"as_of": f"lt.{as_of.isoformat()}"}, timeout=60,
                        headers={**headers, "Prefer": "return=representation"})
    if r.status_code >= 300:
        raise RuntimeError(f"Stale-row delete failed ({r.status_code}): {r.text}")
    removed = [x["symbol"] for x in r.json()]
    print(f"✅ Upserted {len(records)} rows into biotech_market; removed {len(removed)} stale"
          + (f": {', '.join(removed)}" if removed else ""))


def file_date(path):
    m = re.search(r"(\d{2})-(\d{2})-(\d{4})", os.path.basename(path))
    return datetime(int(m.group(3)), int(m.group(1)), int(m.group(2))).date() if m else None


def rotate_old_csvs(today):
    """Archive a previous month's file (it's that month's last snapshot); drop same-month ones."""
    os.makedirs(HISTORY_DIR, exist_ok=True)
    for path in glob.glob(os.path.join(HERE, "Biotechnology *.csv")):
        d = file_date(path)
        if d and (d.year, d.month) != (today.year, today.month):
            dest = os.path.join(HISTORY_DIR, f"Biotech_{d.strftime('%m-%d-%Y')}.csv.gz")
            with open(path, "rb") as src, gzip.open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
            print(f"📦 Month-end archive: {os.path.basename(path)} -> History/{os.path.basename(dest)}")
        os.remove(path)


def write_csv(records, today):
    path = os.path.join(HERE, f"Biotechnology {today.strftime('%m-%d-%Y')}.csv")

    def fmt(n):
        return "" if n is None else f"{n:g}"

    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(CSV_COLUMNS)
        for r in records:
            w.writerow([
                r["symbol"], r["name"] or "",
                "" if r["last_sale"] is None else f"${r['last_sale']:.2f}",
                fmt(r["net_change"]),
                "" if r["pct_change"] is None else f"{r['pct_change']:.3f}%",
                int(r["market_cap"] / 1_000_000) if r["market_cap"] else 0,  # $M, as before
                r["country"] or "", r["ipo_year"] or "", r["volume"] if r["volume"] is not None else "",
                r["sector"] or "", r["industry"],
            ])
    print(f"✅ Saved {os.path.basename(path)}")


def main():
    today = datetime.now(ZoneInfo("America/New_York")).date()

    if "--force" not in sys.argv and not is_trading_day(today):
        print(f"⏭️ {today} is not an NYSE trading day; nothing to do.")
        return

    records = to_records(fetch_screener(), today)
    print(f"🔄 {len(records)} biotech rows for {today}")
    if len(records) < MIN_ROWS:
        raise RuntimeError(f"Only {len(records)} rows (expected >= {MIN_ROWS}); refusing to write")

    if "--no-db" not in sys.argv:
        write_supabase(records, today)

    rotate_old_csvs(today)
    write_csv(records, today)


if __name__ == "__main__":
    main()

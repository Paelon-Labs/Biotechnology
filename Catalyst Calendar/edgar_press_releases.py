"""
Pull a company's press releases from SEC EDGAR.

Press releases reach EDGAR as EX-99.x exhibits on 8-K (and 8-K/A) filings.
This lists a company's 8-Ks in a lookback window, finds the exhibits that are
press releases, and extracts each release's headline.

Usage:
  python edgar_press_releases.py SION              # last 3 months
  python edgar_press_releases.py SION --months 6
  python edgar_press_releases.py SION --json

Env: SEC_USER_AGENT  required, "Company Name contact@email" - SEC returns 403
     to automated requests without a contact. Read from the environment or
     the repo-root .env.

Note: only releases the company chose to file on an 8-K show up here. Smaller
announcements (conference appearances, etc.) are often posted only on the
company's IR site / newswire.
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import date

import requests
from bs4 import BeautifulSoup

def _load_dotenv():
    """Fill os.environ from the repo-root .env (doesn't override real env vars)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        key, sep, value = line.strip().partition("=")
        if sep and not key.startswith("#"):
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()
SESSION = requests.Session()
SESSION.headers.update({"Accept-Encoding": "gzip, deflate"})

# SEC fair-access limit is 10 requests/second.
_last_request = 0.0


def sec_get(url):
    global _last_request
    if "User-Agent" not in SESSION.headers or "@" not in SESSION.headers["User-Agent"]:
        ua = os.environ.get("SEC_USER_AGENT", "")
        if "@" not in ua:
            raise SystemExit('Set SEC_USER_AGENT (e.g. "Paelon Labs you@yourdomain.com") in the '
                             "environment or the repo .env; SEC returns 403 without a contact email.")
        SESSION.headers["User-Agent"] = ua
    wait = 0.12 - (time.time() - _last_request)
    if wait > 0:
        time.sleep(wait)
    _last_request = time.time()
    r = SESSION.get(url, timeout=30)
    r.raise_for_status()
    return r


def lookup_cik(ticker):
    tickers = sec_get("https://www.sec.gov/files/company_tickers.json").json()
    for row in tickers.values():
        if row["ticker"].upper() == ticker.upper():
            return int(row["cik_str"]), row["title"]
    raise SystemExit(f"Ticker {ticker} not found in SEC company_tickers.json")


def months_ago(d, months):
    y, m = divmod(d.year * 12 + d.month - 1 - months, 12)
    m += 1
    days_in_month = [31, 29 if y % 4 == 0 and (y % 100 or y % 400 == 0) else 28,
                     31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
    return date(y, m, min(d.day, days_in_month))


def list_8ks(cik, since):
    """8-K / 8-K/A filings on or after `since` (newest first)."""
    data = sec_get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json").json()
    recent = data["filings"]["recent"]
    out = []
    for i, form in enumerate(recent["form"]):
        if form in ("8-K", "8-K/A") and recent["filingDate"][i] >= since.isoformat():
            out.append({
                "form": form,
                "filing_date": recent["filingDate"][i],
                "accession": recent["accessionNumber"][i],
                "items": recent["items"][i],
            })
    return out


def exhibit_99_docs(cik, accession):
    """(type, url) for each EX-99.x document in a filing, from its index page."""
    acc = accession.replace("-", "")
    html = sec_get(f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{accession}-index.html").text
    docs = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        if len(cells) < 4:
            continue
        doc_type = re.sub(r"<[^>]+>|&nbsp;", "", cells[3]).strip()
        href = re.search(r'href="([^"]+)"', cells[2])
        if doc_type.upper().startswith("EX-99") and href and re.search(r"\.html?$", href.group(1), re.I):
            path = href.group(1).replace("/ix?doc=", "")
            docs.append((doc_type, "https://www.sec.gov" + path))
    return docs


WIRE_RE = re.compile(r"GLOBE\s*NEWSWIRE|BUSINESS\s*WIRE|PR\s*NEWSWIRE|ACCESSWIRE|press release", re.I)
EXHIBIT_LABEL_RE = re.compile(r"^exhibit\s+99(\.\d+)?$", re.I)
BOLD_STYLE_RE = re.compile(r"font-weight\s*:\s*(bold|[6-9]00)", re.I)


def _clean(text):
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def _bold_el(el):
    return el.name in ("b", "strong") or bool(BOLD_STYLE_RE.search(el.get("style") or ""))


def _is_bold(block):
    """True if (nearly) all of the block's text sits inside bold markup."""
    total = bold = 0
    for s in block.find_all(string=True):
        n = len(_clean(s).replace(" ", ""))
        total += n
        el = s.parent
        while el is not None:
            if _bold_el(el):
                bold += n
                break
            if el is block:
                break
            el = el.parent
    return total > 0 and bold >= 0.9 * total


def text_blocks(soup):
    """Leaf-level paragraph/div blocks in document order."""
    for el in soup.find_all(["p", "div"]):
        if el.find(["p", "div"]):
            continue
        text = _clean(el.get_text(" "))
        if text:
            yield el, text


def extract_headline(html):
    """Headline = the first run of consecutive bold blocks (skipping the 'Exhibit 99.1' label)."""
    soup = BeautifulSoup(html, "lxml")
    title, first_text = [], None
    for el, text in text_blocks(soup):
        if EXHIBIT_LABEL_RE.match(text):
            continue
        first_text = first_text or text
        if _is_bold(el):
            title.append(text)
        elif title:
            break
        if len(title) >= 4:  # headlines don't run longer than this
            break
    return " ".join(title) if title else first_text


def press_releases(ticker, months=3):
    cik, company = lookup_cik(ticker)
    since = months_ago(date.today(), months)
    releases = []
    for f in list_8ks(cik, since):
        for doc_type, url in exhibit_99_docs(cik, f["accession"]):
            html = sec_get(url).text
            if not WIRE_RE.search(BeautifulSoup(html, "lxml").get_text(" ")):
                continue  # EX-99 that isn't a press release (e.g. investor deck)
            releases.append({
                "ticker": ticker.upper(),
                "company": company,
                "filing_date": f["filing_date"],
                "form": f["form"],
                "items": f["items"],
                "exhibit": doc_type,
                "title": extract_headline(html),
                "url": url,
            })
    return {"ticker": ticker.upper(), "company": company, "cik": cik,
            "since": since.isoformat(), "releases": releases}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ticker")
    ap.add_argument("--months", type=int, default=3)
    ap.add_argument("--json", action="store_true", help="print JSON instead of a list")
    args = ap.parse_args()

    result = press_releases(args.ticker, args.months)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return

    print(f"{result['company']} ({result['ticker']}), 8-K press releases since {result['since']}:")
    if not result["releases"]:
        print("  (none)")
    for r in result["releases"]:
        print(f"  {r['filing_date']}  {r['title']}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()

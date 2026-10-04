"""
Catalyst tracker: keeps each company's expected readout / milestone dates current.

For every new press release (EDGAR 8-K EX-99.x, oldest first) the LLM is shown
the company's current catalyst list plus the release text, and returns what the
release says about timing: new catalysts, restated timing, or events that
happened / were dropped. Timing comes back structured (kind + year + value), and
the date window and change type (refined / delayed / accelerated / reaffirmed)
are computed here, deterministically, so "mid-2026" -> "summer of 2026" shows up
as a refinement with a narrower window.

State lives in data/<TICKER>.json (processed sources, catalysts with full
timing history, and token / cost totals).

Usage:
  python catalyst_tracker.py SION                     # process new EDGAR releases (12-month backfill on first run)
  python catalyst_tracker.py SION --months 18         # longer first-run backfill
  python catalyst_tracker.py SION --doc rel.htm --date 2026-08-06 [--title "..."]   # process a local file
  python catalyst_tracker.py SION --show              # print the catalyst calendar
  python catalyst_tracker.py SION --reset             # forget state for the ticker

Env (repo-root .env): OPENAI_API_KEY, SEC_USER_AGENT (EDGAR runs only)
"""
import argparse
import calendar
import json
import os
import re
import sys
from datetime import date

import requests
from bs4 import BeautifulSoup

import edgar_press_releases as edgar  # also loads the repo-root .env

MODEL = os.environ.get("CATALYST_MODEL", "gpt-6-luna")
# USD per 1M tokens (gpt-6-luna standard tier). Override via env if pricing changes.
PRICE_IN = float(os.environ.get("CATALYST_PRICE_IN", "0.10"))
PRICE_CACHED = float(os.environ.get("CATALYST_PRICE_CACHED", "0.01"))
PRICE_OUT = float(os.environ.get("CATALYST_PRICE_OUT", "0.50"))

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

EVENT_TYPES = ["topline_data", "interim_data", "full_data", "data_presentation", "trial_initiation",
               "enrollment_complete", "regulatory_submission", "regulatory_decision", "advisory_committee",
               "commercial_launch", "partnership", "other"]
TIMING_KINDS = ["date", "month", "quarter", "half", "season", "early", "mid", "late", "year",
                "by_end_of", "unspecified"]

# ---------------------------------------------------------------- timing → window

SEASONS = {"winter": (12, 2), "spring": (3, 5), "summer": (6, 8), "fall": (9, 11), "autumn": (9, 11)}


def _end(y, m):
    return date(y, m, calendar.monthrange(y, m)[1])


def timing_window(t, as_of):
    """(start, end) dates for a structured timing, or (None, None)."""
    if not t or t.get("kind") in (None, "unspecified") or not t.get("year"):
        return None, None
    y, kind, v = int(t["year"]), t["kind"], (t.get("value") or "").strip().lower()
    try:
        if kind == "date":
            d = date.fromisoformat(v)
            return d, d
        if kind == "month":
            m = int(v)
            return date(y, m, 1), _end(y, m)
        if kind == "quarter":
            q = int(v.lstrip("q"))
            return date(y, 3 * q - 2, 1), _end(y, 3 * q)
        if kind == "half":
            h = int(v.lstrip("h"))
            return (date(y, 1, 1), _end(y, 6)) if h == 1 else (date(y, 7, 1), _end(y, 12))
        if kind == "season":
            a, b = SEASONS[v]
            return (date(y - 1, 12, 1), _end(y, 2)) if v == "winter" else (date(y, a, 1), _end(y, b))
        if kind == "early":
            return date(y, 1, 1), _end(y, 4)
        if kind == "mid":
            return date(y, 5, 1), _end(y, 8)
        if kind == "late":
            return date(y, 9, 1), _end(y, 12)
        if kind == "year":
            return date(y, 1, 1), _end(y, 12)
        if kind == "by_end_of":
            return as_of, _end(y, 12)
    except (ValueError, KeyError):
        pass
    return None, None


def classify_change(old_start, old_end, new_start, new_end):
    if not new_start:
        return "restated"
    if not old_start:
        return "dated"
    if (new_start, new_end) == (old_start, old_end):
        return "reaffirmed"
    if new_start >= old_start and new_end <= old_end:
        return "refined"
    if new_end > old_end and new_start >= old_start:
        return "delayed"
    if new_start < old_start and new_end <= old_end:
        return "accelerated"
    return "rewindowed"


# ---------------------------------------------------------------- document text

FLS_RE = re.compile(r"(Cautionary Note Regarding|Forward[- ]Looking Statements|Safe Harbor)", re.I)


def release_text(html):
    """Narrative text of a release: numeric tables and the forward-looking boilerplate dropped."""
    soup = BeautifulSoup(html, "lxml")
    for t in soup.find_all(["script", "style", "head"]):
        t.decompose()
    for t in soup.find_all("table"):
        txt = t.get_text(" ")
        if sum(c.isdigit() for c in txt) > 0.15 * max(1, len(txt.replace(" ", ""))):
            t.decompose()
    text = re.sub(r"[ \t\r\f\v]+", " ", soup.get_text("\n"))
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    m = FLS_RE.search(text, 1500)
    return text[:m.start()] if m else text


# ---------------------------------------------------------------- LLM

SYSTEM = f"""You maintain a biotech catalyst calendar: the expected timing of upcoming company milestones
(clinical data readouts, trial starts, enrollment completion, regulatory filings/decisions, launches).

You get the company's CURRENT catalyst list (JSON) and ONE new press release dated AS_OF.
Return every statement in the release about the timing or status of a milestone:

- action "update": the release restates timing for a catalyst already in the list (same program/trial/event),
  even if unchanged. Set catalyst_id to that catalyst's id.
- action "new": a forward-looking milestone not in the list. catalyst_id = null.
- action "occurred": the release reports the milestone happened (data reported, trial started, filing made...).
- action "discontinued": the company dropped the program/trial/milestone or says it will not proceed.
Ignore past events that aren't on the list, financial guidance (cash runway), and generic conference attendance.

timing (null for occurred/discontinued):
  text: the exact phrase the company uses, e.g. "summer of 2026".
  kind: one of {TIMING_KINDS}
  year: the calendar year (resolve "this year" / "next year" against AS_OF)
  value: date -> "YYYY-MM-DD"; month -> "1".."12"; quarter -> "1".."4"; half -> "1" or "2";
         season -> winter|spring|summer|fall; otherwise "".
  Examples: "mid-2026" -> mid/2026; "summer of this year" -> season/summer; "second half of 2026" -> half/2;
            "first quarter of 2027" -> quarter/1/2027; "by year-end" -> by_end_of; "this year" -> year.
quote: the verbatim sentence (or bullet) the information comes from.
Use the program's code name as program (e.g. "SION-719"); for combinations join with " + "."""

TIMING_SCHEMA = {
    "type": ["object", "null"],
    "properties": {
        "text": {"type": "string"},
        "kind": {"type": "string", "enum": TIMING_KINDS},
        "year": {"type": ["integer", "null"]},
        "value": {"type": "string"},
    },
    "required": ["text", "kind", "year", "value"],
    "additionalProperties": False,
}
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "changes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["new", "update", "occurred", "discontinued"]},
                    "catalyst_id": {"type": ["string", "null"]},
                    "program": {"type": "string"},
                    "indication": {"type": ["string", "null"]},
                    "trial": {"type": ["string", "null"]},
                    "event": {"type": "string"},
                    "event_type": {"type": "string", "enum": EVENT_TYPES},
                    "timing": TIMING_SCHEMA,
                    "quote": {"type": "string"},
                },
                "required": ["action", "catalyst_id", "program", "indication", "trial", "event",
                             "event_type", "timing", "quote"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["changes"],
    "additionalProperties": False,
}


def call_llm(catalysts, doc_text, as_of, title):
    key = os.environ.get("OPENAI_API_KEY", "").strip().strip('"')
    if not key:
        raise SystemExit("OPENAI_API_KEY missing (env or repo .env)")
    current = [{k: c[k] for k in ("id", "program", "trial", "event", "event_type", "status", "timing_text")}
               for c in catalysts]
    body = {
        "model": MODEL,
        "instructions": SYSTEM,
        "input": f"AS_OF: {as_of}\nCURRENT CATALYSTS:\n{json.dumps(current, indent=1)}\n\n"
                 f"PRESS RELEASE: {title}\n{doc_text}",
        "text": {"format": {"type": "json_schema", "name": "catalyst_changes", "strict": True,
                            "schema": RESPONSE_SCHEMA}},
    }
    r = requests.post("https://api.openai.com/v1/responses", json=body, timeout=180,
                      headers={"Authorization": f"Bearer {key}"})
    if r.status_code >= 300:
        raise RuntimeError(f"OpenAI {r.status_code}: {r.text[:500]}")
    d = r.json()
    text = next(c["text"] for o in d["output"] if o["type"] == "message"
                for c in o["content"] if c["type"] == "output_text")
    u = d.get("usage") or {}
    cached = (u.get("input_tokens_details") or {}).get("cached_tokens", 0)
    usage = {"input": u.get("input_tokens", 0), "cached": cached, "output": u.get("output_tokens", 0)}
    usage["cost"] = ((usage["input"] - cached) * PRICE_IN + cached * PRICE_CACHED
                     + usage["output"] * PRICE_OUT) / 1e6
    return json.loads(text)["changes"], usage


# ---------------------------------------------------------------- state

def state_path(ticker):
    return os.path.join(DATA_DIR, f"{ticker.upper()}.json")


def load_state(ticker):
    if os.path.exists(state_path(ticker)):
        return json.load(open(state_path(ticker), encoding="utf-8"))
    return {"ticker": ticker.upper(), "processed": [], "catalysts": [],
            "usage": {"calls": 0, "input": 0, "cached": 0, "output": 0, "cost": 0.0}}


def save_state(state):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(state_path(state["ticker"]), "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def apply_changes(state, changes, as_of, source):
    by_id = {c["id"]: c for c in state["catalysts"]}
    log = []
    for ch in changes:
        t = ch["timing"]
        start, end = timing_window(t, date.fromisoformat(as_of))
        entry = {"date": as_of, "source": source, "quote": ch["quote"],
                 "timing_text": t["text"] if t else None,
                 "window_start": start.isoformat() if start else None,
                 "window_end": end.isoformat() if end else None}
        cat = by_id.get(ch["catalyst_id"] or "")
        if cat is not None and cat["status"] != "upcoming":
            continue  # retrospective mention of an event already resolved

        if ch["action"] == "new" or cat is None:
            if ch["action"] in ("occurred", "discontinued"):
                continue  # past event we weren't tracking
            if end and end.isoformat() < as_of:
                continue  # dated in the past: history, not a catalyst
            cid = base = _slug(f"{ch['program']} {ch['event']}")
            n = 2
            while cid in by_id:
                cid, n = f"{base}-{n}", n + 1
            cat = {"id": cid, "program": ch["program"], "indication": ch["indication"], "trial": ch["trial"],
                   "event": ch["event"], "event_type": ch["event_type"], "status": "upcoming",
                   "timing_text": entry["timing_text"], "timing": t,
                   "window_start": entry["window_start"], "window_end": entry["window_end"],
                   "first_seen": as_of, "last_confirmed": as_of, "history": []}
            state["catalysts"].append(cat)
            by_id[cid] = cat
            entry["change"] = "new"
        elif ch["action"] == "update":
            old_s = date.fromisoformat(cat["window_start"]) if cat["window_start"] else None
            old_e = date.fromisoformat(cat["window_end"]) if cat["window_end"] else None
            entry["change"] = classify_change(old_s, old_e, start, end)
            if entry["change"] == "reaffirmed" and t and t["text"] != cat["timing_text"]:
                entry["change"] = "reworded"
            cat["last_confirmed"] = as_of
            if start:
                cat.update(timing_text=entry["timing_text"], timing=t,
                           window_start=entry["window_start"], window_end=entry["window_end"])
        else:
            entry["change"] = ch["action"]
            cat["status"] = ch["action"]
            cat["resolved_on"] = as_of

        cat["history"].append(entry)
        log.append((entry["change"], cat["id"], entry["timing_text"], entry["window_start"], entry["window_end"]))
    return log


def process_doc(state, html, as_of, title, source):
    text = release_text(html)
    changes, usage = call_llm(state["catalysts"], text, as_of, title)
    log = apply_changes(state, changes, as_of, source)
    state["processed"].append({"source": source, "date": as_of, "title": title, "chars": len(text), **usage})
    for k in ("input", "cached", "output", "cost"):
        state["usage"][k] += usage[k]
    state["usage"]["calls"] += 1
    print(f"\n{as_of}  {title}")
    print(f"   {len(text):,} chars -> {usage['input']:,} in / {usage['output']:,} out tokens, ${usage['cost']:.5f}")
    for change, cid, ttext, ws, we in log:
        win = f"  [{ws} .. {we}]" if ws else ""
        print(f"   {change:<12} {cid}: {ttext or ''}{win}")
    if not log:
        print("   (no catalyst timing statements)")


# ---------------------------------------------------------------- CLI

def show(state):
    print(f"\n{state['ticker']} catalyst calendar")
    order = {"upcoming": 0, "occurred": 1, "discontinued": 2}
    for c in sorted(state["catalysts"], key=lambda c: (order.get(c["status"], 3), c["window_start"] or "9999")):
        win = f"{c['window_start']} .. {c['window_end']}" if c["window_start"] else "no date"
        trail = " -> ".join(h["timing_text"] for h in c["history"] if h.get("timing_text"))
        print(f"  [{c['status']:<12}] {c['program']}: {c['event']}")
        print(f"      {c.get('timing_text') or '-'}  ({win})")
        if trail:
            print(f"      guidance: {trail}")
    u = state["usage"]
    print(f"\n  LLM usage: {u['calls']} calls, {u['input']:,} in / {u['output']:,} out tokens, ${u['cost']:.4f}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ticker")
    ap.add_argument("--months", type=int, default=12, help="first-run EDGAR backfill window")
    ap.add_argument("--doc", help="process a local press-release HTML file instead of EDGAR")
    ap.add_argument("--date", help="as-of date for --doc (YYYY-MM-DD)")
    ap.add_argument("--title", default="", help="title for --doc")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--reset", action="store_true")
    args = ap.parse_args()
    ticker = args.ticker.upper()

    if args.reset:
        if os.path.exists(state_path(ticker)):
            os.remove(state_path(ticker))
        print(f"reset {ticker}")
        return

    state = load_state(ticker)
    if args.show:
        show(state)
        return

    if args.doc:
        if not args.date:
            raise SystemExit("--doc needs --date")
        html = open(args.doc, encoding="utf-8", errors="ignore").read()
        title = args.title or edgar.extract_headline(html)
        process_doc(state, html, args.date, title, os.path.basename(args.doc))
        save_state(state)
        return

    cik, company = edgar.lookup_cik(ticker)
    state["company"] = company
    done = {p["source"] for p in state["processed"]}
    since = (date.fromisoformat(max(p["date"] for p in state["processed"]))
             if state["processed"] else edgar.months_ago(date.today(), args.months))
    new = 0
    for f in sorted(edgar.list_8ks(cik, since), key=lambda f: f["filing_date"]):
        for doc_type, url in edgar.exhibit_99_docs(cik, f["accession"]):
            if url in done:
                continue
            html = edgar.sec_get(url).text
            if not edgar.WIRE_RE.search(BeautifulSoup(html, "lxml").get_text(" ")):
                continue
            process_doc(state, html, f["filing_date"], edgar.extract_headline(html), url)
            save_state(state)
            new += 1
    print(f"\n{new} new release(s) processed for {ticker}.")
    show(state)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()

#!/usr/bin/env python3
"""
Sync a Google Calendar (public iCal URL) -> events.json for the website.

Mum's contract (she only ever touches Google Calendar):
  - Title            = event name (e.g. "Kundalini Yoga Class")
  - Date / Time      = when the class runs
  - Description      = FIRST LINE = booking link (e.g. a Stripe Payment Link
                       https://buy.stripe.com/...). Remaining lines = blurb.
  - Location         = venue

Category + price are auto-detected from the title / description, so mum
doesn't have to remember any codes.

Env: ICAL_URL  (the public iCal link for mum's yoga calendar)
"""
import json
import os
import re
import sys
import urllib.request
from datetime import datetime

OUT = os.path.join(os.path.dirname(__file__), "..", "events.json")

CATEGORY_RULES = [
    ("sadhana", ["sadhana"]),
    ("sound", ["gong", "sound bath", "sound healing"]),
    ("workshop", ["workshop", "circle", "retreat", "full moon", "new moon"]),
]
URL_RE = re.compile(r"https?://[^\s,\\]+")
PRICE_RE = re.compile(r"£\s*(\d+(?:\.\d+)?)|\b(\d+(?:\.\d+)?)\s*gbp\b", re.IGNORECASE)


def unfold(text):
    lines = []
    for line in text.splitlines():
        if line.startswith((" ", "\t")) and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return lines


def parse_ics(text):
    events = []
    cur = None
    for line in unfold(text):
        if line == "BEGIN:VEVENT":
            cur = {}
        elif line == "END:VEVENT":
            if cur is not None:
                events.append(cur)
                cur = None
        elif cur is not None and ":" in line:
            prop, _, value = line.partition(":")
            prop = prop.split(";", 1)[0]
            value = value.replace("\\n", "\n").replace("\\,", ",").replace("\\;", ";")
            cur.setdefault(prop, value)
    return events


def parse_dt(s):
    # returns (datetime, is_all_day)
    s = s.strip()
    if "T" not in s:
        return datetime.strptime(s[:8], "%Y%m%d"), True
    return datetime.strptime(s[:15], "%Y%m%dT%H%M%S"), False


def fmt_time(dt):
    h = dt.hour
    ampm = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12}:{dt.minute:02d} {ampm}"


def detect_category(title):
    t = title.lower()
    for cat, keywords in CATEGORY_RULES:
        if any(k in t for k in keywords):
            return cat
    return "class"


def main():
    url = os.environ.get("ICAL_URL", "").strip()
    if not url or url.startswith("REPLACE_"):
        print("ICAL_URL not set; skipping sync.", file=sys.stderr)
        sys.exit(0)

    req = urllib.request.Request(url, headers={"User-Agent": "kundalini-sync"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        ics = resp.read().decode("utf-8", "replace")

    rows = []
    now = datetime.now()
    for e in parse_ics(ics):
        if "DTSTART" not in e:
            continue
        try:
            start, all_day = parse_dt(e["DTSTART"])
        except Exception:
            continue
        # only future + this month events (skip long-past)
        if start < now and (now - start).days > 60:
            continue

        title = e.get("SUMMARY", "Yoga Class").strip()
        desc = e.get("DESCRIPTION", "").strip()
        location = e.get("LOCATION", "").strip()

        # first URL in the description = booking link
        urls = URL_RE.findall(desc)
        payment_link = urls[0] if urls else ""

        # price from description then title (£10 / 10 gbp)
        price = 0
        for m in PRICE_RE.finditer(desc + " " + title):
            price = float(m.group(1) or m.group(2))
            break

        # description blurb = everything after the booking link line
        blurb = desc
        if payment_link:
            blurb = desc.replace(payment_link, "").strip(" \n")

        end = None
        if "DTEND" in e:
            try:
                end, _ = parse_dt(e["DTEND"])
            except Exception:
                end = None
        if all_day or (end and start == end):
            time_str = "All day"
        elif end:
            time_str = f"{fmt_time(start)} – {fmt_time(end)}"
        else:
            time_str = fmt_time(start)

        rows.append(
            {
                "id": e.get("UID", title + start.isoformat()),
                "title": title,
                "date": start.strftime("%Y-%m-%dT%H:%M:%S"),
                "time": time_str,
                "category": detect_category(title),
                "location": location or "The Glebe Centre, Rochdale",
                "price": price,
                "paymentLink": payment_link,
                "description": blurb or f"{title} with your teacher.",
            }
        )

    rows.sort(key=lambda r: r["date"])
    with open(OUT, "w") as f:
        json.dump(rows, f, indent=2)
    print(f"Wrote {len(rows)} events to {OUT}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Sync a Google Calendar (public iCal URL) -> events.json for the website.

Uses the `icalendar` + `dateutil` libraries so recurring events ("repeat weekly"),
EXDATE exceptions, and timezones are handled correctly.

Mum's contract (she only ever touches Google Calendar):
  - Title            = event name (e.g. "Kundalini Yoga Class")
  - Date / Time      = when the class runs (repeat weekly is fine)
  - Description      = FIRST LINE = booking link (e.g. a Stripe Payment Link
                       https://buy.stripe.com/...). Remaining lines = blurb.
  - Location         = venue

Category + price are auto-detected, so mum doesn't remember any codes.

Env: ICAL_URL  (the public iCal link for mum's yoga calendar)
"""
import json
import os
import re
import sys
import traceback
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except Exception:  # python < 3.9 fallback (shouldn't happen)
    from backports.zoneinfo import ZoneInfo

from icalendar import Calendar
from dateutil.rrule import rrulestr

OUT = os.path.join(os.path.dirname(__file__), "..", "events.json")
LONDON = ZoneInfo("Europe/London")

CATEGORY_RULES = [
    ("sadhana", ["sadhana"]),
    ("sound", ["gong", "sound bath", "sound healing"]),
    ("workshop", ["workshop", "circle", "retreat", "full moon", "new moon"]),
]

URL_RE = re.compile(r"https?://[^\s,\\]+")
PRICE_RE = re.compile(r"£\s*(\d+(?:\.\d+)?)|\b(\d+(?:\.\d+)?)\s*gbp\b", re.IGNORECASE)


def wallclock(dt):
    """Return naive wall-clock datetime in the event's own timezone."""
    if dt.tzinfo is not None:
        return dt.replace(tzinfo=None)
    return dt


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


def get_exdates(vevent):
    """Return a set of naive wall-clock start datetimes to exclude."""
    ex = set()
    exdate = vevent.get("EXDATE")
    if exdate is None:
        return ex
    items = exdate if isinstance(exdate, list) else [exdate]
    for item in items:
        dts = getattr(item, "dts", [item]) or [item]
        for pair in dts:
            dt = getattr(pair, "dt", pair)
            if isinstance(dt, (datetime,)):
                ex.add(wallclock(dt).replace(second=0, microsecond=0))
    return ex


def build_row(summary, desc, location, start, end, uid):
    title = (summary or "Yoga Class").strip()
    desc = (desc or "").strip()
    location = (location or "").strip()

    urls = URL_RE.findall(desc)
    payment_link = urls[0] if urls else ""

    price = 0
    for m in PRICE_RE.finditer(desc + " " + title):
        price = float(m.group(1) or m.group(2))
        break

    blurb = desc
    if payment_link:
        blurb = desc.replace(payment_link, "").strip(" \n")

    if end and start.date() == end.date() and start.time() == end.time():
        time_str = "All day"
    elif end:
        time_str = f"{fmt_time(start)} – {fmt_time(end)}"
    else:
        time_str = fmt_time(start)

    return {
        "id": (uid or title + start.isoformat()) + "@" + start.strftime("%Y%m%dT%H%M"),
        "title": title,
        "date": start.strftime("%Y-%m-%dT%H:%M:%S"),
        "end": end.strftime("%Y-%m-%dT%H:%M:%S") if end else None,
        "time": time_str,
        "category": detect_category(title),
        "location": location or "The Glebe Centre, Rochdale",
        "price": price,
        "paymentLink": payment_link,
        "description": blurb or f"{title} with your teacher.",
    }


def expand(vevent, window_start, window_end):
    """Yield (start, end) naive wall-clock datetimes for this VEVENT.

    window_start / window_end are London-aware datetimes.
    """
    dtstart_prop = vevent.get("DTSTART")
    if dtstart_prop is None:
        return
    dtstart = dtstart_prop.dt

    # Normalise dtstart to a London-aware datetime.
    if isinstance(dtstart, datetime):
        if dtstart.tzinfo is None:
            dtstart = dtstart.replace(tzinfo=LONDON)
    else:
        # date (all-day) -> midnight London
        dtstart = datetime.combine(dtstart, datetime.min.time(), tzinfo=LONDON)

    dtend = vevent.get("DTEND")
    duration = None
    if dtend is not None:
        end_val = dtend.dt
        if isinstance(end_val, datetime):
            if end_val.tzinfo is None:
                end_val = end_val.replace(tzinfo=LONDON)
            duration = end_val - dtstart
        else:
            end_val = datetime.combine(end_val, datetime.min.time(), tzinfo=LONDON)
            duration = end_val - dtstart

    exdates = get_exdates(vevent)

    starts = []
    if "RRULE" in vevent:
        rrule_val = vevent["RRULE"]
        rrule_str = "RRULE:" + rrule_val.to_ical().decode()
        try:
            rule = rrulestr(rrule_str, dtstart=dtstart)
            occ = list(rule.between(window_start, window_end, inc=True))
        except Exception:
            traceback.print_exc()
            occ = [dtstart]
        for o in occ:
            starts.append(o)
    else:
        starts.append(dtstart)

    for s in starts:
        sw = wallclock(s).replace(second=0, microsecond=0)
        if sw in exdates:
            continue
        e = None
        if duration is not None:
            e = s + duration
        yield (sw, wallclock(e).replace(second=0, microsecond=0) if e else None)


def main():
    url = os.environ.get("ICAL_URL", "").strip()
    if not url or url.startswith("REPLACE_"):
        print("ICAL_URL not set; skipping sync.", file=sys.stderr)
        sys.exit(0)

    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "kundalini-sync"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        ics = resp.read().decode("utf-8", "replace")

    try:
        cal = Calendar.from_ical(ics)
    except Exception:
        traceback.print_exc()
        print("Failed to parse iCal; leaving events.json untouched.", file=sys.stderr)
        sys.exit(0)

    now = datetime.now(LONDON)
    window_start = now - timedelta(days=60)
    window_end = now + timedelta(days=180)

    rows = []
    for vevent in cal.walk("VEVENT"):
        summary = vevent.get("SUMMARY")
        desc = vevent.get("DESCRIPTION")
        location = vevent.get("LOCATION")
        uid = vevent.get("UID")
        # decode string props
        s_str = summary.to_ical().decode() if summary is not None else ""
        d_str = desc.to_ical().decode() if desc is not None else ""
        l_str = location.to_ical().decode() if location is not None else ""
        u_str = uid.to_ical().decode() if uid is not None else None
        # unescape per iCal rules
        s_str = s_str.replace("\\n", "\n").replace("\\,", ",").replace("\\;", ";")
        d_str = d_str.replace("\\n", "\n").replace("\\,", ",").replace("\\;", ";")
        l_str = l_str.replace("\\n", "\n").replace("\\,", ",").replace("\\;", ";")

        try:
            for start, end in expand(vevent, window_start, window_end):
                rows.append(build_row(s_str, d_str, l_str, start, end, u_str))
        except Exception:
            traceback.print_exc()
            continue

    rows.sort(key=lambda r: r["date"])
    with open(OUT, "w") as f:
        json.dump(rows, f, indent=2)
    print(f"Wrote {len(rows)} events to {OUT}")


if __name__ == "__main__":
    main()

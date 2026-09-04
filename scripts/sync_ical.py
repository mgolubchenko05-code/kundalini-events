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
    """Return naive wall-clock datetime in London time."""
    if dt.tzinfo is not None:
        return dt.astimezone(LONDON).replace(tzinfo=None)
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


def build_row(summary, desc, location, start, end, uid, is_all_day=False):
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
    # drop a leftover "Payment Link:" label line (the URL itself is the Book button)
    def _is_label(line: str) -> bool:
        cleaned = re.sub(r"[^a-z0-9: ]", "", line.lower()).replace(" ", "")
        return cleaned in ("paymentlink", "paymentlink:")
    blurb = "\n".join(ln for ln in blurb.split("\n") if not _is_label(ln)).strip(" \n")

    if is_all_day:
        time_str = "All day"
    elif end and start.date() == end.date() and start.time() == end.time():
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
    """Yield (start, end, is_all_day) for this VEVENT.

    window_start / window_end are London-aware datetimes. Non-recurring
    events outside the window are skipped; recurring events are expanded
    only within the window.
    """
    dtstart_prop = vevent.get("DTSTART")
    if dtstart_prop is None:
        return
    dtstart = dtstart_prop.dt
    is_all_day = not isinstance(dtstart, datetime)

    # Normalise dtstart to a London wall-clock datetime.
    if isinstance(dtstart, datetime):
        if dtstart.tzinfo is not None:
            dtstart = dtstart.astimezone(LONDON)
        else:
            dtstart = dtstart.replace(tzinfo=LONDON)
    else:
        # date (all-day) -> midnight London
        dtstart = datetime.combine(dtstart, datetime.min.time(), tzinfo=LONDON)

    dtend = vevent.get("DTEND")
    duration = None
    if dtend is not None:
        end_val = dtend.dt
        if isinstance(end_val, datetime):
            if end_val.tzinfo is not None:
                end_val = end_val.astimezone(LONDON)
            else:
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
        # single (non-recurring) event — include only if it falls in the window
        if window_start <= dtstart <= window_end:
            starts.append(dtstart)

    for s in starts:
        sw = wallclock(s).replace(second=0, microsecond=0)
        if sw in exdates:
            continue
        e = None
        if duration is not None:
            e = s + duration
        yield (sw, wallclock(e).replace(second=0, microsecond=0) if e else None, is_all_day)


def main():
    url = os.environ.get("ICAL_URL", "").strip()
    if not url or url.startswith("REPLACE_"):
        print("ICAL_URL not set; skipping sync.", file=sys.stderr)
        sys.exit(0)

    import urllib.request

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "kundalini-sync"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            ics = resp.read().decode("utf-8", "replace")
        cal = Calendar.from_ical(ics)
    except Exception:
        traceback.print_exc()
        print("Failed to fetch/parse iCal; leaving events.json untouched.", file=sys.stderr)
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
            for start, end, is_all_day in expand(vevent, window_start, window_end):
                rows.append(build_row(s_str, d_str, l_str, start, end, u_str, is_all_day))
        except Exception:
            traceback.print_exc()
            continue

    rows.sort(key=lambda r: r["date"])
    with open(OUT, "w") as f:
        json.dump(rows, f, indent=2)
    print(f"Wrote {len(rows)} events to {OUT}")


if __name__ == "__main__":
    main()

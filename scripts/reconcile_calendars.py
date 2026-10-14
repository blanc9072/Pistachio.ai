"""
Throwaway diagnostic — NOT wired into the app (no api/ or core/ module
imports this). Run directly: python3 scripts/reconcile_calendars.py

Prints, side by side for the next 30 days, every calendar EventKit sees
vs. every calendar the Google provider sees, plus a flagged "Apple-only"
section — calendars that would go INVISIBLE if the live read/write path
were ever flipped from EventKit to Google (CALENDAR_BACKEND is NOT
flipped by this script or by running it; it only reads).

CRITICAL: the Apple-only section is NOT derived by title-matching against
Google. A same-named calendar on two different accounts (Home, Family,
Birthdays all collide here) is NOT the same calendar — Google's "Home"
does not cover iCloud's "Home". The reliable signal is each EventKit
calendar's SOURCE ACCOUNT (EKCalendar.source().title(), exposed via
calendar_read_ek.enumerate_calendars()'s "source" field) — any calendar
whose source isn't "Google" is invisible to the Google provider,
regardless of what it's named.

This is the gate: a human reads this output and decides, per Apple-only
WRITABLE calendar (Home, Work), whether it holds real commitments that
must be moved into a Google calendar before any future cutover. This
script makes no decision and changes nothing.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

import asyncio
from datetime import datetime, timedelta

import core.db as db
import core.calendar_google as cg
import core.calendar_read_ek as ek

DEV_USER_ID = "fa29e410-27a4-402c-9383-20b4c282ce1b"
WINDOW_DAYS = 30
SAMPLE_SIZE = 10


async def google_rows(creds) -> list[dict]:
    """[{"title", "writable", "count"}, ...]."""
    cals = await cg.enumerate_calendars(creds)
    if not cals:
        print("GOOGLE: enumerate_calendars returned nothing (not connected, or genuinely zero calendars)")
        return []

    start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    end = (start + timedelta(days=WINDOW_DAYS)).replace(hour=23, minute=59, second=59)
    read = await cg.read_window(creds, start, end, writable_only=False)
    if read["status"] == "needs_connection":
        print("GOOGLE: needs_connection — no stored/valid refresh token")
        return []
    if read["status"] != "ok":
        print(f"GOOGLE: read_window status={read['status']!r} — treating as unreachable, not zero")
        return []

    # events' "calendar" field is the real calendarId (see calendar_google's
    # read_window) — map id -> title/writable via enumerate_calendars.
    counts: dict[str, int] = {c["id"]: 0 for c in cals}
    for ev in read["events"]:
        if ev["calendar"] in counts:
            counts[ev["calendar"]] += 1

    return [{"title": c["title"], "writable": c["writable"], "count": counts.get(c["id"], 0)} for c in cals]


async def eventkit_data() -> tuple[list[dict], dict]:
    """Returns (rows, events_by_calendar_title) — rows is
    [{"title", "writable", "source", "count"}, ...]; the second value lets
    the caller pull a title SAMPLE for any specific calendar without a
    second read."""
    cals = await ek.enumerate_calendars()
    if not cals:
        print("EVENTKIT: enumerate_calendars returned nothing (no permission, or genuinely zero calendars)")
        return [], {}

    start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    end = (start + timedelta(days=WINDOW_DAYS)).replace(hour=23, minute=59, second=59)
    read = await ek.read_window(start, end, writable_only=False)
    if read["status"] == "needs_permission":
        print("EVENTKIT: needs_permission — Calendar access not granted")
        return [], {}
    if read["status"] != "ok":
        print(f"EVENTKIT: read_window status={read['status']!r} — treating as unreachable, not zero")
        return [], {}

    by_cal: dict[str, list[dict]] = {c["title"]: [] for c in cals}
    for ev in read["events"]:
        if ev["calendar"] in by_cal:
            by_cal[ev["calendar"]].append(ev)

    rows = [
        {"title": c["title"], "writable": c["writable"], "source": c["source"], "count": len(by_cal.get(c["title"], []))}
        for c in cals
    ]
    return rows, by_cal


def print_table(label: str, rows: list[dict], show_source: bool = False) -> None:
    print(f"\n=== {label} ({len(rows)} calendars) ===")
    if not rows:
        print("  (none)")
        return
    for r in sorted(rows, key=lambda x: x["title"].lower()):
        src = f"  source={r['source']:22}" if show_source else ""
        print(f"  {r['title']:35} writable={str(r['writable']):5}{src}  events(next {WINDOW_DAYS}d)={r['count']}")


def print_sample(label: str, events_by_cal: dict, title: str) -> None:
    events = sorted(events_by_cal.get(title, []), key=lambda e: e["start"] or "")
    print(f"\n--- {label} ({title!r}): {len(events)} event(s) in the next {WINDOW_DAYS} days ---")
    if not events:
        print("  (empty)")
        return
    for ev in events[:SAMPLE_SIZE]:
        print(f"  {ev['start']} | {ev['title']!r}")
    if len(events) > SAMPLE_SIZE:
        print(f"  ... and {len(events) - SAMPLE_SIZE} more")


async def main():
    token = await db.fetch_google_refresh_token(DEV_USER_ID)
    creds = cg.credentials_for_token(token)

    print(f"Window: next {WINDOW_DAYS} days from {datetime.now().date()}")
    print(f"Google connected: {creds is not None}")

    g_rows = await google_rows(creds)
    ek_rows, ek_events_by_cal = await eventkit_data()

    print_table("GOOGLE", g_rows)
    print_table("EVENTKIT", ek_rows, show_source=True)

    # NOT title-matching -- source().title() != "Google" is the reliable
    # "will this go invisible after cutover" signal.
    apple_only = [r for r in ek_rows if r["source"] != "Google"]
    print(f"\n=== Apple-only — INVISIBLE after cutover ({len(apple_only)} calendars) ===")
    print("(grouped by EventKit source account, NOT by title match against Google --")
    print(" a same-named Google calendar does NOT cover these)")
    if not apple_only:
        print("  (none)")
    else:
        for r in sorted(apple_only, key=lambda x: (not x["writable"], x["title"].lower())):
            flag = " <-- WRITABLE, check for commitments" if r["writable"] else ""
            print(f"  {r['title']:35} writable={str(r['writable']):5}  source={r['source']:22}  events(next {WINDOW_DAYS}d)={r['count']}{flag}")

    print("\n" + "=" * 70)
    print("THE TWO THAT DECIDE THE GATE: iCloud Home + iCloud Work")
    print("(Home is Mac Calendar's default writable calendar -- the most likely")
    print(" landing spot for osascript-created events so far, per the executor's")
    print(" 'first calendar whose writable is true' targeting)")
    print("=" * 70)
    print_sample("iCloud Home", ek_events_by_cal, "Home")
    print_sample("iCloud Work", ek_events_by_cal, "Work")

    print(
        "\nGATE: for each of Home/Work above, decide if it holds real "
        "commitments Pistachio must schedule around. If yes, create a "
        "matching Google calendar and move those events there before any "
        "flip. If it's empty/test/don't-care, it goes invisible and the "
        "flip is clean. This script changes nothing — CALENDAR_BACKEND is "
        "untouched either way."
    )


asyncio.run(main())

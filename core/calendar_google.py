"""
Google Calendar READ + WRITE provider (G2a) — a swappable alternative to
core/calendar_read_ek.py (reads) and core/calendar_executor.py (writes),
built and tested standalone; NOT wired into the live app yet (G2b flips
the default, a later change). The read half mirrors calendar_read_ek's
exact event-dict shape {uid, title, start, end, all_day, calendar}; the
write half (create_event/move_event/move_by_uid/delete_event/
delete_by_uid/find_candidates) mirrors calendar_executor's exact result
shapes (ExecutionResult.to_dict() for create, _delete_result/_move_result
for delete/move, _to_candidate for candidates — all REUSED directly from
calendar_executor, not reimplemented, so there is zero chance of shape
drift between the two providers) so api/routes.py and chat.html need no
changes at cutover.

Unlike EventKit (single local machine, one implicit user), this is
multi-user: every call takes a `creds` object built from ONE user's stored
refresh token (core.db.fetch_google_refresh_token) plus app-level
client_id/client_secret/token_uri (.env constants — NOT per-user, the same
one-app-secret-many-users'-own-tokens pattern the ElevenLabs/Gemini keys
already use). This module does not import core.db itself — callers fetch
the refresh token and hand it in via _build_credentials, keeping this
module's only real dependency the Google API client, mirroring calendar_
read_ek's own "no Supabase" boundary.

Probe findings (G1 spike) this module is built on:
  - Scopes calendar.events + calendar.readonly cover event read/write AND
    calendarList enumeration — no broader `calendar` scope needed.
  - events.list(singleEvents=True, orderBy=startTime) expands recurring
    events into real per-occurrence instances, each with its own `id` and
    a `recurringEventId` linking back to the series master — confirmed
    8/8 recurring classes + the daily event, at correct per-occurrence
    times.
  - calendarList.list's `accessRole` (owner/writer vs reader) reproduces
    the writable/read-only split the app already relies on.
  - events.list's timeMin/timeMax match on OVERLAP, not start-time, and
    "primary" is an ALIAS for the user's own calendar (already present
    under its own real id in calendarList.list) — querying both would
    double-count that calendar's events under identical ids. This module
    only ever queries real calendarList ids, never "primary", and
    dedupes by instance id defensively regardless of cause.

NO-TOKEN / AUTH FAILURE: mirrors EventKit's needs_permission principle — a
missing or invalid connection returns {"status": "needs_connection",
"events": []} (reads) or {"status": "needs_connection", ...} (writes),
NEVER a bare empty "ok"/success. A connection problem must never read as
"nothing on your calendar". `creds=None` (no stored token at all) and a
revoked/invalid refresh token both land here identically, everywhere in
this module.

WRITE SURFACE (this half, built on the G1 probe's write findings):
  - create_event TARGETS a specific calendarId, defaulting to "primary"
    (the user's own Google calendar) when none is given — this IS the
    write-targeting fix: events land on Google, not silently on whatever
    iCloud calendar osascript's `first calendar whose writable is true`
    happened to pick. A per-user default-target-calendar preference is a
    follow-on (needs a settings picker); "primary" is the default now.
  - RECURRENCE (G4): move/delete detect a recurring target (recurringEventId
    on an instance, or a recurrence rule on a master) via _is_recurring()
    and, when the caller hasn't already picked a `scope`, return
    "recurring_choice" rather than acting — the client asks "just this
    one, or the whole series?" and re-calls with scope="instance"|"series".
    An instance scope patches/deletes the occurrence's own id; a series
    scope targets the master (recurringEventId), with delete confirmed
    via _confirm_series_gone_sync (events().instances(), not a single-id
    check) and move shifting the master's time-of-day on its own anchor
    date (never relocating the series to the user's occurrence date).
    THE OSASCRIPT/EVENTKIT PATH IS UNCHANGED — it still refuses recurring
    events outright; this instance-vs-series handling is Google-only,
    since osascript has no safe way to address a single occurrence.
  - SNAG #4 — DELETE CONFIRMATION: events.delete leaves the event
    retrievable by GET for a while afterward with status:"cancelled" —
    that is NOT "still there". The only honest "is it really gone" check
    is its ABSENCE from an events.list query (which excludes cancelled
    events by default), so delete confirmation here always re-reads via
    list, never trusts a GET-by-id or a bare recount.
  - RE-VERIFY: immediately before any destructive write, the target is
    re-read via a fresh GET — the same staleness guard calendar_executor
    runs before every delete/move, just collapsed into one API call
    instead of osascript's separate recurrence-check + uid-count scripts.
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta

from google.oauth2.credentials import Credentials
from google.auth.exceptions import RefreshError
from googleapiclient.discovery import build as _build_service
from googleapiclient.errors import HttpError

from core.calendar_executor import (
    ExecutionResult, _delete_result, _move_result, _to_candidate,
    _normalize_title, _titles_match, _osa_distance, _approx_threshold,
    _resolve_delete_window, _parse_dt, _DEFAULT_DURATION,
)

log = logging.getLogger(__name__)

_TOKEN_URI = "https://oauth2.googleapis.com/token"
_SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.readonly",
]


def _build_credentials(refresh_token: str) -> Credentials:
    """Builds a Credentials object from a user's stored refresh token plus
    the app-level OAuth client (GOOGLE_OAUTH_CLIENT_ID/SECRET in .env,
    confirmed working in the G1 probe) — the googleapiclient auto-refreshes
    the short-lived access token from this on first use. Never raises on
    construction itself (no network call here); an invalid/revoked token
    only surfaces as a RefreshError once something actually calls the API,
    which read_window/enumerate_calendars already catch."""
    return Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=_TOKEN_URI,
        client_id=os.environ["GOOGLE_OAUTH_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_OAUTH_CLIENT_SECRET"],
        scopes=_SCOPES,
    )


def credentials_for_token(refresh_token: str | None) -> Credentials | None:
    """Public entry point for turning a stored token into creds: returns
    None directly when there's no token (e.g. db.fetch_google_refresh_
    token came back empty), so callers don't need their own `if token:
    ... else None` branch — every function in this module already treats
    creds=None uniformly as "not connected". Thin wrapper over
    _build_credentials, which stays private since it assumes a non-None
    token."""
    return _build_credentials(refresh_token) if refresh_token else None


async def _run(fn):
    return await asyncio.to_thread(fn)


def _enumerate_calendars_sync(creds: Credentials | None) -> list[dict] | None:
    """Returns None (NOT []) specifically on "couldn't authenticate" (no
    creds, or a revoked/invalid token) so read_window can tell that apart
    from "genuinely talked to Google and got zero calendars" — the same
    distinction EventKit's permission_status() exists to make."""
    if creds is None:
        return None
    try:
        service = _build_service("calendar", "v3", credentials=creds)
        resp = service.calendarList().list().execute()
        items = resp.get("items", [])
        return [
            {
                "id": c["id"],
                "title": c.get("summary", c["id"]),
                "writable": c.get("accessRole") in ("owner", "writer"),
            }
            for c in items
        ]
    except RefreshError as exc:
        log.info("[google-read] refresh token invalid/revoked: %s", exc)
        return None
    except Exception as exc:
        log.error("[google-read] enumerate_calendars crashed: %s", exc)
        return None


async def enumerate_calendars(creds: Credentials | None) -> list[dict]:
    """[{"id": str, "title": str, "writable": bool}, ...] for every Google
    calendar this user can see — never raises, [] on any failure
    (including no/invalid creds). Callers needing to distinguish "no
    calendars" from "couldn't authenticate" should go through read_window,
    which surfaces that distinction via needs_connection."""
    result = await _run(lambda: _enumerate_calendars_sync(creds))
    return result or []


def _parsed_start_end(value: dict, user_tz) -> tuple[str | None, bool]:
    """Google's start/end is {"dateTime": "...-04:00"} for timed events or
    {"date": "YYYY-MM-DD"} for all-day ones. Returns (local-naive ISO
    string, is_all_day). A timed value is normalized to the USER's zone
    (never the host's) before stripping the offset — robust even if an
    event was created by an organizer in a different timezone, and
    correct on a host that isn't in the user's own zone (e.g. a cloud
    box)."""
    if "date" in value:
        try:
            d = datetime.strptime(value["date"], "%Y-%m-%d")
            return d.isoformat(timespec="seconds"), True
        except ValueError:
            return None, True
    dt_str = value.get("dateTime")
    if not dt_str:
        return None, False
    try:
        aware = datetime.fromisoformat(dt_str)
        local = aware.astimezone(user_tz).replace(tzinfo=None)
        return local.isoformat(timespec="seconds"), False
    except ValueError:
        return None, False


def _read_one_calendar_sync(creds: Credentials, cal_id: str, cal_title: str, time_min: str, time_max: str, user_tz) -> list[dict]:
    """One calendar, one synchronous call chain (paginated) — the unit
    read_window fans out across calendars via asyncio.to_thread, mirroring
    the old concurrent per-calendar osascript read's shape (N calendars,
    launched together, bounded by the slowest one) even though the
    underlying mechanism here is threads waiting on HTTP, not subprocesses.
    Never raises to the caller — any failure for THIS calendar degrades to
    "no events from it", not a crash of the whole window read; a total
    auth failure is caught earlier by enumerate_calendars instead."""
    events: list[dict] = []
    try:
        service = _build_service("calendar", "v3", credentials=creds)
        page_token = None
        while True:
            resp = service.events().list(
                calendarId=cal_id,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy="startTime",
                pageToken=page_token,
            ).execute()
            for ev in resp.get("items", []):
                start_val, is_all_day = _parsed_start_end(ev.get("start", {}), user_tz)
                end_val, _ = _parsed_start_end(ev.get("end", {}), user_tz)
                events.append({
                    "uid": ev["id"],
                    "title": ev.get("summary", ""),
                    "start": start_val,
                    "end": end_val,
                    "all_day": is_all_day,
                    # The ADDRESSING token, not a display label — same
                    # convention calendar_read_ek's "calendar" already is:
                    # chat.html only ever round-trips candidate.calendar
                    # straight into delete_by_uid/move_by_uid, never
                    # renders it. For osascript that token IS the
                    # calendar's name (AppleScript addresses by name); for
                    # Google it MUST be the real calendarId — "Family" the
                    # display title and the id Google actually needs are
                    # two different strings, and only the id works for a
                    # later events.get/patch/delete call. Confirmed via a
                    # real create->find_candidates->delete round trip that
                    # using the title here 404s.
                    "calendar": cal_id,
                })
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
    except Exception as exc:
        log.warning("[google-read] calendar %r unreachable this read: %s", cal_title, exc)
    return events


async def read_window(
    creds: Credentials | None, start_dt: datetime, end_dt: datetime, writable_only: bool, user_tz,
) -> dict:
    """THE replacement read, Google-backed: events.list(singleEvents=True)
    expands recurring events into per-occurrence instances (the entire
    point — see module docstring). Never raises. Returns one of:

      {"status": "needs_connection", "events": []}
        No stored refresh token (creds=None) or it's invalid/revoked.
        NEVER a bare empty "ok" — same principle as EventKit's
        needs_permission: a connection problem must never masquerade as
        "nothing on your calendar".

      {"status": "ok", "events": [{uid, title, start, end, all_day,
       calendar}, ...]}
        start/end are LOCAL-naive ISO strings at the OCCURRENCE's actual
        time (never the recurring master's) — the identical convention
        calendar_read_ek.read_window uses, so this is a drop-in swap at
        the I/O layer.

    writable_only=True filters to accessRole in (owner, writer), matching
    calendar_read_ek's writable_only contract exactly (so the scheduler's
    busy-set and add_slots' re-verify would need no changes to use this
    instead, once G2b repoints them).

    Per-calendar reads are fanned out concurrently (asyncio.gather over
    asyncio.to_thread), mirroring the old concurrent osascript read's
    shape — wall-clock bounded by the slowest calendar, not the sum.
    Results are deduped by instance id before returning (see module
    docstring — "primary" aliasing a real calendarList id is the known
    cause, guarded against directly by never querying "primary", but the
    dedup stays as a cheap defensive backstop).
    """
    cals = await _run(lambda: _enumerate_calendars_sync(creds))
    if cals is None:
        return {"status": "needs_connection", "events": []}

    targets = [c for c in cals if not writable_only or c["writable"]]
    if not targets:
        # writable_only=True with genuinely no writable calendars is a
        # real, correct empty — not a failure to distinguish from one.
        return {"status": "ok", "events": []}

    # Naive-local datetimes represent wall-clock time in the USER's zone
    # (not the host's) — .replace(tzinfo=user_tz) attaches that zone
    # directly with no conversion, unlike .astimezone() which would
    # presume the HOST's local zone (wrong on a cloud box not in the
    # user's zone).
    time_min = start_dt.replace(tzinfo=user_tz).isoformat()
    time_max = end_dt.replace(tzinfo=user_tz).isoformat()

    per_cal_results = await asyncio.gather(*(
        asyncio.to_thread(_read_one_calendar_sync, creds, c["id"], c["title"], time_min, time_max, user_tz)
        for c in targets
    ))

    seen: set[str] = set()
    deduped: list[dict] = []
    for events in per_cal_results:
        for ev in events:
            if ev["uid"] in seen:
                continue
            seen.add(ev["uid"])
            deduped.append(ev)

    return {"status": "ok", "events": deduped}


# ---------------------------------------------------------------------------
# WRITE SURFACE (G2b-the-write-half of G2a) — create/move/delete + the
# candidate search they share. See the module docstring's "WRITE SURFACE"
# section for the targeting/recurrence-refusal/snag-#4/re-verify rationale.
# Every result dict is built via calendar_executor's OWN ExecutionResult/
# _delete_result/_move_result/_to_candidate — reused directly, not
# reimplemented, so this provider's output is shape-identical to the
# osascript executor's by construction, not by careful copying.
# ---------------------------------------------------------------------------

_RECURRING_SERIES_ALLDAY_MOVE_MSG = (
    "To reschedule a repeating all-day event, change it in Google Calendar directly"
)


def _get_event_sync(creds: Credentials | None, calendar_id: str, event_id: str) -> tuple[str, dict]:
    """status: "ok" | "not_found" | "needs_connection" | "error"."""
    if creds is None:
        return "needs_connection", {}
    try:
        service = _build_service("calendar", "v3", credentials=creds)
        ev = service.events().get(calendarId=calendar_id, eventId=event_id).execute()
        return "ok", ev
    except RefreshError as exc:
        log.info("[google-write] refresh token invalid/revoked during get: %s", exc)
        return "needs_connection", {}
    except HttpError as exc:
        if exc.resp.status in (404, 410):
            return "not_found", {}
        log.warning("[google-write] get event failed: %s", exc)
        return "error", {}
    except Exception as exc:
        log.error("[google-write] get event crashed: %s", exc)
        return "error", {}


async def _get_event(creds, calendar_id: str, event_id: str) -> tuple[str, dict]:
    return await _run(lambda: _get_event_sync(creds, calendar_id, event_id))


def _is_recurring(ev: dict) -> bool:
    """An event is part of a series if it's ONE OCCURRENCE of one
    (recurringEventId set) or IS the series master (its own recurrence
    rule set) — Google's JSON response is structurally reliable here
    (unlike AppleScript's `recurrence of evt`, which could raise inside
    the script and need a READ_ERROR sentinel), so there's no unreadable-
    property case to fail-safe against directly. The equivalent fail-safe
    is structural: every caller only reaches this after a successful
    ("ok") _get_event — any fetch/parse failure already refuses the
    write via that status check, before _is_recurring is ever called."""
    return bool(ev.get("recurringEventId")) or bool(ev.get("recurrence"))


def _patch_event_sync(creds: Credentials, calendar_id: str, event_id: str, body: dict) -> tuple[str, dict]:
    try:
        service = _build_service("calendar", "v3", credentials=creds)
        ev = service.events().patch(calendarId=calendar_id, eventId=event_id, body=body).execute()
        return "ok", ev
    except RefreshError as exc:
        log.info("[google-write] refresh token invalid/revoked during patch: %s", exc)
        return "needs_connection", {}
    except HttpError as exc:
        if exc.resp.status in (404, 410):
            return "not_found", {}
        log.warning("[google-write] patch event failed: %s", exc)
        return "error", {}
    except Exception as exc:
        log.error("[google-write] patch event crashed: %s", exc)
        return "error", {}


async def _patch_event(creds, calendar_id: str, event_id: str, body: dict) -> tuple[str, dict]:
    return await _run(lambda: _patch_event_sync(creds, calendar_id, event_id, body))


def _delete_event_raw_sync(creds: Credentials, calendar_id: str, event_id: str) -> str:
    """"ok" | "not_found" | "needs_connection" | "error". This is the raw
    DELETE call only — it does NOT by itself confirm the event is gone
    (see _confirm_gone_sync for why a GET right after this can still say
    otherwise)."""
    try:
        service = _build_service("calendar", "v3", credentials=creds)
        service.events().delete(calendarId=calendar_id, eventId=event_id).execute()
        return "ok"
    except RefreshError as exc:
        log.info("[google-write] refresh token invalid/revoked during delete: %s", exc)
        return "needs_connection"
    except HttpError as exc:
        if exc.resp.status in (404, 410):
            return "not_found"
        log.warning("[google-write] delete event failed: %s", exc)
        return "error"
    except Exception as exc:
        log.error("[google-write] delete event crashed: %s", exc)
        return "error"


async def _delete_event_raw(creds, calendar_id: str, event_id: str) -> str:
    return await _run(lambda: _delete_event_raw_sync(creds, calendar_id, event_id))


def _confirm_gone_sync(
    creds: Credentials, calendar_id: str, event_id: str, start_dt: datetime, end_dt: datetime, user_tz,
) -> bool:
    """SNAG #4 — the real post-delete "is it gone" check: events.list
    over a window bracketing the event's own original time (default
    showDeleted=False already excludes cancelled events), checking the
    deleted id is ABSENT from the results. Deliberately NOT a GET-by-id:
    that can still return 200 with status:"cancelled" for a while after a
    genuinely successful delete, which would false-fail this check if
    treated as "still there". On any exception, fails safe toward "not
    confirmed" — never report a delete as successful on a guess.
    start_dt/end_dt are naive wall-clock in the USER's zone — interpreted
    via user_tz, never the host's local zone."""
    try:
        service = _build_service("calendar", "v3", credentials=creds)
        time_min = (start_dt - timedelta(hours=2)).replace(tzinfo=user_tz).isoformat()
        time_max = (end_dt + timedelta(hours=2)).replace(tzinfo=user_tz).isoformat()
        resp = service.events().list(
            calendarId=calendar_id, timeMin=time_min, timeMax=time_max, singleEvents=True,
        ).execute()
        ids = {item["id"] for item in resp.get("items", [])}
        return event_id not in ids
    except Exception as exc:
        log.warning("[google-write] confirm-gone check failed for uid=%s, assuming NOT confirmed: %s", event_id, exc)
        return False


def _confirm_series_gone_sync(creds: Credentials, calendar_id: str, master_id: str) -> bool:
    """SNAG #4, series edition (G4): after deleting a WHOLE recurring
    series via its master id, a single id's absence isn't the right
    check — any surviving occurrence would appear under its OWN instance
    id (recurringEventId == master_id), not the master's id. Uses
    events().instances() — the dedicated Google endpoint for a series'
    occurrences — rather than scanning a date window: a deleted master
    either 404s/410s there (caught as HttpError, treated as confirmed
    gone) or returns occurrences we check aren't all cancelled already.
    Fails safe toward "not confirmed" on any other exception — never
    report a series delete as successful on a guess."""
    try:
        service = _build_service("calendar", "v3", credentials=creds)
        resp = service.events().instances(calendarId=calendar_id, eventId=master_id).execute()
        remaining = [i for i in resp.get("items", []) if i.get("status") != "cancelled"]
        return len(remaining) == 0
    except HttpError as exc:
        if exc.resp.status in (404, 410):
            return True  # the master itself is gone -- the series is confirmed gone
        log.warning(
            "[google-write] confirm-series-gone instances() failed status=%s for master=%s, assuming NOT confirmed: %s",
            exc.resp.status, master_id, exc,
        )
        return False
    except Exception as exc:
        log.warning("[google-write] confirm-series-gone check failed for master=%s, assuming NOT confirmed: %s", master_id, exc)
        return False


async def find_candidates(title_fragment: str, date_str: str | None, creds: Credentials | None, user_tz) -> tuple[str, str, list]:
    """Google equivalent of calendar_executor._find_candidates: the SAME
    exact/approximate matching logic, reused directly (byte-identical
    behavior — _normalize_title/_titles_match/_osa_distance/
    _approx_threshold are pure functions with zero osascript dependency),
    over this module's OWN read instead of the concurrent osascript scan.
    writable_only=True always — a read-only calendar's event must never
    be offered as a delete/move candidate. Returns (status, tier,
    candidates): status is "ok" | "needs_connection" | "error"; tier is
    "exact" | "approx" | "none" (meaningful only when status == "ok")."""
    if creds is None:
        return "needs_connection", "none", []

    start_dt, end_dt = _resolve_delete_window(date_str, user_tz)
    read = await read_window(creds, start_dt, end_dt, writable_only=True, user_tz=user_tz)
    if read["status"] == "needs_connection":
        return "needs_connection", "none", []
    if read["status"] != "ok":
        return "error", "none", []
    events = read["events"]

    query_norm = _normalize_title(title_fragment)
    exact = [
        _to_candidate(ev) for ev in events
        if _titles_match(query_norm, _normalize_title(ev.get("title", "")))
    ]
    if exact:
        return "ok", "exact", exact

    threshold = _approx_threshold(query_norm)
    scored = []
    for ev in events:
        ev_norm = _normalize_title(ev.get("title", ""))
        if not ev_norm:
            continue
        dist = _osa_distance(query_norm, ev_norm)
        if dist <= threshold:
            scored.append((dist, _to_candidate(ev)))
    if scored:
        scored.sort(key=lambda pair: pair[0])
        return "ok", "approx", [c for _, c in scored]

    return "ok", "none", []


def _event_body(start_dt: datetime, end_dt: datetime, all_day: bool, user_tz) -> dict:
    if all_day:
        return {
            "start": {"date": start_dt.strftime("%Y-%m-%d")},
            "end": {"date": end_dt.strftime("%Y-%m-%d")},
        }
    # start_dt/end_dt are naive wall-clock in the USER's zone — stamp
    # THAT zone's offset directly via replace(tzinfo=user_tz), no
    # conversion. .astimezone() would instead presume the HOST's local
    # zone, wrong on a cloud box not in the user's zone — "7pm" must be
    # 7pm for the user, not 7pm host-time.
    return {
        "start": {"dateTime": start_dt.replace(tzinfo=user_tz).isoformat()},
        "end": {"dateTime": end_dt.replace(tzinfo=user_tz).isoformat()},
    }


def _create_event_sync(args: dict, enabled: bool, creds: Credentials | None, calendar_id: str | None, user_tz) -> dict:
    title   = (args.get("title") or "").strip()
    start_s = (args.get("start") or "").strip()
    end_s   = (args.get("end") or "").strip()
    all_day = bool(args.get("all_day", False))

    if not enabled:
        return ExecutionResult(
            status="app_disabled", message="Calendar actions are turned off.",
            title=title, start=start_s or None, end=end_s or None, all_day=all_day,
        ).to_dict()
    if creds is None:
        return ExecutionResult(
            status="needs_connection", message="Google Calendar isn't connected.",
            title=title, start=start_s or None, end=end_s or None, all_day=all_day,
        ).to_dict()

    start_dt = _parse_dt(start_s)
    if not title or start_dt is None:
        return ExecutionResult(
            status="error", message="Couldn't create the event — missing or invalid title/start.",
            title=title, start=start_s or None, end=end_s or None, all_day=all_day,
        ).to_dict()

    end_dt = _parse_dt(end_s) if end_s else None
    if end_dt is None:
        end_dt = (
            start_dt.replace(hour=0, minute=0, second=0) + timedelta(days=1)
            if all_day else start_dt + _DEFAULT_DURATION
        )
    if all_day:
        start_dt = start_dt.replace(hour=0, minute=0, second=0)

    result_kwargs = dict(title=title, start=start_dt.isoformat(), end=end_dt.isoformat(), all_day=all_day)
    # TARGETING: "primary" is Google's own alias for the user's default
    # calendar — this is the write-targeting fix itself; events land on
    # a real Google calendar instead of whatever iCloud calendar
    # osascript's `first calendar whose writable is true` happened to
    # pick. A per-user default-target-calendar preference is a follow-on.
    target_cal = calendar_id or "primary"
    body = {"summary": title, **_event_body(start_dt, end_dt, all_day, user_tz)}

    try:
        service = _build_service("calendar", "v3", credentials=creds)
        service.events().insert(calendarId=target_cal, body=body).execute()
        log.info("[google-write] created event %r %s -> %s on calendar=%r",
                  title, start_dt.isoformat(), end_dt.isoformat(), target_cal)
        return ExecutionResult(status="success", message="Event created.", **result_kwargs).to_dict()
    except RefreshError as exc:
        log.info("[google-write] refresh token invalid/revoked during create: %s", exc)
        return ExecutionResult(
            status="needs_connection", message="Google Calendar isn't connected.", **result_kwargs,
        ).to_dict()
    except Exception as exc:
        log.error("[google-write] create crashed for %r: %s", title, exc)
        return ExecutionResult(status="error", message="Couldn't create the event.", **result_kwargs).to_dict()


async def create_event(args: dict, enabled: bool, creds: Credentials | None, user_tz, calendar_id: str | None = None) -> dict:
    """Create a calendar event — same args/enabled contract as calendar_
    executor.create_calendar_event, plus `creds` (this user's built
    credentials) and `calendar_id` (defaults to "primary" — see
    _create_event_sync). Never raises; returns the identical {status,
    message, title, start, end, all_day} shape (via ExecutionResult,
    reused directly)."""
    return await _run(lambda: _create_event_sync(args, enabled, creds, calendar_id, user_tz))


async def move_by_uid(
    uid: str, calendar: str, new_start: str, new_end: str | None, enabled: bool,
    creds: Credentials | None, user_tz, scope: str | None = None,
) -> dict:
    """The guarded single-event move — app_disabled/needs_connection gate,
    recurrence handling, re-verify, duration-preserving new_end
    computation, the write, and a SEPARATE confirm read-back (never
    trusting patch's own echoed response) — same guard order as
    calendar_executor.move_by_uid, same result shape (_move_result,
    reused directly). Never raises.

    RECURRENCE (G4, Google-only — the osascript/eventkit path still
    refuses outright): `scope` is None | "instance" | "series".
      - scope=None on a recurring target: does NOT act. Returns
        status="recurring_choice" carrying the occurrence as a candidate
        plus the pending new_start/new_end, for the client to re-call
        with an explicit scope once the user picks.
      - scope="instance": patches the INSTANCE id (`uid`) — Google
        records this as a per-occurrence exception; the instance id
        persists, so the confirm read-back below still works unchanged.
      - scope="series": patches the SERIES MASTER (`recurringEventId`),
        shifting its TIME-OF-DAY onto the master's own anchor date — NEVER
        relocating the series to the user's occurrence date. All-day
        series moves are explicitly out of scope (instance-level all-day
        move is fine; see _RECURRING_SERIES_ALLDAY_MOVE_MSG).
    """
    if not enabled:
        return _move_result("app_disabled", "Calendar actions are turned off.")
    if creds is None:
        return _move_result("needs_connection", "Google Calendar isn't connected.")

    new_start_dt = _parse_dt((new_start or "").strip())
    if new_start_dt is None:
        return _move_result("error", "Couldn't tell what time to move it to.")
    new_end_dt = _parse_dt((new_end or "").strip()) if new_end else None

    try:
        status, ev = await _get_event(creds, calendar, uid)
        if status == "needs_connection":
            return _move_result("needs_connection", "Google Calendar isn't connected.")
        if status == "not_found":
            return _move_result("not_found", "couldn't find that event")
        if status != "ok":
            return _move_result("error", "Couldn't check that event.")

        title = ev.get("summary") or ""
        orig_start_val, is_all_day = _parsed_start_end(ev.get("start", {}), user_tz)
        orig_end_val, _ = _parsed_start_end(ev.get("end", {}), user_tz)
        recurring = _is_recurring(ev)

        if recurring and scope is None:
            log.info("[google-write] recurring move needs a scope choice for uid=%s title=%r", uid, title)
            occurrence = {"uid": uid, "title": title, "start": orig_start_val,
                          "end": orig_end_val, "calendar": calendar, "all_day": is_all_day}
            return _move_result(
                "recurring_choice", title=title, start=orig_start_val,
                candidates=[_to_candidate(occurrence)], approximate=False,
                new_start=new_start, new_end=new_end,
            )

        if recurring and scope == "series" and is_all_day:
            return _move_result(
                "recurring_unsupported", _RECURRING_SERIES_ALLDAY_MOVE_MSG, title=title, start=orig_start_val,
            )

        if recurring:
            if scope not in ("instance", "series"):
                return _move_result("error", "Couldn't tell which to move.")
            target_id = uid if scope == "instance" else (ev.get("recurringEventId") or uid)
        else:
            target_id = uid

        # RE-VERIFY immediately before writing — closes the gap between
        # the GET above and the write below (staleness guard). Targets
        # whatever we're ABOUT TO WRITE (the instance, or the series
        # master), not always the original uid.
        status2, ev2 = await _get_event(creds, calendar, target_id)
        if status2 == "not_found":
            return _move_result("not_found", "that event's no longer there", title=title, start=orig_start_val)
        if status2 != "ok":
            return _move_result("error", "Couldn't verify that event.", title=title)

        if recurring and scope == "series":
            # SERIES MOVE — shift time-of-day onto the MASTER's own anchor
            # date; never relocate the series to the user's occurrence
            # date (that would silently move every future occurrence to a
            # single day instead of recurring).
            m_start_val, _ = _parsed_start_end(ev2.get("start", {}), user_tz)
            m_end_val, _ = _parsed_start_end(ev2.get("end", {}), user_tz)
            m_start_dt = _parse_dt(m_start_val)
            m_end_dt = _parse_dt(m_end_val)
            if m_start_dt is None or m_end_dt is None:
                return _move_result("error", "Couldn't read the series' current time.", title=title)
            new_write_start = m_start_dt.replace(
                hour=new_start_dt.hour, minute=new_start_dt.minute, second=0, microsecond=0,
            )
            duration = (new_end_dt - new_start_dt) if new_end_dt is not None else (m_end_dt - m_start_dt)
            new_write_end = new_write_start + duration
        else:
            new_write_start = new_start_dt
            if new_end_dt is None:
                orig_t_start_val, _ = _parsed_start_end(ev2.get("start", {}), user_tz)
                orig_t_end_val, _ = _parsed_start_end(ev2.get("end", {}), user_tz)
                orig_t_start_dt = _parse_dt(orig_t_start_val)
                orig_t_end_dt = _parse_dt(orig_t_end_val)
                if orig_t_start_dt is None or orig_t_end_dt is None:
                    return _move_result("error", "Couldn't read that event's current time.", title=title)
                new_write_end = new_start_dt + (orig_t_end_dt - orig_t_start_dt)
            else:
                new_write_end = new_end_dt

        body = _event_body(new_write_start, new_write_end, is_all_day, user_tz)
        patch_status, _patched = await _patch_event(creds, calendar, target_id, body)
        if patch_status == "not_found":
            return _move_result("not_found", "that event's no longer there", title=title, start=orig_start_val)
        if patch_status != "ok":
            return _move_result("error", "couldn't move it", title=title, start=orig_start_val)

        # Confirm read-back — a SEPARATE get, never trusting patch's own
        # echoed response (same discipline as calendar_executor.move_by_uid).
        # For a series move this confirms the MASTER's new time-of-day.
        confirm_status, confirm_ev = await _get_event(creds, calendar, target_id)
        if confirm_status != "ok":
            return _move_result("error", "Couldn't confirm the move.", title=title)
        confirmed_start_val, _ = _parsed_start_end(confirm_ev.get("start", {}), user_tz)
        confirmed_end_val, _ = _parsed_start_end(confirm_ev.get("end", {}), user_tz)
        if _parse_dt(confirmed_start_val) != new_write_start or _parse_dt(confirmed_end_val) != new_write_end:
            log.warning(
                "[google-write] move did not confirm for target=%s (intended %s-%s, actual %s-%s) — "
                "reporting error, not success", target_id, new_write_start, new_write_end, confirmed_start_val, confirmed_end_val,
            )
            return _move_result("error", "couldn't confirm the move", title=title)

        log.info("[google-write] moved event %r (target=%s, scope=%s) to %s -> %s",
                  title, target_id, scope, new_write_start.isoformat(), new_write_end.isoformat())
        return _move_result(
            "success", title=title, start=new_write_start.isoformat(), end=new_write_end.isoformat(), all_day=is_all_day,
        )

    except Exception as exc:
        log.error("[google-write] executor error moving event uid=%s: %s", uid, exc)
        return _move_result("error", "Couldn't move that event.")


async def move_event(args: dict, enabled: bool, creds: Credentials | None, user_tz) -> dict:
    """Move one specific event, by uid+calendar (skips candidate search)
    or by title fragment — same target-resolution contract as calendar_
    executor.move_event, reusing find_candidates verbatim. Never raises;
    same result shape (_move_result)."""
    if not enabled:
        return _move_result("app_disabled", "Calendar actions are turned off.")
    if creds is None:
        return _move_result("needs_connection", "Google Calendar isn't connected.")

    new_start_s = (args.get("new_start") or "").strip()
    if _parse_dt(new_start_s) is None:
        return _move_result("error", "Couldn't tell what time to move it to.")
    new_end_s = (args.get("new_end") or "").strip() or None
    if new_end_s and _parse_dt(new_end_s) is None:
        return _move_result("error", "Couldn't tell what time to move it to.")

    uid            = (args.get("uid") or "").strip()
    calendar       = (args.get("calendar") or "").strip()
    title_fragment = (args.get("title") or "").strip()

    if uid and calendar:
        return await move_by_uid(uid, calendar, new_start_s, new_end_s, enabled, creds, user_tz)

    if not title_fragment:
        return _move_result("error", "Couldn't tell which event to move.")

    q_status, tier, candidates = await find_candidates(title_fragment, args.get("date"), creds, user_tz)
    if q_status == "needs_connection":
        return _move_result("needs_connection", "Google Calendar isn't connected.")
    if q_status != "ok":
        return _move_result("error", "Couldn't search the calendar.")

    if tier == "none":
        return _move_result("not_found", "couldn't find that event")

    if tier == "exact" and len(candidates) == 1:
        return await move_by_uid(
            candidates[0]["uid"], candidates[0]["calendar"], new_start_s, new_end_s, enabled, creds, user_tz,
        )

    return _move_result(
        "disambiguate",
        approximate=(tier == "approx"),
        candidates=[
            {"uid": c["uid"], "title": c.get("title"), "start": c.get("start"),
             "end": c.get("end"), "calendar": c.get("calendar"), "all_day": c.get("all_day", False)}
            for c in candidates
        ],
        new_start=new_start_s,
        new_end=new_end_s,
    )


async def delete_by_uid(
    uid: str, calendar: str, enabled: bool, creds: Credentials | None, user_tz, scope: str | None = None,
) -> dict:
    """The guarded single-event delete — app_disabled/needs_connection
    gate, recurrence handling, re-verify, delete, and SNAG #4's
    events.list-based confirmation (never a GET-by-id/recount — see
    _confirm_gone_sync) — same guard order as calendar_executor.
    delete_by_uid, same result shape (_delete_result, reused directly).
    Never raises.

    RECURRENCE (G4, Google-only — the osascript/eventkit path still
    refuses outright): `scope` is None | "instance" | "series".
      - scope=None on a recurring target: does NOT act. Returns
        status="recurring_choice" carrying the occurrence as a candidate
        for the client to re-call with an explicit scope once the user
        picks.
      - scope="instance": deletes just the INSTANCE id (`uid`).
      - scope="series": deletes the SERIES MASTER (`recurringEventId`),
        confirmed gone via _confirm_series_gone_sync (events().instances()
        on the master), not the single-id check — a series delete can
        leave OTHER instance ids behind that a single-id absence check
        would miss entirely.
    """
    if not enabled:
        return _delete_result("app_disabled", "Calendar actions are turned off.")
    if creds is None:
        return _delete_result("needs_connection", "Google Calendar isn't connected.")

    try:
        status, ev = await _get_event(creds, calendar, uid)
        if status == "needs_connection":
            return _delete_result("needs_connection", "Google Calendar isn't connected.")
        if status == "not_found":
            return _delete_result("not_found", "couldn't find that event")
        if status != "ok":
            return _delete_result("error", "Couldn't check that event.")

        title = ev.get("summary") or ""
        start_val, _ = _parsed_start_end(ev.get("start", {}), user_tz)
        end_val, _ = _parsed_start_end(ev.get("end", {}), user_tz)
        all_day = bool(ev.get("start", {}).get("date"))
        recurring = _is_recurring(ev)

        if recurring and scope is None:
            log.info("[google-write] recurring delete needs a scope choice for uid=%s title=%r", uid, title)
            occurrence = {"uid": uid, "title": title, "start": start_val,
                          "end": end_val, "calendar": calendar, "all_day": all_day}
            return _delete_result(
                "recurring_choice", title=title, start=start_val,
                candidates=[_to_candidate(occurrence)], approximate=False,
            )

        if recurring:
            if scope not in ("instance", "series"):
                return _delete_result("error", "Couldn't tell which to delete.")
            target_id = uid if scope == "instance" else (ev.get("recurringEventId") or uid)
        else:
            target_id = uid

        # RE-VERIFY immediately before deleting (staleness guard) — targets
        # whatever we're ABOUT TO DELETE (the instance, or the series
        # master), not always the original uid.
        status2, ev2 = await _get_event(creds, calendar, target_id)
        if status2 == "not_found":
            return _delete_result("not_found", "that event's no longer there", title=title, start=start_val)
        if status2 != "ok":
            return _delete_result("error", "Couldn't verify that event.", title=title)

        t_start_val, _ = _parsed_start_end(ev2.get("start", {}), user_tz)
        t_end_val, _ = _parsed_start_end(ev2.get("end", {}), user_tz)
        t_start_dt = _parse_dt(t_start_val) or datetime.now(user_tz).replace(tzinfo=None)
        t_end_dt = _parse_dt(t_end_val) or t_start_dt

        del_status = await _delete_event_raw(creds, calendar, target_id)
        if del_status == "not_found":
            return _delete_result("not_found", "that event's no longer there", title=title, start=start_val)
        if del_status != "ok":
            return _delete_result("error", "couldn't remove it", title=title, start=start_val)

        # SNAG #4: confirm via events.list (list-exclusion) / instances()
        # for a series — NEVER a GET-by-id — a GET right after this can
        # still return 200 with status:"cancelled" for a while, which is
        # NOT "still there".
        if recurring and scope == "series":
            gone = await _run(lambda: _confirm_series_gone_sync(creds, calendar, target_id))
        else:
            gone = await _run(lambda: _confirm_gone_sync(creds, calendar, target_id, t_start_dt, t_end_dt, user_tz))
        if not gone:
            log.warning(
                "[google-write] delete did not confirm removal for target=%s (scope=%s) — "
                "reporting error, not success", target_id, scope,
            )
            return _delete_result("error", "couldn't remove it", title=title, start=start_val)

        log.info("[google-write] deleted event %r (target=%s, scope=%s) from calendar=%r", title, target_id, scope, calendar)
        return _delete_result("success", title=title, start=start_val)

    except Exception as exc:
        log.error("[google-write] executor error deleting event uid=%s: %s", uid, exc)
        return _delete_result("error", "Couldn't delete that event.")


async def delete_event(args: dict, enabled: bool, creds: Credentials | None, user_tz) -> dict:
    """Delete one specific event, by uid+calendar (skips candidate
    search) or by title fragment — same target-resolution contract as
    calendar_executor.delete_event, reusing find_candidates verbatim.
    Never raises; same result shape (_delete_result)."""
    if not enabled:
        return _delete_result("app_disabled", "Calendar actions are turned off.")
    if creds is None:
        return _delete_result("needs_connection", "Google Calendar isn't connected.")

    uid            = (args.get("uid") or "").strip()
    calendar       = (args.get("calendar") or "").strip()
    title_fragment = (args.get("title") or "").strip()

    if uid and calendar:
        return await delete_by_uid(uid, calendar, enabled, creds, user_tz)

    if not title_fragment:
        return _delete_result("error", "Couldn't tell which event to delete.")

    q_status, tier, candidates = await find_candidates(title_fragment, args.get("date"), creds, user_tz)
    if q_status == "needs_connection":
        return _delete_result("needs_connection", "Google Calendar isn't connected.")
    if q_status != "ok":
        return _delete_result("error", "Couldn't search the calendar.")

    if tier == "none":
        return _delete_result("not_found", "couldn't find that event")

    if tier == "exact" and len(candidates) == 1:
        return await delete_by_uid(candidates[0]["uid"], candidates[0]["calendar"], enabled, creds, user_tz)

    return _delete_result(
        "disambiguate",
        approximate=(tier == "approx"),
        candidates=[
            {"uid": c["uid"], "title": c.get("title"), "start": c.get("start"),
             "end": c.get("end"), "calendar": c.get("calendar"), "all_day": c.get("all_day", False)}
            for c in candidates
        ],
    )

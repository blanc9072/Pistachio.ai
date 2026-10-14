"""
Calendar backend dispatch seam (G2b-2). Selects the active calendar I/O
provider from CALENDAR_BACKEND ("eventkit" default | "google") at import
and exposes ONE backend-agnostic surface that api/routes.py calls instead
of importing calendar_read_ek / calendar_executor / calendar_google
directly. Neither provider module is modified; this only chooses between
them and normalizes the one place their vocabularies differ (the
"not set up" status + its user-facing message).

EventKit path: single local machine, one implicit user — user_id is
accepted for a uniform signature but unused for I/O.
Google path: multi-user — every call resolves THIS user's creds from the
stored refresh token (db.fetch_google_refresh_token) before delegating.
"""
import logging
import os

import core.calendar_read_ek as ek_read
import core.calendar_executor as ek_exec
import core.calendar_google as gcal
import core.db as db

log = logging.getLogger(__name__)

_BACKEND = (os.environ.get("CALENDAR_BACKEND") or "eventkit").strip().lower()
if _BACKEND not in ("eventkit", "google"):
    log.warning("CALENDAR_BACKEND=%r unrecognized — falling back to eventkit", _BACKEND)
    _BACKEND = "eventkit"
log.info("[calendar-backend] active provider = %s", _BACKEND)

# Google write target. Production default "primary" (the user's main
# calendar, andrewzeng632 — chosen deliberately: Pistachio is a full
# secretary and writes there). During the Google verify suite this is
# pointed at a THROWAWAY test calendar so verification never touches the
# real primary. EventKit path ignores it. This is the "default-target
# calendar" the G2a docstring flagged as a follow-on — an env now,
# upgradeable to a per-user settings picker later.
_WRITE_CAL = os.environ.get("GOOGLE_WRITE_CALENDAR_ID") or "primary"
log.info("[calendar-backend] google write target = %s", _WRITE_CAL)

_EK_SETUP_MSG = (
    "I need Calendar access — grant Full Calendar access in "
    "System Settings › Privacy & Security › Calendars."
)
_GOOGLE_SETUP_MSG = "Google Calendar isn't connected — connect it in Settings."


def active_backend() -> str:
    return _BACKEND


def setup_message() -> str:
    """Provider-correct text for the 'not set up' state — EventKit's macOS
    grant prompt vs Google's connect prompt. Callers that surface a
    needs-setup message use THIS instead of a hardcoded EventKit string,
    so the message is honest under either backend."""
    return _GOOGLE_SETUP_MSG if _BACKEND == "google" else _EK_SETUP_MSG


async def _creds(user_id: str):
    """Google only: build this user's creds from their stored refresh
    token. None (no token / never connected) propagates straight through
    to the provider, which turns it into needs_connection."""
    token = await db.fetch_google_refresh_token(user_id)
    return gcal.credentials_for_token(token)


# ---- READS -----------------------------------------------------------------
async def read_window(user_id, start_dt, end_dt, writable_only) -> dict:
    """Normalized read. Returns:
         {"status": "ok",          "events": [...]}
         {"status": "needs_setup", "events": [], "message": <provider msg>}
         {"status": "error",       "events": []}
    The provider-native 'needs_permission' (EventKit) and 'needs_connection'
    (Google) both normalize to 'needs_setup' here, with setup_message()
    attached, so routes' read consumers branch on ONE sentinel regardless
    of backend. Event-dict shape is identical across providers already
    ({uid,title,start,end,all_day,calendar})."""
    if _BACKEND == "google":
        r = await gcal.read_window(await _creds(user_id), start_dt, end_dt, writable_only)
        native_setup = "needs_connection"
    else:
        r = await ek_read.read_window(start_dt, end_dt, writable_only)
        native_setup = "needs_permission"
    status = r.get("status")
    if status == native_setup:
        return {"status": "needs_setup", "events": [], "message": setup_message()}
    if status == "ok":
        return {"status": "ok", "events": r.get("events", [])}
    return {"status": "error", "events": []}


# ---- WRITES ----------------------------------------------------------------
# Write result dicts are already shape-identical across providers (Google
# reuses calendar_executor's ExecutionResult/_delete_result/_move_result).
# The ONE new status Google can emit that osascript can't is
# "needs_connection"; chat.html's renderers all have a `default:` branch
# that surfaces action.message (Google always sets a correct one), so the
# client needs no change for correctness — PART E is optional polish only.
# These therefore pass provider results straight through, unnormalized.
async def create_event(user_id, args, enabled) -> dict:
    if _BACKEND == "google":
        # calendar_id comes from _WRITE_CAL (prod: "primary"; test: the
        # throwaway calendar) — this is the ONE place the write target is
        # decided, so chat-create AND add_slots-create both honor it.
        return await gcal.create_event(args, enabled, await _creds(user_id), calendar_id=_WRITE_CAL)
    return await ek_exec.create_calendar_event(args, enabled)


async def move_event(user_id, args, enabled) -> dict:
    if _BACKEND == "google":
        return await gcal.move_event(args, enabled, await _creds(user_id))
    return await ek_exec.move_event(args, enabled)


async def move_by_uid(user_id, uid, calendar, new_start, new_end, enabled, scope=None) -> dict:
    # scope (G4): None | "instance" | "series" — Google-only. A recurring
    # target with scope=None returns "recurring_choice" instead of acting
    # (see calendar_google.move_by_uid). The eventkit path has no way to
    # address a single occurrence safely, so it ignores scope entirely
    # and keeps refusing recurring events outright, unchanged.
    if _BACKEND == "google":
        return await gcal.move_by_uid(uid, calendar, new_start, new_end, enabled, await _creds(user_id), scope=scope)
    return await ek_exec.move_by_uid(uid, calendar, new_start, new_end, enabled)


async def delete_event(user_id, args, enabled) -> dict:
    if _BACKEND == "google":
        return await gcal.delete_event(args, enabled, await _creds(user_id))
    return await ek_exec.delete_event(args, enabled)


async def delete_by_uid(user_id, uid, calendar, enabled, scope=None) -> dict:
    # scope (G4): see move_by_uid above — same Google-only semantics.
    if _BACKEND == "google":
        return await gcal.delete_by_uid(uid, calendar, enabled, await _creds(user_id), scope=scope)
    return await ek_exec.delete_by_uid(uid, calendar, enabled)

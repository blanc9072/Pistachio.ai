"""
Supabase access layer for the backend.

Uses the service role key so RLS is bypassed server-side — this is the
correct pattern for trusted backend code.  The anon key is for clients only.
"""

import asyncio
import logging
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from supabase import create_client, Client

log = logging.getLogger(__name__)

_client: Client | None = None


def _sb() -> Client:
    global _client
    if _client is None:
        url = os.environ["SUPABASE_URL"]
        key = os.environ["SUPABASE_SERVICE_KEY"]
        if not key:
            raise RuntimeError(
                "SUPABASE_SERVICE_KEY is not set.  "
                "Add it to .env — Supabase Dashboard → Project Settings → API → service_role."
            )
        _client = create_client(url, key)
    return _client


async def _run(fn):
    return await asyncio.to_thread(fn)


# ---------------------------------------------------------------------------
# messages
# ---------------------------------------------------------------------------

async def fetch_messages(user_id: str, limit: int = 30) -> list[dict]:
    """Return the most recent `limit` messages for a user, oldest first."""
    def _():
        return (
            _sb().table("messages")
            .select("id,role,content")
            .eq("user_id", user_id)
            .order("created_at", desc=False)
            .limit(limit)
            .execute()
            .data
        )
    return await _run(_)


async def insert_message(user_id: str, role: str, content: str) -> str:
    """Insert a message row and return its id."""
    def _():
        return (
            _sb().table("messages")
            .insert({"user_id": user_id, "role": role, "content": content})
            .execute()
            .data[0]["id"]
        )
    return await _run(_)


async def delete_messages(ids: list[str]) -> None:
    if not ids:
        return
    def _():
        _sb().table("messages").delete().in_("id", ids).execute()
    await _run(_)


# ---------------------------------------------------------------------------
# long_term_memory
# ---------------------------------------------------------------------------

async def fetch_ltm(user_id: str) -> str:
    def _():
        rows = (
            _sb().table("long_term_memory")
            .select("summary")
            .eq("user_id", user_id)
            .execute()
            .data
        )
        return rows[0]["summary"] if rows else ""
    return await _run(_)


async def upsert_ltm(user_id: str, summary: str) -> None:
    def _():
        _sb().table("long_term_memory").upsert(
            {"user_id": user_id, "summary": summary,
             "updated_at": datetime.now(timezone.utc).isoformat()},
            on_conflict="user_id",
        ).execute()
    await _run(_)


# ---------------------------------------------------------------------------
# working_memory
# ---------------------------------------------------------------------------

async def fetch_wm(user_id: str) -> dict:
    def _():
        rows = (
            _sb().table("working_memory")
            .select("location,activity,mood")
            .eq("user_id", user_id)
            .execute()
            .data
        )
        return rows[0] if rows else {"location": "apartment", "activity": "unknown", "mood": "chill"}
    return await _run(_)


async def upsert_wm(user_id: str, location: str, activity: str, mood: str) -> None:
    def _():
        _sb().table("working_memory").upsert(
            {"user_id": user_id, "location": location, "activity": activity,
             "mood": mood, "updated_at": datetime.now(timezone.utc).isoformat()},
            on_conflict="user_id",
        ).execute()
    await _run(_)


# ---------------------------------------------------------------------------
# avatar
# ---------------------------------------------------------------------------

async def fetch_avatar_config(user_id: str) -> dict:
    """Return {file_path, expression_map} for the user's avatar; falls back to the is_default avatar."""
    def _():
        profile_rows = (
            _sb().table("profiles")
            .select("avatar_id")
            .eq("id", user_id)
            .execute()
            .data
        )
        avatar_id = profile_rows[0].get("avatar_id") if profile_rows else None

        if avatar_id:
            rows = (
                _sb().table("avatars")
                .select("file_path,expression_map")
                .eq("id", avatar_id)
                .execute()
                .data
            )
            if rows and rows[0].get("file_path"):
                return {"file_path": rows[0]["file_path"], "expression_map": rows[0].get("expression_map")}

        # No profile row, avatar_id is NULL, or avatar missing file_path → use default.
        rows = (
            _sb().table("avatars")
            .select("file_path,expression_map")
            .eq("is_default", True)
            .limit(1)
            .execute()
            .data
        )
        if rows:
            return {"file_path": rows[0]["file_path"], "expression_map": rows[0].get("expression_map")}
        return {"file_path": None, "expression_map": None}
    return await _run(_)


# ---------------------------------------------------------------------------
# voice
# ---------------------------------------------------------------------------

async def fetch_voice_tts_ref(user_id: str) -> str | None:
    """Return tts_ref for the user's voice; falls back to the is_default voice."""
    def _():
        profile_rows = (
            _sb().table("profiles")
            .select("voice_id")
            .eq("id", user_id)
            .execute()
            .data
        )
        voice_id = profile_rows[0].get("voice_id") if profile_rows else None

        if voice_id:
            rows = (
                _sb().table("voices")
                .select("tts_ref")
                .eq("id", voice_id)
                .execute()
                .data
            )
            if rows and rows[0].get("tts_ref"):
                return rows[0]["tts_ref"]

        # No profile row, voice_id is NULL, or voice missing tts_ref → use default.
        rows = (
            _sb().table("voices")
            .select("tts_ref")
            .eq("is_default", True)
            .limit(1)
            .execute()
            .data
        )
        return rows[0]["tts_ref"] if rows else None
    return await _run(_)


# ---------------------------------------------------------------------------
# calendar permission
# ---------------------------------------------------------------------------

async def fetch_calendar_enabled(user_id: str) -> bool:
    """Return profiles.calendar_enabled (the master switch) for the user.
    Default-deny: no profile row, NULL column, or any DB error all return
    False — never assume consent."""
    def _():
        try:
            rows = (
                _sb().table("profiles")
                .select("calendar_enabled")
                .eq("id", user_id)
                .execute()
                .data
            )
            return bool(rows[0].get("calendar_enabled")) if rows else False
        except Exception:
            log.warning("fetch_calendar_enabled failed for user=%s — defaulting to disabled", user_id)
            return False
    return await _run(_)


async def fetch_calendar_permissions(user_id: str) -> dict:
    """Return the master switch plus per-action calendar grants for the user:
    {"master": bool, "create": bool, "list": bool, "move": bool, "delete": bool}.

    Default-deny across the board: no profile row, a NULL/missing column
    (e.g. pre-migration), or any DB error all resolve every flag to False —
    this never raises, so a caller can trust the dict even before the
    per-action columns exist.
    """
    def _():
        try:
            rows = (
                _sb().table("profiles")
                .select("calendar_enabled,calendar_create_enabled,calendar_list_enabled,"
                        "calendar_move_enabled,calendar_delete_enabled")
                .eq("id", user_id)
                .execute()
                .data
            )
            row = rows[0] if rows else {}
            return {
                "master": bool(row.get("calendar_enabled")),
                "create": bool(row.get("calendar_create_enabled")),
                "list":   bool(row.get("calendar_list_enabled")),
                "move":   bool(row.get("calendar_move_enabled")),
                "delete": bool(row.get("calendar_delete_enabled")),
            }
        except Exception:
            log.warning("fetch_calendar_permissions failed for user=%s — defaulting to all disabled", user_id)
            return {"master": False, "create": False, "list": False, "move": False, "delete": False}
    return await _run(_)


# ---------------------------------------------------------------------------
# nudge_queue (Phase 3, NG1) — calendar-aware accountability nudges. One row
# per just-ended event judged worth a check-in; (user_id, event_uid) is the
# table's primary key, so enqueue_nudge's insert-on-conflict-skip IS the
# dedup guarantee — a re-scan of the same event is a safe no-op, never a
# second nudge for it.
# ---------------------------------------------------------------------------

async def fetch_accountability_nudges_enabled(user_id: str) -> bool:
    """Return profiles.accountability_nudges_enabled. Default-deny: no
    profile row, NULL column, or any DB error all return False — same
    discipline as fetch_calendar_enabled above."""
    def _():
        try:
            rows = (
                _sb().table("profiles")
                .select("accountability_nudges_enabled")
                .eq("id", user_id)
                .execute()
                .data
            )
            return bool(rows[0].get("accountability_nudges_enabled")) if rows else False
        except Exception:
            log.warning("fetch_accountability_nudges_enabled failed for user=%s — defaulting to disabled", user_id)
            return False
    return await _run(_)


async def enqueue_nudge(user_id: str, fields: dict) -> bool:
    """Insert one pending nudge. `fields` carries event_uid, event_calendar,
    event_title, event_start (ISO), event_end (ISO), question. Returns
    False (never raises) on a PK conflict (this exact event was already
    enqueued — the idempotency guarantee a caller relies on to re-scan
    safely) OR any other DB error; True only on a genuine new insert."""
    def _():
        try:
            _sb().table("nudge_queue").insert({"user_id": user_id, **fields}).execute()
            return True
        except Exception as exc:
            log.info("enqueue_nudge: skipped for user=%s event_uid=%r (likely already queued): %s",
                     user_id, fields.get("event_uid"), exc)
            return False
    return await _run(_)


async def fetch_pending_nudges(user_id: str) -> list[dict]:
    """Every status='pending' nudge for the user, oldest first. Empty list
    on no rows or any DB error — never raises."""
    def _():
        try:
            return (
                _sb().table("nudge_queue")
                .select("event_uid,event_calendar,event_title,question,event_start,event_end")
                .eq("user_id", user_id)
                .eq("status", "pending")
                .order("event_end", desc=False)
                .execute()
                .data
            )
        except Exception:
            log.warning("fetch_pending_nudges failed for user=%s — returning empty", user_id)
            return []
    return await _run(_)


async def mark_nudges_delivered(user_id: str, event_uids: list[str]) -> None:
    """Flip status pending -> delivered for the given event_uids, so the
    client's next poll doesn't see them again. Never raises — a failure
    here just means the SAME nudges may be re-delivered on the next poll,
    which is a harmless duplicate notification, not a correctness bug."""
    if not event_uids:
        return
    def _():
        try:
            (
                _sb().table("nudge_queue")
                .update({"status": "delivered", "delivered_at": datetime.now(timezone.utc).isoformat()})
                .eq("user_id", user_id)
                .in_("event_uid", event_uids)
                .execute()
            )
        except Exception:
            log.warning("mark_nudges_delivered failed for user=%s event_uids=%s", user_id, event_uids)
    await _run(_)


async def record_nudge_answer(user_id: str, event_uid: str, response: str) -> bool:
    """Store the user's reply to a nudge and flip status -> answered.
    Returns False (never raises) on any DB error."""
    def _():
        try:
            (
                _sb().table("nudge_queue")
                .update({
                    "status": "answered",
                    "response": response,
                    "answered_at": datetime.now(timezone.utc).isoformat(),
                })
                .eq("user_id", user_id)
                .eq("event_uid", event_uid)
                .execute()
            )
            return True
        except Exception:
            log.warning("record_nudge_answer failed for user=%s event_uid=%r", user_id, event_uid)
            return False
    return await _run(_)


async def fetch_recent_nudge_history(user_id: str, since) -> list[dict]:
    """Answered nudges since `since` (a datetime), oldest first — for
    accountability recall (NG5). Structured facts only (title, question,
    response, event_end): see THE notes WALL precedent in
    scheduling_preferences above for why this stays narrow rather than
    becoming a general free-text memory dump. Empty list on no rows or any
    DB error — never raises."""
    def _():
        try:
            return (
                _sb().table("nudge_queue")
                .select("event_title,question,response,event_end")
                .eq("user_id", user_id)
                .eq("status", "answered")
                .gte("event_end", since.isoformat())
                .order("event_end", desc=False)
                .execute()
                .data
            )
        except Exception:
            log.warning("fetch_recent_nudge_history failed for user=%s", user_id)
            return []
    return await _run(_)


# ---------------------------------------------------------------------------
# scheduling_preferences
#
# THE notes WALL: `notes` (and this table generally) is read ONLY by the
# calendar-blind activity-inference step to phrase a scheduling proposal's
# breakdown, and by the see/reset valve. It must NEVER be selected into,
# concatenated onto, or otherwise routed toward the Stage-0 persona prompt
# (core/prompts.py build_dynamic_prompt) — that function has no import of
# or call into this module, and no caller of the readers below may pass
# their result toward it. If a caller is ever tempted to surface `notes`
# outside the scheduling proposal/breakdown path, generate the copy from
# the numeric fields instead and leave `notes` unused. See the migration
# (supabase/migrations/20260919120000_add_scheduling_preferences.sql) for
# the same wall stated at the schema level.
# ---------------------------------------------------------------------------

_SCHEDULING_PREF_COLUMNS = (
    "activity,default_duration_min,pad_before_min,pad_after_min,tod_pref,notes,updated_at"
)


async def fetch_scheduling_preference(user_id: str, activity: str) -> dict | None:
    """Return the stored scheduling preference row for one normalized
    activity key ("gym", not "Gym" or "the gym"), or None on no row or any
    DB error — never raises. A miss is indistinguishable from "no
    preference learned yet" by design: the inference step falls back to
    its own defaults either way."""
    def _():
        try:
            rows = (
                _sb().table("scheduling_preferences")
                .select(_SCHEDULING_PREF_COLUMNS)
                .eq("user_id", user_id)
                .eq("activity", activity)
                .execute()
                .data
            )
            return rows[0] if rows else None
        except Exception:
            log.warning("fetch_scheduling_preference failed for user=%s activity=%r — treating as no preference",
                        user_id, activity)
            return None
    return await _run(_)


async def list_scheduling_preferences(user_id: str) -> list[dict]:
    """Return every stored scheduling preference for a user, ordered by
    activity — the "what have you learned" valve (Part E). Empty list on
    no rows or any DB error, never raises."""
    def _():
        try:
            return (
                _sb().table("scheduling_preferences")
                .select(_SCHEDULING_PREF_COLUMNS)
                .eq("user_id", user_id)
                .order("activity")
                .execute()
                .data
            )
        except Exception:
            log.warning("list_scheduling_preferences failed for user=%s — defaulting to empty", user_id)
            return []
    return await _run(_)


async def upsert_scheduling_preference(user_id: str, activity: str, fields: dict) -> bool:
    """Write a learned correction. Called ONLY on a confirmed accept (see
    POST /calendar/add_slots) — never speculatively, never before the user
    has actually agreed to the corrected plan. `fields` may include any of
    default_duration_min/pad_before_min/pad_after_min/tod_pref/notes;
    columns not present in `fields` are left at their existing/default
    values via upsert's merge-on-conflict semantics (not overwritten to
    NULL). Returns True on success, False on any DB error — never raises,
    so a failed write degrades to "the correction didn't stick" rather
    than breaking the turn that already added the calendar events."""
    def _():
        try:
            _sb().table("scheduling_preferences").upsert(
                {
                    "user_id": user_id,
                    "activity": activity,
                    **fields,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
                on_conflict="user_id,activity",
            ).execute()
            return True
        except Exception:
            log.warning("upsert_scheduling_preference failed for user=%s activity=%r", user_id, activity)
            return False
    return await _run(_)


async def delete_scheduling_preference(user_id: str, activity: str | None = None) -> bool:
    """Reset one learned preference (`activity` given) or ALL of them
    (`activity=None`) — the reset half of the see/reset valve (Part E).
    Returns True on success, False on any DB error — never raises."""
    def _():
        try:
            q = _sb().table("scheduling_preferences").delete().eq("user_id", user_id)
            if activity is not None:
                q = q.eq("activity", activity)
            q.execute()
            return True
        except Exception:
            log.warning("delete_scheduling_preference failed for user=%s activity=%r", user_id, activity)
            return False
    return await _run(_)



async def fetch_username(user_id: str) -> str:
    """Return the email local-part for a user as a username (matches /chat prefix convention)."""
    def _():
        try:
            result = _sb().auth.admin.get_user_by_id(user_id)
            email = (result.user.email or "") if result.user else ""
            return email.split("@")[0] if email else "user"
        except Exception:
            return "user"
    return await _run(_)


# ---------------------------------------------------------------------------
# timezone — per-user IANA zone, so "now"/"today" and event times are
# computed in the USER's zone, never the host's (see the timezone-
# correctness fix: the host running the backend is no longer necessarily
# in the user's zone once deployed to a cloud box).
# ---------------------------------------------------------------------------

async def fetch_user_timezone(user_id: str) -> ZoneInfo:
    """The user's stored IANA zone as a ZoneInfo, or a fallback — never
    raises. Falls back through: no profile row / NULL column / DB error /
    an invalid stored string -> DEFAULT_TZ env var -> "UTC". A bad stored
    value must never propagate as an exception into a chat turn."""
    def _():
        try:
            rows = (
                _sb().table("profiles")
                .select("timezone")
                .eq("id", user_id)
                .execute()
                .data
            )
            tz_name = rows[0].get("timezone") if rows else None
            if tz_name:
                try:
                    return ZoneInfo(tz_name)
                except (ZoneInfoNotFoundError, ValueError):
                    log.warning("fetch_user_timezone: invalid stored zone %r for user=%s — falling back", tz_name, user_id)
        except Exception:
            log.warning("fetch_user_timezone failed for user=%s — falling back", user_id)
        try:
            return ZoneInfo(os.getenv("DEFAULT_TZ", "UTC"))
        except (ZoneInfoNotFoundError, ValueError):
            return ZoneInfo("UTC")
    return await _run(_)


async def update_user_timezone(user_id: str, tz: str) -> bool:
    """Validate and store the user's IANA zone. Returns False (never
    raises) on an invalid zone name or any DB error, so a bad client-
    supplied string can't corrupt the stored value or crash the caller."""
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("update_user_timezone: rejected invalid zone %r for user=%s", tz, user_id)
        return False

    def _():
        try:
            _sb().table("profiles").update({"timezone": tz}).eq("id", user_id).execute()
            return True
        except Exception:
            log.warning("update_user_timezone: DB write failed for user=%s", user_id)
            return False
    return await _run(_)


# ---------------------------------------------------------------------------
# google_calendar_credentials (G2a) — a standing refresh token per user for
# core/calendar_google.py's read provider. BACKEND-ONLY: the table has RLS
# enabled with no policies at all (see the migration), so only this
# service-role client can ever read/write it — never expose these two
# functions' results to a client-facing endpoint directly.
# ---------------------------------------------------------------------------

async def fetch_google_refresh_token(user_id: str) -> str | None:
    """The user's stored Google OAuth refresh token, or None if they've
    never connected (or the row/table read fails) — never raises. None
    is the caller's signal to surface calendar_google's "needs_connection"
    status, not an empty calendar."""
    def _():
        try:
            rows = (
                _sb().table("google_calendar_credentials")
                .select("refresh_token")
                .eq("user_id", user_id)
                .execute()
                .data
            )
            return rows[0]["refresh_token"] if rows else None
        except Exception:
            log.warning("fetch_google_refresh_token failed for user=%s", user_id)
            return None
    return await _run(_)


async def upsert_google_credentials(user_id: str, refresh_token: str, scopes: str) -> bool:
    """Store (or replace) a user's Google refresh token after a successful
    OAuth consent. Returns True on success, False on any DB error — never
    raises; connected_at is left at its existing value on repeat upserts
    only if the caller omits it, but a RE-connect is a genuine new grant,
    so this always stamps connected_at to now()."""
    def _():
        try:
            _sb().table("google_calendar_credentials").upsert(
                {
                    "user_id": user_id,
                    "refresh_token": refresh_token,
                    "scopes": scopes,
                    "connected_at": datetime.now(timezone.utc).isoformat(),
                },
                on_conflict="user_id",
            ).execute()
            return True
        except Exception:
            log.warning("upsert_google_credentials failed for user=%s", user_id)
            return False
    return await _run(_)


async def delete_google_credentials(user_id: str) -> bool:
    """Remove a user's stored Google refresh token (G3 disconnect). Returns
    True on success, False on any DB error — never raises. The caller
    (POST /calendar/disconnect) is responsible for best-effort revoking the
    token with Google BEFORE calling this — once this row is gone, there's
    no way to revoke it anymore."""
    def _():
        try:
            _sb().table("google_calendar_credentials").delete().eq("user_id", user_id).execute()
            return True
        except Exception:
            log.warning("delete_google_credentials failed for user=%s", user_id)
            return False
    return await _run(_)


async def fetch_google_connection(user_id: str) -> dict:
    """Connection STATUS only — {"connected": bool, "scopes": str|None,
    "connected_at": str|None}. Deliberately selects ONLY scopes,
    connected_at: this function must be structurally incapable of leaking
    refresh_token, since its result is exposed directly to the client via
    GET /calendar/connection_status. Never raises; {"connected": False,
    "scopes": None, "connected_at": None} on any DB error — a status-check
    failure must read as "not connected", never silently as "connected"
    with stale/empty fields."""
    def _():
        try:
            rows = (
                _sb().table("google_calendar_credentials")
                .select("scopes, connected_at")
                .eq("user_id", user_id)
                .execute()
                .data
            )
            if not rows:
                return {"connected": False, "scopes": None, "connected_at": None}
            return {
                "connected": True,
                "scopes": rows[0].get("scopes"),
                "connected_at": rows[0].get("connected_at"),
            }
        except Exception:
            log.warning("fetch_google_connection failed for user=%s", user_id)
            return {"connected": False, "scopes": None, "connected_at": None}
    return await _run(_)

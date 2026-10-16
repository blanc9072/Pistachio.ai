"""
Phase 3 (NG2) — the accountability nudge engine. Scans a user's calendar
for just-ended events and decides which ones are worth a check-in,
generating the question to ask when they are.

Scope: judge + generate only. No web framework deps (no FastAPI import),
no notification delivery, no DB writes beyond the one enqueue call —
those live in api/routes.py (NG3) and the Electron client (NG4).

Timezone: "ended" is decided in the USER's zone (user_tz), never the
server's — an event that ended at 5pm Eastern must not be judged against
a UTC server clock. See core/prompts.py / core/calendar_google.py for the
same user_tz threading discipline this module follows.
"""

import asyncio
import logging
from datetime import datetime, timedelta

from google.genai import types

import core.calendar_backend as calendar_backend
import core.db as db
from core.gemini import gemini_client, SAFETY_SETTINGS
from core.memory import get_session
from core.prompts import build_proactive_prompt

log = logging.getLogger(__name__)

# How far back a single scan looks for ended events. Bounds the backlog a
# woken/restarted Mac produces to "recent" check-ins, never a flood of
# every event that ended while the app was closed.
STALENESS_CAP = timedelta(hours=4)

# Below this, an event is too short to be worth interrupting about
# ("left a 5-min buffer before the next thing" shouldn't nudge).
_MIN_EVENT_MINUTES = 10

# Cheap, no-model-call substring check against the event's own title AND
# its calendar id/token — Google's holiday calendars have ids like
# "en.usa#holiday@group.v.calendar.google.com", and a human-named
# "Birthdays" calendar or event commonly contains the word directly. This
# is a pre-filter only, to avoid wasting a model call on obvious noise —
# the model classifier (below) is what actually decides worthiness for
# everything that gets this far.
_NOISE_SUBSTRINGS = ("holiday", "birthday")

_JUDGE_MODEL = "gemini-2.5-flash"
_JUDGE_TIMEOUT = 15.0

_JUDGE_EVENT = types.FunctionDeclaration(
    name="judge_event",
    description="Decide whether a just-ended calendar event is worth an accountability "
                "check-in, and if so, write the question to ask about it.",
    parameters=types.Schema(
        type=types.Type.OBJECT,
        properties={
            "worthy": types.Schema(
                type=types.Type.BOOLEAN,
                description="True ONLY for a real task/commitment/self-care block a person "
                            "would want to be asked about (work-on-X, study, gym, sleep, "
                            "meal-prep, chores). False for a passive calendar artifact: a "
                            "holiday, someone else's meeting invite you did not accept, a "
                            "reminder with no action."),
            "question": types.Schema(
                type=types.Type.STRING,
                description="A short, warm question specific to the event, in her voice "
                            "(\"how'd the gym session go?\", \"did you get through the "
                            "deck?\", \"how'd you sleep?\"). Only meaningful when worthy=true."),
        },
        required=["worthy"],
    ),
)
_JUDGE_TOOL = types.Tool(function_declarations=[_JUDGE_EVENT])


def _is_noise(ev: dict) -> bool:
    haystack = f"{ev.get('title', '')} {ev.get('calendar', '')}".lower()
    return any(s in haystack for s in _NOISE_SUBSTRINGS)


def _parse_naive(iso_str: str | None) -> datetime | None:
    if not iso_str:
        return None
    try:
        return datetime.fromisoformat(iso_str)
    except ValueError:
        return None


def _judge_instruction(ev: dict) -> str:
    return (
        f"An event on the user's calendar just ended: {ev.get('title') or '(untitled)'!r}, "
        f"from {ev.get('start')} to {ev.get('end')}. Call judge_event to decide whether "
        f"this is worth an accountability check-in, and if so, write the question."
    )


async def _judge_one(session, user_tz, ev: dict) -> dict | None:
    """One model call for one event. Returns {"worthy": bool, "question":
    str} or None on any failure (never raises) — a failed judge call must
    be treated as "skip this event," not "assume worthy" or crash the
    scan."""
    try:
        resp = await asyncio.wait_for(
            gemini_client.aio.models.generate_content(
                model=_JUDGE_MODEL,
                contents=[types.Content(
                    role="user", parts=[types.Part.from_text(text=_judge_instruction(ev))],
                )],
                config=types.GenerateContentConfig(
                    system_instruction=build_proactive_prompt(session, user_tz),
                    tools=[_JUDGE_TOOL],
                    tool_config=types.ToolConfig(
                        function_calling_config=types.FunctionCallingConfig(
                            mode=types.FunctionCallingConfigMode.AUTO,
                        )
                    ),
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                    temperature=0,
                    safety_settings=SAFETY_SETTINGS,
                ),
            ),
            timeout=_JUDGE_TIMEOUT,
        )
        fcs = resp.function_calls
        if not fcs:
            return None
        args = dict(fcs[0].args)
        worthy = bool(args.get("worthy"))
        question = (args.get("question") or "").strip()
        if not worthy or not question:
            return None
        return {"worthy": True, "question": question}
    except Exception as exc:
        log.warning("[nudges] judge call failed for event %r (skipping, not enqueuing): %s",
                    ev.get("title"), exc)
        return None


async def scan_and_enqueue(user_id: str, user_tz) -> int:
    """Scan this user's calendar for just-ended events and enqueue a nudge
    for each one judged worthy. Returns the count enqueued. Never raises
    — every event is wrapped so one bad event can't abort the scan, and
    every DB/model call already fails safe on its own.
    """
    if not await db.fetch_accountability_nudges_enabled(user_id):
        return 0
    perms = await db.fetch_calendar_permissions(user_id)
    if not (perms["master"] and perms.get("list", False)):
        return 0

    now = datetime.now(user_tz).replace(tzinfo=None)
    window_start = now - STALENESS_CAP

    read = await calendar_backend.read_window(user_id, window_start, now, writable_only=False, user_tz=user_tz)
    if read["status"] != "ok":
        log.info("[nudges] scan read failed (status=%s) for user=%s — skipping this cycle",
                  read["status"], user_id)
        return 0

    session = await get_session(user_id)
    enqueued = 0

    for ev in read["events"]:
        try:
            if ev.get("all_day"):
                continue
            if _is_noise(ev):
                continue

            start_dt = _parse_naive(ev.get("start"))
            end_dt = _parse_naive(ev.get("end"))
            if start_dt is None or end_dt is None:
                continue
            if end_dt > now:
                continue  # not actually ended yet
            if (end_dt - start_dt) < timedelta(minutes=_MIN_EVENT_MINUTES):
                continue

            uid = ev.get("uid")
            if not uid:
                continue

            judged = await _judge_one(session, user_tz, ev)
            if judged is None:
                continue

            ok = await db.enqueue_nudge(user_id, {
                "event_uid": uid,
                "event_calendar": ev.get("calendar") or "",
                "event_title": ev.get("title") or "",
                "event_start": start_dt.replace(tzinfo=user_tz).isoformat(),
                "event_end": end_dt.replace(tzinfo=user_tz).isoformat(),
                "question": judged["question"],
            })
            if ok:
                enqueued += 1
        except Exception as exc:
            log.warning("[nudges] skipping event %r after error: %s", ev.get("title"), exc)
            continue

    return enqueued

PROJECT: WaifuLive.ai, an agentic Live2D AI secretary and productivity manager

What it is:
An always-on desktop AI companion. A Live2D anime avatar that lives on screen, talks by text and voice, watches the screen when you allow it, remembers you across sessions, and runs your calendar as an agentic secretary. The companion is the hook; the productivity layer (calendar and scheduling now, proactive accountability next) is the core of the product.

# WaifuLive: Tech Stack and Decision Record

This document lists each significant tool in the stack: what it is, why it was chosen, and how it is used here. It runs from the narrowest single-purpose tool up to the most central platform pieces. An Architecture Decisions section at the end captures the design invariants that span multiple tools.

Runtime shape: a Python backend (a FastAPI HTTP API plus a Gemini Live WebSocket relay, both in one process) and an Electron desktop app, with Supabase for identity and persistence. The calendar layer now runs entirely on the Google Calendar API, so the backend no longer depends on macOS. It currently runs on the user's Mac, and a move to a Linux VPS is in progress.

---

## 1. Google Calendar API (via the Google API Python client) — calendar reads and writes

**What it is.** Google's Calendar v3 REST API, reached through the official Python client with per-user OAuth2 credentials. This is the live calendar engine for both reading and writing.

**Why.** The project started on macOS-local calendar access (EventKit for reads, AppleScript for writes, see section 2), which tied the whole product to a Mac. The goal is cloud hosting and mobile reach, so the calendar I/O was moved to the Google Calendar API. It runs anywhere, reads and writes the same account the user already lives in, and exposes recurrence, multi-calendar access, and stable per-event addressing. `events.list(singleEvents=True)` expands recurring events into real per-occurrence instances, which is exactly what a schedule reader needs, and single-occurrence versus whole-series editing is a first-class operation.

**How.** `core/calendar_google.py` is the provider: it builds credentials from a stored refresh token, enumerates calendars, serves `read_window(...)` for reads, and `create_event` / `move_event` / `delete_event` (plus by-uid variants and a candidate search) for writes. Events are addressed by the real `calendarId` and event id, never a display name. A missing or revoked connection returns an explicit `needs_connection` status, never a silent empty result. New events default to the user's primary Google calendar, set per deployment by a `GOOGLE_WRITE_CALENDAR_ID` env var. A `core/calendar_backend.py` seam selects the provider from one `CALENDAR_BACKEND` env var (`google` live, `eventkit` fallback), so reads and writes switch together and the rest of the app does not need to know which backend is active.

**Connecting an account.** Each user gets a real Connect Google Calendar flow. Consent opens in the system browser (Google blocks embedded webviews), Google redirects to a backend callback, and the resulting refresh token is stored server-side in Supabase under a service-role-only table, never exposed to the client. Disconnect and a live connection status share the same surface. The token is keyed per user, so the design is multi-user ready even though it runs for one account today.

**Recurring edits.** A move or delete that targets a repeating event asks "just this one, or the whole series?" A single occurrence is edited by its instance id; the whole series is edited through the master event. Deleting a series removes every occurrence; moving a series shifts its time of day while keeping each occurrence's own date. One known Google behavior: moving a whole series discards any occurrences the user had individually rescheduled, because patching the master regenerates that slot at the series default time. This is inherent to the API, documented rather than worked around.

---

## 2. EventKit and AppleScript / osascript — local calendar fallback

**What it is.** The original macOS-local calendar path: Apple's EventKit framework (via PyObjC) for reads, and AppleScript run through `osascript` for writes, both driving the same store Calendar.app uses.

**Why it existed, and why it is kept.** The first read path used AppleScript, which has a fatal limitation: its `every event whose start date >= X` query tests the master recurrence rule's literal start date, never the expanded occurrences, so any recurring series that began before the window returned nothing. That bug stayed hidden for the entire early build because every test used one-off events. EventKit's `predicateForEvents(withStart:end:calendars:)` expands recurrence natively and is about a thousand times faster (single-digit milliseconds versus tens of seconds), so reads moved to it while hardened osascript stayed for writes. All of this is now the fallback behind `CALENDAR_BACKEND=eventkit`. It is kept working for local development and as a safety net, but it is not the live path.

**How.** `core/calendar_read_ek.py` exposes `permission_status()`, `enumerate_calendars()`, and `read_window(...)`, returning per-occurrence event dicts in local time. `core/calendar_executor.py` builds the create/move/delete AppleScript and runs it off the event loop with timeouts. Hard-won details are baked in: dates are set field by field (string parsing is locale-dependent and broke across machines), moves use order-dependent start/end assignment to avoid the `-10025` "start must precede end" error, and the `-1743` permission-denied signature is detected and surfaced cleanly. This path still refuses recurring move and delete, since osascript cannot do them safely; the Google backend is where recurring edits are supported.

---

## 3. faster-whisper — on-device speech-to-text

**What it is.** A fast, quantized reimplementation of OpenAI Whisper. We use the `base.en` model at int8.

**Why.** Dictation (the mic-to-text button, separate from live voice mode) should be private and free of per-request cost and latency to a cloud STT. On-device transcription keeps audio local and is fast enough for dictation bursts on CPU.

**How.** `/transcribe` receives base64 WAV, runs `WhisperModel("base.en", device="cpu", compute_type="int8")` in a worker thread, and returns the transcript. The model (about 154 MB) downloads on first use and is cached afterward.

---

## 4. ElevenLabs — text-to-speech

**What it is.** A cloud neural TTS API. We use the `eleven_flash_v2_5` model for low latency.

**Why.** The companion needs a natural, expressive voice and per-user voice selection. ElevenLabs gives high-quality voices addressable by ID, which maps cleanly onto per-user voice preferences. It sits behind an abstraction so the provider can be swapped without touching the chat flow.

**How.** `core/tts.py` defines an abstract `Synthesizer` with an `ElevenLabsSynthesizer` implementation (streaming `mp3_44100_128` over `httpx`), selected by the `TTS_ENGINE` env var. Each user's `voices.tts_ref` (an ElevenLabs voice ID) is stored in Supabase and looked up per reply. `clean_for_tts()` strips emoticons, markup, and emoji from what is spoken without altering the displayed text. TTS is non-fatal: a synthesis failure just omits audio.

---

## 5. Live2D (PixiJS + pixi-live2d-display + Cubism SDK) — the avatar

**What it is.** Live2D is 2D character rigging; the Cubism runtime animates the model, rendered in the browser via PixiJS with the `pixi-live2d-display` binding.

**Why.** The avatar is the product's face, an expressive on-screen presence rather than a chat window. Live2D is the standard for this kind of anime-style rigged character and runs in Electron's renderer with no native dependencies.

**How.** `model.html` runs a transparent, always-on-top, click-through Electron window holding the PixiJS canvas. A persistent ticker drives idle motion (breathing, body sway), cursor-following eye and head tracking, and audio-reactive mouth movement (lip-sync from a Web Audio analyser on the TTS stream). Expressions are driven by an emotion classifier on replies and a mood value from working memory, mapped through a per-avatar `expression_map` (stored in Supabase) so different model files can reuse the same emotion keys. Models hot-swap when the user changes avatar.

---

## 6. Gemini Live API — real-time voice conversation

**What it is.** Google's low-latency bidirectional voice model (`gemini-live-2.5-flash-native-audio`), spoken to over a WebSocket.

**Why.** Live voice mode (push-to-talk) needs true real-time streaming audio in and native audio out with barge-in and transcription, a different modality than the text chat path. The Live API is purpose-built for it.

**How.** `main.py` runs a WebSocket relay (default `:8765`). The Electron client streams PCM mic audio up; the relay forwards it to Gemini Live and streams audio back down, along with input and output transcriptions that render as chat bubbles. Per-turn transcripts are buffered and flushed into the same memory system the text path uses, so voice and text share one continuous memory. The voice model runs in `us-west1`; the text brain runs at `global` (see section 10).

---

## 7. Electron — desktop application shell

**What it is.** Chromium plus Node.js for building a cross-platform desktop app from web tech.

**Why.** The product must live on the desktop: a floating always-on-top avatar, global hotkeys, screen capture, system-tray control, idle detection. A browser tab cannot do those; Electron can, while letting the UI stay plain HTML, CSS, and JS.

**How.** `main.js` (main process) owns auth, session and token storage, and acts as an API proxy that injects the user's bearer token before forwarding to the backend, so secrets never reach the renderer. `preload.js` exposes a narrow `contextBridge` IPC surface. Renderer windows: login, chat (text, mic, dictation, settings popup), model (the avatar), and settings. It uses `desktopCapturer` for opt-in screen-watch, `powerMonitor` for idle-based proactive nudges, and a system `Tray` for screen-watch and Do-Not-Disturb toggles. The settings popup includes the Connect Google Calendar control, which opens consent in the system browser via `shell.openExternal`.

---

## 8. Supabase — identity and persistence

**What it is.** Managed Postgres with built-in auth, row-level security, and a CLI for migrations.

**Why.** The app needs real per-user accounts and durable per-user state (persona, voice, avatar, memory, permissions, calendar connection) with hard isolation between users. Supabase gives auth, Postgres, and RLS in one place, and the client can hit it directly with the user's token for settings reads and writes (RLS enforces ownership), keeping that off the backend's critical path.

**How.** Auth issues JWTs the backend verifies. Tables: `profiles` (per-user config plus calendar-permission flags), `personas` / `voices` / `avatars` (lookup tables), `messages`, `long_term_memory`, `working_memory`, `scheduling_preferences`, and `google_calendar_credentials` (the stored Google refresh token). RLS is owner-only on all user-scoped tables (`auth.uid() = user_id`), so a user structurally cannot read or write another's rows. The credentials table is a special case: RLS is enabled with zero policies, so only the backend's service role can touch it and no client ever reads a refresh token. A `handle_new_user` trigger auto-provisions a profile pointing at the default persona, voice, and avatar. Schema changes go through versioned CLI migrations (`supabase db push` to the hosted DB).

---

## 9. FastAPI + Uvicorn (+ websockets) — the backend spine

**What it is.** An async Python web framework (FastAPI) on the Uvicorn ASGI server, plus the `websockets` library for the voice relay.

**Why.** The backend is glue between many async I/O sources (Gemini, Supabase, ElevenLabs, Whisper, the calendar API). FastAPI's async model fits, Pydantic gives typed request and response contracts, and dependency injection makes token-verified auth clean. Running the HTTP API and the Live voice relay as two servers in one asyncio process keeps deployment simple.

**How.** `main.py` runs the FastAPI app (default `:8000`) and the Gemini Live WebSocket relay (default `:8765`) concurrently via `asyncio.gather`. HTTP endpoints: `/chat`, `/nudge`, `/transcribe`, `/profile`, `/memory`, `/sleep`, the Google OAuth endpoints (`/oauth/google/start`, `/oauth/google/callback`, `/calendar/connection_status`, `/calendar/disconnect`), and the calendar action endpoints (`/calendar/...`). Auth is a FastAPI dependency that verifies the Supabase JWT and derives `user_id` from the token only, never the request body, so a client cannot impersonate another user. Hosts, ports, and credential paths are env-driven so the same process runs behind a reverse proxy on a server.

---

## 10. Google Gemini on Vertex AI (google-genai) — the brain

**What it is.** Google's Gemini model family, accessed through the `google-genai` SDK against Vertex AI. This is the core intelligence: persona, conversation, and the tool-use pipeline.

**Why.** The companion's personality, memory synthesis, screen understanding, and agentic tool-calling all run on Gemini. It is the reason the product exists, and everything else is built around feeding it context and executing what it decides.

**How, the models in play:**
- **`gemini-3.6-flash`**, the chat brain. Runs at `location="global"` (the Gemini 3.x family is not served in `us-west1`), `thinking_level=MINIMAL` (a texting companion wants speed and short replies, not visible reasoning), temperature `1.0`. Model, location, and thinking are config-driven, so the previous fine-tuned 2.5 endpoint (in `us-west1`) stays a one-env-var rollback.
- **`gemini-2.5-flash`**, the utility model: the Stage-2 tool resolver (plain English to typed function call, temperature 0), working-memory extraction, and the screen-vision fallback.
- **`gemini-live-2.5-flash-native-audio`**, live voice (see section 6).

Auth is via Vertex AI and Application Default Credentials from a service-account file, with the file path set by env, not an API key. The persona lives entirely in the system prompt, not the weights, which is what made swapping the fine-tune for a stock model low-risk.

---

## Architecture Decisions (cross-cutting)

**Swappable calendar backend.** All calendar I/O goes through one seam (`core/calendar_backend.py`) that picks a provider from the `CALENDAR_BACKEND` env var: Google Calendar API (live) or the macOS EventKit and osascript path (fallback). Reads and writes always use the same provider, result shapes are identical across both, and nothing above the seam knows which one is active. The cutover between them was a single config flip, fully reversible, with a full regression run on each side.

**Model reasons about intent; code guarantees safety.** The scheduler is the clearest example. A calendar-blind model step infers what an activity really needs (for example, "gym" equals workout plus commute plus shower, roughly 2.5 hours), then a deterministic placer finds real free gaps against a live read and can only ever select from computed-free time. The model never sees the calendar; the placer never guesses. Double-booking is structurally impossible regardless of model behavior.

**Two-stage tool pipeline (signal, resolve, execute).** The chat model emits a plain-English `<action>` signal; a separate resolver model turns it into a typed call; an executor runs it. The split originally existed because grounding (web search) and function-calling were mutually exclusive on the fine-tuned endpoint. On `gemini-3.6-flash` they coexist, so the split is no longer forced, but it is kept because each stage is independently testable and the executor stays model-agnostic (reusable by the voice path later).

**The phantom fence.** Because the chat model writes its reply before tools run, it could fabricate a schedule from memory. For read-type actions its pre-tool text is forced to a content-free acknowledgment, and a client-side and TTS-side backstop suppresses any fabricated schedule from reaching the user on screen or in audio. A real read is the only source of calendar facts.

**Per-action, default-deny permissions.** Calendar access is a master switch plus per-action grants (create, list, move, delete) on `profiles`, all defaulting to false, enforced server-side and gated by RLS. Users control exactly what the assistant may do. This is a core product stance (customizable, hard-restricted agency), not just security hygiene.

**Destructive and durable actions confirm; approximate never auto-acts.** Delete and move act only on an unambiguous exact match, and otherwise ask "which one?" Typo-tolerant matching that requires guessing must be confirmed before acting. A repeating event always asks occurrence versus series before it writes. Learned scheduling preferences are echoed and only persisted on user acceptance, and are always inspectable and resettable.

**Secrets stay server-side.** The Electron main process injects the user's bearer token before forwarding to the backend, so the renderer never holds secrets. The Google refresh token lives only in the backend's service-role Supabase table and never appears in an API response or a log line.

**The cache that came and went.** When reads were slow osascript (a serialized multi-calendar race in the tens of seconds), a Supabase snapshot cache with a background refresher was built to move reads off the request path. EventKit then made live reads a few milliseconds, faster than reading the cache would be, so the entire cache was retired. Kept as a lesson: a workaround earns its place against the constraint of its time and should be removed, not preserved, once the real fix lands.

---

## Known gaps / roadmap

- **Cloud deploy (in progress).** The calendar layer no longer needs macOS, so the backend is moving off the Mac onto a Linux VPS behind a TLS reverse proxy. Remaining work is small and mostly configuration: make the last macOS-only import lazy, bind the services behind the proxy, and move secrets to the host.
- **Proactive productivity loop.** A server-side task that checks calendar progress through the day and nudges the user, with Do-Not-Disturb sub-toggles (nudges versus reminders). This is the core differentiator and depends on the always-on cloud backend.
- **Mobile.** A greenfield mobile client (text plus live voice) against the cloud backend, so the companion, calendar, and nudges reach the phone.
- **Model switcher.** A provider abstraction to choose between Gemini and Claude per role, dropping the fine-tune. It mirrors the existing TTS and calendar provider pattern, and it is the biggest lever on running cost.
- **Audio quality.** Closing the gap between the app's TTS output and the ElevenLabs voice demo. The sample-rate issue is fixed; the remaining work is the model and voice-settings tuning.
- **Recurring create.** Reading and editing recurring events is done; creating a new recurring series from a request is the remaining recurrence feature.

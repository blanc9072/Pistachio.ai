const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electronAPI', {
  // Voice relay base URL (ws://localhost:8765 local dev, wss://<DOMAIN>/ws
  // once cloud-deployed — see config.js). A sandboxed preload script can't
  // require() arbitrary local files (only a small Node builtin allowlist is
  // available — requiring './config' here throws "module not found" and
  // silently kills preload entirely, taking down window.electronAPI and
  // every IPC call with it). sendSync asks the main process instead, which
  // CAN read config.js normally, and blocks until it answers — same
  // synchronous value at the call site, just resolved via IPC underneath.
  voiceWsUrl: ipcRenderer.sendSync('config:wsBase'),

  // ── Model window ──────────────────────────────────────────────────────────
  onMouseMove:     (cb)     => ipcRenderer.on('mouse-move', (_, pos) => cb(pos)),
  setIgnoreMouse:  (ignore) => ipcRenderer.send('set-ignore-mouse', ignore),
  startDrag:       (offset) => ipcRenderer.send('start-drag', offset),
  endDrag:         ()       => ipcRenderer.send('end-drag'),
  getAvatarConfig: ()       => ipcRenderer.invoke('avatar:config'),
  setTimezone:     (tz)     => ipcRenderer.invoke('api:setTimezone', { tz }),

  // ── Chat window ───────────────────────────────────────────────────────────
  onToggleMic:    (cb) => ipcRenderer.on('toggle-mic', cb),
  closeChat:      ()   => ipcRenderer.send('close-chat'),
  captureScreen:  ()   => ipcRenderer.invoke('capture-screen'),

  // ── Auth (credentials/token never touch the renderer) ────────────────────
  login:   (email, password) => ipcRenderer.invoke('auth:login',  { email, password }),
  signup:  (email, password) => ipcRenderer.invoke('auth:signup', { email, password }),
  logout:  ()                => ipcRenderer.invoke('auth:logout'),
  getUser: ()                => ipcRenderer.invoke('auth:getUser'),

  // ── Backend proxy (main injects the token before forwarding) ─────────────
  sendChat:     (payload)         => ipcRenderer.invoke('api:chat',       payload),
  transcribe:   (audioB64)        => ipcRenderer.invoke('api:transcribe', audioB64),
  // Phase 3 (NG4) — accountability nudges. Detection + the pending-queue
  // poll both live in main.js; the renderer only ever hears about an
  // ALREADY-delivered nudge (via onNudgeOpened, when its notification is
  // clicked) and reports the answer back (nudgesAnswered) once the
  // user's reply has gone through the normal /chat path.
  onNudgeOpened:   (cb) => ipcRenderer.on('nudge-opened', (_, data) => cb(data)),
  nudgesAnswered:  (eventUid, response) => ipcRenderer.invoke('api:nudgesAnswered', { event_uid: eventUid, response }),
  googleConnectStart:     () => ipcRenderer.invoke('api:googleConnectStart'),
  googleConnectionStatus: () => ipcRenderer.invoke('api:googleConnectionStatus'),
  googleDisconnect:       () => ipcRenderer.invoke('api:googleDisconnect'),
  deleteByUid:  (uid, calendar, scope)   => ipcRenderer.invoke('api:deleteByUid', { uid, calendar, scope }),
  moveByUid:    (uid, calendar, newStart, newEnd, scope) =>
                  ipcRenderer.invoke('api:moveByUid', { uid, calendar, newStart, newEnd, scope }),
  addSlots:     (activity, slots, prefDelta) =>
                  ipcRenderer.invoke('api:addSlots', { activity, slots, prefDelta }),
  correctPlan:  (payload)          => ipcRenderer.invoke('api:correctPlan', payload),
  getSchedulingPrefs:   ()          => ipcRenderer.invoke('api:getSchedulingPrefs'),
  resetSchedulingPrefs: (activity)  => ipcRenderer.invoke('api:resetSchedulingPrefs', { activity }),

  // ── Settings data ─────────────────────────────────────────────────────────
  loadSettings:  ()       => ipcRenderer.invoke('settings:load'),
  saveSettings:  (prefs)  => ipcRenderer.invoke('settings:save', prefs),

  // ── Screen watch — shared state between overlay and system tray ───────────
  getScreenWatch:       ()    => ipcRenderer.invoke('screenwatch:get'),
  setScreenWatch:       (on)  => ipcRenderer.invoke('screenwatch:set', on),
  onScreenWatchChanged: (cb)  => ipcRenderer.on('screenwatch-changed', (_, v) => cb(v)),

  // ── Do Not Disturb — shared state between overlay and system tray ─────────
  getDnd:       ()    => ipcRenderer.invoke('dnd:get'),
  setDnd:       (on)  => ipcRenderer.invoke('dnd:set', on),
  onDndChanged: (cb)  => ipcRenderer.on('dnd-changed', (_, v) => cb(v)),

  // ── Voice relay ───────────────────────────────────────────────────────────
  getVoiceToken: () => ipcRenderer.invoke('voice:token'),
});

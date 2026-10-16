// Public Supabase config. The anon key is intentionally public — it is safe to
// ship in a desktop app. Security comes from server-side RLS policies.
// User session tokens are never stored here; they live encrypted in safeStorage.
//
// API_BASE/WS_BASE default to the hosted OCI backend (always-on, systemd +
// nginx on api.waifulive.ai) — normal `npm start` now hits the real server
// with no env vars needed. For local dev against main.py on this machine
// instead, override explicitly:
//   PISTACHIO_API_BASE=http://localhost:8000 PISTACHIO_WS_BASE=ws://localhost:8765 npm start
module.exports = {
  SUPABASE_URL:      'https://xrohxpmxnrhiqfpqtfff.supabase.co',
  SUPABASE_ANON_KEY: 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Inhyb2h4cG14bnJoaXFmcHF0ZmZmIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODA0NDc1ODcsImV4cCI6MjA5NjAyMzU4N30.CF30KDhe3oPNEbH5jw_ySX-WPa9qPk9RDOqLrBiASkw',
  API_BASE: process.env.PISTACHIO_API_BASE || 'https://api.waifulive.ai',
  WS_BASE:  process.env.PISTACHIO_WS_BASE  || 'wss://api.waifulive.ai/ws',
};

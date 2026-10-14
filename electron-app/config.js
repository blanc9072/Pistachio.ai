// Public Supabase config. The anon key is intentionally public — it is safe to
// ship in a desktop app. Security comes from server-side RLS policies.
// User session tokens are never stored here; they live encrypted in safeStorage.
//
// API_BASE/WS_BASE default to the local backend (main.py on this machine) —
// fully local dev keeps working with no env vars set. Phase 2 cloud deploy
// points the app at the hosted box instead by launching it with
// PISTACHIO_API_BASE=https://<DOMAIN> and PISTACHIO_WS_BASE=wss://<DOMAIN>/ws
// set in the environment (nginx terminates TLS and proxies /ws to the voice
// relay — see /etc/nginx/sites-available/default on the box).
module.exports = {
  SUPABASE_URL:      'https://xrohxpmxnrhiqfpqtfff.supabase.co',
  SUPABASE_ANON_KEY: 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Inhyb2h4cG14bnJoaXFmcHF0ZmZmIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODA0NDc1ODcsImV4cCI6MjA5NjAyMzU4N30.CF30KDhe3oPNEbH5jw_ySX-WPa9qPk9RDOqLrBiASkw',
  API_BASE: process.env.PISTACHIO_API_BASE || 'http://localhost:8000',
  WS_BASE:  process.env.PISTACHIO_WS_BASE  || 'ws://localhost:8765',
};

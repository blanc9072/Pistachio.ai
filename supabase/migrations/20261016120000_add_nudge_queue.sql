-- Phase 3 (NG1) — calendar-aware accountability nudges. One row per
-- just-ended calendar event judged worth a check-in; (user_id, event_uid)
-- is the primary key so a re-scan of the same event is a no-op (idempotent
-- enqueue, never a duplicate nudge for the same event).
create table if not exists nudge_queue (
  user_id        uuid not null references profiles(id) on delete cascade,
  event_uid      text not null,
  event_calendar text not null,
  event_title    text not null,
  event_start    timestamptz not null,
  event_end      timestamptz not null,
  question       text not null,
  status         text not null default 'pending',  -- 'pending' | 'delivered' | 'answered'
  response       text,
  created_at     timestamptz not null default now(),
  delivered_at   timestamptz,
  answered_at    timestamptz,
  primary key (user_id, event_uid)
);

alter table nudge_queue enable row level security;

-- Owner-only, same shape as scheduling_preferences' policies.
create policy "users can read own nudges"
  on nudge_queue for select to authenticated using (auth.uid() = user_id);

create policy "users can insert own nudges"
  on nudge_queue for insert to authenticated with check (auth.uid() = user_id);

create policy "users can update own nudges"
  on nudge_queue for update to authenticated
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id);

create policy "users can delete own nudges"
  on nudge_queue for delete to authenticated using (auth.uid() = user_id);

-- Default-deny, matching the calendar permission stance (calendar_enabled
-- etc.) — the feature is off until explicitly turned on in Settings.
alter table profiles add column if not exists accountability_nudges_enabled boolean not null default false;

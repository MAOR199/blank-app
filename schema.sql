-- Run once in the Supabase SQL Editor. Table access is server-side only.
create table if not exists public.participants (
  id uuid primary key,
  group_id text not null check (group_id in ('A','B')),
  resume_hash text not null unique,
  consent_version text not null,
  consent_at timestamptz not null default now(),
  started_at timestamptz not null default now(),
  completed_at timestamptz,
  withdrawn_at timestamptz
);
create table if not exists public.events (
  id uuid primary key,
  participant_id uuid not null references public.participants(id) on delete cascade,
  group_id text not null check (group_id in ('A','B')),
  event_type text not null,
  detail jsonb not null default '{}'::jsonb,
  occurred_at timestamptz not null default now()
);
create table if not exists public.messages (
  id uuid primary key,
  participant_id uuid not null references public.participants(id) on delete cascade,
  role text not null check (role in ('user','assistant')),
  content text not null,
  stage text,
  model text,
  created_at timestamptz not null default now()
);
create table if not exists public.answers (
  id uuid primary key,
  participant_id uuid not null references public.participants(id) on delete cascade,
  phase text not null check (phase in ('pre','post')),
  question_id text not null,
  value integer not null check (value between 1 and 5),
  created_at timestamptz not null default now(),
  unique(participant_id, phase, question_id)
);
create table if not exists public.submissions (
  id uuid primary key,
  participant_id uuid not null unique references public.participants(id) on delete cascade,
  content text not null,
  submitted_at timestamptz not null default now()
);
create index if not exists ix_events_participant on public.events(participant_id, occurred_at);
create index if not exists ix_messages_participant on public.messages(participant_id, created_at);
create index if not exists ix_answers_participant on public.answers(participant_id, phase);
-- No direct access for anon/authenticated database clients.
alter table public.participants enable row level security;
alter table public.events enable row level security;
alter table public.messages enable row level security;
alter table public.answers enable row level security;
alter table public.submissions enable row level security;
revoke all on public.participants, public.events, public.messages, public.answers, public.submissions from anon, authenticated;
-- service_role key used only on the trusted Streamlit server; never expose it in GitHub or UI.

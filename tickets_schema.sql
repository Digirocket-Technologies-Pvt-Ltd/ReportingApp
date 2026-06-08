-- ============================================================
--  Helpdesk / Ticketing module schema for Supabase
--  (run once in: Supabase -> SQL Editor, after supabase_schema.sql)
--
--  monday.com-style internal work board:
--    client raises a ticket -> triage reviews/approves -> dispatch
--    assigns to a team employee -> employee resolves -> closed.
--  Every stage is stamped (who + when) to power the Amazon-style
--  status roadmap.
-- ============================================================

-- 1) Staff ---------------------------------------------------------
--    Internal team members. Google login looks the user up here by
--    email and grants their role + team. Replaces the hardcoded
--    PMO_ADMINS whitelist over time (both are honoured during rollout).
create table if not exists staff (
    id          uuid primary key default gen_random_uuid(),
    email       text unique not null,
    name        text,
    role        text not null default 'employee',  -- supervisor | triage | dispatcher | employee
    team        text,                               -- seo | content | performance | developer | sales | physio | null
    active      boolean not null default true,
    created_at  timestamptz not null default now()
);
create index if not exists idx_staff_team on staff(team);
create index if not exists idx_staff_role on staff(role);

-- Seed the two known supervisors. ON CONFLICT keeps re-runs idempotent.
insert into staff (email, name, role, team) values
    ('shubhranshu.srivastava@digirockettechnologies.com', 'Shubhranshu Srivastava', 'supervisor', null),
    ('sunny.kumar@digirockettechnologies.com',           'Sunny Kumar',            'supervisor', null)
on conflict (email) do nothing;

-- 2) Tickets -------------------------------------------------------
--    Human-friendly sequential number (#1001, #1002, ...) so staff and
--    clients can reference a ticket by a short id.
create sequence if not exists ticket_number_seq start 1001;

create table if not exists tickets (
    id            uuid primary key default gen_random_uuid(),
    number        bigint not null default nextval('ticket_number_seq'),
    client_id     uuid references clients(id) on delete set null,
    raised_by     text,                               -- email of whoever raised it (client or staff)
    title         text not null,
    description   text,
    team          text,                               -- target team / category
    priority      text not null default 'medium',     -- low | medium | high | urgent
    status        text not null default 'raised',     -- raised | reviewed | assigned | in_progress | resolved | closed
    approved_by    text,  approved_at    timestamptz,  -- triage stage
    dispatched_by  text,  dispatched_at  timestamptz,  -- dispatch stage
    assigned_to    text,  assigned_at    timestamptz,  -- employee email + when
    resolved_by    text,  resolved_at    timestamptz,  -- employee resolution
    closed_by      text,  closed_at      timestamptz,
    review_note   text,                                -- why approved / rejected
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now()
);
create index if not exists idx_tickets_status   on tickets(status);
create index if not exists idx_tickets_team     on tickets(team);
create index if not exists idx_tickets_assigned on tickets(assigned_to);
create index if not exists idx_tickets_client   on tickets(client_id);
create unique index if not exists idx_tickets_number on tickets(number);

-- 3) Ticket thread (chat) -- mirrors query_messages -----------------
create table if not exists ticket_messages (
    id            uuid primary key default gen_random_uuid(),
    ticket_id     uuid references tickets(id) on delete cascade,
    sender_email  text,
    sender_role   text,                               -- client | supervisor | triage | dispatcher | employee
    body          text,
    attachments   jsonb not null default '[]'::jsonb, -- [{name,url,size,type}, ...]
    is_solution   boolean not null default false,     -- the employee's "Re: #N — solution" reply
    created_at    timestamptz not null default now()
);
create index if not exists idx_ticket_messages_ticket on ticket_messages(ticket_id, created_at);

-- 4) Ticket events (the roadmap / audit trail) ----------------------
--    One row per stage transition, so the Amazon-style tracker can list
--    exactly who advanced the ticket and when.
create table if not exists ticket_events (
    id            uuid primary key default gen_random_uuid(),
    ticket_id     uuid references tickets(id) on delete cascade,
    stage         text,                               -- raised | reviewed | assigned | in_progress | resolved | closed | reopened
    actor_email   text,
    note          text,
    created_at    timestamptz not null default now()
);
create index if not exists idx_ticket_events_ticket on ticket_events(ticket_id, created_at);

-- 5) Security: same posture as the rest of the app -- only the backend
--    service_role key (which bypasses RLS) may touch these tables.
alter table staff           enable row level security;
alter table tickets         enable row level security;
alter table ticket_messages enable row level security;
alter table ticket_events   enable row level security;

-- Ticket attachments reuse the existing 'query-attachments' bucket, so
-- no new Storage bucket is required.

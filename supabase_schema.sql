-- ============================================================
--  PMO Portal schema for Supabase (run once in: Supabase -> SQL Editor)
-- ============================================================

-- 1) Clients table -------------------------------------------------
create table if not exists clients (
    id                  uuid primary key default gen_random_uuid(),
    name                text not null,
    email               text,
    ga4_property_id     text,          -- GA4 (GEO-4) property id
    gsc_property_id     text,          -- Search Console property
    nature_of_business  text,
    billing_cycle_day   int,           -- 1..31
    start_date          date,          -- onboarding date
    status              text not null default 'active',   -- active | churned
    notes               text,
    created_at          timestamptz not null default now()
);

-- 2) Report logs table (one row per email sent) -------------------
create table if not exists report_logs (
    id            uuid primary key default gen_random_uuid(),
    client_id     uuid references clients(id) on delete cascade,
    report_period text,               -- e.g. "May 2026" / "Month 7"
    sent_to       text,               -- email it was sent to
    subject       text,
    status        text default 'sent',
    sent_at       timestamptz not null default now()
);

create index if not exists idx_report_logs_client on report_logs(client_id);

-- 3) Activity feed (powers the notification bell) -----------------
create table if not exists activities (
    id          uuid primary key default gen_random_uuid(),
    type        text,               -- report_generated | report_emailed | client_added | ...
    message     text not null,      -- human-readable line shown in the bell
    link        text,               -- page to open when the notification is clicked
    user_email  text,               -- who did it
    created_at  timestamptz not null default now()
);

create index if not exists idx_activities_created on activities(created_at desc);

-- 4) Client queries (raised by clients from the client portal) ----
create table if not exists report_queries (
    id            uuid primary key default gen_random_uuid(),
    client_id     uuid references clients(id) on delete cascade,
    report_log_id uuid references report_logs(id) on delete set null,  -- which report (optional)
    report_period text,               -- snapshot for display, e.g. "May 2026"
    subject       text,               -- short title (optional)
    message       text not null,      -- the client's question / doubt
    status        text not null default 'open',   -- open | answered | resolved
    response      text,               -- PMO team's answer (shown on the portal)
    responded_by  text,               -- admin email who answered
    responded_at  timestamptz,
    created_at    timestamptz not null default now()
);

create index if not exists idx_report_queries_client on report_queries(client_id);
create index if not exists idx_report_queries_status on report_queries(status);

-- 5) Security: lock the tables so only our backend (service_role key)
--    can touch them. The Flask app uses the service_role key, which
--    bypasses RLS; the public anon key gets no access.
alter table clients        enable row level security;
alter table report_logs    enable row level security;
alter table activities     enable row level security;
alter table report_queries enable row level security;

-- ============================================================
--  Group chat for Employee Query (WhatsApp/Teams-style groups)
--  Run once in Supabase -> SQL Editor.
-- ============================================================
create table if not exists staff_groups (
    id          uuid primary key default gen_random_uuid(),
    name        text not null,
    created_by  text,
    created_at  timestamptz not null default now()
);

create table if not exists staff_group_members (
    id          uuid primary key default gen_random_uuid(),
    group_id    uuid references staff_groups(id) on delete cascade,
    email       text not null,
    created_at  timestamptz not null default now(),
    unique (group_id, email)
);
create index if not exists idx_sgm_group on staff_group_members(group_id);
create index if not exists idx_sgm_email on staff_group_members(email);

create table if not exists staff_group_messages (
    id            uuid primary key default gen_random_uuid(),
    group_id      uuid references staff_groups(id) on delete cascade,
    sender_email  text,
    body          text,
    attachments   jsonb not null default '[]'::jsonb,
    created_at    timestamptz not null default now()
);
create index if not exists idx_sgmsg_group on staff_group_messages(group_id, created_at);

-- Per-user pin: each membership can be pinned so the group floats to the top
-- of THAT person's group list (WhatsApp-style pin).
alter table staff_group_members add column if not exists pinned boolean not null default false;

alter table staff_groups         enable row level security;
alter table staff_group_members  enable row level security;
alter table staff_group_messages enable row level security;

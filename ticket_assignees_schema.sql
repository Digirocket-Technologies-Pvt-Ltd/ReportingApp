-- ============================================================
--  Multi-assignee tickets + @mention tagging
--  Run once in Supabase -> SQL Editor.
-- ============================================================

-- One row per (ticket, person). A ticket can now have several assignees.
-- tickets.assigned_to is kept as the "primary" assignee for display.
create table if not exists ticket_assignees (
    id          uuid primary key default gen_random_uuid(),
    ticket_id   uuid references tickets(id) on delete cascade,
    email       text not null,
    added_by    text,
    created_at  timestamptz not null default now(),
    unique (ticket_id, email)
);
create index if not exists idx_ticket_assignees_ticket on ticket_assignees(ticket_id);
create index if not exists idx_ticket_assignees_email   on ticket_assignees(email);
alter table ticket_assignees enable row level security;

-- @mentions on a thread message: list of tagged emails (also added as assignees).
alter table ticket_messages
    add column if not exists mentions jsonb not null default '[]'::jsonb;

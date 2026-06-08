-- ============================================================
--  Staff-to-staff internal chat ("Employee Query")
--  Run once in Supabase -> SQL Editor.
--  A conversation between two people = all rows where
--  {sender, recipient} matches that pair (either direction).
-- ============================================================
create table if not exists staff_messages (
    id              uuid primary key default gen_random_uuid(),
    sender_email    text not null,
    recipient_email text not null,
    body            text,
    attachments     jsonb not null default '[]'::jsonb,  -- [{name,url,size,type}, ...]
    read_at         timestamptz,                          -- NULL = unread by recipient
    created_at      timestamptz not null default now()
);
create index if not exists idx_staff_messages_pair
    on staff_messages(sender_email, recipient_email, created_at);
create index if not exists idx_staff_messages_inbox
    on staff_messages(recipient_email, read_at);

-- Reply-to (WhatsApp-style): the earlier message this one is replying to.
alter table staff_messages
    add column if not exists reply_to_id uuid references staff_messages(id) on delete set null;

-- Same security posture as the rest of the app (backend service_role only).
alter table staff_messages enable row level security;

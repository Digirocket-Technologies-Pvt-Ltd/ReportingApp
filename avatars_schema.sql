-- ============================================================
--  Profile photos (DP) for staff + group photos
--  Run once in Supabase -> SQL Editor.
-- ============================================================
alter table staff        add column if not exists avatar_url text;
alter table staff_groups add column if not exists avatar_url text;

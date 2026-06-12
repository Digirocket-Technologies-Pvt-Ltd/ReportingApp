-- ============================================================
--  DataForSEO cache for clients (domain SEO overview)
--  Run once in Supabase -> SQL Editor.
--  The client's website domain comes from the existing
--  `target_seo_website` field (no new column needed for that).
-- ============================================================
alter table clients add column if not exists seo_data       jsonb;
alter table clients add column if not exists seo_fetched_at  timestamptz;

-- Domain-keyed cache so the report's "Add SEO" checkbox doesn't re-charge
-- DataForSEO every time the same domain's report is generated (7-day fresh).
create table if not exists seo_cache (
    domain      text primary key,
    data        jsonb,
    fetched_at  timestamptz not null default now()
);
alter table seo_cache enable row level security;

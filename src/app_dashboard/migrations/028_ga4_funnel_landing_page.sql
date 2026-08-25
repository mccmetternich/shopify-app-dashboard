-- Add landing_page_type column to ga4_funnel and update the primary key
-- to include it (one row per date × utm_source × utm_medium × landing_page_type).

alter table ga4_funnel
  add column if not exists landing_page_type text not null default 'other'
    check (landing_page_type in ('pdp','listicle','lander','direct_checkout','other'));

-- Drop the old 3-column PK and replace with 4-column PK.
-- The auto-generated name for the inline PK in migration 018 is ga4_funnel_pkey.
alter table ga4_funnel drop constraint if exists ga4_funnel_pkey;

alter table ga4_funnel
  add constraint ga4_funnel_pkey
  primary key (date, utm_source, utm_medium, landing_page_type);

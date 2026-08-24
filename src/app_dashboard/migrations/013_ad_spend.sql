-- Migration 013: ad_spend
-- Daily ad spend rolled up per campaign (Meta / Google / TikTok).
-- Composite PK enforces exactly one row per campaign per day.
-- impressions / clicks may be null when the platform does not report them.

create table if not exists ad_spend (
    date            date        not null,
    campaign_id     text        not null,
    campaign_name   text        not null,
    platform        text        not null,   -- 'meta' | 'google' | 'tiktok'
    spend           numeric(10,2) not null check (spend >= 0),
    impressions     bigint      null,
    clicks          bigint      null,
    created_in_db   timestamptz not null default now(),

    primary key (date, campaign_id)
);

create index if not exists ad_spend_date_idx      on ad_spend (date);
create index if not exists ad_spend_platform_idx  on ad_spend (platform);

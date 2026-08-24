create table if not exists meta_ad_stats (
    date            date not null,
    campaign_id     text not null,
    campaign_name   text not null,
    adset_id        text not null default '',
    adset_name      text not null default '',
    ad_id           text not null default '',
    ad_name         text not null default '',
    spend           numeric(10,2) not null default 0,
    impressions     bigint not null default 0,
    clicks          bigint not null default 0,
    purchases       bigint not null default 0,
    purchase_value  numeric(10,2) not null default 0,
    thumbnail_url   text,
    ads_manager_url text,
    primary key (date, campaign_id, adset_id, ad_id)
);
create index if not exists meta_ad_stats_date on meta_ad_stats(date);
create index if not exists meta_ad_stats_campaign on meta_ad_stats(date, campaign_id);

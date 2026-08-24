-- GA4 funnel data: sessions → ATC → begin_checkout → purchase
-- One row per (date, utm_source, utm_medium) combination; empty string = all traffic / not set
create table if not exists ga4_funnel (
    date            date not null,
    utm_source      text not null default '',
    utm_medium      text not null default '',
    sessions        bigint not null default 0,
    add_to_carts    bigint not null default 0,
    begin_checkouts bigint not null default 0,
    purchases       bigint not null default 0,
    primary key (date, utm_source, utm_medium)
);
create index if not exists ga4_funnel_date on ga4_funnel(date);

-- Omnisend email/SMS metrics per flow/campaign per day
create table if not exists omnisend_sends (
    date              date not null,
    flow_name         text not null default '',
    campaign_name     text not null default '',
    channel           text not null default 'email',
    sends             bigint not null default 0,
    opens             bigint not null default 0,
    clicks            bigint not null default 0,
    attributed_revenue numeric(10,2) not null default 0,
    primary key (date, flow_name, campaign_name, channel)
);
create index if not exists omnisend_sends_date on omnisend_sends(date);

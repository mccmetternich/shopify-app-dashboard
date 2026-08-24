-- Migration 014: subscription_revenue
-- Monthly subscription billing records (Recharge / Shopify Subscriptions).
-- monthly_amount must be > 0 for active records (churned_at IS NULL).
-- customer_id references customers so cohort retention queries can join cleanly.

create table if not exists subscription_revenue (
    id              text        primary key,
    customer_id     text        not null references customers (id) on delete restrict,
    monthly_amount  numeric(10,2) not null check (monthly_amount > 0),
    converted_at    timestamptz not null,
    churned_at      timestamptz null,       -- NULL = still active
    created_in_db   timestamptz not null default now()
);

create index if not exists sub_rev_customer_id_idx  on subscription_revenue (customer_id);
create index if not exists sub_rev_converted_at_idx on subscription_revenue (converted_at);
create index if not exists sub_rev_churned_at_idx   on subscription_revenue (churned_at)
    where churned_at is not null;

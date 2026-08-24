-- Shopify abandoned checkouts
create table if not exists checkouts (
    id            text primary key,
    customer_id   text references customers(id) on delete set null,
    created_at    timestamptz not null,
    abandoned_at  timestamptz,
    recovered_at  timestamptz,
    total         numeric(10,2) not null default 0,
    email_hash    char(64),
    created_in_db timestamptz not null default now()
);
create index if not exists checkouts_created_at on checkouts(created_at);
create index if not exists checkouts_abandoned_at on checkouts(abandoned_at) where abandoned_at is not null;

-- Migration 011: Densologie orders
-- Stores every order from the DTC pipeline (Shopify webhook or CSV import).
-- line_items: JSONB array of {sku, title, quantity, unit_price}
-- source_utm:  JSONB object of utm_* keys; NULL when unknown (never empty '{}')

create table if not exists orders (
    id              text        primary key,
    customer_id     text        not null,
    created_at      timestamptz not null,
    total           numeric(10,2) not null check (total >= -99999),
    refunded        numeric(10,2) not null default 0 check (refunded >= 0),
    currency        char(3)     not null default 'USD',
    is_new_customer boolean     not null default false,
    line_items      jsonb       not null default '[]',
    source_utm      jsonb       null,          -- NULL = unknown; never {}
    created_in_db   timestamptz not null default now()
);

create index if not exists orders_customer_id_idx on orders (customer_id);
create index if not exists orders_created_at_idx  on orders (created_at);

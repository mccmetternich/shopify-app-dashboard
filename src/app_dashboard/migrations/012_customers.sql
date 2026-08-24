-- Migration 012: Densologie customers
-- One row per unique buyer. email_hash is SHA-256(lower(email)) — no raw PII stored.
-- first_order_at is denormalized for cheap cohort queries; kept in sync by pipeline.

create table if not exists customers (
    id              text        primary key,   -- Shopify customer GID
    email_hash      char(64)    not null unique,
    first_order_at  timestamptz not null,
    country         char(2)     null,
    created_in_db   timestamptz not null default now()
);

create index if not exists customers_first_order_at_idx on customers (first_order_at);
create index if not exists customers_country_idx        on customers (country);

-- orders.customer_id must point to a known customer
alter table orders
    add constraint orders_customer_id_fk
    foreign key (customer_id) references customers (id)
    on delete restrict;

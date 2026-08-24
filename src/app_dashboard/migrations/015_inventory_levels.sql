-- Migration 015: inventory_levels + sync_state meta column
--
-- inventory_levels: one row per SKU, updated by the Shopify Admin API poller
-- (Phase C). Seeded with the serum SKU for the days-of-cover tile.
--
-- sync_state.meta: JSONB bag for source-specific cursor data (e.g.
-- shopify_cursor, last_order_created_at). Added here rather than a new table
-- to keep sync state in one place. The existing cursor column is preserved for
-- the existing stale-check code.

create table if not exists inventory_levels (
    sku         text        primary key,
    units_on_hand integer   not null check (units_on_hand >= 0),
    updated_at  timestamptz not null default now()
);

-- Seed the serum SKU with a realistic starting inventory.
insert into inventory_levels (sku, units_on_hand, updated_at)
values ('HAIR-SERUM-50ML', 800, now())
on conflict (sku) do nothing;

-- Extend sync_state with a JSONB bag for source-specific metadata.
-- Existing rows get an empty object; new rows can use it immediately.
alter table sync_state
    add column if not exists meta jsonb not null default '{}';

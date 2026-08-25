create table if not exists upsell_events (
  id         serial primary key,
  order_id   text references orders(id) on delete cascade,
  upsell_type text not null
              check (upsell_type in ('priority_shipping','upsell_t1','upsell_t2',
                                     'upsell_t3','aftersell')),
  accepted   boolean not null default false,
  amount     numeric(10,2),
  created_at timestamptz default now()
);
create index if not exists ix_upsell_events_order on upsell_events(order_id);

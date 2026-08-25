alter table orders
  add column if not exists is_subscription_order boolean not null default false;

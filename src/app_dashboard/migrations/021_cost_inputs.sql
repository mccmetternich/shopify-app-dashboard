create table if not exists cost_inputs (
  sku           text primary key,
  label         text not null,
  cogs_per_unit numeric(10,2) not null default 0,
  updated_at    timestamptz default now()
);

create table if not exists cost_settings (
  key       text primary key,
  value     numeric(10,4) not null,
  label     text not null,
  updated_at timestamptz default now()
);

insert into cost_inputs (sku, label, cogs_per_unit) values
  ('HAIR-SERUM-50ML', 'Hair Serum 50ml', 3.50),
  ('DSL-CAPS-90',     'Capsules 90-day', 5.00),
  ('DSL-BUNDLE',      'Serum + Capsules Bundle', 8.50),
  ('DSL-3MO-SUPPLY',  '3-Month Supply', 10.50)
on conflict (sku) do nothing;

insert into cost_settings (key, value, label) values
  ('shipping_cost_per_order', 6.50,  'Flat shipping cost per order'),
  ('payment_fee_pct',         0.029, 'Payment processor fee (decimal, e.g. 0.029 = 2.9%)'),
  ('return_processing_cost',  5.00,  'Flat cost per returned order')
on conflict (key) do nothing;

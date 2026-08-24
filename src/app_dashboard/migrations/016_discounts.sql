-- Add discount tracking to orders
alter table orders
    add column if not exists discount_code text,
    add column if not exists discount_amount numeric(10,2) not null default 0;

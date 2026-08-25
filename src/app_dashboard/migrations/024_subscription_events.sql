create table if not exists subscription_events (
  id              serial primary key,
  subscription_id text references subscription_revenue(id) on delete cascade,
  customer_id     text references customers(id),
  event_type      text not null
                  check (event_type in ('new','churn','expansion','contraction',
                                        'skip','dunning_start','dunning_resolved',
                                        'mrr_recognized')),
  event_date      date not null,
  mrr_delta       numeric(10,2),
  old_monthly_amount numeric(10,2),
  new_monthly_amount numeric(10,2),
  reason          text,
  created_at      timestamptz default now()
);
create index if not exists ix_sub_events_date on subscription_events(event_date);
create index if not exists ix_sub_events_type on subscription_events(event_type);

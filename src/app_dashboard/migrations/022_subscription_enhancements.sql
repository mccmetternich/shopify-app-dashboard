alter table subscription_revenue
  add column if not exists sub_type      text not null default 'monthly'
                                         check (sub_type in ('monthly','3mo','6mo')),
  add column if not exists cash_collected numeric(10,2) not null default 0,
  add column if not exists churn_type    text check (churn_type in ('voluntary','involuntary')),
  add column if not exists churn_reason  text,
  add column if not exists dunning_started_at timestamptz;

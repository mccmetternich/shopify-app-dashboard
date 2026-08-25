alter table customers
  add column if not exists acquisition_offer text
    check (acquisition_offer in ('full-price','coupon-only','steep-intro-discount','reactivation'));

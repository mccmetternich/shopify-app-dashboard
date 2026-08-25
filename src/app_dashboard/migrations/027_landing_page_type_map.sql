create table if not exists landing_page_type_map (
  url_prefix  text primary key,
  page_type   text not null
              check (page_type in ('pdp','listicle','lander','direct_checkout','other'))
);

insert into landing_page_type_map (url_prefix, page_type) values
  ('/products/', 'pdp'),
  ('/blogs/',    'listicle'),
  ('/pages/',    'lander'),
  ('/',          'lander'),
  ('/checkout',  'direct_checkout')
on conflict (url_prefix) do nothing;

-- biotech_market: point-in-time snapshot of the Nasdaq biotech universe.
--
-- One row per ticker, overwritten every trading day by "Daily Markets/scraper.py".
-- Not a history table: after each run, rows not seen that day (delisted, or moved
-- out of the three target industries) are deleted, so the table always equals the
-- latest screener pull. Month-end CSV snapshots live in History/ in this repo.
--
-- Run once in the opportunity Supabase SQL editor.

create table if not exists public.biotech_market (
  symbol      text primary key,
  name        text,
  last_sale   numeric,
  net_change  numeric,
  pct_change  numeric,          -- percent, e.g. -2.719 means -2.719%
  market_cap  bigint,           -- full USD (the CSV keeps $M)
  country     text,
  ipo_year    integer,
  volume      bigint,
  sector      text,
  industry    text,
  as_of       date not null,    -- trading date (America/New_York) of the pull
  updated_at  timestamptz not null default now()
);

create index if not exists biotech_market_market_cap_idx on public.biotech_market (market_cap desc);
create index if not exists biotech_market_as_of_idx on public.biotech_market (as_of);

-- Reads are public (watchlist-server uses the anon key); writes need the
-- service-role key, which bypasses RLS.
alter table public.biotech_market enable row level security;

drop policy if exists "biotech_market read" on public.biotech_market;
create policy "biotech_market read" on public.biotech_market
  for select using (true);

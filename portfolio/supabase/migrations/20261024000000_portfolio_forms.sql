-- mish4mish.com: the two forms. The site holds only the publishable key, and these policies let
-- that key INSERT and nothing else: it can't read, change or delete a row. Read messages in the
-- Supabase dashboard (Table Editor), which uses your own login.

-- "Say hello" on /contact
create table if not exists public.contact_messages (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  topic text check (topic in ('A role', 'A project', 'A bug on this site', 'Just saying hi')),
  name text not null check (char_length(name) between 1 and 200),
  email text not null check (char_length(email) between 3 and 254 and email ~ '^[^\s@]+@[^\s@]+\.[^\s@]+$'),
  message text not null check (char_length(message) between 1 and 5000),
  replied_at timestamptz
);

comment on table public.contact_messages is 'Messages from the contact form on mish4mish.com. Insert-only for the site.';

-- "Want to try Moodify?" on /privacy. One row per Spotify email.
create table if not exists public.moodify_access_requests (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  name text check (char_length(name) <= 200),
  email text not null check (char_length(email) between 3 and 254 and email ~ '^[^\s@]+@[^\s@]+\.[^\s@]+$'),
  note text check (char_length(note) <= 500),
  -- pending: new · added: added in the Spotify dashboard · waitlist: no spot yet · declined
  status text not null default 'pending' check (status in ('pending', 'added', 'waitlist', 'declined'))
);

create unique index if not exists moodify_access_requests_email_key
  on public.moodify_access_requests (lower(email));

comment on table public.moodify_access_requests is 'Moodify access requests from mish4mish.com/privacy. Insert-only for the site.';

-- Lock both tables down, then open exactly one door: anonymous INSERT.
alter table public.contact_messages enable row level security;
alter table public.moodify_access_requests enable row level security;

revoke all on table public.contact_messages from anon, authenticated;
revoke all on table public.moodify_access_requests from anon, authenticated;
grant insert (topic, name, email, message) on table public.contact_messages to anon;
grant insert (name, email, note) on table public.moodify_access_requests to anon;

drop policy if exists "Anyone can send a message" on public.contact_messages;
create policy "Anyone can send a message"
  on public.contact_messages for insert to anon
  with check (replied_at is null);

drop policy if exists "Anyone can ask for Moodify access" on public.moodify_access_requests;
create policy "Anyone can ask for Moodify access"
  on public.moodify_access_requests for insert to anon
  with check (status = 'pending');

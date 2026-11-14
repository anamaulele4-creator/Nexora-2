-- CONNECTOR AI · 0004 · autenticação própria (AUTH_PROVIDER=local)
-- Esquema separado, inacessível ao papel authenticated. Com AUTH_PROVIDER=supabase
-- estas tabelas ficam vazias (o Supabase Auth guarda credenciais no esquema auth).

create schema if not exists local_auth;
revoke all on schema local_auth from public;

create table local_auth.credentials (
  user_id              uuid primary key references public.users(id) on delete cascade,
  password_hash        text not null check (password_hash like '$argon2id$%'),
  password_updated_at  timestamptz not null default now(),
  failed_attempts      integer not null default 0,
  locked_until         timestamptz
);

create table local_auth.sessions (
  id                  uuid primary key default gen_random_uuid(),
  user_id             uuid not null references public.users(id) on delete cascade,
  refresh_token_hash  text not null unique check (refresh_token_hash ~ '^[0-9a-f]{64}$'),
  created_at          timestamptz not null default now(),
  last_used_at        timestamptz not null default now(),
  expires_at          timestamptz not null,
  revoked_at          timestamptz,
  revoked_reason      text,
  ip                  inet,
  user_agent          text
);
create index on local_auth.sessions (user_id) where revoked_at is null;

create table local_auth.one_time_tokens (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null references public.users(id) on delete cascade,
  purpose     text not null check (purpose in ('email_verification', 'password_reset')),
  token_hash  text not null unique check (token_hash ~ '^[0-9a-f]{64}$'),
  expires_at  timestamptz not null,
  used_at     timestamptz,
  created_at  timestamptz not null default now()
);
create index on local_auth.one_time_tokens (user_id, purpose) where used_at is null;

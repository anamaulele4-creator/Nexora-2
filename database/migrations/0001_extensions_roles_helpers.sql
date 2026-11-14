-- CONNECTOR AI · 0001 · extensões, papéis e funções auxiliares
-- Compatível com PostgreSQL local e com Supabase.

create extension if not exists pgcrypto;
create extension if not exists citext;

-- Papel usado para TODAS as consultas em nome de um utilizador.
-- No Supabase já existe com este nome; localmente é criado aqui.
do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'authenticated') then
    create role authenticated nologin noinherit;
  end if;
end $$;

-- O papel que corre as migrações/backend tem de poder fazer SET ROLE authenticated.
do $$
begin
  execute format('grant authenticated to %I', current_user);
exception when others then
  raise notice 'grant authenticated ignorado: %', sqlerrm;
end $$;

create schema if not exists app;
grant usage on schema app to authenticated;
grant usage on schema public to authenticated;

-- Utilizador atual da sessão SQL. Equivalente a auth.uid() do Supabase:
-- lê o "sub" das claims JWT definidas por transação (request.jwt.claims).
create or replace function app.current_user_id() returns uuid
language sql stable
as $$
  select nullif(
    coalesce(
      nullif(current_setting('request.jwt.claim.sub', true), ''),
      (nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'sub')
    ), ''
  )::uuid
$$;
grant execute on function app.current_user_id() to authenticated;

create or replace function app.touch_updated_at() returns trigger
language plpgsql as $$
begin
  new.updated_at := now();
  return new;
end $$;

-- Tabelas só de acrescento (auditoria): bloqueia UPDATE/DELETE.
-- Única exceção: um processo de serviço que ative explicitamente
-- app.allow_log_purge = 'on' na transação (ex.: eliminação de conta / RGPD).
create or replace function app.forbid_mutation() returns trigger
language plpgsql as $$
begin
  if coalesce(current_setting('app.allow_log_purge', true), '') = 'on' then
    if tg_op = 'DELETE' then return old; end if;
    return new;
  end if;
  raise exception 'a tabela % é só de acrescento (append-only)', tg_table_name
    using errcode = '42501';
end $$;

-- Registo das migrações aplicadas.
create table if not exists public.schema_migrations (
  version     text primary key,
  checksum    text not null,
  applied_at  timestamptz not null default now()
);

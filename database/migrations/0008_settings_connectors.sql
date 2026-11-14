-- CONNECTOR AI · 0008 · definições do utilizador e conectores (GitHub, Gmail, Google Drive, Shopify)
--
-- * user_ai_preferences: fornecedor/modelo de IA preferido por utilizador (motor local por
--   defeito, Groq como recurso). Isolada por RLS como em 0003.
-- * ai_policy: política global (uma linha) definida pelo admin. Por defeito os fornecedores
--   pagos (OpenAI) NÃO podem ser escolhidos pelos utilizadores. Tabela de sistema: sem acesso
--   para o papel authenticated (como ai_providers).
-- * connected_apps: no máximo uma ligação por utilizador e conector (reconectar atualiza a linha).
--   Os tokens ficam em encrypted_credentials (AES-256-GCM, já sem acesso para authenticated).
-- * connector_definitions: o Shopify liga-se com domínio da loja + token de Admin API
--   (auth_type api_key). O estado continua 'planned':
--   ligar a conta já funciona, mas as ferramentas do agente chegam numa etapa seguinte.

-- user_ai_preferences ----------------------------------------------------------------------------
create table public.user_ai_preferences (
  user_id            uuid primary key references public.users(id) on delete cascade,
  provider           text not null default 'ollama'
                       check (provider in ('auto', 'ollama', 'gemini', 'groq', 'openrouter', 'openai')),
  model              text check (model is null or model ~ '^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$'),
  fallback_provider  text default 'groq'
                       check (fallback_provider is null
                              or fallback_provider in ('ollama', 'gemini', 'groq', 'openrouter', 'openai')),
  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now(),
  check (fallback_provider is null or fallback_provider <> provider)
);
create trigger trg_user_ai_preferences_updated_at before update on public.user_ai_preferences
  for each row execute function app.touch_updated_at();

alter table public.user_ai_preferences enable row level security;
revoke all on public.user_ai_preferences from public;
grant select, insert, update, delete on public.user_ai_preferences to authenticated;
create policy user_ai_preferences_own on public.user_ai_preferences for all to authenticated
  using (user_id = app.current_user_id())
  with check (user_id = app.current_user_id());

-- ai_policy (sistema) -----------------------------------------------------------------------------
create table public.ai_policy (
  id                    smallint primary key default 1 check (id = 1),
  allow_paid_providers  boolean not null default false,
  created_at            timestamptz not null default now(),
  updated_at            timestamptz not null default now(),
  updated_by            uuid references public.users(id) on delete set null
);
create trigger trg_ai_policy_updated_at before update on public.ai_policy
  for each row execute function app.touch_updated_at();
insert into public.ai_policy (id, allow_paid_providers) values (1, false) on conflict (id) do nothing;

alter table public.ai_policy enable row level security;
revoke all on public.ai_policy from public;
-- SEM grant e SEM política para authenticated.

-- connected_apps: uma ligação por utilizador e conector ----------------------------------------------
create unique index if not exists connected_apps_user_definition_key
  on public.connected_apps (user_id, connector_definition_id);
create index if not exists encrypted_credentials_user_idx
  on public.encrypted_credentials (user_id) where revoked_at is null;

-- connector_definitions (as linhas só existem depois do seed; em BD novas o seed já traz isto) -------
update public.connector_definitions
   set auth_type = 'api_key',
       scopes = '{read_products,read_orders}',
       description = 'Products, orders and inventory of a store (shop domain + Admin API token).'
 where key = 'shopify';

-- CONNECTOR AI · 0007 · motor local (Ollama), gestor de modelos, API pública v1
--
-- * local_ai_engine: configuração global (uma linha) do motor local Ollama. O URL base e o
--   token opcional ficam só cifrados (AES-256-GCM com CREDENTIALS_ENCRYPTION_KEY), como as
--   chaves de ai_providers. Tabela de sistema: sem acesso para o papel authenticated.
-- * api_keys: tokens da API pública v1 emitidos pela plataforma. Guarda-se só o hash
--   SHA-256 (o token completo só é mostrado uma vez, ao criar) e um prefixo para identificar.
-- * api_usage_logs: um registo por pedido à API v1 (modelo, tokens, latência, estado).
-- * ai_providers/messages: o fornecedor 'ollama' passa a ser aceite no histórico.

-- messages.provider aceita 'ollama' -----------------------------------------------------------
alter table public.messages drop constraint if exists messages_provider_check;
alter table public.messages add constraint messages_provider_check
  check (provider is null or provider in ('ollama', 'gemini', 'groq', 'openrouter', 'openai'));

-- local_ai_engine -----------------------------------------------------------------------------
create table public.local_ai_engine (
  id                    smallint primary key default 1 check (id = 1),
  base_url_ciphertext   bytea,
  base_url_nonce        bytea,
  base_url_key_id       text,
  base_url_hint         text check (base_url_hint is null or char_length(base_url_hint) <= 120),
  token_ciphertext      bytea,
  token_nonce           bytea,
  token_key_id          text,
  token_last4           text check (token_last4 is null or char_length(token_last4) <= 4),
  enabled               boolean not null default false,
  api_mode              text not null default 'native' check (api_mode in ('native', 'openai')),
  default_model         text check (default_model is null or char_length(default_model) between 1 and 200),
  temperature           real not null default 0.7 check (temperature >= 0 and temperature <= 2),
  max_tokens            integer not null default 1024 check (max_tokens between 16 and 32768),
  context_length        integer not null default 4096 check (context_length between 512 and 131072),
  think                 boolean not null default false,
  ram_limit_gb          real not null default 8 check (ram_limit_gb > 0 and ram_limit_gb <= 1024),
  -- se o motor local falhar: só passa para fornecedores na nuvem se o admin autorizar
  allow_cloud_fallback  boolean not null default false,
  -- e nunca para fornecedores pagos (OpenAI) sem autorização explícita à parte
  allow_paid_fallback   boolean not null default false,
  last_tested_at        timestamptz,
  last_test_ok          boolean,
  last_test_message     text check (last_test_message is null or char_length(last_test_message) <= 500),
  created_at            timestamptz not null default now(),
  updated_at            timestamptz not null default now(),
  updated_by            uuid references public.users(id) on delete set null,
  check ((base_url_ciphertext is null) = (base_url_nonce is null)),
  check ((token_ciphertext is null) = (token_nonce is null)),
  check (not enabled or base_url_ciphertext is not null)
);
create trigger trg_local_ai_engine_updated_at before update on public.local_ai_engine
  for each row execute function app.touch_updated_at();
alter table public.local_ai_engine enable row level security;
revoke all on public.local_ai_engine from public;
-- SEM grant e SEM política para authenticated.

-- api_keys ------------------------------------------------------------------------------------
create table public.api_keys (
  id            uuid primary key default gen_random_uuid(),
  user_id       uuid not null references public.users(id) on delete cascade,  -- admin que emitiu
  name          text not null check (char_length(name) between 1 and 100),
  key_prefix    text not null check (char_length(key_prefix) between 4 and 24),
  key_hash      text not null unique check (key_hash ~ '^[0-9a-f]{64}$'),
  scopes        text[] not null default array['models', 'chat'],
  expires_at    timestamptz,
  last_used_at  timestamptz,
  revoked_at    timestamptz,
  created_at    timestamptz not null default now()
);
create index on public.api_keys (created_at desc);
alter table public.api_keys enable row level security;
revoke all on public.api_keys from public;

-- api_usage_logs ------------------------------------------------------------------------------
create table public.api_usage_logs (
  id             bigint generated always as identity primary key,
  api_key_id     uuid references public.api_keys(id) on delete set null,
  user_id        uuid references public.users(id) on delete set null,
  endpoint       text not null check (char_length(endpoint) <= 100),
  provider       text,
  model          text,
  status_code    integer not null,
  streamed       boolean not null default false,
  input_tokens   integer check (input_tokens is null or input_tokens >= 0),
  output_tokens  integer check (output_tokens is null or output_tokens >= 0),
  latency_ms     integer check (latency_ms is null or latency_ms >= 0),
  created_at     timestamptz not null default now()
);
create index on public.api_usage_logs (api_key_id, created_at desc);
create index on public.api_usage_logs (created_at desc);
alter table public.api_usage_logs enable row level security;
revoke all on public.api_usage_logs from public;

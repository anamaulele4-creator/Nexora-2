-- CONNECTOR AI · 0005 · papel de admin, fornecedores de IA e chat
--
-- * users.is_admin já existe (0002). Aqui promove-se a conta do dono (email por defeito
--   de ADMIN_EMAILS) se já existir e tiver o email confirmado. Depois disso o backend
--   promove no registo/login/verificação qualquer email confirmado listado em ADMIN_EMAILS.
-- * ai_providers: configuração global dos fornecedores de IA (Gemini, OpenAI). A chave fica
--   só cifrada (AES-256-GCM com CREDENTIALS_ENCRYPTION_KEY). Tabela de sistema: sem acesso
--   para o papel authenticated (como encrypted_credentials); só o serviço (rotas de admin).
-- * conversations + messages: chat de cada utilizador, isolado por RLS como em 0003.

update public.users
   set is_admin = true
 where email = 'garenadario3@gmail.com'
   and email_verified_at is not null
   and is_admin = false;

-- ai_providers -------------------------------------------------------------------------
create table public.ai_providers (
  provider          text primary key check (provider in ('gemini', 'openai')),
  api_key_ciphertext bytea,
  api_key_nonce     bytea,
  api_key_key_id    text,
  api_key_last4     text check (api_key_last4 is null or char_length(api_key_last4) <= 4),
  model             text not null check (char_length(model) between 1 and 100),
  enabled           boolean not null default false,
  last_tested_at    timestamptz,
  last_test_ok      boolean,
  last_test_message text check (last_test_message is null or char_length(last_test_message) <= 500),
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now(),
  updated_by        uuid references public.users(id) on delete set null,
  -- chave e nonce andam sempre juntos
  check ((api_key_ciphertext is null) = (api_key_nonce is null)),
  -- só se ativa um fornecedor que tem chave
  check (not enabled or api_key_ciphertext is not null)
);
create trigger trg_ai_providers_updated_at before update on public.ai_providers
  for each row execute function app.touch_updated_at();
alter table public.ai_providers enable row level security;
revoke all on public.ai_providers from public;
-- SEM grant e SEM política para authenticated.

-- conversations -------------------------------------------------------------------------
create table public.conversations (
  id               uuid primary key default gen_random_uuid(),
  user_id          uuid not null references public.users(id) on delete cascade,
  workspace_id     uuid not null,
  title            text check (title is null or char_length(title) <= 200),
  last_message_at  timestamptz,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now(),
  unique (id, user_id),
  foreign key (workspace_id, user_id) references public.workspaces(id, owner_user_id) on delete cascade
);
create index on public.conversations (user_id, updated_at desc);
create trigger trg_conversations_updated_at before update on public.conversations
  for each row execute function app.touch_updated_at();

-- messages ---------------------------------------------------------------------------------
create table public.messages (
  id               uuid primary key default gen_random_uuid(),
  conversation_id  uuid not null,
  user_id          uuid not null references public.users(id) on delete cascade,
  workspace_id     uuid not null,
  role             text not null check (role in ('user', 'assistant')),
  content          text not null check (char_length(content) between 1 and 64000),
  provider         text check (provider is null or provider in ('gemini', 'openai')),
  model            text,
  input_tokens     integer check (input_tokens is null or input_tokens >= 0),
  output_tokens    integer check (output_tokens is null or output_tokens >= 0),
  created_at       timestamptz not null default now(),
  -- a mensagem pertence a uma conversa do MESMO utilizador
  foreign key (conversation_id, user_id) references public.conversations(id, user_id) on delete cascade,
  foreign key (workspace_id, user_id) references public.workspaces(id, owner_user_id) on delete cascade
);
create index on public.messages (conversation_id, created_at);
create index on public.messages (user_id, created_at desc);

-- RLS no estilo de 0003 ------------------------------------------------------------------------
alter table public.conversations enable row level security;
alter table public.messages enable row level security;
revoke all on public.conversations from public;
revoke all on public.messages from public;

grant select, insert, update, delete on public.conversations to authenticated;
create policy conversations_own on public.conversations for all to authenticated
  using (user_id = app.current_user_id())
  with check (user_id = app.current_user_id());

-- mensagens: ler, criar e apagar as próprias; nunca editar
grant select, insert, delete on public.messages to authenticated;
create policy messages_own on public.messages for all to authenticated
  using (user_id = app.current_user_id())
  with check (user_id = app.current_user_id());

-- CONNECTOR AI · 0002 · as 20 tabelas do domínio
-- Regras: todas as datas em timestamptz (UTC), user_id em todas as tabelas,
-- workspace_id em todas as tabelas de trabalho, segredos nunca em texto simples.

-- 1. users ------------------------------------------------------------------
create table public.users (
  id                 uuid primary key default gen_random_uuid(),
  email              citext not null unique,
  email_verified_at  timestamptz,
  status             text not null default 'active'
                       check (status in ('active', 'suspended', 'deleted')),
  is_admin           boolean not null default false,
  last_login_at      timestamptz,
  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now(),
  deleted_at         timestamptz
);

-- 3. workspaces (criada antes de user_profiles por causa da FK) ----------------
create table public.workspaces (
  id             uuid primary key default gen_random_uuid(),
  owner_user_id  uuid not null references public.users(id) on delete cascade,
  name           text not null check (char_length(name) between 1 and 120),
  slug           text not null check (slug ~ '^[a-z0-9][a-z0-9-]{0,62}$'),
  plan           text not null default 'free' check (plan in ('free', 'pro', 'team', 'enterprise')),
  settings       jsonb not null default '{}'::jsonb,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now(),
  deleted_at     timestamptz,
  unique (owner_user_id, slug),
  -- permite FKs compostas (workspace_id, user_id) nas tabelas de trabalho,
  -- garantindo que ninguém escreve num workspace que não é seu.
  unique (id, owner_user_id)
);

-- 2. user_profiles -----------------------------------------------------------
create table public.user_profiles (
  user_id               uuid primary key references public.users(id) on delete cascade,
  display_name          text check (char_length(display_name) <= 120),
  avatar_url            text,
  locale                text not null default 'en',
  timezone              text not null default 'UTC',
  theme                 text not null default 'dark' check (theme in ('dark', 'light', 'system')),
  default_workspace_id  uuid,
  preferences           jsonb not null default '{}'::jsonb,
  created_at            timestamptz not null default now(),
  updated_at            timestamptz not null default now(),
  foreign key (default_workspace_id, user_id)
    references public.workspaces(id, owner_user_id) on delete set null (default_workspace_id)
);

-- 4. connector_definitions (catálogo; user_id/workspace_id só para conectores custom)
create table public.connector_definitions (
  id            uuid primary key default gen_random_uuid(),
  key           text not null unique check (key ~ '^[a-z0-9_]{2,64}$'),
  name          text not null,
  description   text not null default '',
  category      text not null default 'other',
  auth_type     text not null check (auth_type in ('oauth2', 'api_key', 'browser', 'none')),
  status        text not null default 'planned'
                  check (status in ('planned', 'beta', 'available', 'deprecated')),
  planned_stage smallint check (planned_stage between 1 and 7),
  version       text not null default '0.0.0',
  scopes        text[] not null default '{}',
  tools         jsonb not null default '[]'::jsonb,
  is_public     boolean not null default true,
  user_id       uuid references public.users(id) on delete cascade,
  workspace_id  uuid,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),
  check (is_public or user_id is not null),
  foreign key (workspace_id, user_id) references public.workspaces(id, owner_user_id) on delete cascade
);

-- 5. connected_apps ----------------------------------------------------------
create table public.connected_apps (
  id                       uuid primary key default gen_random_uuid(),
  user_id                  uuid not null references public.users(id) on delete cascade,
  workspace_id             uuid not null,
  connector_definition_id  uuid not null references public.connector_definitions(id) on delete restrict,
  display_name             text not null,
  status                   text not null default 'pending'
                             check (status in ('pending', 'connected', 'error', 'expired', 'revoked')),
  external_account_id      text,
  external_account_label   text,
  last_health_check_at     timestamptz,
  last_health_status       text check (last_health_status in ('healthy', 'degraded', 'unhealthy')),
  revoked_at               timestamptz,
  metadata                 jsonb not null default '{}'::jsonb,
  created_at               timestamptz not null default now(),
  updated_at               timestamptz not null default now(),
  foreign key (workspace_id, user_id) references public.workspaces(id, owner_user_id) on delete cascade
);

-- 6. connector_permissions ---------------------------------------------------
create table public.connector_permissions (
  id                 uuid primary key default gen_random_uuid(),
  user_id            uuid not null references public.users(id) on delete cascade,
  workspace_id       uuid not null,
  connected_app_id   uuid not null references public.connected_apps(id) on delete cascade,
  permission         text not null check (permission ~ '^[a-z0-9_]+\.[a-z0-9_.]+$'),
  risk_level         text not null check (risk_level in ('low', 'medium', 'high', 'critical')),
  requires_approval  boolean not null default true,
  granted_at         timestamptz not null default now(),
  revoked_at         timestamptz,
  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now(),
  unique (connected_app_id, permission),
  foreign key (workspace_id, user_id) references public.workspaces(id, owner_user_id) on delete cascade
);

-- 7. encrypted_credentials (só cifrado; o papel authenticated não tem acesso) ---
create table public.encrypted_credentials (
  id                uuid primary key default gen_random_uuid(),
  user_id           uuid not null references public.users(id) on delete cascade,
  workspace_id      uuid not null,
  connected_app_id  uuid not null references public.connected_apps(id) on delete cascade,
  credential_type   text not null check (credential_type in ('oauth_token', 'api_key', 'browser_cookie', 'password', 'other')),
  algorithm         text not null default 'AES-256-GCM' check (algorithm = 'AES-256-GCM'),
  key_id            text not null,
  nonce             bytea not null check (octet_length(nonce) = 12),
  ciphertext        bytea not null check (octet_length(ciphertext) >= 17),
  expires_at        timestamptz,
  rotated_at        timestamptz,
  revoked_at        timestamptz,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now(),
  foreign key (workspace_id, user_id) references public.workspaces(id, owner_user_id) on delete cascade
);

-- 18. workflows (antes de tasks por causa das FKs) -----------------------------
create table public.workflows (
  id             uuid primary key default gen_random_uuid(),
  user_id        uuid not null references public.users(id) on delete cascade,
  workspace_id   uuid not null,
  name           text not null check (char_length(name) between 1 and 200),
  description    text not null default '',
  definition     jsonb not null default '{}'::jsonb,
  trigger_type   text not null default 'manual' check (trigger_type in ('manual', 'schedule', 'webhook')),
  schedule_cron  text,
  is_enabled     boolean not null default false,
  version        integer not null default 1 check (version >= 1),
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now(),
  deleted_at     timestamptz,
  check (trigger_type <> 'schedule' or schedule_cron is not null),
  foreign key (workspace_id, user_id) references public.workspaces(id, owner_user_id) on delete cascade
);

-- 19. workflow_runs ------------------------------------------------------------
create table public.workflow_runs (
  id            uuid primary key default gen_random_uuid(),
  workflow_id   uuid not null references public.workflows(id) on delete cascade,
  user_id       uuid not null references public.users(id) on delete cascade,
  workspace_id  uuid not null,
  status        text not null default 'queued'
                  check (status in ('queued', 'planning', 'running', 'waiting_approval',
                                    'completed', 'failed', 'cancelled', 'paused')),
  trigger       text not null default 'manual',
  started_at    timestamptz,
  finished_at   timestamptz,
  error_message text,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),
  foreign key (workspace_id, user_id) references public.workspaces(id, owner_user_id) on delete cascade
);

-- 9. tasks -------------------------------------------------------------------
create table public.tasks (
  id               uuid primary key default gen_random_uuid(),
  user_id          uuid not null references public.users(id) on delete cascade,
  workspace_id     uuid not null,
  title            text not null check (char_length(title) between 1 and 300),
  instruction      text not null check (char_length(instruction) between 1 and 20000),
  mode             text not null default 'manual' check (mode in ('manual', 'autonomous')),
  status           text not null default 'queued'
                     check (status in ('queued', 'planning', 'running', 'waiting_approval',
                                       'completed', 'failed', 'cancelled', 'paused')),
  priority         smallint not null default 5 check (priority between 1 and 10),
  idempotency_key  text,
  attempt_count    integer not null default 0 check (attempt_count >= 0),
  max_attempts     integer not null default 3 check (max_attempts between 1 and 20),
  timeout_seconds  integer not null default 900 check (timeout_seconds between 10 and 86400),
  parent_task_id   uuid references public.tasks(id) on delete set null,
  workflow_run_id  uuid references public.workflow_runs(id) on delete set null,
  error_code       text,
  error_message    text,
  queued_at        timestamptz not null default now(),
  started_at       timestamptz,
  completed_at     timestamptz,
  cancelled_at     timestamptz,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now(),
  unique (user_id, idempotency_key),
  unique (id, user_id),
  foreign key (workspace_id, user_id) references public.workspaces(id, owner_user_id) on delete cascade
);

-- 10. task_steps -----------------------------------------------------------------
create table public.task_steps (
  id            uuid primary key default gen_random_uuid(),
  task_id       uuid not null,
  user_id       uuid not null references public.users(id) on delete cascade,
  workspace_id  uuid not null,
  step_index    integer not null check (step_index >= 0),
  kind          text not null check (kind in ('plan', 'tool_call', 'browser_action', 'verification', 'approval', 'recovery')),
  tool_name     text,
  status        text not null default 'pending'
                  check (status in ('pending', 'running', 'waiting_approval', 'succeeded', 'failed', 'skipped', 'cancelled')),
  risk_level    text not null default 'low' check (risk_level in ('low', 'medium', 'high', 'critical')),
  input         jsonb not null default '{}'::jsonb,
  output        jsonb,
  attempt       integer not null default 1 check (attempt >= 1),
  error_message text,
  started_at    timestamptz,
  finished_at   timestamptz,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),
  unique (task_id, step_index),
  foreign key (task_id, user_id) references public.tasks(id, user_id) on delete cascade,
  foreign key (workspace_id, user_id) references public.workspaces(id, owner_user_id) on delete cascade
);

-- 8. browser_sessions --------------------------------------------------------
create table public.browser_sessions (
  id                   uuid primary key default gen_random_uuid(),
  user_id              uuid not null references public.users(id) on delete cascade,
  workspace_id         uuid not null,
  task_id              uuid references public.tasks(id) on delete set null,
  provider             text not null check (provider in ('browserbase', 'local_playwright')),
  provider_session_id  text,
  status               text not null default 'starting'
                         check (status in ('starting', 'active', 'idle', 'closed', 'failed')),
  started_at           timestamptz not null default now(),
  last_activity_at     timestamptz,
  expires_at           timestamptz,
  ended_at             timestamptz,
  metadata             jsonb not null default '{}'::jsonb,
  created_at           timestamptz not null default now(),
  updated_at           timestamptz not null default now(),
  foreign key (workspace_id, user_id) references public.workspaces(id, owner_user_id) on delete cascade
);

-- 11. action_logs (só acrescento) ----------------------------------------------
create table public.action_logs (
  id            uuid primary key default gen_random_uuid(),
  user_id       uuid not null references public.users(id) on delete cascade,
  workspace_id  uuid not null,
  task_id       uuid references public.tasks(id) on delete set null,
  task_step_id  uuid references public.task_steps(id) on delete set null,
  actor         text not null check (actor in ('user', 'agent', 'system')),
  action        text not null,
  target        text,
  risk_level    text not null default 'low' check (risk_level in ('low', 'medium', 'high', 'critical')),
  outcome       text not null check (outcome in ('success', 'failure', 'blocked', 'pending')),
  details       jsonb not null default '{}'::jsonb,
  created_at    timestamptz not null default now(),
  foreign key (workspace_id, user_id) references public.workspaces(id, owner_user_id) on delete cascade
);

-- 12. approvals --------------------------------------------------------------
create table public.approvals (
  id               uuid primary key default gen_random_uuid(),
  user_id          uuid not null references public.users(id) on delete cascade,
  workspace_id     uuid not null,
  task_id          uuid not null references public.tasks(id) on delete cascade,
  task_step_id     uuid references public.task_steps(id) on delete cascade,
  category         text not null check (category in ('payment', 'purchase', 'send_message', 'publish',
                                                     'delete_data', 'modify_data', 'accept_terms',
                                                     'irreversible', 'other')),
  action_summary   text not null,
  risk_level       text not null check (risk_level in ('medium', 'high', 'critical')),
  payload_preview  jsonb not null default '{}'::jsonb,
  status           text not null default 'pending'
                     check (status in ('pending', 'approved', 'rejected', 'expired', 'cancelled')),
  decided_at       timestamptz,
  decision_note    text,
  expires_at       timestamptz,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now(),
  check (status in ('pending', 'expired', 'cancelled') or decided_at is not null),
  foreign key (workspace_id, user_id) references public.workspaces(id, owner_user_id) on delete cascade
);

-- 13. memories (memória autorizada) ---------------------------------------------
create table public.memories (
  id             uuid primary key default gen_random_uuid(),
  user_id        uuid not null references public.users(id) on delete cascade,
  workspace_id   uuid not null,
  kind           text not null check (kind in ('preference', 'fact', 'instruction')),
  content        text not null check (char_length(content) between 1 and 4000),
  source         text not null check (source in ('user', 'agent')),
  authorized_at  timestamptz,
  expires_at     timestamptz,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now(),
  deleted_at     timestamptz,
  -- memória criada pelo utilizador é autorizada no ato; a do agente fica pendente.
  check (source = 'agent' or authorized_at is not null),
  foreign key (workspace_id, user_id) references public.workspaces(id, owner_user_id) on delete cascade
);

-- 14. files ------------------------------------------------------------------
create table public.files (
  id              uuid primary key default gen_random_uuid(),
  user_id         uuid not null references public.users(id) on delete cascade,
  workspace_id    uuid not null,
  task_id         uuid references public.tasks(id) on delete set null,
  storage_bucket  text not null,
  storage_key     text not null,
  filename        text not null,
  mime_type       text not null default 'application/octet-stream',
  size_bytes      bigint not null check (size_bytes >= 0),
  sha256          text check (sha256 ~ '^[0-9a-f]{64}$'),
  source          text not null check (source in ('upload', 'download', 'screenshot', 'export')),
  created_at      timestamptz not null default now(),
  deleted_at      timestamptz,
  unique (storage_bucket, storage_key),
  foreign key (workspace_id, user_id) references public.workspaces(id, owner_user_id) on delete cascade
);

-- 15. execution_results -------------------------------------------------------
create table public.execution_results (
  id                    uuid primary key default gen_random_uuid(),
  user_id               uuid not null references public.users(id) on delete cascade,
  workspace_id          uuid not null,
  task_id               uuid not null references public.tasks(id) on delete cascade,
  task_step_id          uuid references public.task_steps(id) on delete cascade,
  result_type           text not null,
  data                  jsonb not null default '{}'::jsonb,
  verified              boolean not null default false,
  verification_details  jsonb,
  file_id               uuid references public.files(id) on delete set null,
  created_at            timestamptz not null default now(),
  foreign key (workspace_id, user_id) references public.workspaces(id, owner_user_id) on delete cascade
);

-- 16. audit_logs (só acrescento; user_id pode ser nulo p/ eventos anónimos, ex. login falhado)
create table public.audit_logs (
  id            uuid primary key default gen_random_uuid(),
  user_id       uuid references public.users(id) on delete set null,
  workspace_id  uuid references public.workspaces(id) on delete set null,
  actor_type    text not null check (actor_type in ('user', 'agent', 'system', 'anonymous')),
  event_type    text not null,
  ip            inet,
  user_agent    text,
  details       jsonb not null default '{}'::jsonb,
  created_at    timestamptz not null default now()
);

-- 17. usage_metrics ------------------------------------------------------------
create table public.usage_metrics (
  id            uuid primary key default gen_random_uuid(),
  user_id       uuid not null references public.users(id) on delete cascade,
  workspace_id  uuid not null,
  task_id       uuid references public.tasks(id) on delete set null,
  metric        text not null check (metric in ('llm_input_tokens', 'llm_output_tokens', 'tool_calls',
                                                 'browser_minutes', 'tasks_run', 'storage_bytes')),
  quantity      numeric(20, 4) not null check (quantity >= 0),
  unit          text not null,
  recorded_at   timestamptz not null default now(),
  created_at    timestamptz not null default now(),
  foreign key (workspace_id, user_id) references public.workspaces(id, owner_user_id) on delete cascade
);

-- 20. notifications --------------------------------------------------------------
create table public.notifications (
  id            uuid primary key default gen_random_uuid(),
  user_id       uuid not null references public.users(id) on delete cascade,
  workspace_id  uuid,
  type          text not null,
  title         text not null,
  body          text not null default '',
  link          text,
  read_at       timestamptz,
  created_at    timestamptz not null default now(),
  foreign key (workspace_id, user_id) references public.workspaces(id, owner_user_id) on delete cascade
);

-- Índices ------------------------------------------------------------------------
create index on public.workspaces (owner_user_id);
create index on public.connected_apps (user_id, status);
create index on public.connector_permissions (user_id, connected_app_id);
create index on public.encrypted_credentials (connected_app_id);
create index on public.browser_sessions (user_id, status);
create index on public.tasks (user_id, status, created_at desc);
create index on public.tasks (workspace_id, created_at desc);
create index on public.task_steps (task_id, step_index);
create index on public.action_logs (user_id, created_at desc);
create index on public.approvals (user_id, status);
create index on public.memories (user_id) where deleted_at is null;
create index on public.files (user_id, created_at desc);
create index on public.execution_results (task_id);
create index on public.audit_logs (user_id, created_at desc);
create index on public.usage_metrics (user_id, metric, recorded_at);
create index on public.workflows (user_id) where deleted_at is null;
create index on public.workflow_runs (workflow_id, created_at desc);
create index on public.notifications (user_id) where read_at is null;

-- updated_at automático -------------------------------------------------------------
do $$
declare t text;
begin
  foreach t in array array['users','user_profiles','workspaces','connector_definitions','connected_apps',
                           'connector_permissions','encrypted_credentials','browser_sessions','tasks',
                           'task_steps','approvals','memories','workflows','workflow_runs']
  loop
    execute format('create trigger trg_%1$s_updated_at before update on public.%1$I
                    for each row execute function app.touch_updated_at()', t);
  end loop;
end $$;

-- Auditoria e logs de ação são imutáveis --------------------------------------------
create trigger trg_audit_logs_immutable before update or delete on public.audit_logs
  for each row execute function app.forbid_mutation();
create trigger trg_action_logs_immutable before update or delete on public.action_logs
  for each row execute function app.forbid_mutation();

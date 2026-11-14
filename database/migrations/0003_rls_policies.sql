-- CONNECTOR AI · 0003 · Row Level Security
-- Todas as consultas em nome de um utilizador correm com o papel "authenticated"
-- e com request.jwt.claims definido na transação. Cada utilizador só vê o que é seu.
-- O dono das tabelas (backend em modo serviço / migrações) não é afetado,
-- por isso o backend só usa esse modo para operações de sistema (auth, workers).

do $$
declare t text;
begin
  foreach t in array array['users','user_profiles','workspaces','connector_definitions','connected_apps',
                           'connector_permissions','encrypted_credentials','browser_sessions','tasks',
                           'task_steps','action_logs','approvals','memories','files','execution_results',
                           'audit_logs','usage_metrics','workflows','workflow_runs','notifications']
  loop
    execute format('alter table public.%I enable row level security', t);
    execute format('revoke all on public.%I from public', t);
  end loop;
end $$;

-- Ninguém além do serviço toca no registo de migrações.
revoke all on public.schema_migrations from public;

-- users: só a própria linha; o utilizador não pode promover-se a admin nem mudar o estado.
grant select on public.users to authenticated;
grant update (updated_at) on public.users to authenticated;
create policy users_self_select on public.users for select to authenticated
  using (id = app.current_user_id());
create policy users_self_update on public.users for update to authenticated
  using (id = app.current_user_id()) with check (id = app.current_user_id());

-- user_profiles
grant select, update on public.user_profiles to authenticated;
create policy profiles_own on public.user_profiles for all to authenticated
  using (user_id = app.current_user_id()) with check (user_id = app.current_user_id());

-- workspaces
grant select, insert, update on public.workspaces to authenticated;
create policy workspaces_own on public.workspaces for all to authenticated
  using (owner_user_id = app.current_user_id()) with check (owner_user_id = app.current_user_id());

-- connector_definitions: catálogo público + conectores custom do próprio utilizador
grant select on public.connector_definitions to authenticated;
create policy connector_definitions_read on public.connector_definitions for select to authenticated
  using (is_public or user_id = app.current_user_id());

-- Tabelas de trabalho com acesso total (sob RLS) ao próprio utilizador
do $$
declare t text;
begin
  foreach t in array array['connected_apps','connector_permissions','browser_sessions','tasks','task_steps',
                           'memories','files','execution_results','workflows','workflow_runs']
  loop
    execute format('grant select, insert, update, delete on public.%I to authenticated', t);
    execute format('create policy %1$s_own on public.%1$I for all to authenticated
                    using (user_id = app.current_user_id())
                    with check (user_id = app.current_user_id())', t);
  end loop;
end $$;

-- approvals: o utilizador vê e decide, mas não cria nem apaga (é o motor de execução que cria).
grant select on public.approvals to authenticated;
grant update (status, decided_at, decision_note) on public.approvals to authenticated;
create policy approvals_read on public.approvals for select to authenticated
  using (user_id = app.current_user_id());
create policy approvals_decide on public.approvals for update to authenticated
  using (user_id = app.current_user_id() and status = 'pending')
  with check (user_id = app.current_user_id() and status in ('approved', 'rejected'));

-- notifications: ler e marcar como lida
grant select on public.notifications to authenticated;
grant update (read_at) on public.notifications to authenticated;
create policy notifications_own on public.notifications for all to authenticated
  using (user_id = app.current_user_id()) with check (user_id = app.current_user_id());

-- usage_metrics: só leitura para o utilizador (escrita pelo serviço)
grant select on public.usage_metrics to authenticated;
create policy usage_metrics_read on public.usage_metrics for select to authenticated
  using (user_id = app.current_user_id());

-- action_logs e audit_logs: leitura do próprio histórico; escrita só pelo serviço.
grant select on public.action_logs to authenticated;
create policy action_logs_read on public.action_logs for select to authenticated
  using (user_id = app.current_user_id());
grant select on public.audit_logs to authenticated;
create policy audit_logs_read on public.audit_logs for select to authenticated
  using (user_id = app.current_user_id());

-- encrypted_credentials: SEM grant e SEM política para authenticated.
-- Nem o próprio dono lê o cifrado pela API; só o serviço de conectores o decifra.

-- CONNECTOR AI · 0009 · ferramentas do agente no chat
--
-- * messages.metadata: o que o agente fez numa resposta (ferramentas usadas, pedidos para ligar
--   uma conta, ações à espera de confirmação). O frontend usa-o para voltar a mostrar os botões
--   "Ligar GitHub" e "Confirmar / Recusar" quando a conversa é reaberta.
-- * As ações de escrita (ex.: criar issue) usam as tabelas que já existiam: tasks + task_steps +
--   approvals (criadas pelo serviço; o utilizador só decide, como definido em 0003).

alter table public.messages
  add column if not exists metadata jsonb not null default '{}'::jsonb;

-- encontrar as aprovações de uma conversa
create index if not exists approvals_conversation_idx
  on public.approvals ((payload_preview ->> 'conversation_id'));

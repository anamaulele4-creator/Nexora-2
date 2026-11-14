-- CONNECTOR AI · 0006 · fornecedores com planos gratuitos: Groq e OpenRouter
-- Remove qualquer CHECK antigo sobre a coluna provider (nome gerado pelo Postgres) e cria o novo.
do $$
declare r record;
begin
  for r in
    select c.conrelid::regclass as tbl, c.conname
      from pg_constraint c
     where c.contype = 'c'
       and c.conrelid in ('public.ai_providers'::regclass, 'public.messages'::regclass)
       and pg_get_constraintdef(c.oid) ilike '%provider%'
       and pg_get_constraintdef(c.oid) ilike '%gemini%'
  loop
    execute format('alter table %s drop constraint %I', r.tbl, r.conname);
  end loop;
end $$;

alter table public.ai_providers add constraint ai_providers_provider_check
  check (provider in ('gemini', 'groq', 'openrouter', 'openai'));

alter table public.messages add constraint messages_provider_check
  check (provider is null or provider in ('gemini', 'groq', 'openrouter', 'openai'));

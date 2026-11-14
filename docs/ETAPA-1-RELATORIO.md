# Etapa 1 — Fundação · relatório de validação

Data: 2026-11-13 · Ambiente de validação: sandbox Linux (Debian 13, arm64), Python 3.12.15,
Node 22, PostgreSQL 17.12, Redis 8.

## Entregue

- Documento de arquitetura (`docs/ARCHITECTURE.md`) e contas externas por etapa (`docs/CONTAS-EXTERNAS.md`).
- Estrutura completa do monorepo (pastas das etapas seguintes com README a dizer o que vem e quando).
- Backend FastAPI: configuração validada (recusa arrancar com configuração insegura em produção), `/health`, `/ready`, autenticação completa (registo, login, sessões, refresh rotativo, logout, verificação de email, reenvio, recuperação de palavra-passe), dashboard, catálogo de conectores, logs JSON, X-Request-ID, cabeçalhos de segurança, CSRF por origem, limite de pedidos.
- Base de dados: 4 migrações SQL, 20 tabelas, RLS em todas, papel `authenticated` compatível com Supabase, auditoria só de acrescento, seeds do catálogo (6 conectores, todos `planned`).
- `BaseConnector`, contrato `ToolSpec`, `ToolRegistry`, cifra AES-256-GCM para credenciais.
- Frontend Next.js 15 + React 19 + TypeScript + Tailwind: landing, registo, login, verificar email, recuperar e redefinir palavra-passe, Dashboard com números reais; tema escuro por defeito (claro opcional); inglês e português.
- `docker-compose.yml` (PostgreSQL 17 + Redis 7; perfil `app` com migrações, API e frontend), Dockerfiles, `.env.example` sem segredos.

## Validado no sandbox

| Verificação | Resultado |
|---|---|
| Migrações num PostgreSQL 17 real (`migrate.py up --seed`), 2.ª execução idempotente | ✅ |
| Testes backend (pytest, PostgreSQL + Redis reais) | ✅ **47/47** |
| Testes frontend (node:test: traduções completas, regras de palavra-passe, erros) | ✅ 4/4 |
| `tsc --noEmit` | ✅ sem erros |
| `next build` (produção) | ✅ 8 rotas + middleware |
| API arranca com uvicorn; `/health` 200; `/ready` 200 (BD, migrações, Redis) | ✅ |
| Fluxo ponta a ponta pelo proxy do Next (frontend em `next start` + API): dashboard sem sessão → redireciona para login; registo; login recusado antes de verificar; link de verificação no log; verificação; login com cookies httpOnly; dashboard 200; resumo com zeros reais; refresh; logout; resumo 401; dashboard redireciona | ✅ |

O que os testes cobrem: 20 tabelas com RLS; `user_id` e datas `timestamptz` em todas; isolamento
entre utilizadores; FK composta impede escrever no workspace de outro; utilizador não lê
credenciais cifradas nem o esquema `local_auth`, nem se promove a admin; auditoria imutável;
estados de tarefa e idempotência; aprovações só decididas pelo dono; deteção de migração alterada;
fluxos de auth completos incluindo deteção de reutilização de refresh token, bloqueio após 5
falhas, limite de pedidos (429), CSRF de origem estrangeira (403), token adulterado (401);
validação de configuração; contrato de ferramentas; cifra; Supabase simulado.

## NÃO testado (e porquê)

- **Docker / docker-compose**: o sandbox não tem Docker. O YAML foi validado sintaticamente; as imagens não foram construídas.
- **Supabase real**: sem autorização para chaves. O fornecedor `supabase` só foi testado contra um servidor Supabase Auth simulado. Projetos com chaves JWKS precisam de um ajuste pequeno (ver arquitetura).
- **Envio real de email (SMTP)**: implementado, não testado contra um servidor; em desenvolvimento os links aparecem no log da API.
- **Interface num browser real**: as páginas compilam e respondem 200, e o fluxo foi testado por HTTP, mas não houve teste visual/clique automatizado (Playwright entra na Etapa 2).
- **Vercel / produção / HTTPS**: nada publicado, por regra.
- OpenAI, Browserbase, Temporal, S3: não fazem parte da Etapa 1.

## Pendente de decisão da Ana (para a Etapa 2)

1. Validar esta Etapa 1.
2. Navegador: começar com Playwright local e só depois Browserbase, ou Browserbase logo?
3. Qual o primeiro conector real para a fatia ponta a ponta (sugestão: GitHub — OAuth simples, sem verificação da Google).

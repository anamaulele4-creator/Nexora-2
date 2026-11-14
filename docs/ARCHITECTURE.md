# CONNECTOR AI — Arquitetura

> "One AI. Multiple Platforms. Real Actions."
> Versão do documento: Etapa 1 (Fundação) · 2026-11-13

## 1. Visão geral

O utilizador dá uma instrução em linguagem natural; um agente de IA planeia e executa a tarefa
através de **conectores** (APIs oficiais) e de um **navegador controlado**, em modo manual ou
autónomo, com **aprovação humana obrigatória** antes de qualquer ação sensível.

```
                ┌──────────────────────────── Vercel ───────────────────────────┐
 Utilizador ──► │ Next.js (React, TS, Tailwind)  ── rewrites /api/* (mesma origem) │
                └──────────────────────────────────┬────────────────────────────┘
                                                   │ HTTPS + cookies httpOnly
                ┌──────────────── Infraestrutura própria (contentores) ──────────┐
                │  FastAPI (api/)                                                │
                │   ├─ authentication/  (local | Supabase Auth)                  │
                │   ├─ ai_core/ task_planner/          (Etapa 3, OpenAI)         │
                │   ├─ connector_hub/ ── connectors/*  (Etapa 4)                 │
                │   ├─ browser_engine/                  (Etapa 2)                │
                │   ├─ action_engine/ verification/     (Etapa 5)                │
                │   └─ security/ monitoring/                                    │
                │  workers/ (browser, api, scheduled) ── Temporal (Etapa 5)      │
                │  WebSockets (Etapa 5: progresso em tempo real)                 │
                └───────┬──────────────┬──────────────┬──────────────┬───────────┘
                        │              │              │              │
                  PostgreSQL        Redis        Browserbase     S3-compatível
                (Supabase em prod)  (limites,    (Chromium       (ficheiros,
                 RLS em 20 tabelas   filas)       remoto)         screenshots)
```

Princípios:

1. **O modelo só chama ferramentas registadas** (`connector_hub.registry.ToolRegistry`). Nunca código arbitrário.
2. **Conteúdo de páginas web é não confiável** e nunca concede permissões: permissões vêm só de `connector_permissions` e de aprovações humanas (`approvals`).
3. **Aprovação humana** antes de pagamentos, compras, envio de emails/mensagens, publicações, apagar/alterar dados importantes, aceitar termos e ações irreversíveis. Imposto em três camadas: contrato da ferramenta (`ToolSpec`), `BaseConnector.execute` e base de dados (`approvals` só pode ser decidida pelo dono).
4. **Defesa em profundidade nos dados**: todas as consultas de utilizador correm com o papel `authenticated` + RLS; credenciais de conectores só cifradas (AES-256-GCM) e inacessíveis ao papel `authenticated`.
5. **Nada fictício**: o Dashboard mostra números da BD; nenhum conector aparece como funcional sem testes (todos estão `planned`).

## 2. Monorepo

```
connector-ai/
├─ frontend/          Next.js App Router
│  ├─ app/            rotas: / login register verify-email forgot-password reset-password dashboard
│  ├─ components/     Header, Providers (tema/idioma), AuthShell
│  ├─ dashboard/      vista do Dashboard (dados reais)
│  ├─ lib/            cliente da API, i18n (en/pt), regras de palavra-passe
│  └─ workspace/ browser/ connections/ tasks/ workflows/ approvals/ settings/   (etapas seguintes)
├─ backend/
│  ├─ api/            main.py (create_app), routes/, deps.py, schemas.py
│  ├─ core/           config validada, BD (modos service/as_user), Redis, erros
│  ├─ authentication/ passwords, tokens, cookies, email, providers/{local,supabase}
│  ├─ security/       crypto, rate_limit, origin (CSRF), headers, audit
│  ├─ monitoring/     logs JSON + X-Request-ID
│  ├─ connector_hub/  registry
│  └─ ai_core/ task_planner/ browser_engine/ action_engine/ verification/ memory_engine/
├─ connectors/        base/ (BaseConnector + ToolSpec) + pastas dos conectores planeados
├─ workers/           browser_worker/ api_worker/ scheduled_worker/
├─ database/          migrations/ (SQL), seeds/, schemas/, migrate.py
├─ infrastructure/    docker/, deployment/, monitoring/
├─ tests/backend/     pytest contra PostgreSQL + Redis reais
├─ docs/
├─ docker-compose.yml
└─ .env.example
```

## 3. Autenticação — decisão

**Decisão:** autenticação atrás de uma interface única (`AuthProviderBase`) com dois fornecedores:

| | `local` (por defeito na Etapa 1) | `supabase` (alvo de produção) |
|---|---|---|
| Registo, login, sessões | FastAPI, argon2id, JWT 15 min + refresh opaco rotativo | Supabase Auth (GoTrue) |
| Verificação de email | token de uso único (hash SHA-256), 24 h | email do Supabase |
| Recuperação de palavra-passe | token de uso único, 1 h, revoga todas as sessões | `recover` + `verify(recovery)` |
| Estado na Etapa 1 | **implementado e testado** contra PostgreSQL/Redis reais | implementado, testado só com Supabase **simulado** |

**Porquê:** a stack pede Supabase (auth + RLS), e Supabase Auth traz de graça verificação de email,
recuperação, OAuth (Google/GitHub, úteis para os conectores) e MFA. Mas a Ana ainda não autorizou
chaves reais, e a Etapa 1 tem de ser validada de ponta a ponta. Por isso:

* o esquema da BD é **compatível com Supabase desde o primeiro dia**: o papel chama-se `authenticated`
  (o mesmo do Supabase) e as políticas usam `app.current_user_id()`, que lê `request.jwt.claims`
  exatamente como `auth.uid()`;
* o modo `local` permite desenvolver e testar tudo sem contas externas;
* mudar para Supabase é configuração (`AUTH_PROVIDER=supabase` + 3 variáveis), não reescrita.
  As tabelas de domínio (`users`, `user_profiles`, `workspaces`) são criadas no primeiro login com
  o mesmo `id` do Supabase.

Nota: projetos Supabase recentes podem assinar JWT com chaves assimétricas (JWKS). O código atual
valida com o `SUPABASE_JWT_SECRET` (HS256, chave "legacy"); suportar JWKS é uma alteração pequena
em `SupabaseAuthProvider.authenticate`, a fazer quando houver projeto real.

### Sessões no browser

* Cookies `ca_access` (path `/`) e `ca_refresh` (path `/api/auth`), ambos **httpOnly**, `SameSite=Lax`, `Secure` em produção. O JavaScript nunca vê tokens.
* `ca_session` é só um marcador sem segredo para o middleware do Next saber que há sessão.
* O frontend chama a API pela **mesma origem** (rewrites do Next), por isso não há CORS no browser.
* Pedidos que alteram estado com cookie têm de vir de uma origem permitida (`OriginCheckMiddleware`).
* Clientes de API (CLI/mobile) podem pedir os tokens no corpo com `X-Client-Type: api` e usar `Authorization: Bearer`.

### Proteções

argon2id · bloqueio de 15 min após 5 falhas · limite de pedidos em Redis (por IP e por email) ·
rotação de refresh token com **deteção de reutilização** (revoga todas as sessões) ·
verificação de sessão ativa em cada pedido (revogação imediata) · respostas que não revelam se um
email existe · auditoria de todos os eventos de autenticação.

## 4. Base de dados

PostgreSQL 15+ (testado em 17). Migrações SQL simples e ordenadas (`database/migrations/NNNN_*.sql`),
aplicadas por `database/migrate.py` com checksum (uma migração aplicada nunca pode ser editada) e
lock consultivo. Escolhi SQL puro em vez de Alembic porque o Supabase também usa migrações SQL e as
políticas RLS são SQL de qualquer forma.

| # | Tabela | Para quê | Acesso do utilizador (RLS) |
|---|---|---|---|
| 1 | users | conta (email citext, estado, admin) | lê a própria linha; não se promove a admin |
| 2 | user_profiles | nome, idioma, fuso, tema (dark por defeito) | própria |
| 3 | workspaces | espaço de trabalho (dono, plano) | próprios |
| 4 | connector_definitions | catálogo (status planned/beta/available) | lê públicos + os seus custom |
| 5 | connected_apps | contas ligadas a um conector | própria, CRUD |
| 6 | connector_permissions | permissões concedidas por app (risco, aprovação) | própria, CRUD |
| 7 | encrypted_credentials | tokens/chaves **só cifrados** (AES-256-GCM, nonce 12 B) | **nenhum** (só serviço) |
| 8 | browser_sessions | sessões de navegador (Browserbase/local) | própria |
| 9 | tasks | tarefa, modo, 8 estados, idempotência, tentativas, timeout | própria |
| 10 | task_steps | passos (plan, tool_call, browser_action, …) | própria |
| 11 | action_logs | registo de ações — **só acrescento** | só leitura |
| 12 | approvals | pedidos de aprovação humana | lê; só decide pendentes (approved/rejected) |
| 13 | memories | memória autorizada (a do agente fica pendente até autorizar) | própria |
| 14 | files | metadados de ficheiros em S3 (sha256, origem) | própria |
| 15 | execution_results | resultados verificados | própria |
| 16 | audit_logs | auditoria — **só acrescento** | só leitura |
| 17 | usage_metrics | tokens, chamadas, minutos de navegador | só leitura |
| 18 | workflows | automações (manual/schedule/webhook) | própria |
| 19 | workflow_runs | execuções | própria |
| 20 | notifications | notificações | lê e marca como lida |

Regras transversais: `user_id` em todas (em `users` é o `id`, em `workspaces` o `owner_user_id`),
`workspace_id` em todas as tabelas de trabalho com **FK composta** `(workspace_id, user_id) →
workspaces(id, owner_user_id)` — ninguém escreve num workspace que não é seu, mesmo com bug no
código. Datas sempre `timestamptz` e ligações em UTC. `updated_at` automático. Esquema `local_auth`
(credenciais, sessões, tokens de uso único) inacessível ao papel `authenticated`.

Modelo multi-membro de workspaces (equipas) não faz parte das 20 tabelas; quando for preciso
acrescenta-se `workspace_members` e as políticas passam a verificar pertença.

## 5. Contrato de ferramentas e conectores

`connectors/base/tools.py` — cada `ToolSpec` declara: nome `<conector>.<acao>`, descrição,
parâmetros (Pydantic com `extra="forbid"` → JSON Schema), resposta, permissões, risco
(low/medium/high/critical), aprovação + categoria, idempotência, limite de chamadas, política de
repetição, timeout e estratégia de verificação. Regras validadas na criação:

* risco high/critical ⇒ aprovação obrigatória;
* só risco low pode dispensar verificação;
* ferramentas com efeitos e não idempotentes não repetem automaticamente.

`connectors/base/connector.py` — `BaseConnector` com `connect`, `disconnect`, `health_check`,
`execute`. O `execute` verifica permissões → aprovação → valida argumentos → corre → valida a resposta.

`backend/connector_hub/registry.py` — registo único; gera a lista de ferramentas no formato da
OpenAI (filtrada pelas permissões do utilizador) e resolve nomes (`gmail__send` → `gmail.send`).
Um nome desconhecido é recusado.

## 6. Estados de tarefa

`queued → planning → running ⇄ waiting_approval → completed | failed | cancelled`, com `paused`.
Já impostos por `CHECK` na BD; o motor de execução (Etapa 5) usa Temporal para retentativas,
timeouts e idempotência (`tasks.idempotency_key` único por utilizador).

## 7. API (Etapa 1)

| Método | Caminho | Notas |
|---|---|---|
| GET | /health | liveness |
| GET | /ready | BD + migrações atualizadas + Redis (503 se falhar) |
| POST | /api/auth/register | 201 |
| POST | /api/auth/login | cookies httpOnly |
| POST | /api/auth/refresh | rotação |
| POST | /api/auth/logout | revoga sessão |
| GET | /api/auth/me | utilizador + perfil + workspace |
| POST | /api/auth/verify-email | token de uso único |
| POST | /api/auth/resend-verification | 202 sempre |
| POST | /api/auth/forgot-password | 202 sempre |
| POST | /api/auth/reset-password | revoga sessões |
| GET | /api/dashboard/summary | números reais sob RLS |
| GET | /api/connectors | catálogo (todos planned) |

Erros sempre em `{"error": {"code", "message", "request_id"}}`. `/docs` (OpenAPI) só fora de produção.
Restantes endpoints do documento técnico (tasks, connections, browser, approvals, audit, usage,
workflows, files, notifications, admin) entram nas etapas respetivas.

## 8. Plano por etapas

1. **Fundação e autenticação** ← esta entrega
2. Browser Engine (Playwright local + Browserbase, SSRF, screenshots, S3)
3. AI Core (OpenAI tool calling, planner, risco, memória autorizada)
4. Connector Hub (OAuth, cofre, primeiro conector real — sugestão: Gmail ou GitHub)
5. Execution Engine (Temporal, workers, WebSockets, aprovações na UI)
6. Segurança (centro de segurança, revogação global, botão de emergência, OpenTelemetry)
7. Testes e lançamento (E2E, carga, Supabase/Vercel em produção — só com autorização)

Recomendação da Ana mantida: depois da Etapa 1, fazer primeiro uma fatia ponta a ponta
(navegador remoto + IA + um conector real) e só depois alargar.

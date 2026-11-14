# Conectores — GitHub, Gmail, Google Drive e Shopify

Cada utilizador liga as suas contas em **/conectores**. O administrador vê em **/admin › Conectores**
se o OAuth de cada fornecedor está configurado (sem mostrar segredos), o URL de retorno e quantas contas
estão ligadas.

| Conector | Como liga | Configuração no servidor |
|---|---|---|
| GitHub | OAuth 2.0 (GitHub OAuth App) | `GITHUB_OAUTH_CLIENT_ID`, `GITHUB_OAUTH_CLIENT_SECRET` |
| Gmail | OAuth 2.0 (Google Cloud, PKCE) | `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET` |
| Google Drive | OAuth 2.0 (o mesmo cliente Google) | as mesmas do Gmail |
| Shopify | domínio da loja + token de Admin API, colados pelo utilizador | nenhuma (opcional `SHOPIFY_API_VERSION`) |

Comum a todos: `CREDENTIALS_ENCRYPTION_KEY` tem de estar definida. Os tokens ficam só cifrados
(AES-256-GCM, tabela `encrypted_credentials`, sem acesso para o papel `authenticated`) e nunca são
devolvidos pela API. Desligar apaga o token cifrado e pede a revogação ao fornecedor.

> Estado: ligar, testar e desligar contas funciona e está testado. O **GitHub já tem ferramentas do
> agente no chat** (ver [Ferramentas do agente no chat](#ferramentas-do-agente-no-chat)). Gmail, Google
> Drive e Shopify ligam-se, mas as suas ferramentas ainda não existem — o chat diz isso com honestidade.

## URLs de retorno (produção)

O frontend `https://connector-ai-2yn3.onrender.com` reencaminha `/api/*` para a API, por isso os URLs de
retorno ficam no domínio do frontend:

```
https://connector-ai-2yn3.onrender.com/api/connectors/oauth/github/callback
https://connector-ai-2yn3.onrender.com/api/connectors/oauth/google/callback
```

A API calcula-os a partir de `OAUTH_REDIRECT_BASE_URL` (se vazio, usa `FRONTEND_URL`). O valor tem de
coincidir **exatamente** com o registado no GitHub/Google. Em /admin › Conectores aparece o URL que a
API está a usar, com botão para copiar.

Em desenvolvimento: `http://localhost:3000/api/connectors/oauth/github/callback` (e `/google/callback`).

## 1. GitHub OAuth App

1. Entra no GitHub com a conta que vai ser dona da app → **Settings → Developer settings → OAuth Apps →
   New OAuth App** (ou, para uma organização: *Organization settings → Developer settings → OAuth Apps*).
2. Preenche:
   - **Application name:** `CONNECTOR AI`
   - **Homepage URL:** `https://connector-ai-2yn3.onrender.com`
   - **Authorization callback URL:** `https://connector-ai-2yn3.onrender.com/api/connectors/oauth/github/callback`
   - Deixa *Enable Device Flow* desligado.
3. **Register application**. Copia o **Client ID**.
4. **Generate a new client secret** e copia-o logo (só é mostrado uma vez).
5. No serviço da **API** (não no frontend) define:
   ```
   GITHUB_OAUTH_CLIENT_ID=<Client ID>
   GITHUB_OAUTH_CLIENT_SECRET=<Client secret>
   ```
6. Reinicia a API e confirma em /admin › Conectores que o GitHub aparece como "OAuth configurado".

Escopos pedidos: `repo read:user` (definidos em `database/seeds/0001_connector_definitions.sql`).
Uma OAuth App do GitHub só aceita **um** callback URL: para testar em localhost cria uma segunda app.

## 2. Google Cloud (Gmail e Google Drive)

1. Em <https://console.cloud.google.com/> cria (ou escolhe) um projeto, ex.: `connector-ai`.
2. **APIs e serviços → Biblioteca:** ativa a **Gmail API** e a **Google Drive API**.
3. **Google Auth Platform** (antigo "Ecrã de consentimento OAuth"):
   - *Branding:* nome `CONNECTOR AI`, email de suporte, domínio autorizado `onrender.com`
     (ou o domínio próprio, quando existir), links de política de privacidade e termos.
   - *Audience:* **External**. Enquanto estiver em **Testing**, adiciona em *Test users* os emails que
     podem ligar contas (máximo 100).
   - *Data access:* adiciona os escopos
     `openid`, `email`,
     `https://www.googleapis.com/auth/gmail.readonly`,
     `https://www.googleapis.com/auth/gmail.compose`,
     `https://www.googleapis.com/auth/drive.file`.
4. **Clients → Create client:**
   - *Application type:* **Web application**, nome `CONNECTOR AI web`.
   - *Authorized redirect URIs:*
     `https://connector-ai-2yn3.onrender.com/api/connectors/oauth/google/callback`
     (e, para desenvolvimento, `http://localhost:3000/api/connectors/oauth/google/callback`).
   - Não é preciso "Authorized JavaScript origins" (a troca do código é feita pelo servidor).
5. Copia o **Client ID** e o **Client secret** e define no serviço da **API**:
   ```
   GOOGLE_OAUTH_CLIENT_ID=<...apps.googleusercontent.com>
   GOOGLE_OAUTH_CLIENT_SECRET=<secret>
   ```
6. Reinicia a API e confirma em /admin › Conectores.

Notas importantes:
- O fluxo pede `access_type=offline` e `prompt=consent` para receber *refresh token*; o botão
  **Testar** em /conectores renova o acesso automaticamente quando o token expirou.
- Em modo **Testing** a Google faz expirar os refresh tokens ao fim de 7 dias: o utilizador terá de
  voltar a ligar. Para isso deixar de acontecer, publica a app (*Audience → Publish app*).
- Os escopos do Gmail são **restritos**: para uso público a Google exige verificação da app (e uma
  avaliação de segurança). Até lá, só os *test users* conseguem ligar o Gmail. `drive.file` só dá
  acesso a ficheiros criados ou abertos pela app.

## 3. Shopify (por utilizador)

Não há nada a configurar no servidor. Cada utilizador, na sua loja:
1. Shopify admin → **Settings → Apps and sales channels → Develop apps** → *Create an app*.
2. *Configure Admin API scopes*: pelo menos `read_products` e `read_orders`.
3. *Install app* e copia o **Admin API access token** (`shpat_…`, só é mostrado uma vez).
4. Em /conectores › Shopify cola o domínio (`minha-loja` ou `minha-loja.myshopify.com`) e o token.
   A API confirma o token em `GET /admin/api/<SHOPIFY_API_VERSION>/shop.json` antes de o gravar.

## API

| Método | Caminho | O quê |
|---|---|---|
| GET | `/api/connectors` | catálogo + estado da ligação do utilizador |
| GET | `/api/connectors/oauth/{github\|google}/start?connector=…` | URL de autorização (state de uso único, 10 min) |
| GET | `/api/connectors/oauth/{github\|google}/callback` | retorno do fornecedor → redireciona para `/conectores?connected=…` ou `?error=…` |
| POST | `/api/connectors/{key}/connect` | OAuth: devolve o URL de autorização · Shopify: `{shop_domain, access_token}` |
| GET | `/api/connectors/{key}/status[?check=true]` | estado guardado; `check=true` testa no fornecedor |
| DELETE | `/api/connectors/{key}` | desliga (revoga no fornecedor, apaga o token cifrado) |
| POST | `/api/chat/approvals/{id}` | `{"decision": "approve" \| "reject"}` — confirma/recusa uma ação proposta pelo agente |
| GET | `/api/admin/connectors` | admin: OAuth configurado, variáveis definidas, URL de retorno, contas ligadas |

Segurança: o `state` é guardado no Redis só como hash SHA-256 e consumido uma vez; se quem chega ao
callback tem sessão de outro utilizador, o pedido é recusado. Na Google usa-se PKCE (S256). Os pedidos
de ligação têm limite de 20 por utilizador a cada 15 minutos.

## Ferramentas do agente no chat

O chat (`/chat`) é atendido pelo **DÁRIO** (motor da CONNECTOR AI). Em cada mensagem o backend vê que
contas o utilizador tem ligadas e dá ao modelo **só** as ferramentas dessas contas, mais
`request_connection`. O prompt de sistema é montado por pedido com essa lista (`backend/ai_core/system_prompt.py`).

### GitHub (`connectors/github`)

Usa o token OAuth do próprio utilizador (decifrado do cofre só no servidor; nunca vai para o modelo nem
para o browser) contra `https://api.github.com`.

| Ferramenta (nome para o modelo) | O quê | Corre |
|---|---|---|
| `github__get_me` | perfil da conta ligada | logo |
| `github__list_repos` | repositórios (mais recentes primeiro) | logo |
| `github__get_repo` | detalhes de um repositório | logo |
| `github__list_issues` / `github__get_issue` | issues (sem PRs) / uma issue + comentários | logo |
| `github__list_pull_requests` | pull requests | logo |
| `github__read_file` | ficheiro de texto (até 20 000 caracteres) ou listagem de pasta | logo |
| `github__search_code` | pesquisa de código (opcionalmente num dono/repositório) | logo |
| `github__create_issue` | criar issue | **só depois de o utilizador confirmar** |
| `github__comment_issue` | comentar issue/PR | **só depois de o utilizador confirmar** |

### Pedir para ligar uma conta (`request_connection`)

Se um pedido precisa de uma conta que não está ligada (ou cujo acesso expirou), o DÁRIO não recusa:
chama `request_connection` e o chat mostra o botão **"Ligar GitHub"** (ou Gmail, Google Drive, Shopify).
O botão inicia logo o OAuth (`POST /api/connectors/{key}/connect?return_to=chat&conversation=…`) e, no fim,
o GitHub/Google devolvem o utilizador a `/chat?connected=github&conversation=…` — a conversa reabre com
o texto "Já liguei o GitHub. Continua, por favor." pronto a enviar. O Shopify (token colado) abre
`/conectores`. Para apps sem ferramentas ainda, o botão diz que a ligação funciona mas as ações não.

### Confirmação de escritas

Ações que alteram dados nunca correm quando o modelo as pede. O backend cria uma aprovação pendente nas
tabelas que já existiam — `tasks` (estado `waiting_approval`) + `task_steps` + `approvals` (expira em
1 hora) + `action_logs` — e o chat mostra um cartão com **Confirmar / Recusar**. Só
`POST /api/chat/approvals/{id}` `{"decision": "approve"}` executa a ferramenta (com `approval_id` no
contexto, exigido por `BaseConnector.execute`). A mudança `pending → approved` é atómica: um segundo
clique responde 409 e nada corre duas vezes. O resultado (ex.: link da issue) fica como mensagem na
conversa. As aprovações pendentes também contam no Dashboard ("À espera de aprovação").

### Fornecedores e modelos

| Fornecedor | Function calling |
|---|---|
| Motor DÁRIO / Ollama (`native`) | `/api/chat` com `tools` (streaming) — testado contra Ollama real (qwen3:0.6b) |
| Ollama (`openai`), OpenAI, Groq, OpenRouter | `/chat/completions` com `tools` (streaming; os pedaços de `tool_calls` são juntados) |
| Gemini | `functionDeclarations` (sem streaming, como o chat normal) |

Se o modelo não suportar ferramentas (ex.: Ollama responde `does not support tools`), o chat continua em
modo simples com um prompt honesto (não promete acesso às contas e sugere outro modelo). Cada resposta
faz no máximo **5 voltas de ferramentas**; depois o modelo tem de responder com o que tem.

Eventos SSE novos em `/api/chat/conversations/{id}/messages/stream` (o cliente antigo ignora-os):
`tool` `{name, label, status}`, `connect` `{connector, name, method, tools_available, reason}`,
`approval` `{id, summary, tool, arguments, status, expires_at, …}`. O que o agente fez fica em
`messages.metadata` (migração `0009`) para os botões reaparecerem ao reabrir a conversa.

**Ainda não há navegador** (Etapa Playwright): o DÁRIO não abre sites nem apps no ecrã do utilizador.
Modelos muito pequenos (ex.: qwen3:0.6b) chamam as ferramentas certas mas às vezes descrevem mal o
resultado; para uso real recomenda-se um modelo ≥ 7B no motor DÁRIO ou um fornecedor na nuvem.

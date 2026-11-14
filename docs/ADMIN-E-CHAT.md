# Administração e Chat com IA

Data: 2026-11-14 · migração `0005_admin_ai_chat`

## O que existe

- **Papel de admin.** Uma conta é admin quando o email está em `ADMIN_EMAILS` (por defeito
  `garenadario3@gmail.com`) **e** o email está confirmado. A promoção acontece no registo, no login,
  na confirmação do email e também no primeiro pedido a uma rota de admin (sessões abertas antes do
  deploy). A migração 0005 promove a conta `garenadario3@gmail.com` se já existir e estiver confirmada.
  A promoção fica na auditoria (`auth.admin_granted`). Nunca se despromove automaticamente: retirar um
  admin é um `update public.users set is_admin = false where email = '...'` feito à mão.
- **/admin** (só admins; os outros são enviados para o Dashboard e a API responde 403):
  estatísticas (utilizadores, conversas, mensagens), lista paginada de utilizadores com pesquisa por
  email e um cartão por fornecedor de IA (colar chave, modelo, ativar, **Testar**, remover chave).
- **/chat** (qualquer utilizador com sessão): conversas na barra lateral (painel no telemóvel),
  mensagens, envio com Enter, estado "A pensar…". A IA recebe as últimas 20 mensagens da conversa.
  Limite: 60 mensagens por utilizador por hora (`CHAT_RATE_LIMIT_PER_HOUR`).

## Como ligar o Gemini (gratuito)

1. Entra em **https://aistudio.google.com** com uma conta Google.
2. Abre **Get API key** (https://aistudio.google.com/app/apikey) → **Create API key** → escolhe ou cria um
   projeto → copia a chave (começa normalmente por `AIza…`). O nível gratuito chega para testes; tem
   limites por minuto e por dia.
3. No CONNECTOR AI, entra com a conta admin e abre **/admin**.
4. No cartão **Google Gemini**: cola a chave em *Chave da API*, deixa o modelo `gemini-flash-latest`
   (ou escreve outro, ex. `gemini-3.6-flash`), liga **Ativo** e carrega em **Guardar**.
5. Carrega em **Testar**: deve aparecer "Ligação OK". Se aparecer `API key not valid`, a chave está
   errada; se aparecer `HTTP 404`, o nome do modelo não existe; `HTTP 429` é limite de quota.
6. Abre **/chat** e escreve uma mensagem.

A chave nunca volta a ser mostrada — só `••••` + os últimos 4 caracteres. Para trocar, cola uma nova
e guarda. OpenAI funciona igual (chave em https://platform.openai.com/api-keys, modelo por defeito
`gpt-4o-mini`).

> Nota sobre modelos: o `gemini-2.0-flash` foi desligado pela Google a 2026-06-01, por isso o modelo
> por defeito é o alias `gemini-flash-latest` (a Google avisa por email 2 semanas antes de mudar o
> modelo por trás). O `gpt-4o-mini` da OpenAI funciona até 2027-01-14 (substituto indicado:
> `gpt-6-luna`); muda o modelo no cartão antes dessa data.

## Escolha do fornecedor

O chat usa o primeiro fornecedor **ativo e com chave** pela ordem `AI_PROVIDER_ORDER`
(por defeito `gemini,openai`). Se esse falhar (rede, quota, chave/modelo inválidos), tenta o seguinte.
Sem nenhum ativo, o chat responde **"Nenhum fornecedor de IA ativo"** (HTTP 503). Se a IA falhar, nada
fica gravado e o texto volta para a caixa para reenviar.

## Segurança

- Chaves cifradas com AES-256-GCM (`security/crypto.py`, `CREDENTIALS_ENCRYPTION_KEY`), ligadas ao
  fornecedor por AAD. Sem essa variável, `/admin` mostra um aviso e recusa guardar chaves (503).
  **Se a `CREDENTIALS_ENCRYPTION_KEY` mudar, as chaves guardadas deixam de decifrar**: volta a colá-las.
- `ai_providers` não tem acesso para o papel `authenticated` (como `encrypted_credentials`).
- `conversations`/`messages` com RLS: cada utilizador só vê o que é seu; uma mensagem só pode
  pertencer a uma conversa do mesmo utilizador (FK composta).
- Auditoria: `admin.ai_provider_updated` (sem a chave), `admin.ai_provider_tested`,
  `chat.conversation_deleted`, `auth.admin_granted`.
- Tokens gastos no chat contam em `usage_metrics` (aparecem no Dashboard).

## API

| Método | Caminho | Notas |
|---|---|---|
| GET | /api/admin/stats | admin |
| GET | /api/admin/users?page=&page_size=&q= | admin, paginado (máx. 100) |
| GET | /api/admin/providers | admin; nunca devolve chaves (`configured`, `key_masked`, `key_last4`) |
| PUT | /api/admin/providers/{gemini\|openai} | `{api_key?, model?, enabled?, clear_key?}` |
| POST | /api/admin/providers/{provider}/test | chamada mínima real ao fornecedor; `{ok, message, reply, latency_ms}` |
| GET | /api/chat/conversations | as do utilizador |
| POST | /api/chat/conversations | `{title?}` → 201 |
| DELETE | /api/chat/conversations/{id} | apaga com as mensagens |
| GET | /api/chat/conversations/{id}/messages | |
| POST | /api/chat/conversations/{id}/messages | `{content}` → `{conversation, user_message, assistant_message}` |

## Variáveis de ambiente novas (todas opcionais)

`ADMIN_EMAILS`, `AI_PROVIDER_ORDER`, `GEMINI_DEFAULT_MODEL`, `OPENAI_DEFAULT_MODEL`,
`AI_REQUEST_TIMEOUT_SECONDS`, `CHAT_RATE_LIMIT_PER_HOUR`, `CHAT_HISTORY_MESSAGES`,
`CHAT_MAX_MESSAGE_CHARS` (ver `.env.example`). Obrigatória para guardar chaves:
`CREDENTIALS_ENCRYPTION_KEY` (já existia; gerar com
`python -c "import os,base64; print(base64.b64encode(os.urandom(32)).decode())"`).

## Ainda não incluído

Respostas em streaming, markdown formatado nas respostas, editar/renomear conversas, gestão de
utilizadores pela UI (suspender, promover), e o agente com ferramentas (Etapa 3).

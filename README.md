# CONNECTOR AI

**One AI. Multiple Platforms. Real Actions.**

SaaS em que o utilizador dá instruções em linguagem natural e um agente de IA executa tarefas
através de APIs e de um navegador controlado, sempre com aprovação humana para ações sensíveis.

**Estado: Etapa 1 de 7 (fundação e autenticação) + Admin e Chat com IA.** Já há um painel de
administração (`/admin`: estatísticas, utilizadores, chaves Gemini/OpenAI cifradas) e um chat com IA
para utilizadores com sessão (`/chat`). Ainda não há agente com ferramentas, navegador nem conectores
funcionais; isso chega nas etapas seguintes. Ver `docs/ARCHITECTURE.md`, `docs/ETAPA-1-RELATORIO.md` e
**`docs/ADMIN-E-CHAT.md`** (inclui como criar uma chave gratuita do Gemini e colá-la em /admin).

## Requisitos

- Python 3.12+
- Node.js 22+ (npm 10+)
- PostgreSQL 15+ e Redis 7+ — a forma mais simples é Docker (`docker compose`)

## Arranque rápido (desenvolvimento)

```bash
# 1. Configuração
cp .env.example .env
#    edita .env: define POSTGRES_PASSWORD (e o mesmo valor em DATABASE_URL) e um JWT_SECRET aleatório:
python -c "import secrets; print(secrets.token_urlsafe(48))"

# 2. PostgreSQL + Redis
docker compose up -d postgres redis

# 3. Backend
cd backend
python -m venv .venv && source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
python ../database/migrate.py up --seed                   # lê DATABASE_URL do ambiente
uvicorn --factory api.main:create_app --reload --port 8000
```

> `migrate.py` lê `DATABASE_URL` do ambiente. Se não o exportaste, faz
> `export $(grep -v '^#' ../.env | xargs)` (Linux/macOS) antes, ou passa-o na linha:
> `DATABASE_URL=postgresql://... python ../database/migrate.py up --seed`.

```bash
# 4. Frontend (noutro terminal)
cd frontend
npm install
npm run dev          # http://localhost:3000
```

Abre http://localhost:3000 → **Create account**. Para ser admin, regista-te com um email de
`ADMIN_EMAILS` (por defeito `garenadario3@gmail.com`) e confirma-o; depois `/admin` → cola a chave do Gemini. Com `EMAIL_BACKEND=console`, o link de
confirmação aparece no terminal da API (procura `verify-email?token=`). Abre-o, entra, e vês o
Dashboard com os números reais da tua conta (zeros numa conta nova).

API: http://localhost:8000/health · http://localhost:8000/ready · http://localhost:8000/docs

## Tudo em Docker

```bash
cp .env.example .env    # e preenche como acima
docker compose --profile app up --build
```

Sobe PostgreSQL, Redis, aplica migrações e seeds, arranca a API (porta 8000) e o frontend (3000).

## Testes

```bash
# backend — usa PostgreSQL e Redis reais; a BD de teste é recriada a cada execução
cd backend
TEST_DATABASE_URL=postgresql://connector:<password>@localhost:5432/connector_ai_test \
TEST_REDIS_URL=redis://localhost:6379/15 \
python -m pytest -q
```

O utilizador de `TEST_DATABASE_URL` precisa de poder criar bases de dados (o utilizador do
docker-compose pode). Por defeito usa `postgresql://postgres@127.0.0.1:5432/connector_ai_test`.

```bash
# frontend
cd frontend
npm test            # testes do cliente (node:test)
npm run typecheck
npm run build
```

## Variáveis importantes

Todas estão documentadas em `.env.example`. A API **recusa arrancar** se a configuração for
inválida, e em `ENVIRONMENT=production` também se for insegura (segredo de exemplo, cookies sem
`Secure`, email em consola, sem chave de cifra, frontend sem https).

`API_URL` (frontend) é lida **no build** do Next: indica para onde `/api/*` é reencaminhado.

## Estrutura

Ver `docs/ARCHITECTURE.md` (secção 2). Pastas das etapas seguintes têm um README a dizer o que vai lá e quando.

## Regras do projeto

- Trabalho por etapas; cada etapa é validada pela Ana antes da seguinte.
- Sem repositório GitHub, deploy ou chaves reais sem autorização explícita.
- Nenhum botão decorativo, nenhum conector apresentado como funcional sem testes, nenhum dado fictício.

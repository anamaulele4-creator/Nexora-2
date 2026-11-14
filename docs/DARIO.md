# DÁRIO — infraestrutura oficial da inteligência da CONNECTOR AI

Dois serviços independentes, mas integrados:

| Serviço | Onde | Responsabilidade |
|---|---|---|
| **CONNECTOR AI** | Render / Vercel (como hoje) | Interface, utilizadores, autenticação, chat, conectores |
| **DÁRIO** | VPS próprio (GPU ou RAM elevada) | Modelos locais (Ollama), DÁRIO API, memória, Model Manager |

A inferência principal corre no nosso servidor. Os fornecedores na nuvem (Gemini, Groq, OpenRouter,
OpenAI) ficam **desligados** por defeito no CONNECTOR AI e só entram se o admin os autorizar.

```text
                 INTERNET
                    |
             CONNECTOR AI (Render/Vercel)
                    |   HTTPS + Bearer token
                 Caddy :443  (HTTPS automático)
                    |
                DÁRIO API :8100 ──── PostgreSQL + pgvector (memória)
                    |                Redis (filas, fase seguinte)
                 Ollama :11434  (só rede interna, nunca exposto)
                    |
                 Modelos (volume /models)
```

O código está em `dario/`:

```
dario/
  api/            DÁRIO API (FastAPI) + testes
  deploy/         docker-compose (CPU e GPU), Caddyfile, .env.example
  scripts/        audit.sh · setup-server.sh · install.sh
```

---

## 1. O que a DÁRIO API faz

* **Espelho autenticado do Ollama** — `/api/chat`, `/api/generate`, `/api/embed`, `/api/tags`, `/api/ps`,
  `/api/show`, `/api/version` e `/v1/chat/completions`, `/v1/models`, `/v1/embeddings`.
  Exigem `Authorization: Bearer <DARIO_API_TOKEN>`. `/api/pull`, `/api/delete`, `/api/create`, `/api/copy`
  exigem o `DARIO_ADMIN_TOKEN`. Tudo o resto responde 404.
  **Consequência:** o CONNECTOR AI liga-se ao DÁRIO **sem mudar código** — o adaptador Ollama da fase 1 fala com a DÁRIO API como se fosse um Ollama.
* **Model Manager** — o modelo virtual **`dario:auto`** aparece na lista de modelos. Quando é usado,
  a API escolhe entre os modelos instalados conforme a tarefa:
  código → `qwen2.5-coder:7b`; raciocínio longo → `qwen3:14b`; geral → `qwen3:8b` (listas configuráveis no `.env`).
  Também há `dario:code`, `dario:reasoning`, `dario:general`. O modelo escolhido vem no cabeçalho `x-dario-model`.
* **Memória semântica** — `POST /dario/v1/memory`, `POST /dario/v1/memory/search`, `DELETE /dario/v1/memory/{namespace}/{id}`.
  Embeddings com `nomic-embed-text` no próprio Ollama; pesquisa por cosseno com índice HNSW.
* **Diagnóstico** — `GET /dario/v1/diagnostics`: CPU, RAM, disco, GPU/VRAM, versão do Ollama, modelos
  instalados e carregados, e o perfil/modelos recomendados para o hardware.
* `GET /health` (público) e `GET /ready` (Ollama + memória).

## 2. Dimensionamento

| Perfil | Recursos | Utilização |
|---|---|---|
| Inicial | 16 GB RAM, 8 vCPU, sem GPU | Modelos pequenos e testes |
| Intermédia | 32 GB RAM, 12–16 vCPU, GPU 16 GB VRAM | Chat, programação e agentes |
| Recomendada | 64 GB RAM, 16–24 vCPU, GPU 24 GB VRAM | Vários modelos e tarefas simultâneas |
| Avançada | 128 GB RAM+, GPU 48 GB VRAM+ | Modelos maiores e maior concorrência |

RAM e VRAM não são equivalentes: com GPU, o modelo tem de caber na **VRAM** (deixa ~15% para o contexto).

### O servidor indicado: 16 GB RAM + 12 GB VRAM

Fica entre o perfil inicial e o intermédio. Orçamento ≈ 10 GB de VRAM:

| Modelo | Tamanho (q4) | Papel |
|---|---|---|
| `qwen3:8b` | ~5,2 GB | Principal (conversa, raciocínio) |
| `qwen2.5-coder:7b` | ~4,7 GB | Programação |
| `nomic-embed-text` | ~0,3 GB | Memória (embeddings) |
| `qwen3:14b` | ~9,3 GB | Raciocínio pesado (cabe sozinho, contexto curto) |
| `llama3.1:8b`, `mistral:7b` | ~4,4–4,9 GB | Alternativas |

Não cabem: modelos de 32B+. Com 16 GB de RAM, mantém `OLLAMA_MAX_LOADED_MODELS=2`.
Confirma sempre com `audit.sh` — estes valores são o alvo, não uma medição do teu VPS.

## 3. Instalação, passo a passo

### 3.1 Auditoria (não altera nada)

```bash
git clone https://github.com/anamaulele4-creator/Nexora-2.git && cd Nexora-2/dario
bash scripts/audit.sh          # relatório
bash scripts/audit.sh --json   # para guardar/partilhar
```

### 3.2 Preparar o servidor (Ubuntu 24.04)

```bash
sudo bash scripts/setup-server.sh
```

Atualiza o sistema, ativa firewall (só SSH, 80, 443), fail2ban, atualizações automáticas, Docker e,
se houver GPU NVIDIA, o driver e o NVIDIA Container Toolkit. Se instalar o driver, **reinicia** e confirma com `nvidia-smi`.

### 3.3 DNS

Cria um registo **A**: `dario.teu-dominio.com → IP do VPS`. O Caddy obtém o certificado HTTPS sozinho.

### 3.4 Configurar e subir

```bash
cd deploy && cp .env.example .env
openssl rand -hex 32   # DARIO_API_TOKEN
openssl rand -hex 32   # DARIO_ADMIN_TOKEN
openssl rand -hex 24   # POSTGRES_PASSWORD
nano .env              # DARIO_DOMAIN, ACME_EMAIL, tokens, password
bash ../scripts/install.sh
```

O `install.sh` deteta a GPU, sobe o stack, descarrega `qwen3:8b`, `qwen2.5-coder:7b` e `nomic-embed-text`
(muda com `DARIO_INITIAL_MODELS="..."`), faz um teste de inferência e verifica a API por HTTPS.

### 3.5 Verificar

```bash
curl https://dario.teu-dominio.com/health
curl -H "Authorization: Bearer $DARIO_API_TOKEN" https://dario.teu-dominio.com/dario/v1/diagnostics
curl -H "Authorization: Bearer $DARIO_API_TOKEN" https://dario.teu-dominio.com/api/chat \
  -d '{"model":"dario:auto","messages":[{"role":"user","content":"Escreve uma função Python que soma uma lista"}],"stream":false}' -i
# o cabeçalho x-dario-model mostra qwen2.5-coder:7b;task=code
curl -i https://dario.teu-dominio.com:11434   # tem de falhar: o Ollama não está exposto
```

## 4. Ligar à CONNECTOR AI

1. **/admin → Ollama / Modelos**: URL `https://dario.teu-dominio.com`, token = `DARIO_API_TOKEN`
   (ou `DARIO_ADMIN_TOKEN` se quiseres descarregar/apagar modelos a partir do /admin). Modo **Nativo**.
2. **Testar ligação** → mostra a versão do Ollama e os modelos, incluindo `dario:auto`.
3. Modelo por defeito: **`dario:auto`**. Liga **Ativo**.
4. Mantém desligados "usar fornecedores na nuvem" se o DÁRIO deve ser o único motor.
5. Reverter: desliga **Ativo** no /admin — o CONNECTOR AI volta à cadeia anterior sem tocar no servidor.

## 5. Segurança

* Só as portas 22, 80 e 443 estão abertas; o Ollama, o PostgreSQL e o Redis vivem numa rede Docker interna sem portas publicadas.
* Sem tokens definidos, a DÁRIO API recusa todos os pedidos (falha fechada). Comparação em tempo constante.
* Tokens só no `deploy/.env` do servidor e no /admin (cifrados com `CREDENTIALS_ENCRYPTION_KEY`). Nunca no Git.
* Roda os tokens: muda no `.env`, `docker compose up -d dario-api`, atualiza no /admin.

## 6. Próximas fases

| Fase | Conteúdo |
|---|---|
| ✅ 2 (esta) | Servidor, Ollama, DÁRIO API, Model Manager, memória, diagnóstico |
| 3 | CONNECTOR AI passa a guardar e consultar memórias por utilizador (`/dario/v1/memory`) |
| 4 | Agentes e workers (Redis), planeamento e execução de ferramentas |
| 5 | Playwright (navegador) e conectores a correr no DÁRIO |
| 6 | Prometheus + Grafana, testes de carga e recuperação de falhas |

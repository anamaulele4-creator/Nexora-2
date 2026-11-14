# Motor local (Ollama) no CONNECTOR AI

Fase 1 do "HARK LOCAL AI ENGINE": o CONNECTOR AI passa a usar um servidor **Ollama**
próprio como fornecedor de IA com **prioridade 1**, gerido a partir de **/admin → Ollama / Modelos**,
e expõe uma **API pública v1** compatível com OpenAI.

> O plano grátis do Render (512 MB de RAM) **não** consegue correr modelos. O Ollama vive num
> servidor à parte (8–16 GB de RAM) e o backend no Render fala com ele por um túnel privado com token.

---

## 1. Como funciona

```
Navegador ──► Next.js (/api/*) ──► API FastAPI (Render) ──► túnel HTTPS + token ──► proxy (Caddy) ──► Ollama :11434 (só localhost)
                                          │
                                          └─► (opcional, se o admin autorizar) Gemini / Groq / OpenRouter / OpenAI
```

* **Configuração**: URL base e token do Ollama são guardados **cifrados** na tabela `local_ai_engine`
  (AES-256-GCM com `CREDENTIALS_ENCRYPTION_KEY`, como as chaves dos fornecedores). O navegador só vê
  uma pista (`https://ollama.exemplo.com`) e os últimos 4 caracteres do token.
* **Encaminhamento automático** (`ai_core/router.py`):
  1. Motor local, se estiver **ativo**, com URL e **modelo por defeito**.
  2. Se o motor local falhar: só passa para a nuvem se o admin ligou **"usar fornecedores na nuvem"**;
     fornecedores **pagos** (OpenAI) exigem ainda o interruptor **"incluindo fornecedores pagos"**.
     Sem autorização, o utilizador recebe o erro real (ex.: `Motor local inacessível`) — nunca uma resposta inventada.
  3. Sem motor local ativo, tudo funciona como antes (cadeia Gemini → Groq → OpenRouter → OpenAI).
* **Escolha explícita** no /chat (seletor de modelo) usa só esse fornecedor/modelo, sem recurso.
* **Modo da API** (no /admin):
  * `Nativo` (recomendado) — `POST /api/chat`. Respeita contexto (`num_ctx`), máx. tokens, temperatura e `think`.
  * `Compatível com OpenAI` — `POST /v1/chat/completions`. Serve para servidores que não são Ollama
    (llama.cpp, vLLM, LM Studio). Atenção: o endpoint `/v1` do Ollama **ignora o contexto** configurado
    (verificado com Ollama 0.40.16).
* **Gestão de modelos** usa a API nativa: `/api/tags`, `/api/ps`, `/api/pull`, `/api/delete`, `/api/show`, `/api/version`.

### Novas tabelas (migração `0007_ollama_local_engine.sql`)

| Tabela | Para quê |
|---|---|
| `local_ai_engine` | Configuração (1 linha): URL/token cifrados, modelo por defeito, temperatura, máx. tokens, contexto, `think`, limite de RAM, regras de recurso, último teste. Só o serviço lhe acede. |
| `api_keys` | Tokens da API v1: **só o hash SHA-256**, prefixo para reconhecer, âmbitos (`models`, `chat`), expiração, revogação. |
| `api_usage_logs` | Um registo por pedido à API v1 (modelo, fornecedor, tokens, latência, estado, streaming). |

`messages.provider` passa a aceitar `ollama`.

---

## 2. Instalar o Ollama num servidor (8–16 GB de RAM)

Qualquer VPS Linux serve (Hetzner, Contabo, OVH, DigitalOcean…). Sem GPU funciona, só é mais lento.

```bash
# Ubuntu/Debian
curl -fsSL https://ollama.com/install.sh | sh        # instala e cria o serviço systemd "ollama"
```

Ajustes recomendados para CPU e pouca RAM:

```bash
sudo systemctl edit ollama
```

```ini
[Service]
# Só escuta em localhost: NUNCA expor a porta 11434 à Internet (o Ollama não tem autenticação).
Environment="OLLAMA_HOST=127.0.0.1:11434"
Environment="OLLAMA_MAX_LOADED_MODELS=1"
Environment="OLLAMA_NUM_PARALLEL=1"
Environment="OLLAMA_KEEP_ALIVE=30m"
Environment="OLLAMA_CONTEXT_LENGTH=4096"
```

```bash
sudo systemctl daemon-reload && sudo systemctl restart ollama
ollama pull qwen3:1.7b        # ou escolhe no /admin depois de ligar o túnel
curl http://127.0.0.1:11434/api/version
```

Firewall: `sudo ufw default deny incoming && sudo ufw allow OpenSSH && sudo ufw enable`
(a porta 11434 fica fechada; o túnel é uma ligação de saída).

### Que modelo para que servidor

O /admin mostra o catálogo com tamanho, RAM estimada, capacidades e licença, e recusa descarregar
modelos acima do **limite de RAM** configurado (a não ser que confirmes).

| RAM do servidor | Sugestões |
|---|---|
| 4 GB | `qwen3:1.7b`, `gemma3:1b`, `llama3.2:1b` |
| 8 GB | `qwen3:4b`, `llama3.2:3b`, `phi4-mini`, `gemma3:4b` (visão) |
| 16 GB | `qwen3:8b`, `llama3.1:8b`, `qwen2.5-coder:7b`, `gemma3:12b` (apertado) |

Medido no sandbox de desenvolvimento (2 vCPU ARM, sem GPU): `qwen3:1.7b` gerou ~35 tokens em ~5 s
(com o modelo já carregado). O primeiro pedido após arrancar demora mais (carregar o modelo).

`think` (raciocínio) fica **desligado** por defeito: em modelos como Qwen3/DeepSeek-R1 o raciocínio
gasta os tokens todos antes da resposta e torna tudo muito mais lento em CPU.

---

## 3. Expor o Ollama de forma privada, com token

O Ollama não tem autenticação. Põe à frente um proxy pequeno que **exige** `Authorization: Bearer <token>`
e só depois liga o túnel a esse proxy. O backend do CONNECTOR AI envia sempre esse cabeçalho quando
o token está definido no /admin.

### 3.1 Proxy com token (Caddy)

```bash
sudo apt install -y caddy
openssl rand -hex 32          # gera o token; guarda-o só no gestor de palavras-passe e no /admin
```

`/etc/caddy/Caddyfile` (o token vem de uma variável de ambiente, não fica no ficheiro):

```caddy
:8080 {
	bind 127.0.0.1
	@autorizado header Authorization "Bearer {$OLLAMA_PROXY_TOKEN}"
	handle @autorizado {
		reverse_proxy 127.0.0.1:11434 {
			# o Ollama em 127.0.0.1 só aceita pedidos com Host local
			header_up Host localhost:11434
			# streaming: entregar cada pedaço logo
			flush_interval -1
		}
	}
	respond "unauthorized" 401
}
```

```bash
sudo systemctl edit caddy      # [Service]  Environment="OLLAMA_PROXY_TOKEN=<o token>"
sudo systemctl restart caddy
curl -i http://127.0.0.1:8080/api/version                                  # 401
curl -H "Authorization: Bearer <o token>" http://127.0.0.1:8080/api/version  # 200
```

### 3.2 Opção A — Cloudflare Tunnel (recomendado para o Render)

Precisa de um domínio na Cloudflare (plano grátis chega).

```bash
# instalar cloudflared: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/
cloudflared tunnel login
cloudflared tunnel create ollama
cloudflared tunnel route dns ollama ollama.teu-dominio.com
```

`~/.cloudflared/config.yml`:

```yaml
tunnel: ollama
credentials-file: /root/.cloudflared/<ID-do-túnel>.json
ingress:
  - hostname: ollama.teu-dominio.com
    service: http://127.0.0.1:8080      # o Caddy (com token), nunca o 11434 diretamente
  - service: http_status:404
```

```bash
sudo cloudflared service install && sudo systemctl enable --now cloudflared
curl -H "Authorization: Bearer <o token>" https://ollama.teu-dominio.com/api/version
```

No /admin: **URL** `https://ollama.teu-dominio.com`, **Token** `<o token>`.

Opcional: proteger ainda o hostname com Cloudflare Access / WAF (ex.: só permitir os IPs de saída do Render).

### 3.3 Opção B — Tailscale

* **Backend também na tailnet** (ex.: backend noutro VPS): instala o Tailscale nos dois e usa
  `http://100.x.y.z:8080` (IP Tailscale do servidor do Ollama). O tráfego nunca sai da rede privada.
  O token continua recomendado.
* **Backend no Render** (que não entra na tailnet sem configuração extra): usa **Tailscale Funnel**
  para publicar o Caddy em HTTPS:

```bash
sudo tailscale up
sudo tailscale funnel --bg 8080           # publica https://<máquina>.<tailnet>.ts.net -> 127.0.0.1:8080
```

No /admin: **URL** `https://<máquina>.<tailnet>.ts.net`, **Token** `<o token>`. Com o Funnel o endereço é
público, por isso o **token é obrigatório**.

---

## 4. Ligar no /admin

1. **/admin → Ollama / Modelos**: cola o URL e o token → **Guardar** → **Testar ligação**
   (mostra versão do Ollama, modelos instalados e uma resposta real curta).
2. **Catálogo**: **Descarregar** um modelo (progresso em direto). Modelos acima do limite de RAM pedem confirmação.
   Se a ligação cair a meio, carrega outra vez em Descarregar: o Ollama retoma as camadas já descarregadas.
   Para modelos grandes também podes usar `ollama pull <modelo>` por SSH.
3. Escolhe o **modelo por defeito**, ajusta temperatura / máx. tokens / contexto e liga **Ativo**.
4. Decide as regras de recurso (nuvem / pagos). Por defeito estão **desligadas**.
5. No **/chat**, o seletor mostra "Automático", os modelos locais e os fornecedores na nuvem ativos.
   Se o motor não estiver configurado aparece o aviso **"Motor local não configurado"**.
   As respostas chegam em streaming e o botão **Parar** interrompe (o texto já recebido fica guardado).

---

## 5. API pública v1 (compatível com OpenAI)

Cria um token em **/admin → API Keys** (mostrado **uma única vez**; na BD fica só o hash). Revoga quando quiseres.

| Método | Rota | Token | Descrição |
|---|---|---|---|
| GET | `/api/v1/health` | não | `{"status":"ok","local_engine":"ok"\|"not_configured"\|"unreachable"}` |
| GET | `/api/v1/models` | `models` | Lista no formato OpenAI (`auto` + modelos instalados no Ollama) |
| GET | `/api/v1/models/status` | `models` | Estado do motor: versão, instalados, em memória, modelo por defeito |
| POST | `/api/v1/chat/completions` | `chat` | Formato OpenAI; `stream: true` devolve SSE com `data: [DONE]` |

`model`: `auto` (regras normais), `local`/`default` (modelo por defeito do Ollama) ou o nome de um modelo
instalado (ex.: `qwen3:1.7b`). `temperature` e `max_tokens` por pedido são respeitados pelo motor local.
Limite por token: `API_V1_RATE_LIMIT_PER_MINUTE` (60/min por defeito).

```bash
curl https://connector-ai-api.onrender.com/api/v1/chat/completions \
  -H "Authorization: Bearer cai_..." -H "Content-Type: application/json" \
  -d '{"model":"auto","stream":true,"messages":[{"role":"user","content":"Olá"}]}'
```

```python
from openai import OpenAI
client = OpenAI(base_url="https://connector-ai-api.onrender.com/api/v1", api_key="cai_...")
for chunk in client.chat.completions.create(model="auto", stream=True,
                                            messages=[{"role": "user", "content": "Olá"}]):
    print(chunk.choices[0].delta.content or "", end="")
```

Erros seguem `{"error": {"code", "message"}}` — por exemplo `local_engine_not_configured` (503,
"Motor local não configurado"), `local_engine_unreachable` (503), `model_not_found` (404),
`invalid_api_key` (401), `insufficient_scope` (403), `rate_limited` (429).

---

## 6. Testes

```bash
cd backend
pytest                                            # unitários + integração (mocks do Ollama, BD e Redis reais)
OLLAMA_E2E_URL=http://127.0.0.1:11434 OLLAMA_E2E_MODEL=qwen3:1.7b pytest -s -k e2e   # contra um Ollama real
```

Atrás de um proxy HTTP acrescenta `NO_PROXY=127.0.0.1,localhost` (o httpx respeita `HTTP_PROXY`).

---

## 7. Variáveis de ambiente novas (sem segredos)

| Variável | Defeito | Notas |
|---|---|---|
| `LOCAL_ENGINE_TIMEOUT_SECONDS` | 180 | tempo máximo por resposta do motor local |
| `LOCAL_ENGINE_PULL_TIMEOUT_SECONDS` | 3600 | tempo máximo de um download de modelo |
| `API_V1_RATE_LIMIT_PER_MINUTE` | 60 | pedidos por token por minuto |

URL e token do Ollama **não** são variáveis de ambiente: definem-se no /admin e ficam cifrados na BD.

---

## 8. Próximas fases (fora desta entrega)

* Memória com embeddings (pgvector + `nomic-embed-text`).
* Documentos (upload, extração, RAG).
* Agent Studio (agentes configuráveis com ferramentas).

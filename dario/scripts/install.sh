#!/usr/bin/env bash
# DÁRIO · sobe o stack e descarrega os modelos iniciais.  Corre na pasta dario/deploy:  bash ../scripts/install.sh
set -euo pipefail
cd "$(dirname "$0")/../deploy"
[ -f .env ] || { cp .env.example .env; echo "Criei deploy/.env — preenche DARIO_DOMAIN, tokens e POSTGRES_PASSWORD e corre outra vez."; exit 1; }
set -a; . ./.env; set +a
for v in DARIO_DOMAIN DARIO_API_TOKEN DARIO_ADMIN_TOKEN POSTGRES_PASSWORD; do
  [ -n "${!v:-}" ] || { echo "Falta $v em deploy/.env (gera tokens com: openssl rand -hex 32)"; exit 1; }
done

files=(-f docker-compose.yml)
if command -v nvidia-smi >/dev/null && nvidia-smi >/dev/null 2>&1; then
  files+=(-f docker-compose.gpu.yml); echo "GPU NVIDIA detetada: Ollama com GPU."
else
  echo "Sem GPU ativa: Ollama em CPU."
fi
docker compose "${files[@]}" up -d --build

echo "À espera do Ollama…"
for _ in $(seq 1 60); do docker compose exec -T ollama ollama list >/dev/null 2>&1 && break; sleep 2; done

MODELS=${DARIO_INITIAL_MODELS:-"qwen3:8b qwen2.5-coder:7b nomic-embed-text"}
for m in $MODELS; do echo "A descarregar $m…"; docker compose exec -T ollama ollama pull "$m"; done

echo "Teste de inferência:"
docker compose exec -T ollama ollama run qwen3:8b "Responde só: DÁRIO operacional." --hidethinking 2>/dev/null \
  || docker compose exec -T ollama ollama run qwen3:8b "Responde só: DÁRIO operacional."
docker compose restart dario-api >/dev/null   # recria a ligação à memória agora que tudo está pronto
sleep 3
echo
echo "Verificação pública:"
curl -fsS "https://${DARIO_DOMAIN}/health" && echo
curl -fsS -H "Authorization: Bearer ${DARIO_API_TOKEN}" "https://${DARIO_DOMAIN}/dario/v1/diagnostics" | jq '{recommendation, installed, ollama_version}'

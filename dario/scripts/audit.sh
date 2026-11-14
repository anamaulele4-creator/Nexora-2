#!/usr/bin/env bash
# DÁRIO · auditoria do VPS. Não altera nada; só lê o hardware e diz que perfil e modelos cabem.
# Uso:  bash audit.sh            (relatório legível)
#       bash audit.sh --json     (para colar no chat / guardar)
set -uo pipefail

os=$(. /etc/os-release 2>/dev/null && echo "${PRETTY_NAME:-desconhecido}")
cpu_model=$(lscpu 2>/dev/null | awk -F: '/Model name/ {gsub(/^ +/,"",$2); print $2; exit}')
vcpus=$(nproc 2>/dev/null || echo 0)
ram_gb=$(awk '/MemTotal/ {printf "%.1f", $2/1024/1024}' /proc/meminfo)
disk_free_gb=$(df -BG --output=avail / | tail -1 | tr -dc '0-9')
disk_type=$(lsblk -dno NAME,ROTA,TYPE 2>/dev/null | awk '$3=="disk" {print ($2=="0" ? "SSD/NVMe" : "HDD"); exit}')
virt=$(systemd-detect-virt 2>/dev/null || echo desconhecido)

gpu_name=""; vram_gb=""; driver=""
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
  IFS=',' read -r gpu_name vram_mb driver < <(nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader,nounits | head -1)
  gpu_name=$(echo "$gpu_name" | xargs); driver=$(echo "$driver" | xargs)
  vram_gb=$(awk -v m="$vram_mb" 'BEGIN {printf "%.1f", m/1024}')
elif lspci 2>/dev/null | grep -qi nvidia; then
  gpu_name="NVIDIA detetada por lspci (driver ainda não instalado)"
fi

# Orçamento para modelos: 85% da VRAM com GPU; metade da RAM sem GPU
if [ -n "$vram_gb" ]; then budget=$(awk -v v="$vram_gb" 'BEGIN {printf "%.1f", v*0.85}'); runs_on=gpu
else budget=$(awk -v r="$ram_gb" 'BEGIN {printf "%.1f", r*0.5}'); runs_on=cpu; fi

fits=""
while read -r name size; do
  awk -v s="$size" -v b="$budget" 'BEGIN {exit !(s<=b)}' && fits="$fits $name"
done <<'MODELS'
qwen3:4b 2.6
mistral:7b 4.4
qwen2.5-coder:7b 4.7
llama3.1:8b 4.9
qwen3:8b 5.2
qwen2.5-coder:14b 9.0
qwen3:14b 9.3
qwen3:32b 20.0
MODELS
fits=$(echo "$fits" | xargs)

warnings=()
awk -v r="$ram_gb" 'BEGIN {exit !(r<15)}' && warnings+=("RAM abaixo de 16 GB: usa modelos de 4B ou menos")
[ "${disk_free_gb:-0}" -lt 60 ] && warnings+=("Menos de 60 GB livres: cada modelo 7–14B ocupa 5–10 GB")
[ -z "$vram_gb" ] && warnings+=("Sem GPU NVIDIA ativa: inferência em CPU (mais lenta)")

if [ "${1:-}" = "--json" ]; then
  printf '{"os":"%s","cpu":"%s","vcpus":%s,"ram_gb":%s,"disk_free_gb":%s,"disk_type":"%s","virtualization":"%s",' \
    "$os" "$cpu_model" "$vcpus" "$ram_gb" "${disk_free_gb:-0}" "${disk_type:-?}" "$virt"
  printf '"gpu":"%s","vram_gb":%s,"driver":"%s","runs_on":"%s","budget_gb":%s,"fits":"%s","warnings":"%s"}\n' \
    "$gpu_name" "${vram_gb:-null}" "$driver" "$runs_on" "$budget" "$fits" "$(IFS='; '; echo "${warnings[*]:-}")"
  exit 0
fi

cat <<REPORT
================= DÁRIO · auditoria do servidor =================
Sistema         $os  ($virt)
Processador     ${cpu_model:-?}  ·  $vcpus vCPU
RAM             $ram_gb GB
Disco livre     ${disk_free_gb:-?} GB  (${disk_type:-?})
GPU             ${gpu_name:-nenhuma}${vram_gb:+  ·  $vram_gb GB VRAM  ·  driver $driver}
-----------------------------------------------------------------
Inferência em   $runs_on  (orçamento ~$budget GB para o modelo)
Modelos que cabem: ${fits:-nenhum dos do catálogo}
REPORT
for w in "${warnings[@]:-}"; do [ -n "$w" ] && echo "AVISO: $w"; done
echo "================================================================="

#!/usr/bin/env bash
# DÁRIO · preparação do VPS (Ubuntu Server 24.04 LTS). Corre como root:  sudo bash setup-server.sh
# Faz: atualizações, firewall (só SSH/80/443), fail2ban, atualizações automáticas, Docker,
#      e — se houver GPU NVIDIA — driver + NVIDIA Container Toolkit.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Corre com sudo"; exit 1; }
. /etc/os-release
[ "${VERSION_ID:-}" = "24.04" ] || echo "AVISO: testado para Ubuntu 24.04; estás em ${PRETTY_NAME:-?}"

export DEBIAN_FRONTEND=noninteractive
apt-get update -y && apt-get upgrade -y
apt-get install -y ca-certificates curl gnupg ufw fail2ban unattended-upgrades pciutils jq git

# Firewall: o Ollama (11434), PostgreSQL e Redis nunca ficam acessíveis de fora
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable
systemctl enable --now fail2ban
dpkg-reconfigure -f noninteractive unattended-upgrades

# Docker (repositório oficial)
if ! command -v docker >/dev/null; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -y
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
# Nota: o Docker publica portas por cima do ufw. Este stack só publica 80/443 (Caddy), de propósito.

# GPU NVIDIA
if lspci | grep -qi nvidia; then
  if ! command -v nvidia-smi >/dev/null || ! nvidia-smi >/dev/null 2>&1; then
    apt-get install -y ubuntu-drivers-common
    ubuntu-drivers install --gpgpu
    NEED_REBOOT=1
  fi
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
    | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
    > /etc/apt/sources.list.d/nvidia-container-toolkit.list
  apt-get update -y && apt-get install -y nvidia-container-toolkit
  nvidia-ctk runtime configure --runtime=docker
  systemctl restart docker
fi

echo "Pronto."
[ "${NEED_REBOOT:-0}" = 1 ] && echo "Driver NVIDIA instalado: reinicia (sudo reboot) e confirma com nvidia-smi antes do install.sh."
exit 0

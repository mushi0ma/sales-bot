#!/usr/bin/env bash
# One-time (idempotent) VM setup. Runs ON the VM as root:  sudo bash /srv/salesbot/deploy/vm/provision.sh <user>
# Docker from Docker's apt repository (no curl|sh), log rotation, swap, the stack directory, the nightly backup.
set -euo pipefail
owner="${1:?usage: provision.sh <login-user>}"
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
export DEBIAN_FRONTEND=noninteractive

if ! command -v docker >/dev/null; then
  apt-get update -qq
  apt-get install -y -qq ca-certificates curl gnupg >/dev/null
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  . /etc/os-release
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null
fi

# Container logs: local, rotated (no message text leaves the VM through logging).
cat > /etc/docker/daemon.json <<'JSON'
{ "log-driver": "json-file", "log-opts": { "max-size": "20m", "max-file": "5" }, "live-restore": true }
JSON
systemctl restart docker

# The login user runs compose without sudo (docker group = root on this VM; only admins log in here).
gpasswd -a "$owner" docker >/dev/null

# Swap: headroom for image builds, not for steady state.
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

install -d -o "$owner" -g "$owner" -m 0750 /srv/salesbot

# Nightly database backup to the bucket (deploy/vm/backup.sh), 03:15 local VM time (UTC).
cat > /etc/systemd/system/salesbot-backup.service <<UNIT
[Unit]
Description=salesbot: dump Postgres databases to Cloud Storage
After=docker.service
[Service]
Type=oneshot
ExecStart=/usr/bin/bash /srv/salesbot/deploy/vm/backup.sh
UNIT
cat > /etc/systemd/system/salesbot-backup.timer <<'UNIT'
[Unit]
Description=salesbot nightly backup
[Timer]
OnCalendar=*-*-* 03:15:00
Persistent=true
[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now salesbot-backup.timer >/dev/null

docker --version; docker compose version
echo "provisioned: docker, swap, /srv/salesbot (owner $owner), salesbot-backup.timer"

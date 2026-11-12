#!/usr/bin/env bash
# Manage the sales-bot test VM: plain QEMU/KVM + passt (no libvirt, no root).
# Usage: infra/vm.sh {create|start|stop|status|ssh|tunnel|wait|snapshot NAME|revert NAME|snapshots|console}
set -euo pipefail

INFRA="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=vm.conf
source "$INFRA/vm.conf"

VM_DIR="$INFRA/vm"
RUN="$VM_DIR/run"
IMG_DIR="$VM_DIR/images"
BASE_IMG="$IMG_DIR/$(basename "$UBUNTU_IMG_URL")"
DISK="$VM_DIR/$VM_NAME.qcow2"
SEED="$VM_DIR/seed.iso"
KEY="$VM_DIR/ssh/id_ed25519"
KNOWN="$VM_DIR/ssh/known_hosts"
PASST_SOCK="$RUN/passt.sock"
PASST_PID="$RUN/passt.pid"
QEMU_PID="$RUN/qemu.pid"
MON="$RUN/monitor.sock"

SSH_OPTS=(-i "$KEY" -p "$SSH_PORT" -o IdentitiesOnly=yes -o UserKnownHostsFile="$KNOWN"
  -o StrictHostKeyChecking=accept-new -o ConnectTimeout=5 -o ServerAliveInterval=30)

die() { echo "vm.sh: $*" >&2; exit 1; }
alive() { [[ -f "$1" ]] && kill -0 "$(cat "$1")" 2>/dev/null; }
running() { alive "$QEMU_PID"; }

ensure_tools() {
  for t in qemu-system-x86_64 qemu-img passt xorriso ssh; do
    command -v "$t" >/dev/null || die "missing '$t' (host packages: qemu-base passt libisoburn)"
  done
  [[ -r /dev/kvm && -w /dev/kvm ]] || die "/dev/kvm is not accessible"
}

render() { # template -> stdout
  local pub egress
  pub="$(cat "$KEY.pub")"
  egress="$(base64 -w0 "$INFRA/guest/egress-fw.sh")"
  sed -e "s|@VM_NAME@|$VM_NAME|g" -e "s|@VM_USER@|$VM_USER|g" -e "s|@SSH_PUBKEY@|$pub|g" \
      -e "s|@EGRESS_B64@|$egress|g" -e "s|@GUEST_DNS@|$GUEST_DNS|g" "$1"
}

cmd_create() {
  ensure_tools
  [[ -e "$DISK" ]] && die "$DISK already exists (delete it yourself to recreate)"
  [[ -f "$BASE_IMG" ]] || die "base image missing: $BASE_IMG (download + verify it first, see docs/SETUP.md)"
  [[ -f "$KEY" ]] || ssh-keygen -q -t ed25519 -N '' -C "$VM_NAME" -f "$KEY"
  mkdir -p "$RUN"
  # Standalone qcow2 (no backing file) so internal snapshots are self-contained.
  qemu-img convert -p -O qcow2 "$BASE_IMG" "$DISK"
  qemu-img resize "$DISK" "$VM_DISK_SIZE"
  local tmp; tmp="$(mktemp -d)"
  render "$INFRA/cloud-init/user-data.tmpl" > "$tmp/user-data"
  render "$INFRA/cloud-init/network-config.tmpl" > "$tmp/network-config"
  printf 'instance-id: %s-1\nlocal-hostname: %s\n' "$VM_NAME" "$VM_NAME" > "$tmp/meta-data"
  xorriso -as mkisofs -quiet -output "$SEED" -volid cidata -joliet -rock \
    "$tmp/user-data" "$tmp/meta-data" "$tmp/network-config"
  rm -rf "$tmp"
  echo "created $DISK ($VM_DISK_SIZE) and $SEED. Next: infra/vm.sh start && infra/vm.sh wait"
}

passt_isolation_flag() {
  # Do not map the gateway address to the host: the guest must not reach host loopback services.
  echo "--no-map-gw"
}

cmd_start() {
  ensure_tools
  running && { echo "already running (pid $(cat "$QEMU_PID"))"; return; }
  [[ -f "$DISK" ]] || die "no disk; run: infra/vm.sh create"
  mkdir -p "$RUN"
  rm -f "$PASST_SOCK" "$MON"
  if ! alive "$PASST_PID"; then
    # shellcheck disable=SC2046
    passt --ipv4-only $(passt_isolation_flag) \
      -t "$SSH_BIND/$SSH_PORT:22" -u none \
      --socket "$PASST_SOCK" --pid "$PASST_PID" --log-file "$RUN/passt.log" --quiet
  fi
  for _ in $(seq 50); do [[ -S "$PASST_SOCK" ]] && break; sleep 0.1; done
  [[ -S "$PASST_SOCK" ]] || die "passt did not create its socket (see $RUN/passt.log)"
  local seed_args=()
  [[ -f "$SEED" ]] && seed_args=(-drive "file=$SEED,if=virtio,media=cdrom,readonly=on")
  qemu-system-x86_64 \
    -name "$VM_NAME" -machine q35,accel=kvm -cpu host \
    -smp "$VM_CPUS" -m "$VM_RAM_MB" \
    -drive "file=$DISK,if=virtio,format=qcow2,discard=unmap,cache=none,aio=threads" \
    "${seed_args[@]}" \
    -netdev "stream,id=n0,server=off,addr.type=unix,addr.path=$PASST_SOCK" \
    -device virtio-net-pci,netdev=n0 \
    -device virtio-rng-pci \
    -display none -serial "file:$RUN/console.log" \
    -monitor "unix:$MON,server,nowait" \
    -pidfile "$QEMU_PID" -daemonize
  echo "started (ssh on $SSH_BIND:$SSH_PORT). Console log: $RUN/console.log"
}

cmd_stop() {
  if running; then
    echo "system_powerdown" | socat - "UNIX-CONNECT:$MON" >/dev/null 2>&1 \
      || ssh "${SSH_OPTS[@]}" "$VM_USER@$SSH_BIND" sudo poweroff >/dev/null 2>&1 || true
    for _ in $(seq 120); do running || break; sleep 1; done
    running && { echo "graceful shutdown timed out; killing qemu"; kill "$(cat "$QEMU_PID")"; sleep 2; }
  fi
  alive "$PASST_PID" && kill "$(cat "$PASST_PID")" || true
  rm -f "$QEMU_PID" "$PASST_PID" "$PASST_SOCK" "$MON"
  echo "stopped"
}

cmd_status() {
  running && echo "qemu: running (pid $(cat "$QEMU_PID"))" || echo "qemu: stopped"
  alive "$PASST_PID" && echo "passt: running (pid $(cat "$PASST_PID"))" || echo "passt: stopped"
}

cmd_ssh() { exec ssh "${SSH_OPTS[@]}" "$VM_USER@$SSH_BIND" "$@"; }

cmd_wait() { # wait until cloud-init finished
  for i in $(seq 180); do
    if ssh "${SSH_OPTS[@]}" -o BatchMode=yes "$VM_USER@$SSH_BIND" test -f /var/lib/sales-bot-ready 2>/dev/null; then
      echo "VM ready"; return 0; fi
    sleep 5; (( i % 12 == 0 )) && echo "…still provisioning ($((i*5))s)"
  done
  die "VM not ready after 15 min (see $RUN/console.log)"
}

cmd_tunnel() { # forward web UIs to host loopback only; Ctrl-C to close
  local fw=()
  for p in $TUNNEL_PORTS; do fw+=(-L "127.0.0.1:$p:127.0.0.1:$p"); done
  echo "tunnel: $(for p in $TUNNEL_PORTS; do printf 'http://localhost:%s ' "$p"; done)(Ctrl-C to close)"
  exec ssh "${SSH_OPTS[@]}" -N -o ExitOnForwardFailure=yes "${fw[@]}" "$VM_USER@$SSH_BIND"
}

need_stopped() { running && die "stop the VM first (infra/vm.sh stop)"; return 0; }
cmd_snapshot() { need_stopped; qemu-img snapshot -c "${1:?name}" "$DISK"; echo "snapshot '$1' created"; }
cmd_revert()   { need_stopped; qemu-img snapshot -a "${1:?name}" "$DISK"; echo "reverted to '$1'"; }
cmd_snapshots(){ qemu-img snapshot -l "$DISK"; }
cmd_console()  { tail -n 50 -f "$RUN/console.log"; }

case "${1:-}" in
  create|start|stop|status|wait|tunnel|snapshots|console) c="$1"; shift; "cmd_$c" "$@" ;;
  ssh) shift; cmd_ssh "$@" ;;
  snapshot|revert) c="$1"; shift; "cmd_$c" "$@" ;;
  *) sed -n '2,3p' "$0"; exit 1 ;;
esac

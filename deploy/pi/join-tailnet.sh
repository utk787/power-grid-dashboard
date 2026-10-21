#!/usr/bin/env bash
# Joins the Raspberry Pi to the power dashboard's private network (Headscale on the Oracle VM),
# so the VM can reach the dashboard on this Pi. setup-vm.sh prints this command with a
# one-time key:
#
#   sudo deploy/pi/join-tailnet.sh https://<headscale domain> <one-time key>
#
# Installs the Tailscale client (official installer, stable channel) if needed and logs in to
# your Headscale server, not Tailscale's. DNS settings on the Pi are left unchanged. Safe to run
# again with a new key, e.g. after the Pi was removed from Headscale.
set -euo pipefail

URL="${1:-}"
KEY="${2:-}"
NAME=power-pi
die() { echo "error: $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run with sudo"
[[ "$URL" =~ ^https://[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]] || die "first argument must be the Headscale URL, e.g. https://hs-greenpark.duckdns.org"
[[ "$KEY" =~ ^[A-Za-z0-9_-]{16,}$ ]] || die "second argument must be the one-time key printed by setup-vm.sh"
curl -fs -m 10 "$URL/health" >/dev/null || die "can't reach $URL/health from the Pi; is setup-vm.sh finished on the VM?"

if ! command -v tailscale >/dev/null; then
  echo "== installing the Tailscale client"
  curl -fsSL https://tailscale.com/install.sh | sh
fi

echo "== joining $URL as $NAME"
# --reset: start from clean settings (if this Pi was on another tailnet, it moves to this one).
# --accept-dns=false: keep the Pi's DNS as it is. --accept-routes=false: no routes from others.
tailscale up --reset --login-server "$URL" --authkey "$KEY" --hostname "$NAME" --accept-dns=false --accept-routes=false
IP="$(tailscale ip -4)"
echo "   joined: $NAME = $IP"

# The earlier SSH-tunnel setup isn't needed any more.
if systemctl cat powersensor-tunnel.service >/dev/null 2>&1; then
  systemctl disable --now powersensor-tunnel.service || true
  rm -f /etc/systemd/system/powersensor-tunnel.service
  systemctl daemon-reload
  echo "   removed the old powersensor-tunnel service"
fi

if curl -fs -m 5 "http://$IP:8080/healthz" | grep -qx ok; then
  echo "   ok: the dashboard answers on $IP:8080, so the VM can reach it"
else
  echo "   WARNING: the dashboard doesn't answer on $IP:8080. Is it running, and is 'host = 0.0.0.0'"
  echo "   under [server] in config.ini? (127.0.0.1 only accepts connections from the Pi itself.)"
fi
echo "Done. Back on the VM, setup-vm.sh will notice the Pi and finish."

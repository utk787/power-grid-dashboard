#!/usr/bin/env bash
# One-time setup of an Oracle Cloud (Ubuntu) VM as the public front door for the dashboard.
#
#   sudo ./setup-vm.sh <domain> "<Pi's tunnel public key>"
#   e.g. sudo ./setup-vm.sh power-greenpark.duckdns.org "ssh-ed25519 AAAA... utkarsh@raspberrypi"
#
# What it does (safe to run again):
#   1. installs Caddy (HTTPS reverse proxy; gets and renews the certificate itself)
#   2. opens ports 80 and 443 in the VM's own firewall (Oracle's Ubuntu images block them)
#   3. creates a 'tunnel' user for the Pi's key
#   4. sshd: that user may ONLY publish the Pi on 127.0.0.1:18080 (ssh -R); outbound forwarding,
#      shell, terminal and agent are refused; dead tunnel sessions are dropped within ~90 s
#   5. installs the Caddyfile for <domain> and the "monitor offline" page, then reloads Caddy
#
# Also needed (done in the Oracle console, not here): ingress rules for TCP 80 and 443 in the
# instance's VCN security list, and a DNS name pointing at the VM. See ADMINISTRATION.md §4.
set -euo pipefail

DOMAIN="${1:-}"
PUBKEY="${2:-}"
HERE="$(cd "$(dirname "$0")" && pwd)"
TUNNEL_PORT=18080

die() { echo "error: $*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die "run with sudo"
[[ "$DOMAIN" =~ ^[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]] || die "first argument must be the site's domain, e.g. power-greenpark.duckdns.org"
[[ "$PUBKEY" =~ ^ssh-(ed25519|rsa)\ [A-Za-z0-9+/=]+ ]] || die "second argument must be the Pi's public key (the line from ~/.ssh/powersensor_tunnel.pub)"
[[ -f "$HERE/Caddyfile" && -f "$HERE/offline.html" ]] || die "run from the deploy/oracle folder (Caddyfile and offline.html must be next to this script)"
command -v apt-get >/dev/null || die "this script expects Ubuntu/Debian"

echo "== 1. Caddy"
if ! command -v caddy >/dev/null; then
  apt-get update -q
  apt-get install -y -q debian-keyring debian-archive-keyring apt-transport-https curl gnupg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -q
  apt-get install -y -q caddy
fi
caddy version

echo "== 2. Firewall: allow 80 and 443"
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow 80,443/tcp
else
  # Oracle's images use iptables with a REJECT rule at the end of INPUT; insert before it.
  for port in 80 443; do
    iptables -C INPUT -p tcp -m state --state NEW --dport "$port" -j ACCEPT 2>/dev/null ||
      iptables -I INPUT 1 -p tcp -m state --state NEW --dport "$port" -j ACCEPT
  done
  if command -v netfilter-persistent >/dev/null; then
    netfilter-persistent save
  else
    echo "   note: netfilter-persistent not found; the rules last until reboot (apt install iptables-persistent)"
  fi
fi

echo "== 3. 'tunnel' user (port forwarding to 127.0.0.1:$TUNNEL_PORT only)"
id tunnel >/dev/null 2>&1 || useradd --create-home --shell /usr/sbin/nologin tunnel
install -d -m 700 -o tunnel -g tunnel /home/tunnel/.ssh
# restrict = no shell, no agent/X11 forwarding; then allow only listening on 127.0.0.1:18080.
printf 'restrict,port-forwarding,permitlisten="127.0.0.1:%s" %s\n' "$TUNNEL_PORT" "$PUBKEY" > /home/tunnel/.ssh/authorized_keys
chown tunnel:tunnel /home/tunnel/.ssh/authorized_keys
chmod 600 /home/tunnel/.ssh/authorized_keys

echo "== 4. sshd: lock the tunnel user down; drop dead sessions within ~90 s"
mkdir -p /etc/ssh/sshd_config.d
cat > /etc/ssh/sshd_config.d/powersensor-tunnel.conf <<EOF
# Added by powersensor setup-vm.sh.
# When the Pi's link dies (power or internet out at home), close its session quickly so the
# forwarded port is free when the Pi reconnects.
ClientAliveInterval 30
ClientAliveCountMax 3

# The tunnel key may only publish the Pi's dashboard on 127.0.0.1:$TUNNEL_PORT (ssh -R). Outbound
# forwarding (ssh -L / -D) is refused, so a stolen key can't use this VM to reach other hosts
# (e.g. the cloud metadata service). No shell, terminal, agent or X11 either.
Match User tunnel
    AllowTcpForwarding remote
    PermitListen 127.0.0.1:$TUNNEL_PORT
    PermitOpen none
    GatewayPorts no
    AllowAgentForwarding no
    X11Forwarding no
    PermitTTY no
    ForceCommand /usr/sbin/nologin
# End the Match block here, so nothing read after this file (e.g. the main sshd_config) is
# applied only to the tunnel user.
Match all
EOF
sshd -t
# Check the effective settings: locked down for 'tunnel', unchanged for everyone else.
eff_tunnel="$(sshd -T -C user=tunnel,host=localhost,addr=127.0.0.1)"
eff_other="$(sshd -T -C user=ubuntu,host=localhost,addr=127.0.0.1)"
grep -qx "allowtcpforwarding remote" <<<"$eff_tunnel" || die "sshd didn't apply the tunnel restrictions (allowtcpforwarding)"
grep -qx "permitopen none" <<<"$eff_tunnel" || die "sshd didn't apply the tunnel restrictions (permitopen)"
grep -qx "permitlisten 127.0.0.1:$TUNNEL_PORT" <<<"$eff_tunnel" || die "sshd didn't apply the tunnel restrictions (permitlisten)"
grep -qx "forcecommand /usr/sbin/nologin" <<<"$eff_other" && die "tunnel restrictions leaked to other users; check /etc/ssh/sshd_config.d"
systemctl reload ssh 2>/dev/null || systemctl reload sshd

echo "== 5. Caddy site for $DOMAIN"
install -d -m 755 /var/www/powersensor
install -m 644 "$HERE/offline.html" /var/www/powersensor/offline.html
sed "s/{{DOMAIN}}/$DOMAIN/" "$HERE/Caddyfile" > /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
systemctl enable --now caddy
systemctl reload caddy

PUBLIC_IP="$(curl -s -m 5 https://ifconfig.me || echo '<this VM'"'"'s public IP>')"
cat <<EOF

Done. Next:
  * DNS: $DOMAIN must point to $PUBLIC_IP (Caddy gets the HTTPS certificate once it does).
  * Oracle console: the VCN security list must allow ingress TCP 80 and 443 from 0.0.0.0/0.
  * On the Pi: install deploy/pi/powersensor-tunnel.service with VM_HOST=$PUBLIC_IP and start it.
  * Then open https://$DOMAIN . Until the Pi connects you'll see the "Status unavailable" page.
EOF

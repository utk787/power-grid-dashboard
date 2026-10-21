#!/usr/bin/env bash
# Sets up an Oracle Cloud (Ubuntu) VM as the public front door for the power dashboard, with a
# private WireGuard network to the Pi coordinated by a self-hosted Headscale server.
#
#   sudo ./setup-vm.sh <site domain> <headscale domain>
#   e.g. sudo ./setup-vm.sh power-greenpark.duckdns.org hs-greenpark.duckdns.org
#
# Both names must already point at this VM (DNS), with TCP 80/443 and UDP 41641 open in the
# Oracle VCN security list. Safe to run again: it upgrades Headscale to the latest stable
# release, re-checks everything, and picks up a Pi that joined (or re-joined) since.
#
# What it does:
#   1. installs Caddy (HTTPS for both names; gets and renews the certificates itself)
#   2. opens TCP 80/443 and UDP 41641 in the VM's own firewall (Oracle's Ubuntu images block them)
#   3. installs the latest stable Headscale release (verified against its published checksums),
#      with config.yaml and the access policy from ./headscale/
#   4. creates the users 'proxy' (this VM) and 'home' (the Pi)
#   5. joins this VM to the private network with the Tailscale client
#   6. prints the command to run on the Pi, waits for the Pi to join, then points Caddy at it
#   7. checks it all: dashboard reachable through the private network, Pi's SSH port refused
#      by the policy, and the public HTTPS site answering
#
# Set HEADSCALE_VERSION=v0.29.5 (for example) to pin a release instead of the latest stable.
set -euo pipefail

SITE="${1:-}"
HS="${2:-}"
HERE="$(cd "$(dirname "$0")" && pwd)"
PI_NAME=power-pi
VM_NAME=power-proxy
WAIT_FOR_PI_MIN="${WAIT_FOR_PI_MIN:-15}"

die() { echo "error: $*" >&2; exit 1; }
say() { echo; echo "== $*"; }
domain_ok() { [[ "$1" =~ ^[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]]; }

[[ $EUID -eq 0 ]] || die "run with sudo"
domain_ok "$SITE" || die "first argument must be the dashboard's domain, e.g. power-greenpark.duckdns.org"
domain_ok "$HS" || die "second argument must be the Headscale domain, e.g. hs-greenpark.duckdns.org"
[[ "$SITE" != "$HS" ]] || die "the dashboard and Headscale need two different names"
for f in Caddyfile offline.html headscale/config.yaml headscale/policy.hujson; do
  [[ -f "$HERE/$f" ]] || die "missing $f: run this from the deploy/oracle folder of the repo"
done
command -v apt-get >/dev/null || die "this script expects Ubuntu/Debian"
ARCH="$(dpkg --print-architecture)"
[[ "$ARCH" == amd64 || "$ARCH" == arm64 ]] || die "unsupported CPU architecture: $ARCH (Headscale ships amd64 and arm64)"

json() { python3 -c "import sys, json; d = json.load(sys.stdin); $1"; }

# ---------------------------------------------------------------------------------------------
say "0. Remove the earlier SSH-tunnel setup, if present"
if id tunnel >/dev/null 2>&1 || [[ -f /etc/ssh/sshd_config.d/powersensor-tunnel.conf ]]; then
  rm -f /etc/ssh/sshd_config.d/powersensor-tunnel.conf
  id tunnel >/dev/null 2>&1 && userdel -r tunnel 2>/dev/null || true
  sshd -t && (systemctl reload ssh 2>/dev/null || systemctl reload sshd)
  echo "   removed the 'tunnel' user and its sshd rules (also run on the Pi: sudo systemctl disable --now powersensor-tunnel)"
else
  echo "   nothing to remove"
fi

# ---------------------------------------------------------------------------------------------
say "1. Caddy"
if ! command -v caddy >/dev/null; then
  apt-get update -q
  apt-get install -y -q debian-keyring debian-archive-keyring apt-transport-https curl gnupg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -q
  apt-get install -y -q caddy
fi
caddy version

# ---------------------------------------------------------------------------------------------
say "2. Firewall: TCP 80, 443 (HTTPS) and UDP 41641 (direct WireGuard from the Pi)"
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow 80,443/tcp
  ufw allow 41641/udp
else
  # Oracle's images use iptables with a REJECT rule at the end of INPUT; insert before it.
  for rule in "tcp 80" "tcp 443" "udp 41641"; do
    read -r proto port <<<"$rule"
    iptables -C INPUT -p "$proto" -m state --state NEW --dport "$port" -j ACCEPT 2>/dev/null ||
      iptables -I INPUT 1 -p "$proto" -m state --state NEW --dport "$port" -j ACCEPT
  done
  if command -v netfilter-persistent >/dev/null; then
    netfilter-persistent save
  else
    echo "   note: netfilter-persistent not found; the rules last until reboot (apt install iptables-persistent)"
  fi
fi

# ---------------------------------------------------------------------------------------------
say "3. Headscale"
VERSION="${HEADSCALE_VERSION:-}"
if [[ -z "$VERSION" ]]; then
  # /releases/latest is the newest stable release (never a beta, release candidate or draft).
  VERSION="$(curl -fsSL https://api.github.com/repos/juanfont/headscale/releases/latest | json 'print(d["tag_name"])')"
fi
[[ "$VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "unexpected Headscale version '$VERSION' (stable releases look like v0.29.5)"
INSTALLED="$(headscale version 2>/dev/null | awk '/^headscale version/ {print $3}' || true)"
if [[ "$INSTALLED" != "$VERSION" ]]; then
  echo "   installing $VERSION (installed: ${INSTALLED:-none})"
  TMP="$(mktemp -d)"
  trap 'rm -rf "$TMP"' EXIT
  DEB="headscale_${VERSION#v}_linux_${ARCH}.deb"
  BASE="https://github.com/juanfont/headscale/releases/download/$VERSION"
  curl -fsSL -o "$TMP/$DEB" "$BASE/$DEB"
  curl -fsSL -o "$TMP/checksums.txt" "$BASE/checksums.txt"
  (cd "$TMP" && grep " $DEB\$" checksums.txt | sha256sum -c --quiet -) || die "checksum mismatch for $DEB; not installing"
  if [[ -f /var/lib/headscale/db.sqlite ]]; then
    backup="/var/lib/headscale/db.sqlite.before-$VERSION"
    systemctl stop headscale 2>/dev/null || true
    cp -a /var/lib/headscale/db.sqlite "$backup"
    echo "   backed up the database to $backup"
  fi
  apt-get install -y -q "$TMP/$DEB"
else
  echo "   $VERSION already installed (latest stable)"
fi
install -d -m 755 /etc/headscale
sed "s/{{HS_DOMAIN}}/$HS/" "$HERE/headscale/config.yaml" > /etc/headscale/config.yaml
install -m 644 "$HERE/headscale/policy.hujson" /etc/headscale/policy.hujson
headscale configtest >/dev/null || die "headscale configtest failed (see the messages above); config.yaml may need updating for $VERSION"
systemctl enable headscale >/dev/null
systemctl restart headscale
for _ in $(seq 30); do curl -fs -m 2 http://127.0.0.1:8081/health >/dev/null && break; sleep 1; done
curl -fs -m 2 http://127.0.0.1:8081/health >/dev/null || die "headscale didn't start: sudo journalctl -u headscale -n 50"
headscale policy check --file /etc/headscale/policy.hujson

# ---------------------------------------------------------------------------------------------
say "4. Users"
for u in proxy home; do
  headscale users list -o json | json "import sys; sys.exit(0 if any(u['name'] == '$u' for u in d or []) else 1)" ||
    headscale users create "$u" >/dev/null
done
user_id() { headscale users list -o json | json "print(next(u['id'] for u in d if u['name'] == '$1'))"; }
node_ip() {  # first IPv4 of the user's node called $2, or nothing
  headscale nodes list -o json | json "
nodes = [n for n in (d or []) if n['user']['name'] == '$1' and (n.get('given_name') or n['name']) == '$2']
ips = [ip for n in nodes for ip in n.get('ip_addresses', []) if '.' in ip]
print(ips[0] if ips else '')"
}

# ---------------------------------------------------------------------------------------------
say "5. Caddy sites (and HTTPS for $HS)"
install -d -m 755 /var/www/powersensor
install -m 644 "$HERE/offline.html" /var/www/powersensor/offline.html
write_caddyfile() {  # $1 = where to send dashboard traffic
  sed -e "s/{{SITE_DOMAIN}}/$SITE/" -e "s/{{HS_DOMAIN}}/$HS/" -e "s/{{PI_UPSTREAM}}/$1/" "$HERE/Caddyfile" > /etc/caddy/Caddyfile
  caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null
  systemctl enable caddy >/dev/null
  systemctl reload-or-restart caddy
}
PI_IP="$(node_ip home "$PI_NAME")"
# Until the Pi joins, point at a closed local port so visitors get the "unavailable" page.
write_caddyfile "${PI_IP:-127.0.0.1:9}${PI_IP:+:8080}"
echo "   waiting for the HTTPS certificate for $HS (needs DNS + ports 80/443 open)..."
for _ in $(seq 60); do curl -fs -m 5 "https://$HS/health" >/dev/null && break; sleep 3; done
curl -fs -m 5 "https://$HS/health" >/dev/null ||
  die "https://$HS isn't answering yet. Check that $HS points at this VM and TCP 80/443 are open in the VCN security list, then run this script again (sudo journalctl -u caddy -n 50 shows details)."
echo "   https://$HS is up"

# ---------------------------------------------------------------------------------------------
say "6. Join this VM to the private network"
command -v tailscale >/dev/null || curl -fsSL https://tailscale.com/install.sh | sh
VM_IP="$(node_ip proxy "$VM_NAME")"
if [[ -n "$VM_IP" ]] && tailscale ip -4 2>/dev/null | grep -qx "$VM_IP"; then
  echo "   already joined as $VM_NAME ($VM_IP)"
else
  key="$(headscale preauthkeys create --user "$(user_id proxy)" --expiration 10m -o json | json 'print(d["key"])')"
  tailscale up --reset --login-server "https://$HS" --authkey "$key" --hostname "$VM_NAME" --accept-dns=false --accept-routes=false
  VM_IP="$(tailscale ip -4)"
  echo "   joined as $VM_NAME ($VM_IP)"
fi

# ---------------------------------------------------------------------------------------------
say "7. The Pi"
PI_IP="$(node_ip home "$PI_NAME")"
if [[ -z "$PI_IP" ]]; then
  key="$(headscale preauthkeys create --user "$(user_id home)" --expiration "${WAIT_FOR_PI_MIN}m" -o json | json 'print(d["key"])')"
  cat <<EOF

   Now run this ON THE PI (in the app folder, after 'git pull'); the key works once, for $WAIT_FOR_PI_MIN minutes:

     sudo deploy/pi/join-tailnet.sh https://$HS $key

   Waiting for the Pi to join... (Ctrl+C is fine: run this script again once the Pi has joined)
EOF
  for _ in $(seq $((WAIT_FOR_PI_MIN * 12))); do
    PI_IP="$(node_ip home "$PI_NAME")"
    [[ -n "$PI_IP" ]] && break
    sleep 5
  done
  [[ -n "$PI_IP" ]] || die "the Pi didn't join within $WAIT_FOR_PI_MIN minutes; run this script again for a new key"
fi
echo "   Pi is $PI_NAME ($PI_IP)"
write_caddyfile "$PI_IP:8080"

# ---------------------------------------------------------------------------------------------
say "8. Checks"
ok=1
if curl -fs -m 10 "http://$PI_IP:8080/healthz" | grep -qx ok; then
  echo "   ok    the VM reaches the dashboard on the Pi ($PI_IP:8080)"
else
  echo "   FAIL  can't reach http://$PI_IP:8080 from the VM: is the app running, and is 'host' in the Pi's config.ini 0.0.0.0?"; ok=0
fi
if timeout 5 bash -c "exec 3<>/dev/tcp/$PI_IP/22" 2>/dev/null; then
  echo "   FAIL  the Pi's SSH port is reachable from the VM: the access policy isn't being applied"; ok=0
else
  echo "   ok    the Pi's SSH port (22) is not reachable from the VM, as the policy intends"
fi
if curl -fs -m 10 "https://$SITE/healthz" | grep -qx ok; then
  echo "   ok    https://$SITE is live"
else
  echo "   FAIL  https://$SITE doesn't answer yet: check its DNS points at this VM, then run this script again"; ok=0
fi

echo
if [[ $ok == 1 ]]; then
  echo "All set: https://$SITE  (Headscale $VERSION at https://$HS)"
else
  echo "Finished with problems (see FAIL above)."
  exit 1
fi

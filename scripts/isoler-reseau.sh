#!/bin/sh
# Empêche les bots d'atteindre ton réseau local (routeur, Proxmox, tes autres VM…) et la machine du panel.
# Ils gardent Internet (Discord) et le DNS.  À lancer en root sur la machine Docker :
#   sudo sh scripts/isoler-reseau.sh
# Pour que ça reste après un redémarrage : voir scripts/botpanel-isolation.service
set -eu

NETWORK="${BOT_NETWORK:-botpanel-bots}"
SUBNET=$(docker network inspect "$NETWORK" -f '{{(index .IPAM.Config 0).Subnet}}' 2>/dev/null || true)
if [ -z "$SUBNET" ]; then
  echo "Réseau Docker « $NETWORK » introuvable : démarre le panel une première fois, puis relance ce script."
  exit 1
fi
echo "Réseau des bots : $SUBNET"

# On enlève d'abord nos anciennes règles (le script peut être relancé)
for chain in DOCKER-USER INPUT; do
  iptables -S "$chain" 2>/dev/null | grep -- "--comment botpanel" | sed "s/^-A /-D /" | while read -r rule; do
    eval iptables $rule
  done
done

# Bots -> réseau local : bloqué, sauf le DNS
for net in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 100.64.0.0/10 169.254.0.0/16; do
  iptables -I DOCKER-USER -s "$SUBNET" -d "$net" -j DROP -m comment --comment botpanel
done
iptables -I DOCKER-USER -s "$SUBNET" -p udp --dport 53 -j ACCEPT -m comment --comment botpanel
iptables -I DOCKER-USER -s "$SUBNET" -p tcp --dport 53 -j ACCEPT -m comment --comment botpanel
# Les bots entre eux : bloqué aussi (chacun dans son coin)
iptables -I DOCKER-USER -s "$SUBNET" -d "$SUBNET" -j DROP -m comment --comment botpanel
# Les réponses des bots qui ont un port web (ex. leur panel ouvert depuis ton PC) passent
iptables -I DOCKER-USER -s "$SUBNET" -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN -m comment --comment botpanel

# Bots -> la machine elle-même (le panel, SSH…) : bloqué (sauf les réponses)
iptables -I INPUT -s "$SUBNET" -j DROP -m comment --comment botpanel
iptables -I INPUT -s "$SUBNET" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT -m comment --comment botpanel

echo "C'est fait. Test : docker run --rm --network $NETWORK busybox wget -T 3 -q -O- http://192.168.2.1 (doit échouer)"

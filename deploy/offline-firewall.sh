#!/bin/sh
# Offline mode at the network level (docs/offline-mode.md): reject every new outbound connection
# from Lectern's network namespace, and apply that again each time the container starts. A
# container's network namespace, and so these rules, is rebuilt on every start: a restart, a
# crash, `docker compose up`, a reboot.
#
#   sudo deploy/offline-firewall.sh CONTAINER [UID]
#
# CONTAINER  whose network namespace Lectern runs in: lectern-tailscale-tailscale-1 for
#            deploy/tailscale/compose.yaml (Lectern shares the sidecar's), lectern for compose.yaml.
# UID        only this user's connections (LECTERN_UID, 1000 by default): needed with the
#            Tailscale sidecar, whose own traffic must still reach Tailscale. Omit to cover every
#            process in the namespace.
#
# Runs until stopped; run it as a service (docs/offline-mode.md has a systemd unit). Answers to
# connections that come in (readers, devices, the reverse proxy) and loopback still work.
set -u
CONTAINER=${1:?usage: offline-firewall.sh CONTAINER [UID]}
UID_MATCH=${2:+meta skuid $2 }

rules() {
	cat <<EOF
table inet lectern_offline {
  chain output {
    type filter hook output priority 0; policy accept;
    ${UID_MATCH}oif "lo" accept
    ${UID_MATCH}ct state established,related accept
    ${UID_MATCH}reject
  }
}
EOF
}

WAITING=0

apply() {
	if ! pid=$(docker inspect -f '{{.State.Pid}}' "$CONTAINER" 2>/dev/null) || [ "${pid:-0}" -le 0 ]; then
		# A typo in the name would otherwise look exactly like a container that is not up yet.
		if [ "$WAITING" = 0 ]; then
			echo "waiting for container $CONTAINER: it does not exist or is not running (check the name with docker ps)" >&2
			WAITING=1
		fi
		return 0
	fi
	WAITING=0
	if nsenter -t "$pid" -n nft list table inet lectern_offline >/dev/null 2>&1; then
		return 0
	fi
	if rules | nsenter -t "$pid" -n nft -f -; then
		echo "offline rules applied to $CONTAINER (pid $pid)"
	else
		echo "could not apply offline rules to $CONTAINER" >&2
	fi
}

# Events from before the first apply too, so a start in between is not missed (apply() skips a
# namespace that already has the rules).
SINCE=$(date +%s)
apply
docker events --since "$SINCE" --filter type=container --filter event=start --filter "container=$CONTAINER" |
	while read -r _; do apply; done

#!/bin/sh
# alice-managed-ssh-v1
# Шаблон: PORT заменяется генератором station-tools.py, а не shell env.
[ "${1:-start}" = start ] || exit 0
DIR=/data/opt/etc/dropbear
[ -s "$DIR/hk_ed25519" ] && [ -s "$DIR/authorized_keys" ] || exit 1
if [ -s /var/run/alice-dropbear.pid ]; then
    read -r alice_pid < /var/run/alice-dropbear.pid
    case "$alice_pid" in
        ''|*[!0-9]*) ;;
        *) if kill -0 "$alice_pid" 2>/dev/null &&
              [ "$(readlink /proc/"$alice_pid"/exe)" = /data/opt/bin/dropbear ]; then
               exit 0
           fi ;;
    esac
fi
exec /data/opt/bin/dropbear -r "$DIR/hk_ed25519" -D "$DIR" \
    -p @PORT@ -s -P /var/run/alice-dropbear.pid \
    </dev/null >>"$DIR/dropbear.log" 2>&1

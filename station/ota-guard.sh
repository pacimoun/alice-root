#!/bin/sh
# alice-root-ota-guard-v1
set -eu
Q=/system/vendor/quasar
BK=/data/opt/backup
case "${1:-status}" in
    status)
        if grep -q -- '--updatesd' "$Q/launch.json"; then
            echo 'launch.json : updatesd ПРИСУТСТВУЕТ'
        else echo 'launch.json : updatesd убран'; fi
        if head -n 2 "$Q/device_ota_update.sh" | grep -q 'disabled by yandex-noupdate'; then
            echo 'applier     : заглушён'
        else echo 'applier     : штатный'; fi
        if [ -s "$BK/ota_script.pub.orig" ] && ! cmp -s "$Q/ota_script.pub" "$BK/ota_script.pub.orig"; then
            echo 'ota key     : подменён'
        else echo 'ota key     : штатный'; fi
        running=0
        for p in /proc/[0-9]*/cmdline; do
            [ "$(readlink "${p%/cmdline}/exe" 2>/dev/null || true)" = "$Q/maind" ] || continue
            if tr '\000' '\n' < "$p" | grep -Fxq -- '--updatesd'; then running=1; fi
        done
        [ "$running" = 0 ] && echo 'maind --updatesd не запущен' || echo 'maind --updatesd РАБОТАЕТ'
        ;;
    restore)
        [ "$(id -u)" = 0 ] || exit 10
        for n in launch.json device_ota_update.sh ota_script.pub; do
            [ -s "$BK/$n.orig" ] && [ ! -L "$BK/$n.orig" ] && [ ! -L "$Q/$n" ] || exit 11
        done
        was_ro=0
        case ",$(awk '$2=="/" {print $4}' /proc/mounts)," in *,ro,*) was_ro=1;; esac
        finish() { sync; if [ "$was_ro" = 1 ]; then mount -o remount,ro /; fi; }
        trap finish EXIT
        trap 'exit 130' HUP INT TERM
        [ "$was_ro" = 0 ] || mount -o remount,rw /
        for n in launch.json device_ota_update.sh ota_script.pub; do
            [ ! -e "$Q/$n.alice-new" ] && [ ! -L "$Q/$n.alice-new" ] || exit 12
            cp -p "$BK/$n.orig" "$Q/$n.alice-new"
            mv "$Q/$n.alice-new" "$Q/$n"
        done
        echo OTA_RESTORED
        ;;
    *) echo 'Use: status | restore; disable: station-tools.py ota-disable on Fedora' >&2; exit 2;;
esac

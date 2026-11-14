#!/bin/sh
# Временный DNS для Mini 2. Запускать вручную на своём OpenWrt.
set -eu
umask 077
STATE=${ALICE_ROUTER_STATE:-/root/.alice-rooting}
DNSMASQ=${ALICE_DNSMASQ_INIT:-/etc/init.d/dnsmasq}
SECTION=dhcp.alice_root_dns
TABLE=alice_root_recovery
MARK=alice-root-managed-v1
NAMES='quasar.yandex.net quasar.yandex.ru scbh.yandex.net'
APPLYING=0
LOCKED=0
UCI_TOUCHED=0
BOOT_FILE=${ALICE_ROUTER_BOOT_FILE:-/proc/sys/kernel/random/boot_id}

fail() { echo "ROUTER_ERROR: $*" >&2; exit 1; }
no_links() {
    p=$1
    while [ "$p" != / ] && [ -n "$p" ]; do
        [ ! -L "$p" ] || fail "symlink: $p"
        p=${p%/*}; [ -n "$p" ] || p=/
    done
}
validate() {
    awk -v ip="$1" 'BEGIN { n=split(ip,a,"."); if(n!=4)exit 1; for(i=1;i<=4;i++)if(a[i]!~/^[0-9]+$/||a[i]>255)exit 1; if(a[1]==0||a[1]==127||a[1]>=224)exit 1 }' || fail 'нужен LAN IPv4 ПК'
    case "$2" in *[!0-9a-fA-F:]*) fail 'нужен один Wi-Fi MAC без пробелов/переносов';; esac
    printf '%s\n' "$2" | grep -Eq '^([[:xdigit:]]{2}:){5}[[:xdigit:]]{2}$' || fail 'нужен Wi-Fi MAC колонки'
}
get() { uci -q get "$SECTION${1:+.$1}" 2>/dev/null; }
has_uci() { get '' >/dev/null; }
table_text() { nft -nn -y list table inet "$TABLE" 2>/dev/null; }
normal() { tr -d '[:space:];'; }
owned_table() {
    current=$(table_text) || return 1
    [ "$(printf '%s' "$current" | normal)" = "$(normal < "$STATE/expected.table")" ]
}
owned_uci() {
    partial=${1:-0}
    [ "$(get '')" = hostrecord ] || return 1
    marker=$(get alice_root_managed || true)
    [ "$marker" = "$MARK" ] || { [ "$partial" = 1 ] && [ -z "$marker" ]; } || return 1
    for item in name ip; do
        actual=$(get "$item" || true)
        case "$item" in name) expected=$NAMES;; ip) expected=$PC_IP;; esac
        [ "$actual" = "$expected" ] || { [ "$partial" = 1 ] && [ -z "$actual" ]; } || return 1
    done
    # Сохранить запись, если пользователь добавил собственные параметры.
    unknown=$(uci -q show "$SECTION" | sed 's/=.*//' | while IFS= read -r key; do
        case "$key" in "$SECTION"|"$SECTION.name"|"$SECTION.ip"|"$SECTION.alice_root_managed") ;;
            *) echo changed;; esac
    done)
    [ -z "$unknown" ]
}
load_state() {
    no_links "$STATE"
    [ -d "$STATE" ] && [ "$(cat "$STATE/owner" 2>/dev/null)" = "$MARK" ] || fail 'нет журнала владения'
    [ "$(stat -c %a "$STATE")" = 700 ] || fail 'каталог журнала должен иметь права 0700'
    for f in owner pc_ip wifi_mac phase expected.table rules.nft; do
        no_links "$STATE/$f"; [ -f "$STATE/$f" ] || fail "неполный журнал: $f"
    done
    PC_IP=$(cat "$STATE/pc_ip"); WIFI_MAC=$(cat "$STATE/wifi_mac")
    validate "$PC_IP" "$WIFI_MAC"
}
check_changes() {
    changes=$(uci changes) || fail 'uci changes не выполнен'
    [ -z "$changes" ] || fail 'сначала сохраните или отмените свои UCI-изменения'
}
remove_state() {
    rm -f "$STATE/owner" "$STATE/pc_ip" "$STATE/wifi_mac" "$STATE/phase" "$STATE/rules.nft" "$STATE/expected.table"
    rmdir "$STATE" || fail 'в журнале есть посторонние файлы; они сохранены'
}
cleanup_owned() {
    phase=$(cat "$STATE/phase")
    partial=0
    [ "$phase" != applying ] || partial=1
    if has_uci; then owned_uci "$partial" || fail 'DNS-запись изменена пользователем; сохраняю её'; fi
    if table_text >/dev/null; then owned_table || fail 'nft-таблица изменена пользователем; сохраняю её'; fi
    # Все конфликты проверены до первого удаления.
    printf 'cleaning\n' > "$STATE/phase"
    reload=0
    if has_uci; then uci delete "$SECTION"; uci commit dhcp; reload=1; fi
    [ "$phase" != cleaning ] || reload=1
    if [ "$reload" = 1 ]; then "$DNSMASQ" restart; fi
    if table_text >/dev/null; then nft delete table inet "$TABLE"; fi
    remove_state
}
finish() {
    code=$?
    trap - EXIT HUP INT TERM
    if [ "$code" != 0 ] && [ "$APPLYING" = 1 ]; then
        # Только незакоммиченные изменения собственной секции.
        if [ "$UCI_TOUCHED" = 1 ]; then uci -q revert "$SECTION" 2>/dev/null || true; fi
        if [ "$UCI_TOUCHED" = 0 ]; then ( remove_state ); elif ( cleanup_owned ); then :; else echo 'ROUTER_RECOVERY_REQUIRED: повторите cleanup после проверки журнала' >&2; fi
    fi
    if [ "$LOCKED" = 1 ] && [ "$(readlink "$STATE.lock" 2>/dev/null || true)" = "$LOCK_VALUE" ]; then rm -f "$STATE.lock"; fi
    exit "$code"
}

COMMAND=${1:-status}
case "$COMMAND" in apply|cleanup|status) ;; *) fail 'usage: apply PC_IP WIFI_MAC | cleanup [PC_IP WIFI_MAC] | status';; esac
[ "$(id -u)" = 0 ] || fail 'выполняйте на роутере от root'
case "$STATE" in /*) ;; *) fail 'журнал должен иметь абсолютный путь';; esac
no_links "$STATE"
if [ "$COMMAND" = status ]; then
    if [ ! -e "$STATE" ]; then echo ROUTER_NOT_CONFIGURED; exit 0; fi
    load_state
    if has_uci && owned_uci && table_text >/dev/null && owned_table; then echo ROUTER_DNS_READY
    else echo ROUTER_DNS_INCOMPLETE; fi
    exit 0
fi
check_changes
if [ "$COMMAND" = cleanup ] && [ ! -e "$STATE" ]; then echo ROUTER_NOT_CONFIGURED; exit 0; fi
# Atomic symlink lock: PID and boot ID are recorded in the same operation.
BOOT=$(cat "$BOOT_FILE")
[ -n "$BOOT" ] || fail 'не удалось получить boot ID'
LOCK_VALUE="$MARK:$BOOT:$$"
while ! ln -s "$LOCK_VALUE" "$STATE.lock" 2>/dev/null; do
    [ -L "$STATE.lock" ] || fail 'чужой lock; файл сохранён'
    held=$(readlink "$STATE.lock")
    prefix=${held%%:*}; tail=${held#*:}; old_boot=${tail%%:*}; pid=${tail#*:}
    [ "$prefix" = "$MARK" ] && [ "$old_boot" != "$tail" ] || fail 'чужой lock; ссылка сохранена'
    case "$pid" in ''|*[!0-9]*|0) fail 'неверный PID в lock';; esac
    if [ "$old_boot" = "$BOOT" ] && kill -0 "$pid" 2>/dev/null; then fail 'другая операция активна'; fi
    [ "$(readlink "$STATE.lock")" = "$held" ] || continue
    rm "$STATE.lock"
done
LOCKED=1
trap finish EXIT
trap 'exit 130' HUP INT TERM
if [ "$COMMAND" = cleanup ]; then
    wanted_ip=${2:-}; wanted_mac=${3:-}
    load_state
    if [ -n "$wanted_ip" ]; then
        validate "$wanted_ip" "$wanted_mac"
        wanted_mac=$(printf '%s' "$wanted_mac" | tr 'A-F' 'a-f')
        [ "$PC_IP" = "$wanted_ip" ] && [ "$WIFI_MAC" = "$wanted_mac" ] || fail 'параметры не совпали с журналом'
    fi
    cleanup_owned
    echo ROUTER_CLEANED
    exit 0
fi
[ "$#" = 3 ] || fail 'usage: apply PC_IP WIFI_MAC'
validate "$2" "$3"
wanted_ip=$2; wanted_mac=$(printf '%s' "$3" | tr 'A-F' 'a-f')
if [ -e "$STATE" ]; then
    load_state
    [ "$PC_IP" = "$wanted_ip" ] && [ "$WIFI_MAC" = "$wanted_mac" ] || fail 'другие параметры; сначала cleanup'
    if [ "$(cat "$STATE/phase")" = applied ] && has_uci && owned_uci && table_text >/dev/null && owned_table; then
        echo ROUTER_ALREADY_APPLIED; exit 0
    fi
    fail 'незавершённая/изменённая настройка; сначала cleanup'
fi
has_uci && fail 'имя DNS-записи уже занято'
table_text >/dev/null && fail 'имя nft-таблицы уже занято'
PC_IP=$wanted_ip; WIFI_MAC=$wanted_mac
mkdir -m 700 "$STATE"
printf '%s\n' "$MARK" > "$STATE/owner"
printf '%s\n' "$PC_IP" > "$STATE/pc_ip"
printf '%s\n' "$WIFI_MAC" > "$STATE/wifi_mac"
printf 'applying\n' > "$STATE/phase"
cat > "$STATE/rules.nft" <<EOF_RULES
create table inet $TABLE { comment "$MARK"; }
add chain inet $TABLE dns_redirect { type nat hook prerouting priority -160; policy accept; }
add rule inet $TABLE dns_redirect ether saddr $WIFI_MAC udp dport 53 redirect to :53
add rule inet $TABLE dns_redirect ether saddr $WIFI_MAC tcp dport 53 redirect to :53
EOF_RULES
cat > "$STATE/expected.table" <<EOF_TABLE
table inet $TABLE {
    comment "$MARK"
    chain dns_redirect {
        type nat hook prerouting priority -160; policy accept;
        ether saddr $WIFI_MAC udp dport 53 redirect to :53
        ether saddr $WIFI_MAC tcp dport 53 redirect to :53
    }
}
EOF_TABLE
APPLYING=1
nft -c -f "$STATE/rules.nft"
UCI_TOUCHED=1
uci set "$SECTION=hostrecord"
uci set "$SECTION.alice_root_managed=$MARK"
uci set "$SECTION.name=$NAMES"
uci set "$SECTION.ip=$PC_IP"
uci commit dhcp
"$DNSMASQ" restart
nft -f "$STATE/rules.nft"
owned_table || fail 'nft-таблица отличается от ожидаемой'
printf 'applied\n' > "$STATE/phase"
APPLYING=0
echo ROUTER_APPLIED

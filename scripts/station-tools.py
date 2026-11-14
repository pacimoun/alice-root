#!/usr/bin/env python3
"""Подготовка Mini 2: проверки, приватный backup и генерация SSH-конфигурации.

prepare устанавливает SSH и отключает OTA через root ADB/SSH; сброс/DNS не меняет.
"""
import argparse
import base64
import struct
import gzip
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
# Явный allowlist программ/оригиналов. Отдельно от browser profiles/accounts.
BACKUP_PATHS = (
    'etc/init.d/S99zdropbear', 'data/opt/bin/dropbear', 'data/opt/bin/dropbearkey',
    'data/opt/etc/dropbear', 'data/opt/yandex-noupdate.sh', 'data/opt/backup',
    'data/opt/alice-voice', 'data/opt/alice-root-preparation', 'system/vendor/quasar/version_full',
)


def regular_file(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError('Нужен обычный файл, не symlink')
    return path


def render_init(port):
    if not 1 <= port <= 65535:
        raise ValueError('Некорректный SSH port')
    return (ROOT / 'station/dropbear-init.sh').read_text().replace('@PORT@', str(port))


def add_key_script(public_key):
    """Идемпотентное добавление отдельного ключа: старый доступ сохраняется.

    Код только генерируется. Выполнить через существующий проверенный канал.
    """
    public_key = public_key.strip()
    if not re.fullmatch(r'ssh-ed25519 [A-Za-z0-9+/]+={0,2}(?: [^\r\n]*)?', public_key):
        raise ValueError('Нужен один public key Ed25519 без опций')
    try:
        blob = base64.b64decode(public_key.split()[1], validate=True)
        expected_prefix = struct.pack('>I', 11) + b'ssh-ed25519' + struct.pack('>I', 32)
        if len(blob) != len(expected_prefix) + 32 or not blob.startswith(expected_prefix):
            raise ValueError('bad blob')
    except (ValueError, IndexError):
        raise ValueError('Неверный public key Ed25519') from None
    key = shlex.quote(public_key)
    return '''#!/bin/sh
set -eu
DIR=/data/opt/etc/dropbear
[ "$(id -u)" = 0 ] || exit 10
[ -d "$DIR" ] && [ ! -L "$DIR" ] || exit 11
[ -f "$DIR/.alice-managed-ssh-v1" ] || exit 12
FILE="$DIR/authorized_keys"
[ -f "$FILE" ] && [ ! -L "$FILE" ] || exit 13
KEY=''' + key + '''
grep -Fqx "$KEY" "$FILE" && { echo KEY_ALREADY_PRESENT; exit 0; }
# Никогда не перетирать первоначальный backup.
[ ! -L "$FILE.before-key-add" ] || exit 14
[ -e "$FILE.before-key-add" ] || { cp -p "$FILE" "$FILE.before-key-add"; chmod 600 "$FILE.before-key-add"; }
[ ! -e "$FILE.alice-new" ] && [ ! -L "$FILE.alice-new" ] || exit 14
umask 077
trap 'rm -f "$FILE.alice-new"' EXIT HUP INT TERM
cp -p "$FILE" "$FILE.alice-new"
printf '\\n%s\\n' "$KEY" >> "$FILE.alice-new"
chmod 600 "$FILE.alice-new"
mv "$FILE.alice-new" "$FILE"
trap - EXIT HUP INT TERM
echo KEY_ADDED_OLD_ACCESS_PRESERVED
'''


def ssh_args(host, port, identity):
    if not re.fullmatch(r'[A-Za-z0-9_.:-]+', host) or host.startswith('-'):
        raise ValueError('Некорректное имя станции')
    if not 1 <= port <= 65535:
        raise ValueError('Некорректный SSH port')
    return ['ssh', '-i', str(regular_file(identity)), '-p', str(port),
            '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes',
            '-o', 'ConnectTimeout=5', '-o', 'StrictHostKeyChecking=yes', 'root@' + host]


def backup_station(host, port, identity, output):
    """Удалённый tar без -z; gzip локально, оба результата проверяются.

    Содержит SSH-ключи станции: хранить только локально. Ничего не извлекает.
    """
    output = Path(output).expanduser()
    if output.exists() or output.is_symlink():
        raise ValueError('Не перезаписываю существующий backup')
    if not output.parent.is_dir() or output.parent.is_symlink():
        raise ValueError('Сначала создайте приватный каталог backup с правами 0700')
    if output.parent.stat().st_mode & 0o077:
        raise ValueError('Каталог backup должен иметь права 0700')
    command = ('set -eu; cd /; set --; for p in ' +
               ' '.join(shlex.quote(p) for p in BACKUP_PATHS) +
               '; do if [ -e "$p" ] || [ -L "$p" ]; then set -- "$@" "$p"; fi; done; '
               '[ -f system/vendor/quasar/version_full ] || exit 12; tar -cf - "$@"')
    args = ssh_args(host, port, identity) + [command]
    fd, temporary = tempfile.mkstemp(prefix='.station-backup-', dir=output.parent)
    os.close(fd)
    temporary = Path(temporary)
    process = None
    try:
        # stderr подавлен: диагностические ошибки не должны печатать личные параметры.
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        with temporary.open('wb') as raw, gzip.GzipFile(fileobj=raw, mode='wb', mtime=0) as zipped:
            total = 0
            # read1 позволяет контролировать общий timeout при оборванном соединении.
            import selectors
            import time
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ)
            deadline = time.monotonic() + 180
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise ValueError('STOP_BACKUP_TIMEOUT')
                block = os.read(process.stdout.fileno(), 1024 * 1024)
                if not block:
                    break
                total += len(block)
                if total > 512 * 1024 * 1024:
                    raise ValueError('STOP_BACKUP_TOO_LARGE')
                zipped.write(block)
            selector.close()
        if process.wait(timeout=5) != 0 or total < 1024:
            raise ValueError('STOP_BACKUP_SSH_OR_TAR_FAILED: backup не сохранён')
        with tarfile.open(temporary, 'r:gz') as archive:
            from pathlib import PurePosixPath
            count = 0
            has_version = False
            for member in archive:
                count += 1
                if count > 100000:
                    raise ValueError('STOP_BACKUP_TOO_MANY_FILES')
                parts = PurePosixPath(member.name)
                if parts.is_absolute() or '..' in parts.parts or not any(member.name == p or member.name.startswith(p + '/') for p in BACKUP_PATHS):
                    raise ValueError('STOP_BACKUP_UNEXPECTED_PATH')
                has_version |= member.name == 'system/vendor/quasar/version_full' and member.isfile()
            if not count or not has_version:
                raise ValueError('STOP_BACKUP_EMPTY_OR_NO_VERSION')
        # O_EXCL не даст заменить backup, созданный параллельно.
        os.link(temporary, output)
        return {'backup': 'SAVED', 'file': str(output), 'private': True}
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait()
            if process.stdout:
                process.stdout.close()
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    init = commands.add_parser('render-ssh-init')
    init.add_argument('--port', type=int, default=2222)
    key = commands.add_parser('key-add-script')
    key.add_argument('--public-key-file', type=Path, required=True)
    backup = commands.add_parser('backup')
    backup.add_argument('--host', required=True)
    backup.add_argument('--port', type=int, default=2222)
    backup.add_argument('--identity', type=Path, required=True)
    backup.add_argument('--output', type=Path, required=True)
    for name in ('inspect','prepare','ota-disable','ota-status','ota-restore'):
        command=commands.add_parser(name)
        command.add_argument('--config',type=Path,default=Path('config.local.toml'))
        command.add_argument('--adb',help='ADB serial из adb devices; без параметра используется SSH из config')
        if name=='prepare':
            command.add_argument('--public-key-file',type=Path,required=True)
            command.add_argument('--ssh-port',type=int,default=2222)
        if name in ('prepare','ota-disable'):command.add_argument('--dry-run',action='store_true')
    recover=commands.add_parser('recover')
    recover.add_argument('--config',type=Path,default=Path('config.local.toml'))
    recover.add_argument('--adb')
    recover.add_argument('--backup',required=True)
    args = parser.parse_args()
    try:
        if args.command == 'render-ssh-init':
            sys.stdout.write(render_init(args.port))
            return 0
        elif args.command == 'key-add-script':
            sys.stdout.write(add_key_script(regular_file(args.public_key_file).read_text()))
            return 0
        elif args.command in ('inspect','prepare','ota-disable','ota-status','ota-restore','recover'):
            from alice_root.config import load
            from alice_root import preparation as prep
            config=load(args.config);connection=prep.Connection(config,args.adb)
            if args.command=='inspect':result=prep.inspect(connection)
            elif args.command=='ota-status':result={**prep.inspect(connection),**prep.ota_status(connection)}
            elif args.command=='ota-restore':result=prep.restore_ota(connection)
            elif args.command=='recover':
                prep.inspect(connection)
                if not re.fullmatch(re.escape(prep.BACKUPS)+r'/tx\.[A-Za-z0-9]+',args.backup):raise ValueError('Неверный backup подготовки')
                connection.run('set -eu; '+prep._safe_commands(args.backup+'/rollback.sh')+'; test -f '+shlex.quote(args.backup+'/rollback.sh')+'; test \"$(cat '+shlex.quote(args.backup+'/state')+')\" = pending; sh '+shlex.quote(args.backup+'/rollback.sh'))
                result={'result':'PREPARATION_RECOVERED'}
            else:
                key=regular_file(args.public_key_file).read_text() if args.command=='prepare' else None
                result=prep.prepare(connection,config['paths']['cache'],key,getattr(args,'ssh_port',2222),args.dry_run,ota_only=args.command=='ota-disable')
        else:
            result = backup_station(args.host, args.port, args.identity.expanduser(), args.output)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, tarfile.TarError, subprocess.SubprocessError) as error:
        # Не включать subprocess.args и private paths в обычную диагностику.
        print('STOP_STATION_OPERATION: ' + (str(error) if isinstance(error, ValueError) else type(error).__name__), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())

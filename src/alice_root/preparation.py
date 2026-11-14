"""Постоянный SSH и блокировка OTA на Mini 2 через уже полученный root ADB/SSH."""
import base64
import io
import json
from pathlib import Path
import re
import shlex
import struct
import subprocess
import tempfile
import uuid
import tarfile

from .artifacts import download
from .transactions import safe
from .diagnostics import FIRMWARE, ssh_args, remote_path_checks

ROOT = Path(__file__).resolve().parents[2]
Q = '/system/vendor/quasar'
ORIGINALS = '/data/opt/backup'
BACKUPS = '/data/opt/alice-root-preparation'
SSH_DIR = '/data/opt/etc/dropbear'
OTA_FILES = ('launch.json', 'device_ota_update.sh', 'ota_script.pub')
SOURCE = 'https://raw.githubusercontent.com/Marker284/yandex-station-binaries/main/bin/'


class Connection:
    def __init__(self, config, adb=None):
        self.config, self.adb = config, adb
        if adb and (adb.startswith('-') or not re.fullmatch(r'[A-Za-z0-9._:-]+', adb)):
            raise ValueError('Некорректный ADB serial')

    @staticmethod
    def _failure(code, output=b''):
        marker=re.search(rb'PREPARATION_BACKUP (/data/opt/alice-root-preparation/tx\.[A-Za-z0-9]+)',output or b'')
        detail='; backup: '+marker[1].decode() if marker else ''
        return ValueError(code+': проверьте доступ/вывод на устройстве'+detail)

    @staticmethod
    def _execute(command, data, timeout):
        stdin={'input':data} if data is not None else {'stdin':subprocess.DEVNULL}
        try:
            return subprocess.run(command,capture_output=True,timeout=timeout,**stdin)
        except subprocess.TimeoutExpired as error:
            raise Connection._failure('STOP_STATION_TIMEOUT',error.stdout) from None

    def _legacy(self, script, data, timeout):
        # exec-out is one-way and always returns client status, not remote status.
        # For legacy adbd upload first, then redirect that file into the command.
        if data is None:
            return self._adb_result(script,None,timeout,legacy=True)
        temporary=self._adb_result('umask 077; mktemp /tmp/alice-root-input.XXXXXX',None,timeout,legacy=True)
        if temporary.returncode:
            raise self._failure('STOP_STATION_COMMAND',temporary.stdout)
        remote=temporary.stdout.decode().strip()
        if not re.fullmatch(r'/tmp/alice-root-input\.[A-Za-z0-9]+',remote):
            raise ValueError('STOP_ADB_TEMP_PATH')
        try:
            with tempfile.TemporaryDirectory(prefix='alice-adb-input-') as local:
                path=Path(local)/'payload';path.write_bytes(data);path.chmod(0o600)
                pushed=self._execute(['adb','-s',self.adb,'push',str(path),remote],None,timeout)
                if pushed.returncode:raise self._failure('STOP_ADB_UPLOAD')
            redirected='sh -c '+shlex.quote(script)+' < '+shlex.quote(remote)
            return self._adb_result(redirected,None,timeout,legacy=True)
        finally:
            # Only the exact mktemp path created by this call; never a broad rm.
            try:self._adb_result('rm -f '+shlex.quote(remote),None,5,legacy=True)
            except (ValueError,OSError):pass

    def _adb_result(self, script, data, timeout, legacy=False):
        token='__ALICE_ADB_'+uuid.uuid4().hex+'__'
        trailer=('\n'+token+':').encode()
        wrapped='sh -c '+shlex.quote(script)+'; alice_status=$?; printf '+shlex.quote('\n'+token+':%s\n')+' "$alice_status"'
        if legacy:
            # adb exec-out escapes these argv itself; prequoting wrapped breaks sh -c.
            command=['adb','-s',self.adb,'exec-out','sh','-c',wrapped]
        else:
            # adb shell joins its arguments without escaping; one complete shell command.
            command=['adb','-s',self.adb,'shell','-T','-e','none',wrapped]
        result=self._execute(command,data,timeout)
        stderr=getattr(result,'stderr',b'') or b''
        if not legacy and result.returncode and b'only supports allocating a pty' in stderr:
            # This error occurs before remote command execution. Never retry an
            # arbitrary failure or timeout that may already have changed files.
            return self._legacy(script,data,timeout)
        if len(result.stdout)>32*1024*1024:raise ValueError('STOP_STATION_OUTPUT_SIZE')
        output,found,status=result.stdout.rpartition(trailer)
        if not found or not re.fullmatch(rb'[0-9]{1,3}\n',status) or int(status)>255:
            raise self._failure('STOP_ADB_STATUS_MISSING',result.stdout)
        # Preserve binary bytes and trailing newlines exactly; strip our frame only.
        result.stdout=output
        result.returncode=result.returncode or int(status)
        return result

    def run(self, script, data=None, timeout=30, check=True):
        result=(self._adb_result(script,data,timeout) if self.adb
                else self._execute(ssh_args(self.config)+[script],data,timeout))
        if check and result.returncode:raise self._failure('STOP_STATION_COMMAND',result.stdout)
        if len(result.stdout)>32*1024*1024:raise ValueError('STOP_STATION_OUTPUT_SIZE')
        return result.stdout

    def read(self, name):
        q = shlex.quote(name)
        # Every ancestor is checked, including absent-path parents.
        checks = remote_path_checks(name)
        result = self.run('set -eu; '+checks+f'; if [ -e {q} ]; then test -f {q}; printf "PRESENT\\n"; cat {q}; else printf "ABSENT\\n"; fi')
        flag, _, data = result.partition(b'\n')
        if flag==b'ABSENT': return None
        if flag!=b'PRESENT': raise ValueError('STOP_STATION_FILE')
        return data

    def mode(self, name):
        q=shlex.quote(name)
        raw=self.run('set -eu; '+_safe_commands(name)+f'; if [ -e {q} ]; then test -f {q}; stat -c %a {q}; else echo ABSENT; fi').strip()
        return None if raw==b'ABSENT' else int(raw,8)


def inspect(connection):
    raw = connection.run('set -eu; id -u; cat /proc/device-tree/compatible; echo; cat /system/vendor/quasar/version_full; echo')
    lines = raw.replace(b'\0',b' ').decode().splitlines()
    if not lines or lines[0]!='0': raise ValueError('Нужен root ADB/SSH')
    if len(lines)<3 or 'yandex,station-mini2' not in lines[1]: raise ValueError('Поддерживается только Станция Мини 2')
    version = lines[2].strip()
    if version!=FIRMWARE: raise ValueError('Сначала установите прошивку '+FIRMWARE)
    return {'model':'yandexmini_2','firmware':version,'root':True}


def without_updatesd(data):
    launch = json.loads(data)
    if not isinstance(launch,dict) or not isinstance(launch.get('nodes'),list): raise ValueError('Не распознана структура launch.json')
    roles = set()
    for node in launch['nodes']:
        if not isinstance(node,dict) or not isinstance(node.get('tasks'),list): raise ValueError('Не распознаны задачи launch.json')
        keep=[]
        for task in node['tasks']:
            if not isinstance(task,dict):raise ValueError('Не распознана задача launch.json')
            args=task.get('arg',[])
            if not isinstance(args,list): raise ValueError('Не распознаны аргументы launch.json')
            if '--updatesd' not in args:
                keep.append(task)
                roles.update(a for a in args if isinstance(a,str))
        node['tasks']=keep
    if not {'--platformd','--mediad'}<=roles: raise ValueError('В launch.json отсутствуют platformd/mediad')
    return (json.dumps(launch,ensure_ascii=False,indent=2)+'\n').encode()


def public_key(text):
    parts=text.strip().split()
    if len(parts)<2 or parts[0]!='ssh-ed25519' or '\n' in text.strip() or '\r' in text:
        raise ValueError('Нужен один Ed25519 public key')
    try: blob=base64.b64decode(parts[1],validate=True)
    except ValueError: raise ValueError('Неверный public key') from None
    prefix=struct.pack('>I',11)+b'ssh-ed25519'+struct.pack('>I',32)
    if not blob.startswith(prefix) or len(blob)!=len(prefix)+32: raise ValueError('Неверный public key')
    return text.strip().encode()


def append_key(existing, key):
    if any(line.split()[:2]==key.split()[:2] for line in existing.splitlines()): return existing
    return existing.rstrip(b'\n')+b'\n'+key+b'\n'


def replacement_key():
    # Private material exists only in process memory and is discarded.
    private=subprocess.run(['openssl','genrsa','2048'],capture_output=True,check=True,timeout=30).stdout
    return subprocess.run(['openssl','rsa','-RSAPublicKey_out'],input=private,capture_output=True,check=True,timeout=15).stdout


def ota_status(connection):
    helper=connection.read('/data/opt/yandex-noupdate.sh')
    if helper:
        text=connection.run('sh /data/opt/yandex-noupdate.sh status').decode(errors='replace')
        protected=all(x in text for x in ('updatesd убран','заглушён','подменён','не запущен'))
        return {'ota_disabled':protected}
    return {'ota_disabled':False}


def ota_payload(connection):
    payload={}
    current={}
    for name in OTA_FILES:
        data=connection.read(Q+'/'+name)
        if not data: raise ValueError('Не найден системный файл '+name)
        current[name]=data
        original=connection.read(ORIGINALS+'/'+name+'.orig')
        if original is None: payload[ORIGINALS+'/'+name+'.orig']=(data,connection.mode(Q+'/'+name))
    payload[Q+'/launch.json']=(without_updatesd(current['launch.json']),0o644)
    payload[Q+'/device_ota_update.sh']=(b'#!/bin/sh\n# disabled by yandex-noupdate (alice-root)\nexit 0\n',0o755)
    payload[Q+'/ota_script.pub']=(replacement_key(),0o644)
    if connection.read('/data/opt/yandex-noupdate.sh') is None:
        payload['/data/opt/yandex-noupdate.sh']=((ROOT/'station/ota-guard.sh').read_bytes(),0o700)
    return payload


def _safe_commands(path):
    return remote_path_checks(path)


def transaction_script(paths, start_ssh=False, new_host_key=False, root='/'):
    """Remote shell transaction; root override is only used by isolated tests."""
    base=root.rstrip('/')
    if any(not p.startswith(('/data/opt/','/etc/init.d/S99zdropbear',Q+'/')) or '..' in Path(p).parts for p in paths):
        raise ValueError('Недопустимый путь подготовки')
    targets=[base+p for p in paths]
    backup=base+BACKUPS
    # Generated rollback has fixed target paths and no account/connection data.
    undo=['#!/bin/sh','set -eu','BK=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)',
          '[ ! -e "$BK/restored" ] || exit 0','saved=$(cat "$BK/root-mode")',
          f'trap \'sync; if [ "$saved" = ro ]; then mount -o remount,ro {shlex.quote(root)}; fi\' EXIT',
          f'[ "$saved" != ro ] || mount -o remount,rw {shlex.quote(root)}']
    if start_ssh:
        pidfile=shlex.quote(base+'/var/run/alice-dropbear.pid')
        undo.extend([f'current=$(cat {pidfile} 2>/dev/null || true)',
            'old=$(cat "$BK/old-ssh-pid")',
            'case "$current" in \'\'|*[!0-9]*) ;; *)',
            f'if [ "$current" != "$old" ] && [ "$(readlink {shlex.quote(base+"/proc")}/"$current"/exe 2>/dev/null || true)" = /data/opt/bin/dropbear ]; then kill "$current" || true; fi;; esac'])
    for i,target in reversed(list(enumerate(targets))):
        q=shlex.quote(target)
        undo.extend([_safe_commands(target),f'if [ -f "$BK/{i}.old" ]; then cp -p "$BK/{i}.old" {q}; else rm -f {q}; fi',f'rm -f {shlex.quote(target+".alice-new")}'])
    undo.append('touch "$BK/restored"')
    script=['set -eu','umask 077','[ "$(id -u)" = 0 ]',
            f'grep -qF yandex,station-mini2 {shlex.quote(base+"/proc/device-tree/compatible")}',
            f'[ "$(cat {shlex.quote(base+Q+"/version_full")})" = {shlex.quote(FIRMWARE)} ]',
            _safe_commands(backup),f'mkdir -p {shlex.quote(backup)}',
            f'BK=$(mktemp -d {shlex.quote(backup+"/tx.XXXXXX")})','mkdir "$BK/stage"',
            'tar -xf - -C "$BK/stage"',f"mode=$(awk '$2==\"/\" {{print $4}}' {shlex.quote(base+'/proc/mounts')})",
            'case ",$mode," in *,ro,*) echo ro > "$BK/root-mode";; *) echo rw > "$BK/root-mode";; esac']
    for i,target in enumerate(targets):
        q=shlex.quote(target)
        script.extend([_safe_commands(target),_safe_commands(target+'.alice-new'),f'[ ! -e {shlex.quote(target+".alice-new")} ]',
                       f'if [ -e {q} ]; then test -f {q}; cp -p {q} "$BK/{i}.old"; fi'])
    if start_ssh:script.append('cat '+shlex.quote(base+'/var/run/alice-dropbear.pid')+' > "$BK/old-ssh-pid" 2>/dev/null || : > "$BK/old-ssh-pid"')
    script.extend(["cat > \"$BK/rollback.sh\" <<'ALICE_ROLLBACK'",'\n'.join(undo),'ALICE_ROLLBACK',
                   'chmod 700 "$BK/rollback.sh"','echo pending > "$BK/state"',
                   'printf "PREPARATION_BACKUP %s\\n" "$BK"',
                   'finish() { code=$?; trap - EXIT; if [ "$code" != 0 ]; then sh "$BK/rollback.sh" || true; fi; sync; if [ "$(cat "$BK/root-mode")" = ro ]; then mount -o remount,ro '+shlex.quote(root)+' || code=1; fi; exit "$code"; }',
                   'trap finish EXIT',"trap 'exit 130' HUP INT TERM",
                   f'[ "$(cat "$BK/root-mode")" != ro ] || mount -o remount,rw {shlex.quote(root)}'])
    if new_host_key:
        key_index=paths.index(SSH_DIR+'/hk_ed25519')
        binary_index=paths.index('/data/opt/bin/dropbearkey')
        script.extend([f'chmod 700 "$BK/stage/{binary_index}"',f'rm "$BK/stage/{key_index}"',
            f'"$BK/stage/{binary_index}" -t ed25519 -f "$BK/stage/{key_index}" >/dev/null 2>&1'])
    for i,target in enumerate(targets):
        q=shlex.quote(target);tmp=shlex.quote(target+'.alice-new')
        script.extend([f'mkdir -p {shlex.quote(str(Path(target).parent))}',f'cp -p "$BK/stage/{i}" {tmp}',f'mv {tmp} {q}'])
    # Stop only the updater process; never maind in general or quasar_launcher.
    script.extend([f'for cmd in {shlex.quote(base+"/proc")}/[0-9]*/cmdline; do',
        f'  [ "$(readlink "${{cmd%/cmdline}}/exe" 2>/dev/null || true)" = {shlex.quote(Q+"/maind")} ] || continue',
        '  if tr \'\\000\' \'\\n\' < "$cmd" | grep -Fxq -- \'--updatesd\'; then pid=${cmd%/cmdline}; kill "${pid##*/}"; fi','done'])
    if start_ssh:
        script.extend([shlex.quote(base+'/etc/init.d/S99zdropbear')+' start',
            f'pid=$(cat {shlex.quote(base+"/var/run/alice-dropbear.pid")})',
            'case "$pid" in \'\'|*[!0-9]*) exit 19;; esac',
            'kill -0 "$pid"',
            f'[ "$(readlink {shlex.quote(base+"/proc")}/"$pid"/exe)" = /data/opt/bin/dropbear ]'])
    script.extend(['echo committed > "$BK/state"','printf "PREPARED %s\\n" "$BK"'])
    return '\n'.join(script)+'\n'


def commit(connection, payload, start_ssh=False, new_host_key=False):
    buffer=io.BytesIO()
    for path in payload:
        if not path.startswith(('/data/opt/','/etc/init.d/S99zdropbear',Q+'/')): raise ValueError('STOP_PREPARATION_PATH')
    with tarfile.open(fileobj=buffer,mode='w') as archive:
        for i,(data,mode) in enumerate(payload.values()):
            info=tarfile.TarInfo(str(i));info.mode=mode;info.size=len(data)
            archive.addfile(info,io.BytesIO(data))
    text=connection.run(transaction_script(list(payload),start_ssh,new_host_key),buffer.getvalue(),timeout=120).decode()
    lines=[s for s in text.splitlines() if s.startswith('PREPARED ')]
    if not lines: raise ValueError('Нет подтверждения подготовки; проверьте backup на колонке')
    return {'result':'PREPARED','backup':lines[-1].split(' ',1)[1],'rebooted':False}


def prepare(connection, cache, key_text=None, ssh_port=2222, dry_run=False, ota_only=False):
    inspect(connection)
    status=ota_status(connection)
    if ota_only and status['ota_disabled']: return {'result':'OTA_ALREADY_DISABLED'}
    if dry_run:
        return {'result':'PREPARATION_PLAN','ssh':not ota_only,'ota':'disable','reboot':False,
                'files':[Q+'/'+n for n in OTA_FILES]+([] if ota_only else ['/etc/init.d/S99zdropbear',SSH_DIR+'/authorized_keys'])}
    payload={} if status['ota_disabled'] else ota_payload(connection)
    new_host_key=False
    if not ota_only:
        if type(ssh_port) is not int or not 1<=ssh_port<=65535: raise ValueError('Неверный SSH port')
        key=public_key(key_text or '')
        init=connection.read('/etc/init.d/S99zdropbear')
        if init and b'# alice-managed-ssh-v1' not in init: raise ValueError('Чужой SSH init; файл сохранён')
        if init:
            old_port=re.search(rb'-p\s+(\d+)',init)
            if not old_port or int(old_port[1])!=ssh_port:
                raise ValueError('Порт существующего SSH не меняется через prepare; сохраните прежний port')
        payload['/etc/init.d/S99zdropbear']=((ROOT/'station/dropbear-init.sh').read_text().replace('@PORT@',str(ssh_port)).encode(),0o755)
        for name in ('dropbear','dropbearkey'):
            existing=connection.read('/data/opt/bin/'+name)
            if existing is not None and init is None: raise ValueError('Чужой SSH binary; файл сохранён')
            if existing is None:
                cached=safe(Path(cache)/'station'/name)
                if not cached.exists():download(SOURCE+name,cached,16*1024*1024)
                binary=cached.read_bytes()
                if len(binary)<20 or binary[:4]!=b'\x7fELF' or int.from_bytes(binary[18:20],'little') not in (40,183):
                    raise ValueError('Нужен ARM/AArch64 ELF binary')
                payload['/data/opt/bin/'+name]=(binary,0o700)
            elif name=='dropbearkey': payload['/data/opt/bin/dropbearkey']=(existing,0o700)
        existing=connection.read(SSH_DIR+'/authorized_keys') or b''
        payload[SSH_DIR+'/authorized_keys']=(append_key(existing,key),0o600)
        payload[SSH_DIR+'/.alice-managed-ssh-v1']=(b'alice-managed-ssh-v1\n',0o600)
        if connection.mode(SSH_DIR+'/hk_ed25519') is None:
            payload[SSH_DIR+'/hk_ed25519']=(b'',0o600);new_host_key=True
    payload={path:value for path,value in payload.items()
             if path==SSH_DIR+'/hk_ed25519' or connection.read(path)!=value[0] or connection.mode(path)!=value[1]}
    if not payload:return {'result':'ALREADY_PREPARED'}
    # Key generation always needs its tool in the staging archive.
    if new_host_key and '/data/opt/bin/dropbearkey' not in payload:
        payload['/data/opt/bin/dropbearkey']=(connection.read('/data/opt/bin/dropbearkey'),0o700)
    return commit(connection,payload,start_ssh=not ota_only,new_host_key=new_host_key)


def restore_ota(connection):
    inspect(connection)
    payload={}
    for name in OTA_FILES:
        original=ORIGINALS+'/'+name+'.orig'
        data=connection.read(original)
        if not data:raise ValueError('Нет исходного backup '+name)
        if name=='launch.json':without_updatesd(data)  # validate core tasks without altering original
        payload[Q+'/'+name]=(data,connection.mode(original))
    result=commit(connection,payload)
    result['result']='OTA_RESTORED'
    return result

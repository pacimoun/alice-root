"""Отдельная доставка bridge на уже rooted станцию; без root/OTA/router.

Не вызывается doctor/install. Перезапуск/запуск моста — отдельная команда.
"""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
from . import artifacts
from .config import station
from .diagnostics import remote, ssh_args, station_report, system_service, remote_path_checks
from .transactions import safe, atomic, read_json, write_json, _private_directory

BASE='/data/opt/alice-voice'
BINARIES=['capture_tap.so','vqe_tap.so','playback_worker.so','packet_playback.so','libopus.so.0','opus-copyright','native_agent.so','tts_gate.so']


def module(path,name):
    spec=importlib.util.spec_from_file_location(name,path)
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    return m


def payload(c):
    root=c['paths']['bridge']; artifacts.package_files(root)
    folder=root/'engines/bridge'
    profile=module(folder/'tts_profile.py','alice_profile')
    focus=module(folder/'audio_focus.py','alice_focus')
    stock=remote(c,'cat /etc/asound.conf').decode()
    if stock.count('pcm "hw:0,1"')!=1:raise ValueError('STOP_STOCK_ALSA')
    launch=json.loads(remote(c,'cat /system/vendor/quasar/launch.json'))
    quasar=json.loads(remote(c,'cat /system/vendor/quasar/quasar.cfg'))
    values={BASE+'/launch.bridge.json':(json.dumps(profile.launcher_profile(launch,BASE,'vqe'),indent=2)+'\n').encode(),
        BASE+'/profile.sh':(folder/'station/profile.sh').read_bytes(),
        BASE+'/quasar.speech.json':(json.dumps(profile.speech_profile(quasar),indent=2)+'\n').encode(),
        BASE+'/tts-asound.conf':profile.ALSA_CONFIG.encode(),
        BASE+'/commands.txt':'1\tоткрой чат\n2\tсоздай новый чат\n3\tзакрой чат\n4\tоткрой предыдущий чат\n'.encode()}
    for name in focus.FOCUS_FILES: values[BASE+'/'+name]=focus.silent_lease()
    for name in BINARIES: values[BASE+'/'+name]=(folder/'build'/name).read_bytes()
    socat=artifacts.fetch('socat',c['paths']['cache'])
    values['/data/opt/entware/bin/socat']=socat.read_bytes()
    return {name:(data,0o700 if name.endswith('.sh') or name.endswith('/socat') else 0o600) for name,data in values.items()}


def allowed(name):
    return name.startswith(BASE+'/') and Path(name).parent==Path(BASE) or name=='/data/opt/entware/bin/socat'


def inspect(c,name):
    if not allowed(name):raise ValueError('STOP_REMOTE_PATH')
    q=shlex.quote(name)
    check=remote_path_checks(name)
    raw=remote(c,'set -eu; '+check+f'; if [ -e {q} ]; then test -f {q}; stat -c %a {q}; cat {q}; else printf "ABSENT\\n"; fi')
    mode,_,data=raw.partition(b'\n')
    if mode==b'ABSENT':return None
    if len(data)>16*1024*1024:raise ValueError('STOP_REMOTE_FILE_LIMIT')
    return data,int(mode,8)


def put(c,name,data,mode):
    if not allowed(name):raise ValueError('STOP_REMOTE_PATH')
    q=shlex.quote(name); tmp=shlex.quote(name+'.alice-new');parent=shlex.quote(str(Path(name).parent))
    check=remote_path_checks(name)+'\n'+remote_path_checks(name+'.alice-new')
    script=f'''set -eu
umask 077
{check}
mkdir -p {parent}
[ ! -e {tmp} ]
trap 'rm -f {name}.alice-new' EXIT
cat > {tmp}
[ "$(wc -c < {tmp})" -eq {len(data)} ]
chmod {mode:o} {tmp}
sync
mv {tmp} {q}
sync
trap - EXIT
'''
    r=subprocess.run(ssh_args(c)+[script],input=data,capture_output=True,timeout=90)
    if r.returncode:raise ValueError('STOP_REMOTE_WRITE')
    if inspect(c,name)!=(data,mode):raise ValueError('STOP_REMOTE_WRITE_VERIFY')


def config_bytes(c):
    s=station(c)
    return (json.dumps({'version':1,'mode':'bridge','host':s['host'],'user':s.get('user','root'),
        'port':s.get('port',2222),'identity':str(Path(s['identity']).expanduser()) if s.get('identity') else None,
        'autostart':False,'led':'rainbow','led_scope':'speaking','listen_led':False,'disable_stun':False,
        'display':'eyes','phrases':{'open':'открой чат','close':'закрой чат','new':'создай новый чат','previous':'открой предыдущий чат'},
        'installed':True,'model':'mini2','vpn':{'enabled':False,'autostart':False,'required':False}},indent=2)+'\n').encode()


def endpoint(c):
    s=station(c)
    return {k:s.get(k) for k in ('host','user','port','identity')}


def deploy(c):
    if system_service('is-active')=='active':raise ValueError('STOP_ACTIVE_BRIDGE')
    report=station_report(c)
    if not report['supported'] or not report['ota_guard_observed']:raise ValueError('STOP_UNSUPPORTED_STATION_OR_GUARD')
    free=remote(c,"df -Pk /data | tail -n 1").split()
    if len(free)<4 or int(free[3])<20000:raise ValueError('STOP_STATION_SPACE')
    marker=c['paths']['state']/'remote-deploy.json'
    if marker.exists():raise ValueError('STOP_ALREADY_DEPLOYED: проверьте status; повторная доставка не нужна')
    config=safe(c['paths']['bridge_state']/'bridge/config.json')
    if config.exists():raise ValueError('STOP_EXISTING_BRIDGE_CONFIG: настройки bridge уже существуют')
    values=payload(c);before={};changes={}
    # Existing files are reused; only newly created files belong to this delivery.
    for name,(data,mode) in values.items():
        current=inspect(c,name)
        if current and (current[0]!=data or current[1] not in ({mode,0o755} if mode==0o700 else {mode})):raise ValueError('STOP_FOREIGN_STATION_FILE: '+name)
        if current is None:changes[name]=(data,mode)
        before[name]=current
    _private_directory(c['paths']['backup'])
    folder=safe(c['paths']['backup']/('station-'+str(time.time_ns())))
    folder.mkdir(parents=True,mode=0o700)
    entries={}
    for i,(name,current) in enumerate(before.items()):
        if current:
            atomic(folder/str(i),current[0]);entries[name]={'blob':str(i),'mode':current[1]}
        else:entries[name]=None
    after={}
    for i,(name,(data,mode)) in enumerate(changes.items()):
        blob='new-'+str(i);atomic(folder/blob,data);after[name]={'blob':blob,'mode':mode}
    atomic(folder/'bridge-config',config_bytes(c))
    journal={'schema':1,'endpoint':endpoint(c),'folder':str(folder),'before':entries,
        'after':after,'config':str(config), 'phase':'prepared'}
    write_json(folder/'remote.json',journal);write_json(marker,journal)
    try:
        for name,(data,mode) in changes.items():put(c,name,data,mode)
        atomic(config,config_bytes(c))
        journal['phase']='committed';write_json(marker,journal);write_json(folder/'remote.json',journal)
    except BaseException:
        rollback(c)
        raise
    return {'result':'DEPLOYED','remote_files_added':len(changes),'started':False,'backup':str(folder)}


def rollback(c):
    if system_service('is-active')=='active':raise ValueError('STOP_ACTIVE_BRIDGE')
    marker=safe(c['paths']['state']/'remote-deploy.json')
    if not marker.exists():return {'result':'NOTHING_TO_ROLLBACK'}
    value=read_json(marker)
    if value.get('endpoint')!=endpoint(c):raise ValueError('STOP_STATION_ENDPOINT_CHANGED')
    folder=safe(value['folder'])
    if folder.parent!=c['paths']['backup']:raise ValueError('STOP_BACKUP_PATH')
    # Whole preflight, including local config, before the first remote change.
    config=safe(value['config'])
    if config!=c['paths']['bridge_state']/'bridge/config.json':raise ValueError('STOP_CONFIG_PATH')
    if config.exists() and config.read_bytes()!=safe(folder/'bridge-config').read_bytes():raise ValueError('STOP_CONFIG_MODIFIED')
    for name,after in value['after'].items():
        current=inspect(c,name)
        if current and (current[0]!=safe(folder/after['blob']).read_bytes() or current[1]!=after['mode']):raise ValueError('STOP_REMOTE_MODIFIED: '+name)
        if value['before'][name] is not None:raise ValueError('STOP_BAD_DEPLOY_JOURNAL')
    for name,after in reversed(list(value['after'].items())):
        # after-map contains only new files; pre-existing files are preserved.
        if inspect(c,name):remote(c,'rm '+shlex.quote(name))
    if config.exists():config.unlink()
    value['phase']='restored';write_json(folder/'remote.json',value)
    marker.unlink()
    return {'result':'STATION_ROLLED_BACK','backup':str(folder)}

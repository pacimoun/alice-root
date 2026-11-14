"""Чтение состояния ПК и станции без запуска браузера и изменения аудио."""
import json
import platform
import shutil
import shlex
import subprocess
from pathlib import Path
from .config import station, sc_paths

FIRMWARE='0.369.1.46.4778262943.20261013.299'


def run(args,timeout=10):
    return subprocess.run(args,stdin=subprocess.DEVNULL,capture_output=True,timeout=timeout)


def ssh_args(c):
    s=station(c)
    args=['ssh','-T','-o','BatchMode=yes','-o','IdentitiesOnly=yes','-o','StrictHostKeyChecking=yes',
          '-o','ConnectTimeout=5','-o','ServerAliveInterval=5','-o','ServerAliveCountMax=2',
          '-p',str(s.get('port',2222)),'-l',s.get('user','root')]
    if s.get('identity'):args+=['-i',str(Path(s['identity']).expanduser())]
    return args+[s['host']]


def remote_path_checks(name):
    path=Path(name)
    if not path.is_absolute() or '..' in path.parts:raise ValueError('STOP_REMOTE_PATH')
    commands=[]
    for item in [path,*list(path.parents)[:-1]]:
        if str(item)=='/data':
            # Stock Mini 2 maps /data to the persistent UBI partition.
            commands.append('if [ -L /data ]; then [ "$(readlink /data)" = /bkupgrade/data ]; test ! -L /bkupgrade; test ! -L /bkupgrade/data; else test -d /data; fi')
        else:commands.append('test ! -L '+shlex.quote(str(item)))
    return '\n'.join(commands)


def remote(c, command):
    r=run(ssh_args(c)+[command],25)
    if r.returncode:raise ValueError('STOP_SSH: проверьте доступ и host key')
    return r.stdout


def station_report(c):
    raw=remote(c,"set -eu; cat /proc/device-tree/compatible; echo; cat /system/vendor/quasar/version_full; echo; sh /data/opt/yandex-noupdate.sh status")
    text=raw.decode(errors='replace')
    numbers=[int(x) for x in remote(c,'od -An -tu1 -N20 /system/vendor/quasar/maind').split()]
    abi=numbers[:6]==[127,69,76,70,1,1] and numbers[18:20]==[40,0]
    expected=c.get('station',{}).get('firmware',FIRMWARE)
    model='yandex,station-mini2' in text
    firmware=expected in text
    return {'ssh':True,'model_supported':model,'firmware_supported':firmware,
            'elf32_arm':abi,'supported':model and firmware and abi,
            'ota_guard_observed':all(x in text for x in ('updatesd убран','заглушён','подменён','не запущен'))}


def system_service(action):
    if not shutil.which('systemctl'):return 'unavailable'
    r=run(['systemctl','--user',action,'alisa-voice-bridge.service'])
    return r.stdout.decode().strip() or 'unavailable'


def local_report(c):
    tools={t:bool(shutil.which(t)) for t in ['ssh','pactl','Xvfb','xauth','systemctl','systemd-analyze']}
    tools['chromium']=bool(shutil.which('chromium') or shutil.which('chromium-browser'))
    p=c['paths']
    result={'platform':platform.system(),'architecture':platform.machine(),'fedora':Path('/etc/fedora-release').exists(),
            'tools':tools,'bridge_service':system_service('is-active'),'bridge_autostart':system_service('is-enabled')}
    if tools['pactl']:
        result['audio_defaults_available']=all(run(['pactl','get-default-'+kind]).returncode==0 for kind in ('sink','source'))
        r=run(['pactl','-f','json','list','sinks'])
        try:
            sinks=json.loads(r.stdout);mac=c.get('station',{}).get('bluetooth_mac','').replace(':','_').upper()
            result['bluetooth_sink_present']=any(mac+'.' in s.get('name','').upper() for s in sinks) if mac else None
        except ValueError:result['bluetooth_sink_present']=None
    result['account_profiles_present']={'soundcloud':sc_paths(c)['profile'].is_dir(),
        'chatgpt':(p['bridge_state']/'bridge/runtime/browser-profile').is_dir()}
    return result

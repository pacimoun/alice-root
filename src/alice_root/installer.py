"""Подготовка кандидата в temp; запись только через ownership Store."""
import ast
import json
import platform
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from . import artifacts
from .config import sc_paths
from .diagnostics import system_service
from .transactions import safe, Store

SOURCE=Path(__file__).resolve().parent
COMPONENTS=('bridge','soundcloud','voice','all')


def service_arg(value):
    value=str(value)
    if any(ord(c)<32 for c in value):raise ValueError('STOP_CONTROL_CHARACTER')
    return '"'+value.replace('\\','\\\\').replace('"','\\"').replace('%','%%')+'"'


def unit(c):
    p=c['paths']; root=p['bridge']; state=p['bridge_state']
    working=str(root).replace('\\',r'\x5c').replace(' ',r'\x20').replace('%','%%')
    if any(ord(ch)<32 for ch in working):raise ValueError('STOP_UNIT_PATH')
    env={'ALISA_HOME':state,'ALICE_ROOT_CONFIG':c['_file'],
         'ALICE_SC_CONTROL_DIR':sc_paths(c)['state'],'ALICE_SC_LAUNCHER':p['bin']/'verity-sc'}
    return ('\n'.join(['# alice-root-managed-unit-v1','[Unit]','Description=Alisa browser voice bridge',
        'After=pipewire-pulse.service','Wants=pipewire-pulse.service','','[Service]','Type=simple',
        'WorkingDirectory='+working,*['Environment='+service_arg(k+'='+str(v)) for k,v in env.items()],
        'ExecStart='+service_arg(sys.executable)+' '+service_arg(root/'alisa.py')+' bridge run',
        'ExecStop='+service_arg(sys.executable)+' '+service_arg(root/'alisa.py')+' bridge stop',
        'Restart=on-failure','RestartSec=15','TimeoutStopSec=45','KillMode=mixed','',
        '[Install]','WantedBy=default.target',''])).encode()


def launcher(script, env):
    return ('#!/bin/sh\n'+''.join('export '+k+'='+shlex.quote(str(v))+'\n' for k,v in env.items())+
            'exec '+shlex.quote(sys.executable)+' '+shlex.quote(str(script))+' "$@"\n').encode()


def verify_unit(data):
    if not shutil.which('systemd-analyze'):raise ValueError('STOP_SYSTEMD_ANALYZE_MISSING')
    with tempfile.TemporaryDirectory(prefix='alice-unit-') as d:
        p=Path(d)/'alisa-voice-bridge.service';p.write_bytes(data)
        r=subprocess.run(['systemd-analyze','--user','--man=no','verify',str(p)],capture_output=True,timeout=20)
        if r.returncode:raise ValueError('STOP_UNIT_VERIFY')


def file_plan(c,component):
    p=c['paths']; paths=[]
    if component in ('bridge','voice','all'):
        paths += [str(p['bridge'])+'/** (файлы релиза и интеграция Fedora/Music)']
        paths += [str(p['bin']/'alisa'),str(p['unit'])]
    if component in ('soundcloud','all'):
        paths += [str(p['data']/'soundcloud/main.py'),str(p['bin']/'verity-sc')]
    return {'component':component,'files':paths,'packages':['chromium','xorg-x11-server-Xvfb','xorg-x11-xauth','pulseaudio-utils','opus','openssh-clients'],
        'package_action':'только показать; DNF отдельно','services_started':[],
        'remote_writes':False,'profiles_preserved':True,'accounts':'интерактивный вход на новом ПК',
        'voice_agent_required':component in ('voice','all')}


def requirements():
    if platform.machine() not in ('x86_64','amd64'):raise ValueError('STOP_UNSUPPORTED_HOST_ARCH')
    missing=[x for x in ['ssh','pactl','Xvfb','xauth','systemd-analyze'] if not shutil.which(x)]
    if not(shutil.which('chromium') or shutil.which('chromium-browser')):missing.append('chromium-browser')
    if missing:raise ValueError('STOP_DEPENDENCIES: '+', '.join(missing))
    if not Path('/etc/fedora-release').exists():raise ValueError('STOP_INSTALL_REQUIRES_FEDORA')


def controller_protocol(data):
    values={}
    for node in ast.parse(data).body:
        if isinstance(node,ast.Assign) and len(node.targets)==1 and isinstance(node.targets[0],ast.Name) and node.targets[0].id in ('VERSION','MARKER','ROUTES'):
            values[node.targets[0].id]=ast.literal_eval(node.value)
    required={'status','play','search','likes','pause','resume','next','previous','stop'}
    if values.get('MARKER')!='verity-soundcloud-control-v1' or not required.issubset(values.get('ROUTES', set())):
        raise ValueError('STOP_CONTROLLER_PROTOCOL')
    return values.get('VERSION')


def require_controller(c,component):
    if component=='all':return controller_protocol((SOURCE/'soundcloud.py').read_bytes())
    if component=='voice':
        target=c['paths']['data']/'soundcloud/main.py'
        st=Store(c['paths']['state'],c['paths']['backup']); owned=st.owned()
        if str(target) not in owned['files']:raise ValueError('STOP_INSTALL_SOUNDCLOUD_FIRST')
        st.check([str(target)],owned)
        return controller_protocol(target.read_bytes())


def candidate(c,component,archive=None,agent=None):
    p=c['paths']; files={}
    require_controller(c,component)
    if component in ('bridge','voice','all'):
        requirements()
        # Never stop/restart a working bridge implicitly.
        if system_service('is-active')=='active':raise ValueError('STOP_ACTIVE_BRIDGE: сначала остановите мост перед установкой')
        archive=Path(archive) if archive else artifacts.fetch('alisa-voice',p['cache'])
        from .voice.integration import integrate_tree
        with tempfile.TemporaryDirectory(prefix='alice-candidate-') as d:
            root=artifacts.unpack(archive,Path(d))
            integrate_tree(root,include_music=component in ('voice','all'),agent_binary=Path(agent) if agent else None)
            for name in artifacts.package_files(root):
                src=safe(root/name)
                files[str(p['bridge']/name)]=(src.read_bytes(),0o700 if src.stat().st_mode&0o111 else 0o600)
        files[str(p['bin']/'alisa')]=(launcher(p['bridge']/'alisa.py',{'ALISA_HOME':p['bridge_state'],
            'ALICE_ROOT_CONFIG':c['_file'],'ALICE_SC_CONTROL_DIR':sc_paths(c)['state'],
            'ALICE_SC_LAUNCHER':p['bin']/'verity-sc'}),0o700)
        data=unit(c);verify_unit(data);files[str(p['unit'])]=(data,0o600)
    if component in ('soundcloud','all'):
        requirements()
        files[str(p['data']/'soundcloud/main.py')]=((SOURCE/'soundcloud.py').read_bytes(),0o600)
        files[str(p['bin']/'verity-sc')]=(launcher(p['data']/'soundcloud/main.py',
            {'ALICE_ROOT_CONFIG':c['_file'],'ALICE_SC_CONTROL_DIR':sc_paths(c)['state']}),0o700)
    return files


def install(c,component,archive=None,agent=None):
    if os.geteuid()==0:raise ValueError('STOP_RUN_WITHOUT_SUDO')
    files=candidate(c,component,archive,agent)
    total=sum(len(b) for b,m in files.values())
    parent=c['paths']['data']
    while not parent.exists():parent=parent.parent
    if shutil.disk_usage(parent).free<total*3+20*1024*1024:raise ValueError('STOP_DISK_SPACE')
    return Store(c['paths']['state'],c['paths']['backup']).install(files)

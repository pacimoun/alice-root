"""CLI установки и управления bridge/SoundCloud."""
import argparse
import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
from .config import load, station
from .diagnostics import local_report, station_report, system_service, run
from .transactions import Store, safe
from . import installer, artifacts


def emit(value):
    print(json.dumps(value,ensure_ascii=False,indent=2))


def store(c):
    return Store(c['paths']['state'],c['paths']['backup'])


def owned_launcher(c,name):
    path=safe(c['paths']['bin']/name); value=store(c).owned()
    store(c).check([str(path)],value)
    if str(path) not in value['files']:raise ValueError('STOP_NOT_OWNED: команда не установлена этим проектом')
    return path


def service(c,action):
    s=store(c); value=s.owned(); unit=str(c['paths']['unit'])
    if unit not in value['files']:raise ValueError('STOP_UNIT_NOT_OWNED: используйте установленный сервис')
    s.check([unit],value)
    if action in ('start','enable'):
        installer.verify_unit(Path(unit).read_bytes())
    if action=='start':
        if system_service('is-active')=='active':return {'result':'ALREADY_RUNNING'}
        if not station_report(c)['supported']:raise ValueError('STOP_STATION_COMPATIBILITY')
        config=c['paths']['bridge_state']/'bridge/config.json'
        if not config.is_file() or not json.loads(config.read_bytes()).get('installed'):raise ValueError('STOP_DEPLOY_FIRST')
    for args in ([['systemctl','--user','daemon-reload']] if action in ('enable','disable') else [])+[['systemctl','--user',action,'alisa-voice-bridge.service']]:
        r=run(args,75)
        if r.returncode:raise ValueError('STOP_SYSTEMD_'+action.upper())
    return {'result':'SERVICE_'+action.upper()}


def guarded_rollback(c,uninstall=False):
    s=store(c)
    bridge_owned=str(c['paths']['unit']) in s.owned()['files']
    if bridge_owned and system_service('is-active')=='active':raise ValueError('STOP_ACTIVE_BRIDGE: сначала service stop')
    if bridge_owned and system_service('is-enabled')=='enabled':raise ValueError('STOP_ENABLED_UNIT: сначала service disable')
    if (c['paths']['state']/'remote-deploy.json').exists():raise ValueError('STOP_ROLLBACK_STATION_FIRST')
    # SoundCloud may run with an owned file mapped in Chromium's Python parent.
    # Require an explicit stop; never kill a foreign controller.
    from .config import sc_paths
    if (sc_paths(c)['state']/'control.sock').exists():raise ValueError('STOP_SOUNDCLOUD: сначала verity-sc stop')
    count=0
    while True:
        result=s.rollback();count+=result['result']=='ROLLED_BACK'
        if not uninstall or result['result']=='NOTHING_TO_ROLLBACK':break
    return {'result':'UNINSTALLED' if uninstall else result['result'],'transactions_restored':count,
            'preserved':['profiles','accounts','backups','station SSH','OTA guard','system packages']}


def interactive(c):
    before={}
    for kind in ('sink','source'):
        r=run(['pactl','get-default-'+kind])
        if r.returncode:raise ValueError('STOP_AUDIO_BASELINE')
        before[kind]=r.stdout.decode().strip()
    try:
        launcher=owned_launcher(c,'alisa')
        # This is the only automatic voice UI probe; requires an explicit flag.
        r=subprocess.run([str(launcher),'bridge','verify'],timeout=180)
        if r.returncode:raise ValueError('STOP_INTERACTIVE_VERIFY')
    finally:
        for kind,value in before.items():
            r=run(['pactl','get-default-'+kind])
            if r.stdout.decode().strip()!=value:
                restored=run(['pactl','set-default-'+kind,value])
                if restored.returncode:raise ValueError('STOP_AUDIO_RESTORE: проверьте исходный default '+kind)
    return {'result':'INTERACTIVE_GPT_VERIFY_PASSED','manual_checklist':'README.md'}


def main(argv=None):
    parser=argparse.ArgumentParser(description='Fedora: bridge и SoundCloud — установка и управление')
    parser.add_argument('--config',type=Path,help='локальный TOML (по умолчанию config.local.toml)')
    sub=parser.add_subparsers(dest='action',required=True)
    for action in ('doctor','status'):
        p=sub.add_parser(action,help='только чтение, без запуска браузера');p.add_argument('--ssh',action='store_true')
    p=sub.add_parser('plan');p.add_argument('--component',choices=installer.COMPONENTS,default='all')
    p=sub.add_parser('fetch');p.add_argument('--artifact',choices=('alisa-voice','socat'),default='alisa-voice')
    p=sub.add_parser('install');p.add_argument('--component',choices=installer.COMPONENTS,required=True)
    p.add_argument('--archive',type=Path);p.add_argument('--agent',type=Path);p.add_argument('--dry-run',action='store_true')
    p=sub.add_parser('deploy',help='явная доставка на уже rooted станцию');p.add_argument('--dry-run',action='store_true')
    sub.add_parser('rollback-station',help='откат только своей доставки, без SSH/OTA guard')
    p=sub.add_parser('service');p.add_argument('operation',choices=('enable','disable','start','stop'))
    p=sub.add_parser('login');p.add_argument('account',choices=('chatgpt','soundcloud'))
    p=sub.add_parser('verify');p.add_argument('--interactive',action='store_true')
    sub.add_parser('backup',help='проверенная локальная копия owned-файлов')
    sub.add_parser('rollback');sub.add_parser('uninstall')
    p=sub.add_parser('recover',help='восстановить прерванную локальную транзакцию');p.add_argument('backup_directory',type=Path)
    args=parser.parse_args(argv)
    try:
        c=load(args.config)
        if args.action in ('doctor','status'):
            result=local_report(c)
            if args.ssh:result['station']=station_report(c)
            result['mode']='read-only';result['result']='DIAGNOSTICS'
        elif args.action=='plan':result=installer.file_plan(c,args.component)
        elif args.action=='fetch':result={'result':'VERIFIED_DOWNLOAD','file':str(artifacts.fetch(args.artifact,c['paths']['cache']))}
        elif args.action=='install':
            if args.dry_run:result=installer.file_plan(c,args.component)
            else:result=installer.install(c,args.component,args.archive,args.agent)
        elif args.action=='deploy':
            if args.dry_run:result={'remote_directory':'/data/opt/alice-voice','additional_file':'/data/opt/entware/bin/socat',
                'preflight':['model','firmware','ELF32 ARM','space','OTA guard','foreign files'],
                'service_started':False,'router_changes':False,'result':'DEPLOY_PLAN'}
            else:
                from .deployment import deploy
                with store(c).lock():result=deploy(c)
        elif args.action=='rollback-station':
            from .deployment import rollback
            with store(c).lock():result=rollback(c)
        elif args.action=='service':result=service(c,args.operation)
        elif args.action=='login':
            name='alisa' if args.account=='chatgpt' else 'verity-sc'
            cmd=['bridge','login'] if name=='alisa' else ['window']
            r=subprocess.run([str(owned_launcher(c,name)),*cmd],timeout=600)
            if r.returncode:raise ValueError('STOP_LOGIN')
            result={'result':'LOGIN_WINDOW_FINISHED','account':args.account}
        elif args.action=='verify':
            if args.interactive:result=interactive(c)
            else:result={'result':'MANUAL_VERIFICATION_REQUIRED','checklist':'README.md','interactive_command':'verify --interactive'}
        elif args.action=='backup':result=store(c).backup_current()
        elif args.action in ('rollback','uninstall'):result=guarded_rollback(c,args.action=='uninstall')
        elif args.action=='recover':
            if system_service('is-active')=='active':raise ValueError('STOP_ACTIVE_BRIDGE')
            result=store(c).recover(args.backup_directory.absolute())
        emit(result);return 0
    except (ValueError,OSError,RuntimeError,subprocess.SubprocessError) as error:
        # Known exceptions contain fixed markers, file paths or dependency names;
        # never print arbitrary remote output or account state.
        print(str(error),file=sys.stderr);return 1
    except KeyboardInterrupt:
        print('Остановлено. Проверьте journal в private backup перед повтором.',file=sys.stderr);return 130


if __name__=='__main__':
    raise SystemExit(main())

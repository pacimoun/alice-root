"""Пошаговая подготовка новой Mini 2. Роутер и физический сброс — вручную."""
import contextlib
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time

from . import firmware, preparation, root_server
from .artifacts import download
from .config import load, station
from .transactions import atomic, safe, write_json, read_json, _private_directory

ROOT=Path(__file__).resolve().parents[2]
KIT_URL='https://raw.githubusercontent.com/krevetkqqq/lite-kit/main/'
KIT_FILES=('scripts/equasar_server.py','scripts/sync_template.json','certs/fullchain.pem','certs/quasar-all.key','LICENSE')
STEPS=('wizard','fetch','check','server','prepare','verify','backup','cleanup','router-script')


def profile(path=None):
    c=load(path);s=station(c);r=c.get('rooting',{})
    if not isinstance(r,dict):raise ValueError('[rooting] должен быть TOML-таблицей')
    def address(value,field):
        try:return root_server.ipv4(value)
        except ValueError:raise ValueError('Заполните '+field+' действительным LAN IPv4') from None
    pc=address(r.get('pc_ip',''),'rooting.pc_ip')
    target=address(s['host'],'station.host');router=address(r.get('router_ip',''),'rooting.router_ip')
    if len({pc,target,router})!=3:raise ValueError('Адреса ПК, колонки и роутера должны различаться')
    import re
    mac=r.get('wifi_mac','')
    if not isinstance(mac,str) or not re.fullmatch(r'(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}',mac):raise ValueError('Заполните rooting.wifi_mac (не Bluetooth MAC)')
    zone=r.get('firewall_zone','')
    if not isinstance(zone,str) or not re.fullmatch(r'[A-Za-z0-9_.-]*',zone):raise ValueError('Неверная rooting.firewall_zone')
    if s.get('user','root')!='root':raise ValueError('Для подготовки требуется station.user=root')
    c.update(pc_ip=pc,station_ip=target,router_ip=router,wifi_mac=mac,zone=zone)
    c['asset_cache']=safe(ROOT/'.cache/rooting')
    c['kit']=c['asset_cache']/'lite-kit'
    c['firmware']=safe(Path(r.get('firmware_file',ROOT/'firmware/yandexmini_2'/(firmware.FIRMWARE+'.zip'))).expanduser().absolute())
    c['work']=safe(c['paths']['state']/'rooting'/target.replace('.','_'))
    c['identity']=safe(Path(s.get('identity','~/.ssh/alice_station_ed25519')).expanduser().absolute())
    c['public']=Path(str(c['identity'])+'.pub');c['serial']=target+':5555'
    c['upstreams']=c['work']/'upstreams.json';c['pid']=c['work']/'server.pid';c['journal']=c['work']/'firewall.json'
    return c


def command(args,**kwargs):
    return subprocess.run([str(x) for x in args],check=True,**kwargs)


def fetch_assets(c):
    firmware.fetch(c['firmware'])
    for name in KIT_FILES:
        path=safe(c['kit']/name)
        if not path.exists():download(KIT_URL+name,path,256*1024)
    for name in ('dropbear','dropbearkey'):
        path=safe(c['asset_cache']/'station'/name)
        if not path.exists():download(preparation.SOURCE+name,path,16*1024*1024)
        data=path.read_bytes()
        if len(data)<20 or data[:4]!=b'\x7fELF' or int.from_bytes(data[18:20],'little') not in (40,183):raise ValueError('Неверный ARM binary '+name)
    return {'result':'ROOTING_FILES_READY','firmware':str(c['firmware']),'kit':str(c['kit'])}


def check(c):
    missing=[name for name in ('adb','ssh','ssh-keygen','openssl','curl','ip','dig') if not shutil.which(name)]
    if missing:raise ValueError('Установите зависимости: sudo dnf install android-tools openssh-clients openssl curl iproute bind-utils; нет '+', '.join(missing))
    interfaces=json.loads(command(['ip','-j','address','show'],capture_output=True,text=True).stdout)
    c['interface']=next((i['ifname'] for i in interfaces if any(a.get('local')==c['pc_ip'] for a in i.get('addr_info',[]))),None)
    if not c['interface']:raise ValueError('rooting.pc_ip не назначен этому ПК')
    _private_directory(c['work'])
    return root_server.run(c['kit'],c['firmware'],c['pc_ip'],c['station_ip'],c['upstreams'],check=True)


def router_script(c):
    _private_directory(c['work'])
    body=(ROOT/'scripts/router-rooting.sh').read_text()
    # One self-contained file to copy to the router. No IP/MAC in tracked files.
    wrapper='#!/bin/sh\nset -- "${1:-apply}" '+shlex.quote(c['pc_ip'])+' '+shlex.quote(c['wifi_mac'])+'\n'
    path=c['work']/'router-rooting.sh'
    atomic(path,(wrapper+body.split('\n',1)[1]).encode(),0o700)
    return path


def _firewall(args,check=True):
    return subprocess.run(['sudo','firewall-cmd',*args],capture_output=True,text=True,check=check,timeout=30)


def cleanup_firewall(c):
    if not c['journal'].exists():return
    owned=read_json(c['journal'])
    for entry in list(owned):
        if time.time()>=entry.get('expires_at',0):
            # The timed rule has expired; an identical current rule may be somebody else's.
            owned.remove(entry);write_json(c['journal'],owned);continue
        zone,rule=entry['zone'],entry['rule']
        result=_firewall(['--zone='+zone,'--query-rich-rule='+rule],check=False)
        if result.returncode==0:_firewall(['--zone='+zone,'--remove-rich-rule='+rule])
        elif result.returncode!=1:raise ValueError('Не удалось проверить firewalld; журнал сохранён')
        owned.remove(entry);write_json(c['journal'],owned)
    c['journal'].unlink()


def open_firewall(c):
    if not shutil.which('firewall-cmd'):return
    state=subprocess.run(['firewall-cmd','--state'],capture_output=True,text=True,timeout=10)
    if state.returncode: return  # Never start or disable firewalld on behalf of the user.
    cleanup_firewall(c)
    zone=c['zone'] or _firewall(['--get-zone-of-interface='+c['interface']]).stdout.strip()
    if not zone or zone=='no zone':raise ValueError('Укажите rooting.firewall_zone для LAN-интерфейса')
    owned=[]
    for port in (80,443):
        rule=f'rule family=ipv4 source address={c["station_ip"]} port port={port} protocol=tcp accept'
        query=_firewall(['--zone='+zone,'--query-rich-rule='+rule],check=False)
        if query.returncode==0:continue
        if query.returncode!=1:raise ValueError('Не удалось проверить правило firewalld')
        # Record intent first: cleanup must also work after power loss.
        owned.append({'zone':zone,'rule':rule,'expires_at':time.time()+3600});write_json(c['journal'],owned)
        _firewall(['--zone='+zone,'--add-rich-rule='+rule,'--timeout=3600'])


def owns_server(c,pid):
    if pid<=1 or pid==os.getpid():return False
    try:args=Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
    except FileNotFoundError:return False
    return os.fsencode(ROOT/'scripts/root-server.py') in args and os.fsencode(c['pid']) in args


def stop_server(c):
    if not c['pid'].exists():return
    text=c['pid'].read_text().strip()
    if not text: c['pid'].unlink();return
    try:pid=int(text)
    except ValueError:raise ValueError('Неверный PID journal; не останавливаю чужой процесс') from None
    if owns_server(c,pid):
        command(['sudo','kill','-TERM',str(pid)],capture_output=True,timeout=15)
        for _ in range(50):
            if not owns_server(c,pid):break
            time.sleep(.1)
        else:raise ValueError('Root-сервер не остановился; журнал сохранён')
    c['pid'].unlink(missing_ok=True)


def cleanup(c):
    stop_server(c);cleanup_firewall(c)
    return {'result':'PC_CLEANUP_DONE','router':'на роутере выполните sh router-rooting.sh cleanup; backups сохранены'}


@contextlib.contextmanager
def server(c):
    _private_directory(c['work'])
    lock=safe(c['work']/'session.lock')
    with lock.open('a') as handle:
        os.chmod(lock,0o600);fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        cleanup(c)
        process=None
        try:
            command(['sudo','-v'],timeout=90)
            open_firewall(c)
            atomic(c['pid'],b'')
            logfile=safe(c['work']/'server.log')
            with logfile.open('ab') as log:
                os.chmod(logfile,0o600)
                process=subprocess.Popen(['sudo',sys.executable,str(ROOT/'scripts/root-server.py'),
                    '--kit-dir',str(c['kit']),'--firmware-file',str(c['firmware']),'--bind',c['pc_ip'],
                    '--station-ip',c['station_ip'],'--upstream-file',str(c['upstreams']),'--pid-file',str(c['pid'])],
                    stdout=log,stderr=log)
                for _ in range(300):
                    if process.poll() is not None:raise ValueError('Root-сервер завершился; смотрите '+str(logfile))
                    if c['pid'].read_text().strip():break
                    time.sleep(.1)
                else:raise ValueError('Root-сервер не готов; смотрите '+str(logfile))
                yield process
        finally:
            try:cleanup(c)
            finally:
                if process is not None:
                    if process.poll() is None:
                        # sudo may still be waiting for authentication before Python starts.
                        process.terminate()
                    try:process.wait(timeout=10)
                    except subprocess.TimeoutExpired:raise ValueError('Не удалось завершить launcher root-сервера')


def keypair(c):
    if not c['identity'].exists():
        c['identity'].parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        command(['ssh-keygen','-t','ed25519','-a','64','-f',c['identity']])
    if not c['public'].exists():
        result=command(['ssh-keygen','-y','-f',c['identity']],capture_output=True)
        atomic(c['public'],result.stdout,0o600)
    preparation.public_key(c['public'].read_text())


def prepare(c):
    keypair(c)
    command(['adb','connect',c['serial']],timeout=20)
    connection=preparation.Connection(c,c['serial'])
    result=preparation.prepare(connection,c['asset_cache'],c['public'].read_text(),c['station'].get('port',2222))
    if not preparation.ota_status(connection)['ota_disabled']:raise ValueError('OTA не полностью отключено; DNS пока не возвращайте')
    return result


def backup(c):
    base=safe(c['work']/'backups');_private_directory(base)
    directory=base/str(time.time_ns());_private_directory(directory)
    for remote,name in [('/data/opt/backup','originals'),(preparation.BACKUPS,'preparation')]:
        safe(directory/name)
        command(['adb','-s',c['serial'],'pull',remote,str(directory/name)],timeout=180)
    return {'result':'ROOT_BACKUP_SAVED','directory':str(directory)}


def verify(c):
    connection=preparation.Connection(c,c['serial'])
    public=connection.run('/data/opt/bin/dropbearkey -y -f /data/opt/etc/dropbear/hk_ed25519')
    lines=[line for line in public.splitlines() if line.startswith(b'ssh-ed25519 ')]
    if not lines:raise ValueError('Не получен public host key')
    fingerprint=command(['ssh-keygen','-lf','-'],input=lines[0]+b'\n',capture_output=True).stdout.decode().strip()
    print('Fingerprint колонки из ADB: '+fingerprint,flush=True)
    print('Сравните его с запросом SSH; пароль ключа вводится интерактивно.',flush=True)
    result=command(['ssh','-i',c['identity'],'-o','IdentitiesOnly=yes','-o','StrictHostKeyChecking=ask',
                   '-o','ConnectTimeout=8','-p',str(c['station'].get('port',2222)),
                   'root@'+c['station_ip'],'id -u'],stdout=subprocess.PIPE,text=True,timeout=90)
    if result.stdout.strip()!='0':raise ValueError('SSH не подтвердил root')
    status=preparation.ota_status(connection)
    if not status['ota_disabled']:raise ValueError('Не подтверждено отключение OTA')
    return {'result':'SSH_AND_OTA_READY'}


def dns_check(c,restored=False):
    if not shutil.which('dig'):raise ValueError('Установите bind-utils для проверки DNS')
    for host in root_server.DOMAINS[:3]:
        a=command(['dig','@'+c['router_ip'],host,'A','+short','+time=3','+tries=1'],capture_output=True,text=True,timeout=8).stdout.splitlines()
        addresses=[x for x in a if _is_ip(x)]
        if not restored:
            aaaa=command(['dig','@'+c['router_ip'],host,'AAAA','+short','+time=3','+tries=1'],capture_output=True,text=True,timeout=8).stdout.strip()
            if addresses!=[c['pc_ip']] or aaaa:raise ValueError('DNS '+host+' ещё не направлен только на IPv4 ПК')
        elif not addresses or c['pc_ip'] in addresses:raise ValueError('DNS '+host+' ещё не восстановлен')


def _is_ip(text):
    try:ipaddress.ip_address(text);return True
    except ValueError:return False


def wizard(c,prompt=input):
    if prompt('Это подготовка НОВОЙ Mini 2, с ручным сбросом настроек. Продолжить? [yes/no] ').strip()!='yes':return {'result':'CANCELLED'}
    print('1. Загружаем прошивку, локальный kit и SSH binaries.');fetch_assets(c)
    print('2. Проверяем ПК, TLS и реальные upstream IP до DNS override.');check(c)
    router=router_script(c);keypair(c)
    with server(c):
        print('3. Скопируйте на роутер '+str(router)+' и выполните: sh router-rooting.sh apply')
        prompt('После настройки DNS на роутере нажмите Enter. ')
        dns_check(c)
        print('4. Mini 2: отключить питание → зажать отключение микрофонов → питание → 5 с до жёлтого → отпустить.')
        print('В «Доме с Алисой» настройте Wi-Fi; телефон — через мобильный интернет/Bluetooth. Дождитесь OTA и перезапуска. Питание во время OTA не отключать.')
        while True:
            if prompt('Enter — проверить ADB/root; q — остановить стенд: ').strip().lower()=='q':
                print('После остановки на роутере: sh router-rooting.sh cleanup')
                return {'result':'CANCELLED'}
            try:
                command(['adb','connect',c['serial']],timeout=20)
                preparation.inspect(preparation.Connection(c,c['serial']));break
            except (ValueError,subprocess.SubprocessError) as error:print('ADB/прошивка ещё не готовы: '+str(error))
        print('5. Устанавливаем SSH и отключаем OTA. DNS пока не возвращать.');print(json.dumps(prepare(c),ensure_ascii=False))
        print('6. Сохраняем backup и проверяем вход по SSH.');print(json.dumps(backup(c),ensure_ascii=False));verify(c)
    print('7. На роутере выполните: sh router-rooting.sh cleanup')
    prompt('После удаления временного DNS нажмите Enter. ');dns_check(c,restored=True)
    c['upstreams'].unlink(missing_ok=True)
    print('Проверьте обычную Алису, Bluetooth и VPN. Reboot при необходимости выполняется отдельно после проверки SSH.')
    return {'result':'ROOTING_DONE','backups':str(c['work']/'backups')}

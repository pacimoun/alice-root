"""Локальный eQuasar для одной Mini 2; без изменения DNS/firewall роутера."""
import contextlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import socket
import ssl
import tempfile
import threading
from urllib.parse import parse_qs, urlsplit
from http.server import ThreadingHTTPServer

from .diagnostics import FIRMWARE
from .firmware import inspect as inspect_firmware
from .transactions import safe, write_json, read_json

DOMAINS=('quasar.yandex.net','quasar.yandex.ru','scbh.yandex.net','oauth.yandex.ru')
PLATFORM='yandexmini_2'


def ipv4(value):
    ip=ipaddress.ip_address(value)
    if ip.version!=4 or ip.is_unspecified or ip.is_multicast or ip.is_loopback:
        raise ValueError('Нужен IPv4 адрес устройства в LAN')
    return str(ip)


def upstreams(bind, cache=None):
    cached=bool(cache and Path(cache).exists())
    if cached:
        values=read_json(cache)
    else:
        values={host:socket.getaddrinfo(host,443,socket.AF_INET,socket.SOCK_STREAM)[0][4][0] for host in DOMAINS}
    for host in DOMAINS:
        value=values.get(host,'')
        try: valid=ipaddress.ip_address(value).is_global and value!=bind
        except ValueError: valid=False
        if not valid: raise ValueError('Upstream DNS ведёт в LAN/fake-IP. Получите реальные адреса до DNS override')
    if cache and not cached: write_json(cache,values)
    return values


def load_kit(folder):
    path=safe(Path(folder).absolute()/'scripts/equasar_server.py')
    spec=importlib.util.spec_from_file_location('alice_root_equasar',path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in ('EquasarHandler','PinnedHTTPS','sync_payload','_downgrade_payload'):
        if not hasattr(module,name):raise ValueError('Изменилась структура lite-kit; нужна адаптация wrapper')
    return module


def tls_context(folder):
    folder=Path(folder)
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    # Factory firmware uses legacy TLS settings; this does not change PC trust.
    with contextlib.suppress(ValueError,ssl.SSLError):
        context.minimum_version=ssl.TLSVersion.TLSv1
        context.set_ciphers('DEFAULT@SECLEVEL=0')
    context.load_cert_chain(str(safe(folder/'certs/fullchain.pem')),str(safe(folder/'certs/quasar-all.key')))
    return context


def configure(module, firmware, bind, station_ip, addresses, scratch):
    firmware=safe(firmware)
    inspect_firmware(firmware)
    fw={'path':str(firmware),'name':FIRMWARE+'.zip','version':FIRMWARE,'platform':PLATFORM,'crc32':None}
    module.LOCAL_IP=bind;module.BIND_IP=bind;module.COLUMN_IPS={station_ip}
    module.OTA_FILE=str(firmware);module.OTA_VERSION=FIRMWARE;module.OTA_PLATFORM=PLATFORM
    module.OTA_PREFIX='/'+PLATFORM+'/ota/'
    module._OTA_CRC={'v':None};module._CRC_CACHE={}
    module.MODE_PASSTHROUGH=False;module.MODE_OTA_ONLY=False;module.MODE_DOWNGRADE=False;module.SYNC_LOCAL=False
    module.CACHE_DIR=str(scratch);module.SYNC_DUMP=str(Path(scratch)/'disabled-dump')
    module.SYNC_TEMPLATE=str(Path(scratch)/'unused-template')
    module.print=lambda *args,**kwargs:None
    module.log_yandex=module.log_local=module.log_dns=lambda *args,**kwargs:None
    module.real_ip=lambda host:addresses[host]
    module.mirror_platform=lambda version:None
    module.ota_platform=lambda:PLATFORM
    module.fw_manifest=lambda platform:dict(fw) if platform==PLATFORM else None
    module.no_downgrade=lambda:False
    base=module.EquasarHandler

    class Handler(base):
        def log_message(self,*args):pass
        def _log_sync_request(self):pass
        def _log_payload(self,*args):
            print('ROOT_SERVER_ADB_CONFIG_SENT',flush=True)
        def handle_request(self):
            host=(self.headers.get('Host') or 'quasar.yandex.net').split(':')[0].lower()
            query=parse_qs(urlsplit(self.path).query)
            platform=(query.get('platform') or query.get('device_type') or [''])[0]
            if host not in DOMAINS or platform not in ('',PLATFORM):
                self.send_error(400);return
            return super().handle_request()
        def proxy_upstream(self,dump_path=None,fallback=None):
            return super().proxy_upstream(dump_path=None,fallback=fallback)
        def serve_ota(self,platform=None,name=None):
            if platform not in (None,PLATFORM) or name not in (None,fw['name']):return False
            # Force the explicit local file; never a filename supplied by a request.
            print('ROOT_SERVER_OTA_REQUEST',flush=True)
            return super().serve_ota()

    return Handler


class Server(ThreadingHTTPServer):
    daemon_threads=True
    allow_reuse_address=True
    def __init__(self,address,handler,station_ip):
        self.station_ip=station_ip
        super().__init__(address,handler)
    def verify_request(self,request,client_address):
        return client_address[0]==self.station_ip
    def handle_error(self,*args):pass  # no account-bearing request data in tracebacks


def run(kit, firmware, bind, station_ip, upstream_file=None, check=False, http_port=80, https_port=443, pid_file=None):
    bind=ipv4(bind);station_ip=ipv4(station_ip)
    if bind==station_ip:raise ValueError('ПК и колонка должны иметь разные адреса')
    addresses=upstreams(bind,upstream_file)
    context=tls_context(kit)
    # Never enable raw sync dumps via an inherited environment.
    os.environ['DUMP_SYNC']='0'
    os.environ['LOCAL_IP']=bind
    os.environ['BIND_IP']=bind
    with tempfile.TemporaryDirectory(prefix='alice-root-server-') as scratch:
        module=load_kit(kit)
        handler=configure(module,Path(firmware).absolute(),bind,station_ip,addresses,Path(scratch))
        if check:return {'result':'ROOT_SERVER_READY','platform':PLATFORM,'firmware':FIRMWARE,'ports':[http_port,https_port]}
        servers=[];threads=[]
        try:
            servers.append(Server((bind,http_port),handler,station_ip))
            https=Server((bind,https_port),handler,station_ip);servers.append(https)
            https.socket=context.wrap_socket(https.socket,server_side=True)
            for server in servers:
                t=threading.Thread(target=server.serve_forever,daemon=True);t.start();threads.append(t)
            if pid_file:
                # The caller precreates this private file; truncation preserves its owner.
                fd=os.open(safe(pid_file),os.O_WRONLY|os.O_TRUNC|os.O_NOFOLLOW)
                with os.fdopen(fd,'w') as f:f.write(str(os.getpid()))
            print('ROOT_SERVER_RUNNING: HTTP/HTTPS; Ctrl+C — stop',flush=True)
            threads[0].join()
        except KeyboardInterrupt:pass
        finally:
            for server,thread in zip(servers,threads):server.shutdown()
            for server in servers:server.server_close()
            for thread in threads:thread.join(timeout=5)
            if pid_file and Path(pid_file).exists():
                if Path(pid_file).read_text().strip()==str(os.getpid()):Path(pid_file).unlink()
    return {'result':'ROOT_SERVER_STOPPED'}

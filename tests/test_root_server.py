import io
import json
from pathlib import Path
import socket
import tempfile
import types
import unittest
from unittest.mock import patch
import zipfile

from alice_root import root_server as r


class HandlerBase:
    def handle_request(self):return 'upstream'
    def serve_ota(self,*args):return args
    def proxy_upstream(self,**kwargs):return kwargs


class RootServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name);self.firmware=self.path/'mini2.zip'
        with zipfile.ZipFile(self.firmware,'w') as z:z.writestr('payload','fixture, not real firmware')
        self.module=types.SimpleNamespace(EquasarHandler=HandlerBase)
        self.addresses={host:'8.8.8.8' for host in r.DOMAINS}

    def configure(self):
        return r.configure(self.module,self.firmware,'192.0.2.10','192.0.2.11',self.addresses,self.path/'cache')

    def test_only_local_mini2_firmware_is_selected_and_mirrors_are_disabled(self):
        self.configure()
        self.assertEqual(self.module.fw_manifest('yandexmini_2')['path'],str(self.firmware))
        self.assertIsNone(self.module.fw_manifest('yandexmicro'))
        self.assertIsNone(self.module.mirror_platform('any-version'))
        self.assertEqual(self.module.ota_platform(),'yandexmini_2')
        self.assertEqual(self.module.real_ip('quasar.yandex.net'),'8.8.8.8')
        with self.assertRaises(KeyError):self.module.real_ip('other.invalid')

    def test_requests_cannot_select_another_firmware_or_arbitrary_upstream(self):
        cls=self.configure();handler=cls();handler.headers={'Host':'quasar.yandex.net'}
        handler.path='/check_updates?platform=yandexmini_2';handler.send_error=lambda code:setattr(handler,'error',code)
        self.assertEqual(handler.handle_request(),'upstream')
        for path in ('/check_updates?platform=yandexmicro','/check_updates?device_type=yandexmicro'):
            handler.path=path;handler.handle_request();self.assertEqual(handler.error,400)
        handler.headers={'Host':'other.invalid'};handler.path='/get_sync_info';handler.handle_request();self.assertEqual(handler.error,400)
        self.assertFalse(handler.serve_ota('yandexmicro','other.zip'))
        self.assertFalse(handler.serve_ota('yandexmini_2','../../other.zip'))
        self.assertEqual(handler.serve_ota('yandexmini_2',r.FIRMWARE+'.zip'),())
        self.assertEqual(handler.proxy_upstream(dump_path='private.json'),{'dump_path':None,'fallback':None})

    def test_network_loop_and_fake_ip_are_rejected(self):
        for value in ('127.0.0.1','10.0.0.1','198.18.0.1','100.64.0.1'):
            answers=[(socket.AF_INET,socket.SOCK_STREAM,6,'',(value,443))]
            with self.subTest(value=value),patch.object(r.socket,'getaddrinfo',return_value=answers):
                with self.assertRaises(ValueError):r.upstreams('192.0.2.10')

    def test_cached_upstream_map_is_not_rewritten_by_sudo_server(self):
        path=self.path/'upstreams.json';path.write_text(json.dumps(self.addresses));before=path.read_bytes()
        with patch.object(r.socket,'getaddrinfo',side_effect=AssertionError('DNS already overridden')),patch.object(r,'write_json',side_effect=AssertionError('do not change owner')):
            self.assertEqual(r.upstreams('192.0.2.10',path),self.addresses)
        self.assertEqual(path.read_bytes(),before)

    def test_wrong_source_address_is_rejected_before_request(self):
        server=object.__new__(r.Server);server.station_ip='192.0.2.11'
        self.assertTrue(server.verify_request(None,('192.0.2.11',1234)))
        self.assertFalse(server.verify_request(None,('192.0.2.12',1234)))

    def test_check_does_not_open_listeners_or_change_dns(self):
        module=types.SimpleNamespace(EquasarHandler=HandlerBase)
        with patch.object(r,'upstreams',return_value=self.addresses),patch.object(r,'tls_context'),patch.object(r,'load_kit',return_value=module),patch.object(r,'Server',side_effect=AssertionError('no listeners')):
            result=r.run(self.path,self.firmware,'192.0.2.10','192.0.2.11',check=True)
        self.assertEqual(result['result'],'ROOT_SERVER_READY')

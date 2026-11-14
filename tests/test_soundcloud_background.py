import asyncio
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from alice_root import soundcloud as m

XVFB=r'''
import signal,sys,time
from pathlib import Path
auth=Path(sys.argv[sys.argv.index('-auth')+1])
(auth.parent/'ready').write_text('ready')
signal.signal(signal.SIGTERM,lambda *args:sys.exit(0))
while True:time.sleep(.1)
'''
CHROME=r'''
import json,os,sys
from pathlib import Path
profile=Path(next(x.split('=',1)[1] for x in sys.argv if x.startswith('--user-data-dir=')))
observed={key:os.environ.get(key) for key in ['DISPLAY','WAYLAND_DISPLAY','XAUTHORITY','XDG_SESSION_TYPE','PULSE_SINK']}
observed['args']=sys.argv[1:]
(profile/'observed.json').write_text(json.dumps(observed))
pending=b''
while True:
 data=os.read(3,65536)
 if not data:break
 pending+=data
 while b'\0' in pending:
  raw,pending=pending.split(b'\0',1)
  packet=json.loads(raw);method=packet['method'];result={}
  if method=='Target.createTarget':result={'targetId':'soundcloud'}
  elif method=='Target.attachToTarget':result={'sessionId':'soundcloud-session'}
  elif method=='Target.getTargets':result={'targetInfos':[{'type':'page','url':'about:blank','targetId':'blank'}]}
  elif method=='Runtime.evaluate':result={'result':{'value':{'ok':True,'ready':True,'playing':False,'page':'https://soundcloud.com/you/likes','track':'Колонка'}}}
  reply=json.dumps({'id':packet['id'],'result':result},ensure_ascii=False).encode()+b'\0'
  os.write(4,reply[:7]);os.write(4,reply[7:])
  if method=='Browser.close':sys.exit(0)
'''


def script(path,source):
    path.write_text('#!'+sys.executable+'\n'+source.lstrip());path.chmod(0o700);return path


class Display(m.VirtualDisplay):
    def socket_path(self,number):
        return self.authority.parent/'ready' if self.authority else self.log.parent/f'unused-X{number}'


class BrowserTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.environment=patch.dict(os.environ,{'DISPLAY':':42','WAYLAND_DISPLAY':'wayland-existing','PULSE_SINK':'desktop-original'})
        self.environment.start()
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.state=self.root/'state';self.state.mkdir()
        self.profile=self.root/'profile';self.profile.mkdir()
        self.sentinel=self.profile/'account-preserved';self.sentinel.write_bytes(b'fake session sentinel')
        self.chrome=script(self.root/'fake-chromium',CHROME)
        self.xvfb=script(self.root/'fake-Xvfb',XVFB)
        self.log=self.state/'browser.log'
    def tearDown(self):self.temp.cleanup();self.environment.stop()

    async def test_background_fd_transport_environment_and_cleanup(self):
        environment=dict(os.environ)
        display=Display(self.log,binary=str(self.xvfb))
        browser=m.Browser(self.profile,self.log,'bluez_output.test',binary=str(self.chrome),display=display)
        try:
            await browser.start()
            state=await browser.page({'op':'status'})
            self.assertTrue(state['ok']);self.assertEqual(state['track'],'Колонка')
            controller=m.Controller(browser,'bluez_output.test')
            status=await controller.perform('status')
            self.assertIs(status['background'],True)
            self.assertEqual(status['virtual_display'],display.name)
            self.assertTrue(201 <= display.number <= 300)
            observed=json.loads((self.profile/'observed.json').read_text())
            self.assertEqual(observed['DISPLAY'],display.name)
            self.assertEqual(observed['PULSE_SINK'],'bluez_output.test')
            self.assertIsNone(observed['WAYLAND_DISPLAY'])
            self.assertIn('--ozone-platform=x11',observed['args'])
            authority=Path(observed['XAUTHORITY'])
            self.assertTrue(authority.is_file());self.assertEqual(authority.stat().st_mode&0o777,0o600)
            self.assertGreater(authority.stat().st_size,0)
            display_proc=display.proc;chrome_proc=browser.proc
        finally:await browser.close()
        self.assertIsNotNone(display_proc.poll());self.assertIsNotNone(chrome_proc.poll())
        self.assertFalse(authority.parent.exists())
        self.assertEqual(self.sentinel.read_bytes(),b'fake session sentinel')
        self.assertEqual(dict(os.environ),environment)

    async def test_visible_mode_uses_desktop_and_never_starts_xvfb(self):
        display=Display(self.log,binary=str(self.xvfb))
        browser=m.Browser(self.profile,self.log,'bluez_output.test',binary=str(self.chrome),background=False,display=display)
        with patch.dict(os.environ,{'DISPLAY':':42','WAYLAND_DISPLAY':'wayland-test'}):
            try:
                await browser.start()
                status=await m.Controller(browser,'bluez_output.test').perform('status')
                self.assertIs(status['background'],False);self.assertIsNone(status['virtual_display'])
                observed=json.loads((self.profile/'observed.json').read_text())
                self.assertEqual(observed['DISPLAY'],':42')
                self.assertEqual(observed['WAYLAND_DISPLAY'],'wayland-test')
                self.assertNotIn('--ozone-platform=x11',observed['args'])
                self.assertIsNone(display.proc);self.assertIsNone(display.temporary)
            finally:await browser.close()

    async def test_failed_xvfb_cleans_auth_and_does_not_launch_browser(self):
        failure=script(self.root/'failed-Xvfb','import sys\nsys.exit(3)\n')
        display=Display(self.log,binary=str(failure))
        browser=m.Browser(self.profile,self.log,'bluez_output.test',binary=str(self.chrome),display=display)
        try:
            with self.assertRaisesRegex(m.Failure,'VIRTUAL_DISPLAY_START_FAILED'):await browser.start()
        finally:await browser.close()
        self.assertEqual(list(self.state.glob('xvfb-*')),[])
        self.assertFalse((self.profile/'observed.json').exists())

    async def test_failed_chromium_start_also_cleans_xvfb(self):
        display=Display(self.log,binary=str(self.xvfb))
        browser=m.Browser(self.profile,self.log,'bluez_output.test',binary=str(self.root/'missing-chrome'),display=display)
        try:
            with self.assertRaises((OSError,m.Failure)):await browser.start()
        finally:await browser.close()
        self.assertEqual(list(self.state.glob('xvfb-*')),[])

    async def test_display_death_is_detected(self):
        display=Display(self.log,binary=str(self.xvfb))
        browser=m.Browser(self.profile,self.log,'bluez_output.test',binary=str(self.chrome),display=display)
        try:
            await browser.start();display.proc.terminate()
            await asyncio.wait_for(browser.closed.wait(),2)
        finally:await browser.close()
        self.assertEqual(list(self.state.glob('xvfb-*')),[])


class ModeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.paths={"state":self.root,"profile":self.root/"profile","socket":self.root/"control.sock"}
        self.stack=contextlib.ExitStack()
        self.stack.enter_context(patch.object(m.os,"umask"))
        self.stack.enter_context(patch.object(m,"locations",return_value=self.paths))
    def tearDown(self):self.stack.close();self.temp.cleanup()

    def test_extra_argument_cannot_trigger_mode_switch(self):
        with patch.object(m.os,'geteuid',return_value=1000):
            for action in ['background','window']:
                with patch.object(sys,'argv',['verity-sc',action,'unexpected']):
                    with self.assertRaisesRegex(m.Failure,'UNEXPECTED_ARGUMENT'):m.main()

    def test_mode_switch_checks_dependencies_before_stopping(self):
        m.locations()['socket'].touch();calls=[]
        def request(action,**kwargs):calls.append(action);return {'ok':True,'running':True,'background':False}
        with patch.object(m,'request',side_effect=request),patch.object(m,'display_dependencies',side_effect=m.Failure('MISSING')):
            with self.assertRaisesRegex(m.Failure,'MISSING'):m.switch_mode(False)
        self.assertEqual(calls,['status'])

    def test_same_mode_preserves_playback(self):
        m.locations()['socket'].touch();state={'ok':True,'running':True,'background':True,'playing':True}
        with patch.object(m,'request',return_value=state) as request,patch.object(m,'start') as start:
            self.assertEqual(m.switch_mode(False),state)
            request.assert_called_once_with('status',timeout=25);start.assert_not_called()

    def test_switch_stops_then_starts_requested_mode(self):
        socket=m.locations()['socket'];socket.touch();events=[]
        def request(action,**kwargs):
            events.append(action)
            if action=='status':return {'ok':True,'running':True,'background':False}
            socket.unlink();return {'ok':True}
        def start(**kwargs):events.append(('start',kwargs));return {'ok':True,'background':True}
        with patch.object(m,'request',side_effect=request),patch.object(m,'start',side_effect=start),patch.object(m,'display_dependencies'):
            self.assertTrue(m.switch_mode(False)['background'])
        self.assertEqual(events,['status','stop',('start',{'window':False})])


if __name__=='__main__':unittest.main(verbosity=2)

import asyncio
import contextlib
import ctypes
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from alice_root.voice import verity_music as music

ROOT = Path(os.environ.get("ALICE_AGENT_TEST_DIR", ".cache/build-agent"))


@unittest.skipUnless((ROOT / "native_agent_music.host.so").exists() and (ROOT / "agent-harness").exists(), "Соберите C harness: scripts/build-agent.py --fetch --host-tests и ALICE_AGENT_TEST_DIR")
class NativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch.dict(os.environ, {'ALICE_NATIVE_AGENT': '0', 'VERITY_MUSIC_PROBE': '0'}):
            cls.lib=ctypes.CDLL(str(ROOT/'native_agent_music.host.so'))
        cls.lib.alice_native_command.argtypes=[ctypes.c_char_p,ctypes.c_size_t]
        cls.lib.alice_native_command.restype=ctypes.c_int

    def classify(self,text):
        data=text.encode();return self.lib.alice_native_command(data,len(data))

    def harness(self,text,chat=0,state=3,final=1):
        r=subprocess.run([str(ROOT/'agent-harness'),str(chat),str(state),str(final),text],capture_output=True,check=True,text=True,env={**os.environ,"ALICE_NATIVE_AGENT":"0","VERITY_MUSIC_PROBE":"0"})
        return [json.loads(line) for line in r.stdout.splitlines()]

    def test_configured_gpt_phrases_are_preserved(self):
        with tempfile.NamedTemporaryFile() as cfg:
            cfg.write('1\tоткрой чат\n2\tсоздай новый чат\n3\tзакрой чат\n4\tоткрой предыдущий чат\n'.encode());cfg.flush()
            with patch.dict(os.environ,{'ALISA_COMMANDS_FILE':cfg.name}):
                for text,want in [('открой чат',1),('создай новый чат',2),('закрой чат',3),('открой предыдущий чат',4),('Музыка, мои лайки',5),('какая погода',0)]:
                    self.assertEqual(self.classify(text),want,text)

    def test_exact_prefixes_only(self):
        for marker in music.MARKERS:
            text='Алиса, '+marker.upper()+', включи песню'
            self.assertEqual(self.classify(text),5,text)
            self.assertEqual(music.parse_spoken(text),('search','песню'))
        for text in ['расскажи про музыку','это музыка мои лайки','музыка','музыкальный включи музыку','саундклауд мои лайки','soundcloud мои лайки','пауза','включи музыку','алиса открой чат']:
            self.assertNotEqual(self.classify(text),5,text)

    def test_final_busy_request_only_and_deduplicated(self):
        rows=self.harness('Музыка, мои лайки')
        self.assertEqual(rows,[{'event':'verity_music','text':'Музыка, мои лайки'},{'commands':1,'clears':1,'chat':0,'seen':1}])
        for state,final in [(1,1),(2,1),(3,0),(0,1)]:
            rows=self.harness('музыка мои лайки',state=state,final=final)
            self.assertEqual(rows[-1]['commands'],0)
            self.assertEqual(rows[-1]['clears'],0)

    def test_gpt_conversation_is_not_redirected_to_music(self):
        rows=self.harness('музыка включи мои лайки',chat=1)
        self.assertEqual(rows,[{'commands':0,'clears':1,'chat':1,'seen':1}])

    def test_ordinary_alice_request_is_untouched(self):
        self.assertEqual(self.harness('какая погода'),[{'commands':0,'clears':0,'chat':0,'seen':0}])

    def test_quotes_backslashes_and_control_bytes_are_escaped(self):
        for text in ['Музыка, включи "a\\b"','музыка включи a\nb\tпривет']:
            rows=self.harness(text)
            self.assertEqual(rows[0],{'event':'verity_music','text':text})
        rows=self.harness('музыка включи '+'a'*1024)
        self.assertEqual(rows[-1]['commands'],0)

    def test_module_load_probe_does_not_connect_to_station(self):
        env={**os.environ,'LD_PRELOAD':str(ROOT/'native_agent_music.host.so'),'LD_BIND_NOW':'1','VERITY_MUSIC_PROBE':'1','ALICE_NATIVE_AGENT':'0'}
        r=subprocess.run(['/usr/bin/true'],env=env,capture_output=True,check=True)
        self.assertEqual(r.stdout,b'VERITY_MUSIC_MODULE_OK\n');self.assertEqual(r.stderr,b'')


class ParserTests(unittest.TestCase):
    def test_commands_and_query(self):
        for text,want in [('Алиса, Музыка, мои лайки',('likes',None)),('МУЗЫКА, включи мои любимые треки, пожалуйста!',('likes',None)),('музыка pause',None),('музыка пауза',('pause',None)),('музыка продолжи',('resume',None)),('музыка следующий трек',('next',None)),('музыка предыдущая песня',('previous',None)),('Музыка, включи песню собакам тут не место',('search','собакам тут не место')),('музыка найди AC/DC - Song "Remix"',('search','AC/DC - Song "Remix"'))]:
            if want is None:
                with self.assertRaises(music.MusicError):music.parse_spoken(text)
            else:self.assertEqual(music.parse_spoken(text),want,text)

    def test_input_limits_and_no_prefix(self):
        for text in [None,'','включи мои лайки','музыка включи\nкоманду','музыка включи '+'a'*301,'музыка '+('\ud800')]:
            with self.assertRaises((music.MusicError,UnicodeError)):music.parse_spoken(text)


class DispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_is_private_and_nonblocking(self):
        status={};snapshots=[];gate=asyncio.Event();calls=[]
        async def rpc(action,query=None):
            calls.append((action,query));await gate.wait();return {'ok':True}
        module=music.Music(status,lambda:snapshots.append(json.dumps(status)))
        with patch.object(music,'rpc',rpc):
            module.submit({'text':'музыка включи секретное название песни'})
            await asyncio.sleep(0)
            self.assertEqual(status['soundcloud_voice']['phase'],'working')
            module.submit({'text':'музыка следующий'})
            self.assertEqual(status['soundcloud_voice']['busy_commands'],1)
            gate.set();await module.task
        self.assertEqual(calls,[('search','секретное название песни')])
        self.assertEqual(status['soundcloud_voice']['completed'],1)
        self.assertNotIn('секретное',' '.join(snapshots))

    async def test_lazy_start_only_for_playback_requests(self):
        for text,should_start in [('музыка мои лайки',True),('музыка включи песню',True),('музыка продолжи',True),('музыка пауза',False),('музыка следующий',False)]:
            calls=[]
            async def rpc(*args):
                calls.append('rpc')
                if len(calls)==1:raise music.MusicError('CONTROLLER_NOT_RUNNING')
                return {'ok':True}
            async def start():calls.append('start')
            state={};module=music.Music(state,lambda:None)
            with patch.object(music,'rpc',rpc),patch.object(music,'ensure_running',start):
                module.submit({'text':text});await module.task
            self.assertEqual('start' in calls,should_start,text)

    async def test_shutdown_cancels_owned_task(self):
        closed=[]
        async def rpc(*args):
            try:await asyncio.Event().wait()
            finally:closed.append(True)
        state={};module=music.Music(state,lambda:None)
        with patch.object(music,'rpc',rpc):
            module.submit({'text':'музыка мои лайки'});await asyncio.sleep(0);await module.close()
        self.assertTrue(module.task.done());self.assertEqual(closed,[True])


if __name__=='__main__':unittest.main(verbosity=2)

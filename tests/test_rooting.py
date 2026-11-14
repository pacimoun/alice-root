import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch,Mock
from alice_root import rooting as r

ROOT=Path(__file__).resolve().parents[1]


class RootingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.config=self.root/'config.toml'
        self.config.write_text('[station]\nhost="192.0.2.11"\nidentity="'+str(self.root/'key')+'"\n[rooting]\npc_ip="192.0.2.10"\nrouter_ip="192.0.2.1"\nwifi_mac="AA:BB:CC:DD:EE:FF"\n[paths]\nstate="'+str(self.root/'state')+'"\n')
        self.c=r.profile(self.config)
        self.c['asset_cache']=self.root/'assets';self.c['kit']=self.root/'assets/kit';self.c['firmware']=self.root/'firmware.zip'

    def test_wizard_can_cancel_before_any_action(self):
        with patch.object(r,'fetch_assets',side_effect=AssertionError('download')):
            self.assertEqual(r.wizard(self.c,prompt=lambda _: 'no')['result'],'CANCELLED')
        self.assertEqual(list(self.root.iterdir()),[self.config])

    def test_dry_run_cli_has_no_processes_network_or_filesystem_mutations(self):
        spec=importlib.util.spec_from_file_location('rooting_cli',ROOT/'scripts/rooting.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with patch('subprocess.run',side_effect=AssertionError('subprocess')),contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(module.main(['--config',str(self.config),'--dry-run']),0)
        self.assertFalse(json.loads(output.getvalue())['changes'])
        self.assertEqual(list(self.root.iterdir()),[self.config])

    def test_preexisting_firewall_rule_is_not_owned_or_removed(self):
        r._private_directory(self.c['work'])
        calls=[]
        def firewall(args,check=True):
            calls.append(args)
            if '--get-zone-of-interface=lan' in args:return Mock(stdout='workstation\n',returncode=0)
            if any(a.startswith('--query-rich-rule=') for a in args):return Mock(returncode=0 if 'port=80 ' in ' '.join(args) else 1)
            return Mock(returncode=0)
        self.c['interface']='lan'
        with patch.object(r.shutil,'which',return_value='/tool'),patch.object(r.subprocess,'run',return_value=Mock(returncode=0)),patch.object(r,'_firewall',side_effect=firewall):
            r.open_firewall(self.c)
        entries=r.read_json(self.c['journal']);self.assertEqual(len(entries),1);self.assertIn('port=443 ',entries[0]['rule'])
        with patch.object(r,'_firewall',return_value=Mock(returncode=0)) as command:r.cleanup_firewall(self.c)
        self.assertTrue(any('--remove-rich-rule=' in str(call) and 'port=443 ' in str(call) for call in command.call_args_list))
        self.assertFalse(any('port=80 ' in str(call) for call in command.call_args_list))

    def test_firewall_intent_survives_add_failure_and_can_be_cleaned(self):
        r._private_directory(self.c['work']);self.c['interface']='lan';self.c['zone']='workstation'
        def fail(args,check=True):
            if any(a.startswith('--query-rich-rule=') for a in args):return Mock(returncode=1)
            raise subprocess.CalledProcessError(1,args)
        with patch.object(r.shutil,'which',return_value='/tool'),patch.object(r.subprocess,'run',return_value=Mock(returncode=0)),patch.object(r,'_firewall',side_effect=fail):
            with self.assertRaises(subprocess.CalledProcessError):r.open_firewall(self.c)
        self.assertEqual(len(r.read_json(self.c['journal'])),1)
        with patch.object(r,'_firewall',return_value=Mock(returncode=1)):r.cleanup_firewall(self.c)
        self.assertFalse(self.c['journal'].exists())

    def test_expired_firewall_intent_does_not_delete_a_later_identical_user_rule(self):
        r._private_directory(self.c['work'])
        r.write_json(self.c['journal'],[{'zone':'workstation','rule':'same-rule','expires_at':0}])
        with patch.object(r,'_firewall',side_effect=AssertionError('user rule must remain')):
            r.cleanup_firewall(self.c)
        self.assertFalse(self.c['journal'].exists())

    def test_cleanup_never_kills_unrelated_reused_pid(self):
        r._private_directory(self.c['work']);self.c['pid'].write_text('12345')
        with patch.object(r,'owns_server',return_value=False),patch.object(r,'command',side_effect=AssertionError('kill')):r.stop_server(self.c)
        self.assertFalse(self.c['pid'].exists())

    def test_keypair_never_replaces_existing_private_key(self):
        self.c['identity'].write_bytes(b'existing private key')
        self.c['public'].write_text('unused fake public key')
        with patch.object(r.preparation,'public_key'),patch.object(r,'command',side_effect=AssertionError('keygen')):r.keypair(self.c)
        self.assertEqual(self.c['identity'].read_bytes(),b'existing private key')

    def test_server_failure_cleans_only_own_firewall_and_process(self):
        r._private_directory(self.c['work'])
        process=Mock();process.poll.return_value=None
        cleanup_calls=[]
        def cleanup(c):
            cleanup_calls.append(True)
            c['pid'].unlink(missing_ok=True)
        def start(*args,**kwargs):self.c['pid'].write_text('123');return process
        with patch.object(r,'command'),patch.object(r,'cleanup',side_effect=cleanup),patch.object(r,'open_firewall'),patch.object(r.subprocess,'Popen',side_effect=start):
            with self.assertRaisesRegex(ValueError,'failure'):
                with r.server(self.c):raise ValueError('failure')
        self.assertEqual(len(cleanup_calls),2);process.terminate.assert_called_once();process.wait.assert_called_once()

    def test_wizard_preserves_server_until_backup_and_ssh_verification(self):
        calls=[]
        @contextlib.contextmanager
        def live(c):
            calls.append('server-start')
            try:yield
            finally:calls.append('server-stop')
        def record(name,result=None):
            def run(*args,**kwargs):calls.append(name);return result
            return run
        answers=iter(['yes','','',''])
        with contextlib.ExitStack() as patches,contextlib.redirect_stdout(io.StringIO()):
            for name,result in [('fetch_assets',None),('check',None),('router_script',self.root/'router.sh'),('keypair',None),('prepare',{'result':'PREPARED'}),('backup',{'result':'BACKUP'}),('verify',None)]:
                patches.enter_context(patch.object(r,name,side_effect=record(name,result)))
            patches.enter_context(patch.object(r,'server',side_effect=live))
            patches.enter_context(patch.object(r,'command'))
            patches.enter_context(patch.object(r.preparation,'inspect'))
            patches.enter_context(patch.object(r,'dns_check',side_effect=lambda c,restored=False:calls.append('dns-restored' if restored else 'dns-override')))
            result=r.wizard(self.c,prompt=lambda _:next(answers))
        self.assertEqual(result['result'],'ROOTING_DONE')
        self.assertLess(calls.index('backup'),calls.index('verify'))
        self.assertLess(calls.index('verify'),calls.index('server-stop'))
        self.assertLess(calls.index('server-stop'),calls.index('dns-restored'))

    def test_missing_dig_is_detected_before_starting_the_stand(self):
        with patch.object(r.shutil,'which',side_effect=lambda name:None if name=='dig' else '/tool'),patch.object(r,'command',side_effect=AssertionError('no commands')):
            with self.assertRaisesRegex(ValueError,'dig'):r.check(self.c)
        self.assertFalse(self.c['work'].exists())

    def test_repeated_backups_use_distinct_destinations(self):
        with patch.object(r,'command') as command:
            first=r.backup(self.c)['directory'];second=r.backup(self.c)['directory']
        self.assertNotEqual(first,second)
        for call in command.call_args_list:self.assertTrue(str(call.args[0][-1]).startswith(str(self.c['work']/'backups')))

    def test_router_file_contains_parameters_but_does_not_connect_to_router(self):
        with patch.object(r,'command',side_effect=AssertionError('router command')):
            path=r.router_script(self.c)
        text=path.read_text();self.assertIn('192.0.2.10',text);self.assertIn('AA:BB:CC:DD:EE:FF',text)
        subprocess.run(['sh','-n',str(path)],check=True)

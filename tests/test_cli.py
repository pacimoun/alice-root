import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from alice_root import cli, installer
from alice_root.config import load


class CliTests(unittest.TestCase):
    def test_help_and_dry_run_do_not_call_processes_or_write(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'missing.toml'
            for args in [['--config',str(p),'install','--component','all','--dry-run'],
                         ['--config',str(p),'deploy','--dry-run']]:
                with patch('subprocess.run',side_effect=AssertionError('dry-run subprocess')),contextlib.redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(cli.main(args),0)
                value=json.loads(out.getvalue())
                self.assertIn('result' if args[-2]=='deploy' else 'component',value)
                self.assertEqual(list(Path(d).iterdir()),[])

    def test_install_rejects_active_bridge_before_fetch(self):
        with patch.object(installer,'requirements'),patch.object(installer,'system_service',return_value='active'),patch.object(installer.artifacts,'fetch',side_effect=AssertionError('must not download')):
            with self.assertRaisesRegex(ValueError,'STOP_ACTIVE_BRIDGE'):
                installer.candidate(load('/nonexistent/config.toml'),'bridge')

    def test_unit_for_space_percent_backslash_path(self):
        c=load('/nonexistent/config.toml')
        c['paths']['bridge']=Path('/tmp/space and % percent')
        data=installer.unit(c)
        self.assertIn(b'WorkingDirectory=/tmp/space\\x20and\\x20%%\\x20percent',data)
        installer.verify_unit(data)
        self.assertIn(b'KillMode=mixed',data)
        self.assertIn(b'ALICE_SC_LAUNCHER=',data)

    def test_verify_restores_audio_defaults_on_failure(self):
        from subprocess import CompletedProcess
        calls=[]
        def command(args,timeout=10):
            calls.append(args)
            if args[1].startswith('set-default'):return CompletedProcess(args,0,b'',b'')
            original=b'original-'+args[1].encode()+b'\n'
            return CompletedProcess(args,0,original if len(calls)<=2 else b'changed\n',b'')
        with patch.object(cli,'run',side_effect=command),patch.object(cli,'owned_launcher',return_value=Path('/bin/false')),patch('subprocess.run',return_value=CompletedProcess([],1)):
            with self.assertRaisesRegex(ValueError,'STOP_INTERACTIVE_VERIFY'):
                cli.interactive(load('/nonexistent/config.toml'))
        self.assertEqual([c[1] for c in calls if c[1].startswith('set-')],['set-default-sink','set-default-source'])

    def test_runtime_does_not_stop_services_during_import(self):
        with patch('subprocess.run',side_effect=AssertionError('unexpected process')):
            from alice_root import soundcloud
            self.assertEqual(soundcloud.VERSION,'0.1.3')

class ControllerCompatibilityTests(unittest.TestCase):
    def test_controller_is_checked_by_protocol_not_version(self):
        routes={'status','play','search','likes','pause','resume','next','previous','stop'}
        for version in ('0.1.3','2.0.0','latest'):
            data=f'VERSION={version!r}\nMARKER="verity-soundcloud-control-v1"\nROUTES={routes!r}\n'.encode()
            self.assertEqual(installer.controller_protocol(data),version)
        with self.assertRaisesRegex(ValueError,'STOP_CONTROLLER_PROTOCOL'):
            installer.controller_protocol(b'MARKER="verity-soundcloud-control-v1"\nROUTES={"status"}\n')

    def test_voice_requires_owned_controller(self):
        with tempfile.TemporaryDirectory() as d:
            c=load('/nonexistent/config.toml')
            for key in ('data','state','backup'):c['paths'][key]=Path(d)/key
            with self.assertRaisesRegex(ValueError,'STOP_INSTALL_SOUNDCLOUD_FIRST'):
                installer.require_controller(c,'voice')
            self.assertEqual(list(Path(d).iterdir()),[])

    def test_architecture_rejected_before_dependency_or_fetch(self):
        with patch.object(installer.platform,'machine',return_value='aarch64'),patch.object(installer.shutil,'which',side_effect=AssertionError('must stop earlier')):
            with self.assertRaisesRegex(ValueError,'STOP_UNSUPPORTED_HOST_ARCH'):
                installer.requirements()

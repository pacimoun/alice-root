"""Current release package -> local install/rollback in isolated temp paths.

No systemctl/SSH/browser starts; the active-service query is explicitly faked.
"""
import json
import os
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import patch

from alice_root import installer
from alice_root.config import load
from alice_root.transactions import Store


ARCHIVE=os.environ.get('ALICE_RELEASE_ARCHIVE')
AGENT=os.environ.get('ALICE_MUSIC_AGENT')


@unittest.skipUnless(ARCHIVE and AGENT,'Full package integration requires release archive and compiled Music module')
class StagedInstallTests(unittest.TestCase):
    def test_full_package_install_repeat_backup_and_reverse(self):
        with tempfile.TemporaryDirectory(prefix='alice stage % ') as d:
            root=Path(d); c=load(root/'config.toml')
            for key in c['paths']:c['paths'][key]=root/key
            c['paths']['unit']=root/'unit/alisa-voice-bridge.service'
            c['station']={'host':'station.invalid','port':2222,'user':'root','bluetooth_mac':'AA:BB:CC:DD:EE:FF'}
            # Only dependency/platform and service query faked; real archive,
            # patch guards, unit verification, ownership and disk writes run.
            with patch.object(installer,'requirements'),patch.object(installer,'system_service',return_value='inactive'):
                files=installer.candidate(c,'all',Path(ARCHIVE),Path(AGENT))
                self.assertGreater(len(files),400)
                s=Store(c['paths']['state'],c['paths']['backup'])
                result=s.install(files)
                self.assertEqual(result['result'],'INSTALLED')
                self.assertIn(b'chromium-browser',(c['paths']['bridge']/'alisa_cli/login_browser.py').read_bytes())
                self.assertTrue((c['paths']['bridge']/'engines/bridge/verity_music.py').is_file())
                self.assertFalse((c['paths']['bridge']/'package-manifest.json').exists())
                self.assertEqual(s.install(files)['result'],'ALREADY_INSTALLED')
                # Dedicated launcher, not a path to the checkout; no accounts copied.
                launcher=(c['paths']['bin']/'verity-sc').read_text()
                self.assertIn(str(c['paths']['data']/'soundcloud/main.py'),launcher)
                self.assertFalse((c['paths']['state']/'soundcloud-profile').exists())
                version=subprocess.run([str(c['paths']['bin']/'alisa'),'--version'],capture_output=True,text=True,timeout=15)
                self.assertEqual(version.returncode,0,version.stderr)
                self.assertTrue(version.stdout.strip())
                help_result=subprocess.run([str(c['paths']['bin']/'verity-sc'),'--help'],capture_output=True,text=True,timeout=15)
                self.assertEqual(help_result.returncode,0,help_result.stderr)
                saved=s.backup_current(); self.assertEqual(saved['result'],'BACKUP_VERIFIED')
                self.assertEqual(s.rollback()['result'],'ROLLED_BACK')
                self.assertTrue(all(not Path(name).exists() for name in files))

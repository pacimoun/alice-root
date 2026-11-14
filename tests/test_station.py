"""Проверки без колонки, ключей и сетевых изменений."""
import importlib.util
import base64
import struct
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('station_tools', ROOT / 'scripts/station-tools.py')
station = importlib.util.module_from_spec(spec)
spec.loader.exec_module(station)


class StationTests(unittest.TestCase):
    def test_init_fd_detachment_and_ownership(self):
        script = station.render_init(2200)
        self.assertIn('</dev/null >>"$DIR/dropbear.log" 2>&1', script)
        self.assertIn('-p 2200', script)
        self.assertIn('/proc/"$alice_pid"/exe', script)
        subprocess.run(['sh', '-n'], input=script, text=True, check=True)
        for port in (0, 65536, -1):
            with self.assertRaises(ValueError):
                station.render_init(port)

    def test_key_add_not_replace_and_injection_safe(self):
        blob = struct.pack('>I', 11) + b'ssh-ed25519' + struct.pack('>I', 32) + b'x'*32
        key = 'ssh-ed25519 ' + base64.b64encode(blob).decode() + " comment ' $(false)"
        script = station.add_key_script(key)
        subprocess.run(['sh', '-n'], input=script, text=True, check=True)
        self.assertIn('grep -Fqx "$KEY" "$FILE"', script)
        self.assertIn('before-key-add', script)
        self.assertIn('>> "$FILE.alice-new"', script)
        self.assertIn('KEY=' + shlex.quote(key), script)
        for bad in ('command="evil" ssh-ed25519 AAAA', 'ssh-ed25519 AAAA\nsecond', 'ssh-rsa AAAA'):
            with self.assertRaises(ValueError):
                station.add_key_script(bad)

    def test_ssh_strict_hostkey_and_no_option_injection(self):
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / 'identity'
            key.write_text('fixture')
            args = station.ssh_args('station.invalid', 2222, key)
            self.assertIn('StrictHostKeyChecking=yes', args)
            self.assertIn('BatchMode=yes', args)
            for bad in ('-oProxyCommand=evil', 'host;false', 'host$(true)'):
                with self.assertRaises(ValueError):
                    station.ssh_args(bad, 2222, key)

    def fake_ssh(self, tar_bytes, status):
        # Real pipes/process completion, no SSH, accounts or device writes.
        code = 'import sys;sys.stdout.buffer.write(bytes.fromhex(' + repr(tar_bytes.hex()) + '));sys.exit(' + str(status) + ')'
        return [sys.executable, '-c', code]

    def test_backup_failed_ssh_does_not_leave_success_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'station.tar.gz'
            with patch.object(station, 'ssh_args', return_value=self.fake_ssh(b'', 17)):
                # ssh_args includes args before remote shell. Python treats command as extra argv.
                with self.assertRaisesRegex(ValueError, 'STOP_BACKUP_SSH_OR_TAR_FAILED'):
                    station.backup_station('station.invalid', 2222, Path('fixture'), output)
            self.assertFalse(output.exists())
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_backup_tar_validated_private_and_no_overwrite(self):
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode='w') as archive:
            info = tarfile.TarInfo('system/vendor/quasar/version_full')
            payload = b'fixture version'
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'station.tar.gz'
            with patch.object(station, 'ssh_args', return_value=self.fake_ssh(raw.getvalue(), 0)):
                result = station.backup_station('station.invalid', 2222, Path('fixture'), output)
            self.assertEqual(result['backup'], 'SAVED')
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            with tarfile.open(output) as archive:
                self.assertEqual(len(archive.getmembers()), 1)
            with self.assertRaises(ValueError):
                station.backup_station('station.invalid', 2222, Path('fixture'), output)

    def test_unexpected_backup_path_rejected(self):
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode='w') as archive:
            info = tarfile.TarInfo('etc/shadow')
            archive.addfile(info)
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'station.tar.gz'
            with patch.object(station, 'ssh_args', return_value=self.fake_ssh(raw.getvalue(), 0)):
                with self.assertRaisesRegex(ValueError, 'STOP_BACKUP_UNEXPECTED_PATH'):
                    station.backup_station('station.invalid', 2222, Path('fixture'), output)
            self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()

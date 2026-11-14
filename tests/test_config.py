import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from alice_root import config, diagnostics, soundcloud


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {
            'XDG_DATA_HOME': str(self.root/'data'),
            'XDG_STATE_HOME': str(self.root/'state'),
            'XDG_CACHE_HOME': str(self.root/'cache'),
            'XDG_CONFIG_HOME': str(self.root/'conf'),
            'ALICE_ROOT_CONFIG': str(self.root/'missing.toml'),
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_read_only_defaults_agree_with_soundcloud_and_do_not_create_directories(self):
        with patch.object(Path, 'home', return_value=self.root/'home'):
            loaded = config.load()
            self.assertEqual(config.sc_paths(loaded)['state'], soundcloud.locations()['state'])
            self.assertEqual(config.sc_paths(loaded)['profile'], soundcloud.locations()['profile'])
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertEqual(loaded['paths']['backup'], self.root/'state/alice-root/backups')

    def test_local_overrides_and_control_dir_environment_agree(self):
        local = self.root/'local.toml'
        local.write_text('[paths]\nstate="'+str(self.root/'custom')+'"\n[soundcloud]\nprofile="'+str(self.root/'sc-profile')+'"\n')
        with patch.dict(os.environ, {'ALICE_ROOT_CONFIG': str(local), 'ALICE_SC_CONTROL_DIR': str(self.root/'socket-state')}):
            loaded = config.load()
            expected = config.sc_paths(loaded)
            actual = soundcloud.locations()
            for key in ('state', 'profile'):
                self.assertEqual(expected[key], actual[key])
        self.assertEqual(list(self.root.iterdir()), [local])

    def test_invalid_station_values_reject_ssh_option_injection(self):
        for station in ({'host': '-oProxyCommand=unexpected'}, {'host': 'STATION_HOST'},
                        {'host': 'station.example', 'user': '-o option'},
                        {'host': 'station.example', 'user': 123},
                        {'host': 'station.example', 'port': True},
                        {'host': 'station.example', 'port': 65536},
                        {'host': 'station.example', 'model': 'unknown'},
                        {'host': 'station.example', 'identity': 42}):
            with self.subTest(station=station), self.assertRaises(ValueError):
                diagnostics.ssh_args({'station': station})
        ssh = diagnostics.ssh_args({'station': {'host': 'station.example', 'identity': '~/.ssh/station_key'}})
        self.assertIn('StrictHostKeyChecking=yes', ssh)
        self.assertIn('BatchMode=yes', ssh)

    def test_invalid_path_types_and_relative_overrides_fail_before_writes(self):
        local = self.root/'invalid.toml'
        for content in ('paths = "invalid"', '[paths]\nstate=42',
                        '[paths]\nstate="relative/path"', '[paths]\nstate="/tmp/../outside"'):
            local.write_text(content)
            with self.subTest(content=content), self.assertRaises(ValueError):
                config.load(local)
        self.assertEqual(list(self.root.iterdir()), [local])

    def test_doctor_only_reads_original_audio_defaults_without_launching_or_setting_them(self):
        loaded = config.load()
        calls = []
        def run(args, timeout=10):
            calls.append(args)
            stdout = b'[]' if args == ['pactl', '-f', 'json', 'list', 'sinks'] else (b'active\n' if args[0] == 'systemctl' else b'existing-user-default\n')
            return type('Result', (), {'returncode': 0, 'stdout': stdout})()
        with patch.object(diagnostics, 'run', side_effect=run), patch.object(diagnostics.shutil, 'which', return_value='/available'), patch.object(Path, 'home', return_value=self.root/'home'):
            report = diagnostics.local_report(loaded)
        self.assertTrue(report['audio_defaults_available'])
        self.assertNotIn('existing-user-default', json.dumps(report))
        self.assertEqual(calls, [
            ['systemctl', '--user', 'is-active', 'alisa-voice-bridge.service'],
            ['systemctl', '--user', 'is-enabled', 'alisa-voice-bridge.service'],
            ['pactl', 'get-default-sink'], ['pactl', 'get-default-source'],
            ['pactl', '-f', 'json', 'list', 'sinks'],
        ])
        self.assertEqual(list(self.root.iterdir()), [])


if __name__ == '__main__':
    unittest.main()

class ExpectedCompatibilityTests(unittest.TestCase):
    def test_unsupported_abi_or_invalid_mac_fails_before_ssh(self):
        from alice_root.config import station
        for field,value in [('abi','aarch64'),('bluetooth_mac','invalid')]:
            c={'station':{'host':'station.invalid',field:value}}
            with self.subTest(field=field),self.assertRaises(ValueError):station(c)

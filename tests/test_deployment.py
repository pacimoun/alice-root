"""Транзакции доставки: in-memory станция и реальные локальные journals."""
import contextlib
import copy
import json
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest.mock import patch

from alice_root import deployment, diagnostics


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.c = {'station': {'host': 'station.invalid', 'port': 2222, 'user': 'root',
                              'identity': str(root/'fixture-key'), 'model': 'yandexmini_2'},
                  'paths': {'bridge': root/'bridge', 'bridge_state': root/'bridge-state',
                            'state': root/'state', 'backup': root/'backups', 'cache': root/'cache'}}
        self.c['paths']['backup'].mkdir(mode=0o700)
        self.values = {
            deployment.BASE+'/native_agent.so': (b'new ARM fixture', 0o600),
            deployment.BASE+'/profile.sh': (b'new script fixture', 0o700),
            '/data/opt/entware/bin/socat': (b'new transport fixture', 0o700),
        }
        self.files = {}
        self.writes = []
        self.removes = []

    def inspect(self, c, name):
        return self.files.get(name)

    def put(self, c, name, data, mode):
        # Real installer must have durable backup and journal before first write.
        marker = c['paths']['state']/'remote-deploy.json'
        self.assertTrue(marker.is_file())
        record = json.loads(marker.read_bytes())
        self.assertEqual(record['phase'], 'prepared')
        self.assertTrue((Path(record['folder'])/'remote.json').is_file())
        self.writes.append(name)
        self.files[name] = data, mode

    def remote(self, c, command):
        if command == 'df -Pk /data | tail -n 1':
            return b'/dev/data 100000 10000 90000 10% /data\n'
        if command.startswith('rm '):
            name = shlex.split(command)[1]
            self.removes.append(name)
            del self.files[name]
            return b''
        raise AssertionError('Unexpected remote command: '+command)

    @contextlib.contextmanager
    def environment(self, report=None, put=None):
        report = report or {'supported': True, 'ota_guard_observed': True}
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(deployment, 'system_service', return_value='inactive'))
            stack.enter_context(patch.object(deployment, 'station_report', return_value=report))
            stack.enter_context(patch.object(deployment, 'payload', return_value=self.values))
            stack.enter_context(patch.object(deployment, 'inspect', side_effect=self.inspect))
            stack.enter_context(patch.object(deployment, 'put', side_effect=put or self.put))
            stack.enter_context(patch.object(deployment, 'remote', side_effect=self.remote))
            yield

    def test_foreign_last_target_causes_zero_writes_and_no_journal(self):
        last = list(self.values)[-1]
        self.files[last] = (b'foreign transport', 0o755)
        before = dict(self.files)
        with self.environment():
            with self.assertRaisesRegex(ValueError, 'STOP_FOREIGN_STATION_FILE'):
                deployment.deploy(self.c)
        self.assertEqual(self.files, before)
        self.assertEqual(self.writes, [])
        self.assertEqual(self.removes, [])
        self.assertFalse((self.c['paths']['state']/'remote-deploy.json').exists())
        self.assertEqual(list(self.c['paths']['backup'].iterdir()), [])

    def test_partial_transfer_rolls_back_new_files_preserves_preexisting(self):
        existing = list(self.values)[-1]
        self.files[existing] = self.values[existing]
        calls = 0
        def fail_second(c, name, data, mode):
            nonlocal calls
            calls += 1
            self.put(c, name, data, mode)
            if calls == 2:
                raise ValueError('SIMULATED_CONNECTION_LOSS_AFTER_RENAME')
        with self.environment(put=fail_second):
            with self.assertRaisesRegex(ValueError, 'SIMULATED_CONNECTION_LOSS'):
                deployment.deploy(self.c)
        self.assertEqual(self.files, {existing: self.values[existing]})
        self.assertEqual(set(self.removes), set(self.values)-{existing})
        self.assertFalse((self.c['paths']['bridge_state']/'bridge/config.json').exists())
        self.assertFalse((self.c['paths']['state']/'remote-deploy.json').exists())
        folders = list(self.c['paths']['backup'].iterdir())
        self.assertEqual(len(folders), 1)
        self.assertEqual(json.loads((folders[0]/'remote.json').read_bytes())['phase'], 'restored')

    def test_success_then_rollback_removes_only_created_objects(self):
        existing = list(self.values)[0]
        self.files[existing] = self.values[existing]
        with self.environment():
            result = deployment.deploy(self.c)
            self.assertEqual(result['result'], 'DEPLOYED')
            self.assertFalse(result['started'])
            self.assertTrue((self.c['paths']['bridge_state']/'bridge/config.json').exists())
            result = deployment.rollback(self.c)
        self.assertEqual(result['result'], 'STATION_ROLLED_BACK')
        self.assertEqual(self.files, {existing: self.values[existing]})
        self.assertNotIn(existing, self.removes)
        self.assertFalse((self.c['paths']['bridge_state']/'bridge/config.json').exists())

    def test_modified_remote_file_prevents_all_rollback_deletions(self):
        with self.environment():
            deployment.deploy(self.c)
            last = list(self.values)[-1]
            self.files[last] = (b'modified after deployment', 0o700)
            before = dict(self.files)
            with self.assertRaisesRegex(ValueError, 'STOP_REMOTE_MODIFIED'):
                deployment.rollback(self.c)
        self.assertEqual(self.files, before)
        self.assertEqual(self.removes, [])
        self.assertTrue((self.c['paths']['state']/'remote-deploy.json').exists())
        self.assertTrue((self.c['paths']['bridge_state']/'bridge/config.json').exists())

    def test_modified_local_config_prevents_all_remote_changes(self):
        with self.environment():
            deployment.deploy(self.c)
            config = self.c['paths']['bridge_state']/'bridge/config.json'
            config.write_bytes(b'user modifications')
            before = dict(self.files)
            with self.assertRaisesRegex(ValueError, 'STOP_CONFIG_MODIFIED'):
                deployment.rollback(self.c)
        self.assertEqual(self.files, before)
        self.assertEqual(self.removes, [])
        self.assertEqual(config.read_bytes(), b'user modifications')

    def test_changed_remote_mode_prevents_all_rollback_deletions(self):
        with self.environment():
            deployment.deploy(self.c)
            last = list(self.values)[-1]
            self.files[last] = (self.files[last][0], 0o777)
            before = dict(self.files)
            with self.assertRaisesRegex(ValueError, 'STOP_REMOTE_MODIFIED'):
                deployment.rollback(self.c)
        self.assertEqual(self.files, before)
        self.assertEqual(self.removes, [])
        self.assertTrue((self.c['paths']['state']/'remote-deploy.json').exists())

    def test_changed_endpoint_blocks_rollback_before_remote_inspection(self):
        with self.environment():
            deployment.deploy(self.c)
            before = dict(self.files)
            for field, changed in (('host', 'other.invalid'), ('port', 2223),
                                   ('user', 'other'), ('identity', '/PRIVATE/other-key')):
                config = copy.deepcopy(self.c)
                config['station'][field] = changed
                with self.subTest(field=field), \
                     patch.object(deployment, 'inspect', side_effect=AssertionError('wrong endpoint must not be contacted')):
                    with self.assertRaisesRegex(ValueError, 'STOP_STATION_ENDPOINT_CHANGED'):
                        deployment.rollback(config)
        self.assertEqual(self.files, before)
        self.assertEqual(self.removes, [])
        self.assertTrue((self.c['paths']['state']/'remote-deploy.json').exists())

    def test_foreign_mode_prevents_deployment_before_first_write(self):
        last = list(self.values)[-1]
        self.files[last] = (self.values[last][0], 0o777)
        with self.environment():
            with self.assertRaisesRegex(ValueError, 'STOP_FOREIGN_STATION_FILE'):
                deployment.deploy(self.c)
        self.assertEqual(self.writes, [])
        self.assertEqual(self.removes, [])
        self.assertFalse((self.c['paths']['state']/'remote-deploy.json').exists())

    def test_unsupported_station_or_missing_guard_never_builds_payload(self):
        for report in ({'supported': False, 'ota_guard_observed': True},
                       {'supported': True, 'ota_guard_observed': False}):
            with self.subTest(report=report), self.environment(report=report):
                with patch.object(deployment, 'payload', side_effect=AssertionError('must not build')):
                    with self.assertRaisesRegex(ValueError, 'STOP_UNSUPPORTED_STATION_OR_GUARD'):
                        deployment.deploy(self.c)
        self.assertEqual(self.writes, [])
        self.assertEqual(self.removes, [])

    def test_running_bridge_blocked_without_remote_read(self):
        with self.environment():
            with patch.object(deployment, 'system_service', return_value='active'), \
                 patch.object(deployment, 'station_report', side_effect=AssertionError('must not call')):
                with self.assertRaisesRegex(ValueError, 'STOP_ACTIVE_BRIDGE'):
                    deployment.deploy(self.c)
        self.assertEqual(self.writes, [])

    def test_wrong_model_firmware_or_quasar_architecture_is_not_supported(self):
        correct=('yandex,station-mini2\x00amlogic,axg\n'+diagnostics.FIRMWARE+'\n'+
                 'updatesd убран\nзаглушён\nподменён\nне запущен\n').encode()
        armhf=bytes([127,69,76,70,1,1,1]+[0]*11+[40,0])
        elf64=bytes([127,69,76,70,2,1,1]+[0]*11+[183,0])
        for text,header in ((correct.replace(diagnostics.FIRMWARE.encode(),b'other firmware'),armhf),
                            (correct.replace(b'yandex,station-mini2',b'other device'),armhf),(correct,elf64)):
            with self.subTest(header=header),patch.object(diagnostics,'remote',side_effect=[text,b' '.join(str(x).encode() for x in header)]):
                self.assertFalse(diagnostics.station_report(self.c)['supported'])
        with patch.object(diagnostics,'remote',side_effect=[correct,b' '.join(str(x).encode() for x in armhf)]):
            self.assertTrue(diagnostics.station_report(self.c)['supported'])

    def test_existing_config_preserved_without_remote_writes(self):
        config = self.c['paths']['bridge_state']/'bridge/config.json'
        config.parent.mkdir(parents=True)
        config.write_bytes(b'existing private config')
        with self.environment():
            with self.assertRaisesRegex(ValueError, 'STOP_EXISTING_BRIDGE_CONFIG'):
                deployment.deploy(self.c)
        self.assertEqual(config.read_bytes(), b'existing private config')
        self.assertEqual(self.writes, [])

    def test_remote_path_allowlist(self):
        for name in ('/etc/passwd', deployment.BASE+'/../secret', deployment.BASE+'/nested/file'):
            self.assertFalse(deployment.allowed(name))
        for name in self.values:
            self.assertTrue(deployment.allowed(name))


if __name__ == '__main__':
    unittest.main()

import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from alice_root import soundcloud as m


class FakeBrowser:
    def __init__(self, current):
        self.current = current
        self.closed = asyncio.Event()
        self.operations = []
        self.navigations = []

    async def page(self, operation):
        self.operations.append(operation['op'])
        return {'ok': True, 'track_url': self.current, 'playing': True}

    async def navigate(self, url):
        self.current = url
        self.navigations.append(url)


class PlaybackTests(unittest.IsolatedAsyncioTestCase):
    async def test_replaying_paused_current_track_preserves_queue_and_position(self):
        url = 'https://soundcloud.com/example/track'
        browser = FakeBrowser(url)
        result = await m.Controller(browser, None).perform('play', url)
        self.assertTrue(result['ok'])
        self.assertEqual(browser.navigations, [])
        self.assertEqual(browser.operations, ['status', 'resume', 'status'])

    async def test_different_track_requires_navigation(self):
        browser = FakeBrowser('https://soundcloud.com/example/old-track')
        target = 'https://soundcloud.com/example/new-track'
        await m.Controller(browser, None).perform('play', target)
        self.assertEqual(browser.navigations, [target])
        self.assertIn('play-track', browser.operations)

    async def test_unknown_socket_prevents_browser_start_and_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            control = root / 'control.sock'
            control.write_bytes(b'foreign sentinel')
            with patch.object(m, 'locations', return_value={'state': root, 'profile': root/'profile', 'socket': control}), patch.object(m, 'bluetooth_sink', return_value='bluez_output.example.1'), patch.object(m, 'Browser') as browser:
                with self.assertRaisesRegex(m.Failure, 'STOP_EXISTING_SOCKET'):
                    await m.serve()
                browser.assert_not_called()
            self.assertEqual(control.read_bytes(), b'foreign sentinel')

    async def test_bluetooth_reconnection_prevents_playing_to_wrong_sink(self):
        browser = FakeBrowser('https://soundcloud.com/example/track')
        with patch.object(m, 'bluetooth_sink', return_value='bluez_output.changed'):
            with self.assertRaisesRegex(m.Failure, 'BT_OUTPUT_CHANGED'):
                await m.Controller(browser, 'bluez_output.before').perform('resume')
        self.assertEqual(browser.operations, [])


class ProtocolTests(unittest.TestCase):
    def test_rejects_foreign_origins_playlists_credentials_and_shell_text(self):
        for value in ('http://soundcloud.com/example/track',
                      'https://other.example/example/track',
                      'https://soundcloud.com/example/sets/album',
                      'https://user@ soundcloud.com/example/track',
                      'https://soundcloud.com/example/track?token=secret',
                      'https://soundcloud.com/example/track\ncommand'):
            with self.subTest(value=value), self.assertRaises(m.Failure):
                m.validate('play', value)
        for action in ('pause', 'stop', 'status', 'diagnose'):
            with self.assertRaises(m.Failure):
                m.validate(action, 'unexpected')
        with self.assertRaises(m.Failure):
            m.validate('search', 'x' * 301)
        with self.assertRaises(m.Failure):
            m.validate('search', 'secret\ncommand')

    def test_dynamic_bluetooth_suffix_exact_device_and_preserved_defaults(self):
        result = type('Result', (), {'stdout': '1 bluez_output.AA_BB_CC_DD_EE_FF.9 module\n2 bluez_output.AA_BB_CC_DD_EE_00.1 module\n3 bluez_output.AA_BB_CC_DD_EE_FF00.1 module\n'})()
        with patch.object(m, 'configuration', return_value={'station': {'bluetooth_mac': 'AA:BB:CC:DD:EE:FF'}}), patch.object(m.subprocess, 'run', return_value=result) as run:
            self.assertEqual(m.bluetooth_sink(), 'bluez_output.AA_BB_CC_DD_EE_FF.9')
            self.assertEqual(run.call_args.args[0], ['pactl', 'list', 'short', 'sinks'])

    def test_locations_uses_local_config_without_creating_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / 'config.toml'
            config.write_text('[paths]\nstate="' + str(root / 'private') + '"\n')
            with patch.dict(os.environ, {'ALICE_ROOT_CONFIG': str(config)}):
                paths = m.locations()
            self.assertEqual(paths['socket'], root / 'private/soundcloud-control/control.sock')
            self.assertEqual(list(root.iterdir()), [config])

    def test_symlinked_state_ancestor_and_world_readable_profile_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / 'target'
            target.mkdir(mode=0o700)
            symlink = root / 'linked'
            symlink.symlink_to(target, target_is_directory=True)
            for state in (symlink / 'child',):
                with patch.object(m, 'configuration', return_value={'paths': {'state': str(state)}}):
                    with self.assertRaisesRegex(m.Failure, 'STOP_SYMLINK'):
                        m.locations()
            profile = root / 'shared'
            profile.mkdir(mode=0o755)
            profile.chmod(0o755)
            with patch.object(m, 'configuration', return_value={'soundcloud': {'profile': str(profile)}}):
                with self.assertRaisesRegex(m.Failure, 'PRIVATE_OWNED_DIRECTORY_REQUIRED'):
                    m.locations()


if __name__ == '__main__':
    unittest.main()

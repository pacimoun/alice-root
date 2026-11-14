import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
from alice_root.voice import integration as p

FIXTURES = Path(os.environ.get('ALICE_INTEGRATION_FIXTURES', '.cache/fixtures'))


class IntegrationTests(unittest.TestCase):
    def test_ambiguous_context_is_not_silently_replaced(self):
        with self.assertRaisesRegex(ValueError, 'STOP_UPSTREAM_STRUCTURE'):
            p.once('a\na\n', 'a\n', 'b\n')

    def test_agent_requires_armhf_not_a_fixed_binary(self):
        header=bytearray(40);header[:6]=b'\x7fELF\x01\x01';header[18]=40
        header[36:40]=(0x05000400).to_bytes(4,'little')
        p.require_agent(bytes(header)+b'any current build')
        for data in (b'not elf',bytes(header[:18])+b'\xb7\x00'+bytes(header[20:])):
            with self.assertRaisesRegex(ValueError,'STOP_AGENT_ARMHF_REQUIRED'):p.require_agent(data)


@unittest.skipUnless(all((FIXTURES/name).is_file() for name in p.INTEGRATION_FILES),
                     'Загрузите fixtures через scripts/fetch-integration-fixtures.py')
class UpstreamIntegrationTests(unittest.TestCase):
    def fixture(self,name):return (FIXTURES/name).read_bytes()

    def make_package(self,root):
        for name in p.INTEGRATION_FILES:
            path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(self.fixture(name))

    def test_current_source_changes_are_allowed_and_integration_is_repeatable(self):
        for name in p.INTEGRATION_FILES:
            original=self.fixture(name)+b'\n# local change unrelated to integration\n'
            adapted=p.integrate_python(name,original)
            self.assertEqual(p.integrate_python(name,adapted),adapted)
            self.assertIn(b'local change unrelated',adapted)

    def test_source_structure_error_precedes_all_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);self.make_package(root)
            target=root/'engines/bridge/browser_control.py';target.write_text('pass\n')
            before={str(x):x.read_bytes() for x in root.rglob('*') if x.is_file()}
            with self.assertRaisesRegex(ValueError,'STOP_UPSTREAM_STRUCTURE'):
                p.integrate_tree(root)
            self.assertEqual(before,{str(x):x.read_bytes() for x in root.rglob('*') if x.is_file()})

    def test_tree_repeat_and_symlink_refusal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);self.make_package(root)
            first=p.integrate_tree(root)
            before={str(x):x.read_bytes() for x in root.rglob('*') if x.is_file()}
            self.assertEqual(p.integrate_tree(root),first)
            self.assertEqual(before,{str(x):x.read_bytes() for x in root.rglob('*') if x.is_file()})
            target=root/'alisa_cli/autostart.py';target.unlink();target.symlink_to(root/'foreign')
            with self.assertRaisesRegex(ValueError,'STOP_SYMLINK'):p.integrate_tree(root)

    def load_autostart(self, root):
        pkg = types.ModuleType("fixture_alisa")
        pkg.__path__ = []
        state = types.ModuleType("fixture_alisa.state")
        state.ROOT, state.HOME, state.write_private = root, root / "state", lambda *args: None
        ssh = types.ModuleType("fixture_alisa.ssh")
        ssh.Station = object
        modules = {"fixture_alisa": pkg, "fixture_alisa.state": state, "fixture_alisa.ssh": ssh}
        module = types.ModuleType("fixture_alisa.autostart")
        module.__package__ = "fixture_alisa"
        with patch.dict(sys.modules, modules):
            exec(compile(p.integrate_python("alisa_cli/autostart.py", self.fixture("alisa_cli/autostart.py")), "autostart.py", "exec"), module.__dict__)
        return module

    def test_unit_paths_spaces_percent_and_verify(self):
        with tempfile.TemporaryDirectory(prefix="unit 100% ") as temporary:
            root = Path(temporary)
            (root / "alisa.py").write_text("pass\n")
            unit = self.load_autostart(root).unit_text({})
            self.assertIn("WorkingDirectory=" + str(root).replace("%", "%%") + "\n", unit)
            self.assertIn('ExecStart="', unit)
            self.assertNotIn('WorkingDirectory="', unit)
            path = root / "alisa-voice-bridge.service"
            path.write_text(unit)
            if __import__('shutil').which("systemd-analyze"):
                result = subprocess.run(["systemd-analyze", "--user", "--man=no", "verify", str(path)], capture_output=True, text=True)
                if "Failed to lookup RuntimeDirectory path" in result.stderr or "Failed to initialize manager" in result.stderr:
                    self.skipTest("systemd user manager environment unavailable")
                self.assertEqual(result.returncode, 0, result.stderr)

    def load_login(self):
        module = types.ModuleType("login_fixture")
        exec(compile(p.integrate_python("alisa_cli/login_browser.py", self.fixture("alisa_cli/login_browser.py")), "login_browser.py", "exec"), module.__dict__)
        return module

    def test_fedora_open_window_resolves_fallback_without_real_browser(self):
        from unittest.mock import Mock
        module = self.load_login()
        with tempfile.TemporaryDirectory() as temporary:
            proc = Mock(pid=431, poll=Mock(return_value=None))
            with patch.dict(os.environ, {"DISPLAY": ":200"}), patch.object(module, "profile_pid", return_value=None), patch.object(module.shutil, "which", side_effect=lambda tool: "/test/chromium-browser" if tool == "chromium-browser" else None), patch.object(module.subprocess, "Popen", return_value=proc) as launch, patch.object(module.time, "sleep"):
                self.assertEqual(module.open_window(Path(temporary) / "profile"), 431)
                self.assertEqual(launch.call_args.args[0][0], "/test/chromium-browser")
                self.assertNotIn("--no-sandbox", launch.call_args.args[0])

    def test_fedora_profile_pid_keeps_executable_profile_and_child_guards(self):
        module = self.load_login()
        with tempfile.TemporaryDirectory(prefix="profile spaces ") as temporary:
            root = Path(temporary)
            profile = root / "profile"
            profile.mkdir()
            (profile / "SingletonLock").symlink_to("fake-host-431")
            executable = root / "chromium-browser"
            executable.touch()
            proc = root / "proc/431"
            proc.mkdir(parents=True)
            (proc / "exe").symlink_to(executable)
            module.PROC = root / "proc"
            command = b"chromium-browser\0--user-data-dir=" + os.fsencode(profile)
            (proc / "cmdline").write_bytes(command + b"\0--no-first-run\0")
            self.assertEqual(module.profile_pid(profile), 431)
            for tail in (b"-foreign", b"\0--type=renderer"):
                (proc / "cmdline").write_bytes(command + tail)
                self.assertIsNone(module.profile_pid(profile))
            (proc / "cmdline").write_bytes(command)
            (proc / "exe").unlink()
            foreign = root / "foreign-browser"
            foreign.touch()
            (proc / "exe").symlink_to(foreign)
            self.assertIsNone(module.profile_pid(profile))

    def test_chromium_resolution_and_profile_guards_remain(self):
        doctor = p.integrate_python("alisa_cli/doctor.py", self.fixture("alisa_cli/doctor.py")).decode()
        self.assertIn("shutil.which('chromium-browser')", doctor)
        login = p.integrate_python("alisa_cli/login_browser.py", self.fixture("alisa_cli/login_browser.py")).decode()
        self.assertIn("process/'exe'", login)
        self.assertIn("--user-data-dir=", login)
        self.assertIn("chromium-browser", login)


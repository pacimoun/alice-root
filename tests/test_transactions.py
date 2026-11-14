import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from alice_root import transactions as tx


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = tx.Store(self.root/'state', self.root/'backups')
        self.one, self.two = self.root/'app/one', self.root/'bin/two'

    def files(self, version=b'first'):
        return {self.one: (version, 0o600), self.two: (version+b'-launcher', 0o700)}

    def test_verified_backups_idempotency_and_two_stage_rollback_preserve_profile(self):
        profile = self.root/'account-profile/sentinel'
        profile.parent.mkdir(mode=0o700)
        profile.write_bytes(b'private-account-sentinel')
        first = self.store.install(self.files())
        ownership = self.store.record.read_bytes()
        self.assertEqual(self.store.install(self.files())['result'], 'ALREADY_INSTALLED')
        self.assertEqual(self.store.record.read_bytes(), ownership)
        self.assertEqual(len(list(self.store.backup.iterdir())), 1)
        self.store.install(self.files(b'second'))
        self.assertEqual(self.store.rollback()['result'], 'ROLLED_BACK')
        self.assertEqual(self.one.read_bytes(), b'first')
        self.assertEqual(self.two.stat().st_mode & 0o777, 0o700)
        self.store.rollback()
        self.assertFalse(self.one.exists())
        self.assertFalse(self.two.exists())
        self.assertEqual(profile.read_bytes(), b'private-account-sentinel')
        self.assertEqual(self.store.rollback()['result'], 'NOTHING_TO_ROLLBACK')
        self.assertTrue((Path(first['backup'])/'restored').is_file())

    def test_foreign_conflict_prevents_any_target_write_or_backup(self):
        self.two.parent.mkdir()
        self.two.write_bytes(b'foreign')
        with self.assertRaisesRegex(ValueError, 'STOP_FOREIGN_OR_MODIFIED'):
            self.store.install(self.files())
        self.assertFalse(self.one.exists())
        self.assertEqual(self.two.read_bytes(), b'foreign')
        self.assertFalse(self.store.backup.exists())

    def test_symlink_target_and_ancestor_cannot_be_followed(self):
        target = self.root/'target'
        target.mkdir()
        (target/'sentinel').write_bytes(b'foreign')
        self.one.parent.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'STOP_SYMLINK'):
            self.store.install(self.files())
        self.assertEqual(list(target.iterdir()), [target/'sentinel'])
        self.assertFalse(self.store.state.exists())

    def test_foreign_mode_change_stops_update_and_rollback(self):
        self.store.install(self.files())
        self.two.chmod(0o755)
        with self.assertRaisesRegex(ValueError, 'STOP_MODE_CHANGED'):
            self.store.install(self.files(b'second'))
        with self.assertRaisesRegex(ValueError, 'STOP_MODE_CHANGED'):
            self.store.rollback()
        self.assertEqual(self.one.read_bytes(), b'first')
        self.assertEqual(self.two.stat().st_mode & 0o777, 0o755)

    def test_injected_mid_write_failure_restores_all_originals_and_ownership(self):
        self.store.install(self.files())
        previous = self.store.owned()
        atomic = tx.atomic
        failures = []
        def fail_second(path, data, mode=0o600):
            if Path(path) == self.two and data == b'second-launcher' and not failures:
                failures.append(True)
                raise OSError('injected write failure')
            return atomic(path, data, mode)
        with patch.object(tx, 'atomic', side_effect=fail_second):
            with self.assertRaisesRegex(OSError, 'injected write failure'):
                self.store.install(self.files(b'second'))
        self.assertEqual(self.one.read_bytes(), b'first')
        self.assertEqual(self.two.read_bytes(), b'first-launcher')
        self.assertEqual(self.store.owned(), previous)
        self.assertEqual(self.store.install(self.files())['result'], 'ALREADY_INSTALLED')

    def test_corrupt_backup_is_detected_before_first_rollback_write(self):
        self.store.install(self.files())
        update = self.store.install(self.files(b'second'))
        (Path(update['backup'])/'1').write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError, 'STOP_BACKUP_CORRUPT'):
            self.store.rollback()
        self.assertEqual(self.one.read_bytes(), b'second')
        self.assertEqual(self.two.read_bytes(), b'second-launcher')

    def pending_update(self):
        previous = self.store.owned()
        folder = self.store.snapshot([str(self.one), str(self.two)], previous)
        after = {str(p): {'mode': mode} for p, (b, mode) in self.files(b'second').items()}
        tx.write_json(folder/'intent.json', {'after': after})
        tx.atomic(self.one, b'second', 0o600)
        return folder, previous

    def test_power_loss_partial_transaction_requires_recovery_before_new_install(self):
        self.store.install(self.files())
        folder, previous = self.pending_update()
        with self.assertRaisesRegex(ValueError, 'STOP_PENDING_TRANSACTION'):
            self.store.install({self.root/'other': (b'new', 0o600)})
        self.assertEqual(self.store.recover(folder)['result'], 'RECOVERED')
        self.assertEqual(self.store.owned(), previous)
        self.assertEqual(self.one.read_bytes(), b'first')
        self.assertEqual(self.two.read_bytes(), b'first-launcher')
        self.assertEqual(self.store.recover(folder)['result'], 'ALREADY_RECOVERED')

    def test_recovery_cannot_discard_later_ownership(self):
        self.store.install(self.files())
        folder, previous = self.pending_update()
        newer = json.loads(json.dumps(previous))
        newer['files'][str(self.root/'unrelated')] = {'mode': 0o600}
        tx.write_json(self.store.record, newer)
        with self.assertRaisesRegex(ValueError, 'STOP_STALE_TRANSACTION'):
            self.store.recover(folder)
        self.assertEqual(self.store.owned(), newer)
        self.assertEqual(self.one.read_bytes(), b'second')

    def test_nonprivate_state_and_hardlinked_lock_are_not_chmodded(self):
        self.store.state.mkdir(mode=0o700)
        self.store.state.chmod(0o755)
        with self.assertRaisesRegex(ValueError, 'STOP_PRIVATE_DIRECTORY'):
            self.store.install(self.files())
        self.assertEqual(self.store.state.stat().st_mode & 0o777, 0o755)
        self.store.state.chmod(0o700)
        sentinel = self.root/'sentinel'
        sentinel.write_bytes(b'private')
        sentinel.chmod(0o600)
        os.link(sentinel, self.store.state/'transaction.lock')
        with self.assertRaisesRegex(ValueError, 'STOP_LOCK_OWNERSHIP'):
            self.store.install(self.files())
        self.assertEqual(sentinel.read_bytes(), b'private')
        self.assertEqual(sentinel.stat().st_mode & 0o777, 0o600)

    def test_independent_store_cannot_write_while_transaction_lock_held(self):
        with self.store.lock():
            other = tx.Store(self.store.state, self.store.backup)
            with self.assertRaises(BlockingIOError):
                other.install(self.files())
        self.assertFalse(self.one.exists())

    def test_recover_refuses_arbitrary_backup_and_committed_snapshot(self):
        installed = self.store.install(self.files())
        with self.assertRaisesRegex(ValueError, 'STOP_BACKUP_PATH'):
            self.store.recover(self.root/'foreign-snapshot')
        with self.assertRaisesRegex(ValueError, 'STOP_COMMITTED_TRANSACTION'):
            self.store.recover(installed['backup'])


if __name__ == '__main__':
    unittest.main()

class ChangedContentTests(unittest.TestCase):
    def test_edited_owned_file_can_be_updated_and_restored(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);s=tx.Store(root/'state',root/'backup');file=root/'main.py'
            s.install({file:(b'initial',0o600)})
            file.write_bytes(b'edited locally')
            s.install({file:(b'latest',0o600)})
            self.assertEqual(file.read_bytes(),b'latest')
            s.rollback()
            self.assertEqual(file.read_bytes(),b'edited locally')

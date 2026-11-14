"""Файловая транзакция с проверенными originals, journal и recovery.

Не захватывает чужие файлы. Сервис должен быть остановлен вызывающим кодом.
"""
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time


def safe(path):
    path = Path(path).absolute()
    if '..' in path.parts:
        raise ValueError('STOP_PATH')
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError('STOP_SYMLINK: '+str(part))
    if path.exists() and not (path.is_file() or path.is_dir()):
        raise ValueError('STOP_SPECIAL_FILE')
    return path


def atomic(path, data, mode=0o600):
    path = safe(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix='.alice-', dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'wb') as f:
            f.write(data); f.flush(); os.fsync(f.fileno())
        os.replace(name, path)
        # The directory entry is part of the durable journal, too.
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        with contextlib.suppress(FileNotFoundError): os.unlink(name)


def read_json(path):
    return json.loads(safe(path).read_bytes())


def write_json(path, data):
    atomic(path, (json.dumps(data, ensure_ascii=False, indent=2)+'\n').encode())


def _metadata(value):
    if (not isinstance(value, dict) or
            type(value.get('mode')) is not int or not 0 <= value['mode'] <= 0o777):
        raise ValueError('STOP_OWNERSHIP_SCHEMA')


def _private_directory(path):
    path = safe(path)
    if path.exists():
        st = path.stat()
        if not path.is_dir() or st.st_uid != os.geteuid() or st.st_mode & 0o077:
            raise ValueError('STOP_PRIVATE_DIRECTORY: '+str(path))
    else:
        path.mkdir(parents=True, mode=0o700)
    return path


class Store:
    def __init__(self, state, backup):
        self.state = safe(state)
        self.backup = safe(backup)
        self.record = self.state/'ownership.json'

    @contextlib.contextmanager
    def lock(self):
        _private_directory(self.state)
        lock = safe(self.state/'transaction.lock')
        if lock.exists():
            st = lock.stat()
            if not lock.is_file() or st.st_uid != os.geteuid() or st.st_nlink != 1 or st.st_mode & 0o077:
                raise ValueError('STOP_LOCK_OWNERSHIP')
        fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'r+b') as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield

    def _validate_owned(self, value):
        if (not isinstance(value, dict) or value.get('schema') != 1 or
                not isinstance(value.get('files'), dict) or not isinstance(value.get('transactions'), list)):
            raise ValueError('STOP_OWNERSHIP_SCHEMA')
        for name, meta in value['files'].items():
            if not isinstance(name, str) or not Path(name).is_absolute() or str(safe(name)) != name:
                raise ValueError('STOP_OWNERSHIP_PATH')
            _metadata(meta)
        for name in value['transactions']:
            if not isinstance(name, str) or not Path(name).is_absolute() or safe(name).parent != self.backup:
                raise ValueError('STOP_BACKUP_PATH')
        return value

    def owned(self):
        safe(self.record)
        if not self.record.exists(): return {'schema': 1, 'files': {}, 'transactions': []}
        st = self.record.stat()
        if not self.record.is_file() or st.st_uid != os.geteuid() or st.st_mode & 0o077:
            raise ValueError('STOP_OWNERSHIP_PERMISSIONS')
        return self._validate_owned(read_json(self.record))

    def check(self, paths, owned):
        for name in paths:
            p = safe(name)
            expected = owned['files'].get(str(p))
            if p.exists():
                if not p.is_file() or p.stat().st_uid != os.geteuid() or expected is None:
                    raise ValueError('STOP_FOREIGN_OR_MODIFIED: '+str(p))
                if stat.S_IMODE(p.stat().st_mode) != expected['mode']:
                    raise ValueError('STOP_MODE_CHANGED: '+str(p))
            elif expected is not None:
                raise ValueError('STOP_OWNED_FILE_MISSING: '+str(p))

    def _no_pending(self):
        if not self.backup.exists(): return
        _private_directory(self.backup)
        for folder in self.backup.iterdir():
            folder = safe(folder)
            if folder.is_dir() and (folder/'intent.json').exists() and not (folder/'committed').exists() and not (folder/'restored').exists():
                raise ValueError('STOP_PENDING_TRANSACTION: используйте recover '+str(folder))

    def snapshot(self, paths, owned):
        _private_directory(self.backup)
        folder = safe(self.backup/str(time.time_ns()))
        folder.mkdir(mode=0o700)
        entries = {}
        for idx, name in enumerate(paths):
            p = safe(name)
            if p.exists():
                data = p.read_bytes(); dest = folder/str(idx)
                atomic(dest, data)
                if dest.read_bytes() != data: raise ValueError('STOP_BACKUP_VERIFY')
                entries[str(p)] = {'blob': str(idx), 'size': len(data), 'mode': stat.S_IMODE(p.stat().st_mode)}
            else: entries[str(p)] = None
        write_json(folder/'snapshot.json', {'schema': 1, 'files': entries, 'previous': owned})
        return folder

    def _after_record(self, previous, after, folder):
        new = json.loads(json.dumps(previous))
        new['files'].update(after)
        new['transactions'].append(str(folder))
        return new

    def install(self, files):
        """files maps absolute path to (bytes, mode). All conflicts checked first."""
        normalized = {}
        for name, (data, mode) in files.items():
            if not Path(name).is_absolute(): raise ValueError('STOP_ABSOLUTE_PATH_REQUIRED')
            if not isinstance(data, bytes) or type(mode) is not int or not 0 <= mode <= 0o777:
                raise ValueError('STOP_INSTALL_VALUE')
            p = str(safe(name))
            if p in normalized: raise ValueError('STOP_DUPLICATE_PATH')
            normalized[p] = (data, mode)
        with self.lock():
            self._no_pending()
            owned = self.owned(); self.check(normalized, owned)
            changed = {p: (b, m) for p, (b, m) in normalized.items()
                       if not Path(p).exists() or Path(p).read_bytes() != b or stat.S_IMODE(Path(p).stat().st_mode) != m}
            if not changed: return {'changed': 0, 'result': 'ALREADY_INSTALLED'}
            folder = self.snapshot(changed, owned)
            after = {p: {'mode': m} for p, (b, m) in changed.items()}
            write_json(folder/'intent.json', {'after': after})
            # Ownership is committed only after every file is durable.
            try:
                for name, (data, mode) in changed.items(): atomic(name, data, mode)
                write_json(self.record, self._after_record(owned, after, folder))
                atomic(folder/'committed', b'1\n')
            except BaseException:
                self._restore(folder, allow_partial=True)
                raise
            return {'changed': len(changed), 'backup': str(folder), 'result': 'INSTALLED'}

    def _restore(self, folder, allow_partial=False):
        folder = safe(folder)
        if folder.parent != self.backup: raise ValueError('STOP_BACKUP_PATH')
        _private_directory(folder)
        snap = read_json(folder/'snapshot.json'); intent = read_json(folder/'intent.json')
        if (snap.get('schema') != 1 or not isinstance(snap.get('files'), dict) or
                not isinstance(intent.get('after'), dict) or set(snap['files']) != set(intent['after'])):
            raise ValueError('STOP_SNAPSHOT_SCHEMA')
        previous = self._validate_owned(snap.get('previous'))
        for name, before in snap['files'].items():
            if not Path(name).is_absolute() or str(safe(name)) != name: raise ValueError('STOP_BACKUP_PATH')
            _metadata(intent['after'][name])
            if before is not None:
                _metadata(before)
                if not isinstance(before.get('blob'), str) or not re.fullmatch(r'[0-9]+', before['blob']):
                    raise ValueError('STOP_BACKUP_PATH')
                if safe(folder/before['blob']).stat().st_size != before['size']:
                    raise ValueError('STOP_BACKUP_CORRUPT')
        expected_after = self._after_record(previous, intent['after'], folder)
        if self.owned() not in (previous, expected_after):
            raise ValueError('STOP_STALE_TRANSACTION')
        # Validate the whole set before restoring the first byte.
        for name, before in snap['files'].items():
            p = safe(name)
            if p.exists() and (not p.is_file() or p.stat().st_uid != os.geteuid()):
                raise ValueError('STOP_ROLLBACK_MODIFIED: '+str(p))
            if not p.exists():
                if not allow_partial: raise ValueError('STOP_OWNED_FILE_MISSING: '+str(p))
                continue
            permitted = {intent['after'][name]['mode']}
            if allow_partial and before: permitted.add(before['mode'])
            if stat.S_IMODE(p.stat().st_mode) not in permitted:
                raise ValueError('STOP_MODE_CHANGED: '+str(p))
        for name, before in reversed(list(snap['files'].items())):
            if before: atomic(name, (folder/before['blob']).read_bytes(), before['mode'])
            elif Path(name).exists(): Path(name).unlink()
        write_json(self.record, previous)
        atomic(folder/'restored', b'1\n')

    def rollback(self):
        with self.lock():
            self._no_pending()
            owned = self.owned()
            if not owned['transactions']: return {'result': 'NOTHING_TO_ROLLBACK'}
            folder = Path(owned['transactions'][-1]); self._restore(folder)
            return {'result': 'ROLLED_BACK', 'backup': str(folder)}

    def recover(self, folder):
        with self.lock():
            folder = safe(folder)
            if folder.parent != self.backup: raise ValueError('STOP_BACKUP_PATH')
            if (folder/'restored').exists(): return {'result': 'ALREADY_RECOVERED'}
            if (folder/'committed').exists(): raise ValueError('STOP_COMMITTED_TRANSACTION: используйте rollback')
            self._restore(folder, allow_partial=True)
            return {'result': 'RECOVERED'}

    def backup_current(self):
        with self.lock():
            self._no_pending()
            owned = self.owned(); self.check(owned['files'], owned)
            return {'backup': str(self.snapshot(owned['files'], owned)), 'result': 'BACKUP_VERIFIED'}

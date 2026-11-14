"""Загрузка текущих релизов и распаковка программных файлов."""
import json
from pathlib import Path, PurePosixPath
import tarfile
import urllib.request
from .transactions import safe, atomic

REPOSITORY = 'rison1337/Alisa-ChatGPT-voice-assistant'
ASSETS = {
    'alisa-voice': (REPOSITORY, 'alisa-voice-linux.tar.gz'),
    'socat': ('pierzchalski/static-bins', 'socat-aarch64-linux'),
}


def latest_release(repo=REPOSITORY):
    request = urllib.request.Request('https://api.github.com/repos/'+repo+'/releases/latest',
                                     headers={'User-Agent': 'alice-root'})
    with urllib.request.urlopen(request, timeout=30) as response:
        release = json.load(response)
    tag = release['tag_name']
    if not isinstance(tag, str) or not tag or '/' in tag or tag in ('.', '..'):
        raise ValueError('STOP_RELEASE_TAG')
    return release


def download(url, path, limit=256*1024*1024):
    request = urllib.request.Request(url, headers={'User-Agent': 'alice-root'})
    with urllib.request.urlopen(request, timeout=45) as response:
        data = response.read(limit+1)
    if not data or len(data)>limit:
        raise ValueError('STOP_DOWNLOAD_SIZE')
    atomic(path, data)
    return Path(path)


def fetch(name, cache):
    repo, asset = ASSETS[name]
    release = latest_release(repo)
    url = next((a['browser_download_url'] for a in release['assets'] if a['name']==asset), None)
    if not url: raise ValueError('STOP_RELEASE_ASSET_MISSING: '+asset)
    path = safe(cache)/name/release['tag_name']/asset
    if path.exists():
        if not path.is_file() or not path.stat().st_size: raise ValueError('STOP_EMPTY_ARTIFACT')
        return path
    return download(url, path)


def package_files(root):
    root = safe(root)
    if not (root/'alisa.py').is_file() or not (root/'engines/bridge/voice.py').is_file():
        raise ValueError('STOP_BRIDGE_PACKAGE_REQUIRED')
    files = []
    for path in root.rglob('*'):
        safe(path)
        if path.is_file() and path.name != 'package-manifest.json':
            files.append(path.relative_to(root).as_posix())
    return sorted(files)


def unpack(archive, destination):
    archive = safe(archive)
    destination = safe(destination)
    with tarfile.open(archive, 'r:gz') as source:
        members = source.getmembers()
        if len(members)>20000 or sum(m.size for m in members)>512*1024*1024:
            raise ValueError('STOP_ARCHIVE_LIMIT')
        seen = set()
        for m in members:
            p = PurePosixPath(m.name)
            if (p.is_absolute() or '..' in p.parts or '\\' in m.name or not p.parts or
                    p.parts[0]!='alisa-voice-linux' or not(m.isfile() or m.isdir()) or str(p) in seen):
                raise ValueError('STOP_ARCHIVE_PATH')
            seen.add(str(p))
        for m in members:
            path = safe(destination/m.name)
            if m.isdir(): path.mkdir(parents=True, exist_ok=True, mode=0o700)
            else:
                if path.exists(): raise ValueError('STOP_ARCHIVE_OVERWRITE')
                atomic(path, source.extractfile(m).read(), 0o700 if m.mode&0o111 else 0o600)
    root = destination/'alisa-voice-linux'
    package_files(root)
    return root

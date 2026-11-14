import io
import sys
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from alice_root import artifacts
from alice_root.transactions import atomic


class ArtifactTests(unittest.TestCase):
    def archive(self, path, extra=None):
        with tarfile.open(path, 'w:gz') as archive:
            entries={'alisa-voice-linux/alisa.py':b'print("current")\n',
                     'alisa-voice-linux/engines/bridge/voice.py':b'# changed upstream\n',
                     'alisa-voice-linux/package-manifest.json':b'arbitrary unused metadata'}
            if extra: entries.update(extra)
            for name,data in entries.items():
                item=tarfile.TarInfo(name);item.size=len(data)
                archive.addfile(item,io.BytesIO(data))

    def test_current_package_is_installed_without_a_reference_file_list(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);archive=root/'release.tar.gz'
            self.archive(archive,{'alisa-voice-linux/new_feature.py':b'pass\n'})
            package=artifacts.unpack(archive,root/'unpacked')
            names=artifacts.package_files(package)
            self.assertIn('new_feature.py',names)
            self.assertNotIn('package-manifest.json',names)
            self.assertEqual((package/'engines/bridge/voice.py').read_bytes(),b'# changed upstream\n')

    def test_archive_path_escape_is_rejected_before_extraction(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);archive=root/'bad.tar.gz'
            self.archive(archive,{'alisa-voice-linux/../../escape':b'foreign'})
            with self.assertRaisesRegex(ValueError,'STOP_ARCHIVE_PATH'):
                artifacts.unpack(archive,root/'unpacked')
            self.assertFalse((root/'unpacked').exists())
            self.assertFalse((root/'escape').exists())

    def test_fetch_uses_the_latest_release_and_accepts_cached_content(self):
        def release(tag):
            return {'tag_name':tag,'assets':[{'name':'alisa-voice-linux.tar.gz','browser_download_url':'https://example.invalid/'+tag}]}
        def download(url,path):
            atomic(path,url.encode());return path
        with tempfile.TemporaryDirectory() as d,patch.object(artifacts,'latest_release',side_effect=[release('v1'),release('v1'),release('v2')]),patch.object(artifacts,'download',side_effect=download) as network:
            cache=Path(d)
            first=artifacts.fetch('alisa-voice',cache)
            first.write_bytes(b'changed cache content')
            self.assertEqual(artifacts.fetch('alisa-voice',cache),first)
            current=artifacts.fetch('alisa-voice',cache)
            self.assertNotEqual(current,first)
            self.assertEqual(current.parent.name,'v2')
            self.assertEqual(network.call_count,2)

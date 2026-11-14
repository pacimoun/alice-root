import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from alice_root import firmware as f


def cpio(entries):
    out=bytearray()
    for name,data in [*entries.items(),('TRAILER!!!',b'')]:
        encoded=name.encode()+b'\0';values=[0,0o100644,0,0,1,0,len(data),0,0,0,0,len(encoded),0]
        out+=b'070702'+b''.join(f'{v:08x}'.encode() for v in values)+encoded
        out+=b'\0'*((-len(out))%4);out+=data;out+=b'\0'*((-len(out))%4)
    return bytes(out)


class FirmwareTests(unittest.TestCase):
    def test_real_ota_container_is_cpio_despite_zip_extension(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'mini2.zip';data=cpio({'sw-description':b'fixture','sw-description.sig':b'signature','rootfs.ubifs':b'image'})
            path.write_bytes(data)
            self.assertEqual(f.inspect(path)['format'],'cpio')
            path.write_bytes(data[:-20])
            with self.assertRaises(ValueError):f.inspect(path)

    def test_complete_partial_is_reused_without_network(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'mini2.zip';partial=Path(str(path)+'.part')
            partial.write_bytes(cpio({'sw-description':b'fixture','sw-description.sig':b'sig','rootfs.ubifs':b'image'}))
            with patch.object(f.subprocess,'run',side_effect=AssertionError('network')):
                self.assertEqual(f.fetch(path),path)
            self.assertFalse(partial.exists());self.assertTrue(path.exists())

    def test_interrupted_download_never_becomes_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'mini2.zip'
            def incomplete(args,**kwargs):Path(args[-1]).write_bytes(b'070702truncated')
            with patch.object(f.subprocess,'run',side_effect=incomplete):
                with self.assertRaises(ValueError):f.fetch(path)
            self.assertFalse(path.exists())

    def test_existing_fallback_and_normal_zip_work(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'mini2.zip'
            with zipfile.ZipFile(path,'w') as z:z.writestr('payload','fixture')
            with patch.object(f.subprocess,'run',side_effect=AssertionError('download')):
                self.assertEqual(f.fetch(path),path)
            self.assertEqual(f.inspect(path)['format'],'zip')

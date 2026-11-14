"""Загрузка OTA Mini 2 и проверка полноты контейнера, без эталонных хешей."""
from pathlib import Path
import subprocess
import zipfile
from .diagnostics import FIRMWARE
from .transactions import safe

URL=('https://quasar.s3.yandex.net/yandexmini_2/ota/user/'
     '7ce77cd8-0dba-45ab-8938-b751dc2d1988/'+FIRMWARE+'.zip')


def inspect(path):
    path=safe(path);size=path.stat().st_size
    with path.open('rb') as stream:
        magic=stream.read(6)
        if magic in (b'070701',b'070702'):
            stream.seek(0);names=set()
            for _ in range(10000):
                header=stream.read(110)
                if len(header)!=110 or header[:6] not in (b'070701',b'070702'):break
                try:values=[int(header[6+i*8:14+i*8],16) for i in range(13)]
                except ValueError:break
                length,namesize=values[6],values[11]
                if not 1<=namesize<=4096:break
                raw=stream.read(namesize)
                if len(raw)!=namesize or not raw.endswith(b'\0'):break
                name=raw[:-1].decode('utf-8')
                stream.seek((-stream.tell())%4,1)
                if name=='TRAILER!!!' and length==0:
                    if {'sw-description','sw-description.sig','rootfs.ubifs'}<=names:
                        return {'format':'cpio','size':size}
                    break
                if stream.tell()+length>size:break
                names.add(name);stream.seek(length,1);stream.seek((-stream.tell())%4,1)
            raise ValueError('OTA CPIO обрезан или не содержит образ/подпись')
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            if archive.testzip():raise ValueError('OTA ZIP повреждён')
        return {'format':'zip','size':size}
    raise ValueError('Нужен полный OTA пакет (CPIO или ZIP)')


def fetch(destination):
    destination=safe(destination)
    if destination.exists():inspect(destination);return destination
    destination.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    partial=safe(Path(str(destination)+'.part'))
    # A completed partial from an interrupted session is reusable.
    if partial.exists():
        try:inspect(partial)
        except (ValueError,OSError,zipfile.BadZipFile):pass
        else:partial.chmod(0o600);partial.replace(destination);return destination
    try:
        subprocess.run(['curl','--fail','--location','--retry','2','--connect-timeout','15',
                        '--max-time','300','--silent','--show-error',URL,'--output',str(partial)],check=True,timeout=950)
        inspect(partial);partial.chmod(0o600);partial.replace(destination)
    except BaseException:
        # Keep .part for diagnosis/retry, but never publish it as a ready image.
        raise
    return destination

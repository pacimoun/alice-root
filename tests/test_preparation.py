import base64
import io
import json
import os
import re
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import tarfile

from alice_root import preparation as p


def launch():
    return {'nodes':[{'tasks':[{'arg':['maind','--updatesd']},{'arg':['maind','--platformd']},
                              {'arg':['maind','--mediad']}]}],'unchanged':{'test':True}}


def key():
    return 'ssh-ed25519 '+base64.b64encode(struct.pack('>I',11)+b'ssh-ed25519'+struct.pack('>I',32)+b'x'*32).decode()+' test'


class FakeConnection:
    def __init__(self):
        self.files={p.Q+'/launch.json':json.dumps(launch()).encode(),p.Q+'/device_ota_update.sh':b'#!/bin/sh\noriginal\n',p.Q+'/ota_script.pub':b'original public key'}
        self.calls=[]
    def read(self,path):return self.files.get(path)
    def mode(self,path):return (0o755 if path.endswith(('.sh','.sh.orig')) else 0o644) if path in self.files else None
    def run(self,script,*args,**kwargs):
        self.calls.append(script)
        return ('0\nyandex,station-mini2\x00amlogic,axg\n'+p.FIRMWARE+'\n').encode()


class PreparationTests(unittest.TestCase):
    def test_launch_removes_only_updater_and_preserves_alisa(self):
        value=json.loads(p.without_updatesd(json.dumps(launch()).encode()))
        self.assertEqual(value['nodes'][0]['tasks'],launch()['nodes'][0]['tasks'][1:])
        self.assertEqual(value['unchanged'],launch()['unchanged'])
        self.assertEqual(p.without_updatesd(json.dumps(value).encode()),p.without_updatesd(json.dumps(value).encode()))
        with self.assertRaises(ValueError):p.without_updatesd(b'{"nodes":[]}')

    def test_plan_never_downloads_generates_keys_or_writes(self):
        c=FakeConnection();before=dict(c.files)
        with patch.object(p,'download',side_effect=AssertionError('download')),patch.object(p,'replacement_key',side_effect=AssertionError('openssl')),patch.object(p,'commit',side_effect=AssertionError('write')):
            result=p.prepare(c,Path('/unused'),key(),dry_run=True)
        self.assertEqual(result['result'],'PREPARATION_PLAN')
        self.assertEqual(c.files,before)

    def test_wrong_model_or_firmware_precedes_any_write(self):
        for result in (b'0\nother-model\n'+p.FIRMWARE.encode(),b'0\nyandex,station-mini2\nunknown'):
            c=FakeConnection();c.run=lambda *args:result
            with patch.object(p,'commit',side_effect=AssertionError('write')),self.assertRaises(ValueError):p.prepare(c,Path('/unused'),key())

    def test_existing_keys_are_preserved_and_duplicate_key_is_not_added(self):
        pub=p.public_key(key())
        old=b'ssh-ed25519 other-key old\n'
        updated=p.append_key(old,pub)
        self.assertTrue(updated.startswith(old))
        self.assertEqual(p.append_key(updated,pub+b' another comment'),updated)
        for bad in ('ssh-rsa invalid','ssh-ed25519 AAAA',key()+'\nsecond'):
            with self.assertRaises(ValueError):p.public_key(bad)

    def test_ota_payload_preserves_originals_and_modes(self):
        c=FakeConnection();c.files[p.ORIGINALS+'/ota_script.pub.orig']=b'earliest original'
        with patch.object(p,'replacement_key',return_value=b'new public key'):
            files=p.ota_payload(c)
        self.assertNotIn(p.ORIGINALS+'/ota_script.pub.orig',files)
        self.assertEqual(files[p.ORIGINALS+'/launch.json.orig'],(c.files[p.Q+'/launch.json'],0o644))
        self.assertNotIn(b'--updatesd',files[p.Q+'/launch.json'][0])
        self.assertIn(b'disabled by yandex-noupdate',files[p.Q+'/device_ota_update.sh'][0])
        self.assertIn('/data/opt/yandex-noupdate.sh',files)

    def test_already_disabled_ota_is_a_read_only_noop(self):
        c=FakeConnection()
        with patch.object(p,'ota_status',return_value={'ota_disabled':True}),patch.object(p,'commit',side_effect=AssertionError('write')):
            self.assertEqual(p.prepare(c,Path('/unused'),ota_only=True)['result'],'OTA_ALREADY_DISABLED')

    def test_adb_only_accepts_explicit_transport_target(self):
        for serial in ('-s evil','host;command','host$(command)'):
            with self.assertRaises(ValueError):p.Connection({},serial)

    def test_restore_ota_is_transactional_and_preserves_backup_modes(self):
        c=FakeConnection()
        for name in p.OTA_FILES:c.files[p.ORIGINALS+'/'+name+'.orig']=c.files[p.Q+'/'+name]
        with patch.object(p,'commit',return_value={'result':'PREPARED'}) as commit:
            result=p.restore_ota(c)
        self.assertEqual(result['result'],'OTA_RESTORED')
        payload=commit.call_args.args[1]
        self.assertEqual(payload[p.Q+'/launch.json'][0],c.files[p.ORIGINALS+'/launch.json.orig'])
        self.assertEqual(payload[p.Q+'/device_ota_update.sh'][1],0o755)
        c.files.pop(p.ORIGINALS+'/ota_script.pub.orig')
        with patch.object(p,'commit',side_effect=AssertionError('no write')):
            with self.assertRaises(ValueError):p.restore_ota(c)

    def test_generated_ssh_and_hostkey_transaction_is_valid_shell(self):
        paths=[p.Q+'/launch.json','/etc/init.d/S99zdropbear','/data/opt/bin/dropbearkey',p.SSH_DIR+'/hk_ed25519']
        script=p.transaction_script(paths,start_ssh=True,new_host_key=True)
        subprocess.run(['sh','-n'],input=script,text=True,check=True)
        self.assertIn('old-ssh-pid',script)
        self.assertNotIn('killall',script)


class ShellTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'station';self.root.mkdir()
        self.commands=Path(self.temp.name)/'bin';self.commands.mkdir()
        for name,text in [('proc/device-tree/compatible','yandex,station-mini2'),('proc/mounts','/dev/root / ext4 ro,relatime 0 0\n'),('system/vendor/quasar/version_full',p.FIRMWARE)]:
            dest=self.root/name;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_text(text)
        self.log=Path(self.temp.name)/'mount.log'
        for name,text in [('id','echo 0'),('sync','exit 0'),('mount','printf "%s\\n" "$*" >> "$MOUNT_LOG"')]:
            file=self.commands/name;file.write_text('#!/bin/sh\n'+text+'\n');file.chmod(0o700)
        self.environment={**os.environ,'PATH':str(self.commands)+':'+os.environ['PATH'],'MOUNT_LOG':str(self.log)}
        self.a='/data/opt/test-one';self.b=p.Q+'/launch.json'
        for path,data in [(self.a,b'before one'),(self.b,b'before two')]:
            dest=self.root/path.lstrip('/');dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(data);dest.chmod(0o640)

    def test_stock_data_alias_is_allowed_but_other_symlinks_are_not(self):
        from alice_root.diagnostics import remote_path_checks
        root=Path(self.temp.name)/'alias';(root/'bkupgrade/data/opt').mkdir(parents=True)
        (root/'data').symlink_to(root/'bkupgrade/data',target_is_directory=True)
        source=remote_path_checks('/data/opt/test')
        script=re.sub(r'/bkupgrade/data|/bkupgrade|/data',lambda m:str(root)+m[0],source)
        self.assertEqual(subprocess.run(['sh','-ec',script]).returncode,0)
        (root/'data').unlink();(root/'foreign').mkdir();(root/'data').symlink_to(root/'foreign',target_is_directory=True)
        self.assertNotEqual(subprocess.run(['sh','-ec',script]).returncode,0)

    def run_transaction(self,paths=None):
        paths=paths or [self.a,self.b,'/data/opt/new-file']
        payload=io.BytesIO()
        with tarfile.open(fileobj=payload,mode='w') as archive:
            for i,name in enumerate(paths):
                data=('after '+str(i)).encode();member=tarfile.TarInfo(str(i));member.mode=0o600;member.size=len(data);archive.addfile(member,io.BytesIO(data))
        script=p.transaction_script(paths,root=str(self.root)+'/')
        subprocess.run(['sh','-n'],input=script,text=True,check=True)
        return subprocess.run(['sh','-c',script],input=payload.getvalue(),capture_output=True,env=self.environment,timeout=15)

    def test_real_file_transaction_backup_and_recovery_restore_modes(self):
        result=self.run_transaction();self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn(b'PREPARED ',result.stdout)
        backup=Path(result.stdout.decode().split('PREPARED ')[1].strip())
        self.assertEqual((backup/'0.old').read_bytes(),b'before one')
        self.assertEqual((backup/'state').read_text().strip(),'committed')
        self.assertEqual((self.root/self.a.lstrip('/')).read_bytes(),b'after 0')
        subprocess.run(['sh',str(backup/'rollback.sh')],env=self.environment,check=True,capture_output=True)
        self.assertEqual((self.root/self.a.lstrip('/')).read_bytes(),b'before one')
        self.assertEqual((self.root/self.a.lstrip('/')).stat().st_mode&0o777,0o640)
        self.assertFalse((self.root/'data/opt/new-file').exists())
        self.assertIn('remount,ro',self.log.read_text())

    def test_failure_restores_all_files_and_original_readonly_mount(self):
        command=self.commands/'mv'
        real=shutil.which('mv')
        command.write_text('#!/bin/sh\ncase "$2" in */launch.json) exit 17;; esac\nexec '+real+' "$@"\n');command.chmod(0o700)
        result=self.run_transaction();self.assertNotEqual(result.returncode,0)
        self.assertEqual((self.root/self.a.lstrip('/')).read_bytes(),b'before one')
        self.assertEqual((self.root/self.b.lstrip('/')).read_bytes(),b'before two')
        self.assertFalse((self.root/'data/opt/new-file').exists())
        self.assertIn('remount,ro',self.log.read_text())

    def test_existing_readwrite_mount_is_preserved(self):
        (self.root/'proc/mounts').write_text('/dev/root / ext4 rw,relatime 0 0\n')
        result=self.run_transaction();self.assertEqual(result.returncode,0,result.stderr)
        self.assertFalse(self.log.exists())

    def test_symlink_temporary_target_cannot_modify_outside_file(self):
        foreign=Path(self.temp.name)/'foreign';foreign.write_bytes(b'untouched')
        (self.root/(self.a.lstrip('/')+'.alice-new')).symlink_to(foreign)
        result=self.run_transaction();self.assertNotEqual(result.returncode,0)
        self.assertEqual(foreign.read_bytes(),b'untouched')
        self.assertEqual((self.root/self.a.lstrip('/')).read_bytes(),b'before one')


class AdbTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        adb=self.root/'adb'
        adb.write_text('''#!/usr/bin/env python3
import os,shlex,shutil,subprocess,sys
args=sys.argv[1:]
assert args[:2]==['-s','station.invalid:5555']
args=args[2:]
if args[0]=='shell':
    assert args[1:4]==['-T','-e','none']
    if os.environ.get('FAKE_ADB_LEGACY')=='1':
        sys.stderr.buffer.write(b'error: device only supports allocating a pty\\n');sys.exit(1)
    command=' '.join(args[4:])
elif args[0]=='exec-out':
    command=' '.join(shlex.quote(a) for a in args[1:])
elif args[0]=='push':
    shutil.copyfile(args[1],args[2]);sys.exit(0)
else:raise AssertionError(args)
if os.environ.get('FAKE_ADB_NO_STATUS')=='1':
    sys.stdout.buffer.write(b'incomplete output');sys.exit(0)
data=sys.stdin.buffer.read() if args[0]=='shell' else None
result=subprocess.run(['sh','-c',command],input=data,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
sys.stdout.buffer.write(result.stdout);sys.stderr.buffer.write(result.stderr)
# Legacy client cannot report the actual remote exit status.
sys.exit(0 if args[0]=='exec-out' else result.returncode)
''')
        adb.chmod(0o700)
        self.environment=patch.dict(os.environ,{'PATH':str(self.root)+':'+os.environ['PATH']})
        self.environment.start();self.addCleanup(self.environment.stop)
        self.connection=p.Connection({},'station.invalid:5555')

    def test_raw_binary_input_output_and_exact_shell_quoting(self):
        data=b'\x00\xff\r\n\x01binary\n\n'
        target=self.root/'input with spaces and $special'
        for legacy in ('0','1'):
            with self.subTest(legacy=legacy),patch.dict(os.environ,{'FAKE_ADB_LEGACY':legacy}):
                self.assertEqual(self.connection.run('cat > '+p.shlex.quote(str(target)),data),b'')
                self.assertEqual(target.read_bytes(),data)
                self.assertEqual(self.connection.run('cat '+p.shlex.quote(str(target))),data)
                self.assertEqual(self.connection.run("printf '%s' \"literal 'quotes' and \\\"double\\\"\""),b"literal 'quotes' and \"double\"")

    def test_tar_payload_is_received_and_remote_failure_is_observed_on_legacy(self):
        archive=io.BytesIO()
        with tarfile.open(fileobj=archive,mode='w') as tar:
            member=tarfile.TarInfo('binary');data=bytes(range(256));member.size=len(data);tar.addfile(member,io.BytesIO(data))
        for legacy in ('0','1'):
            target=self.root/('unpacked-'+legacy);target.mkdir()
            with self.subTest(legacy=legacy),patch.dict(os.environ,{'FAKE_ADB_LEGACY':legacy}):
                self.connection.run('tar -xf - -C '+p.shlex.quote(str(target)),archive.getvalue())
                self.assertEqual((target/'binary').read_bytes(),data)
                with self.assertRaisesRegex(ValueError,'STOP_STATION_COMMAND'):
                    self.connection.run('exit 23')
                self.assertEqual(self.connection.run('printf partial; exit 23',check=False),b'partial')

    def test_missing_status_is_never_treated_as_success(self):
        with patch.dict(os.environ,{'FAKE_ADB_NO_STATUS':'1'}):
            for check in (True,False):
                with self.subTest(check=check),self.assertRaisesRegex(ValueError,'STOP_ADB_STATUS_MISSING'):
                    self.connection.run('exit 0',check=check)

    def test_timeout_is_reported_with_backup_without_retry(self):
        expired=subprocess.TimeoutExpired(['adb'],0.01,output=b'PREPARATION_BACKUP /data/opt/alice-root-preparation/tx.ABC123\n')
        with patch.object(p.subprocess,'run',side_effect=expired) as process:
            with self.assertRaisesRegex(ValueError,'STOP_STATION_TIMEOUT.*tx.ABC123'):
                self.connection.run('sleep 100',timeout=0.01)
        self.assertEqual(process.call_count,1)

    def test_remote_error_keeps_only_backup_path_in_public_message(self):
        with patch.dict(os.environ,{'FAKE_ADB_LEGACY':'1'}):
            with self.assertRaisesRegex(ValueError,'backup: /data/opt/alice-root-preparation/tx.ABC123') as error:
                self.connection.run('printf \"PREPARATION_BACKUP /data/opt/alice-root-preparation/tx.ABC123\\nprivate-output\\n\"; exit 17')
        self.assertNotIn('private-output',str(error.exception))

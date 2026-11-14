"""Run the real router shell script against local fake uci/nft commands."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
SCRIPT=ROOT/'scripts/router-rooting.sh'
FAKE=r'''#!/usr/bin/env python3
import json,os,pathlib,re,sys
name=pathlib.Path(sys.argv[0]).name
args=[a for a in sys.argv[1:] if a not in ('-q','-nn','-y')]
path=pathlib.Path(os.environ['MOCK_DB'])
d=json.loads(path.read_text());d['log'].append([name,*args])
def done(code=0,text=''):
 path.write_text(json.dumps(d));sys.stdout.write(text);sys.exit(code)
if name=='id':done(0,'0\n')
tag=name+':'+('check' if args and args[0]=='-c' else args[0] if args else '')
if name=='uci' and args and args[0]=='set':tag+=':'+args[1].split('=')[0]
if os.environ.get('FAIL_ON')==tag and not d.get('failed'):
 d['failed']=True;done(17)
if name=='uci':
 command=args[0];values=dict(d['committed']);values.update(d['pending'])
 if command=='changes':done(0,'pending\n' if d['pending'] else '')
 if command=='get':
  value=values.get(args[1]);done(0 if value is not None else 1,'' if value is None else value+'\n')
 if command=='show':
  key=args[1];items={k:v for k,v in values.items() if k==key or k.startswith(key+'.')}
  done(0 if items else 1,''.join(k+"='"+v+"'\n" for k,v in sorted(items.items())))
 if command=='set':
  key,value=args[1].split('=',1);d['pending'][key]=value;done()
 if command=='commit':d['committed'].update(d['pending']);d['pending']={};done()
 if command=='revert':
  key=args[1];d['pending']={k:v for k,v in d['pending'].items() if k!=key and not k.startswith(key+'.')};done()
 if command=='delete':
  key=args[1]
  for field in ('committed','pending'):d[field]={k:v for k,v in d[field].items() if k!=key and not k.startswith(key+'.')}
  done()
if name=='nft':
 if args[:1]==['-c']:done()
 if args[:1]==['list']:done(0 if d['table'] is not None else 1,(d['table'] or '')+'\n')
 if args[:1]==['delete']:d['table']=None;done()
 if args[:1]==['-f']:
  source=pathlib.Path(args[1]).read_text()
  assert 'flush ruleset' not in source
  mac=re.search(r'ether saddr ([a-f0-9:]+)',source).group(1)
  d['table']='table inet alice_root_recovery {\ncomment "alice-root-managed-v1"\nchain dns_redirect {\ntype nat hook prerouting priority -160; policy accept;\nether saddr '+mac+' udp dport 53 redirect to :53\nether saddr '+mac+' tcp dport 53 redirect to :53\n}\n}'
  done()
if name=='dnsmasq':done()
done(98)
'''


class RouterRootingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);bin=self.root/'bin';bin.mkdir()
        for name in ('uci','nft','id','dnsmasq'):
            p=bin/name;p.write_text(FAKE);p.chmod(0o700)
        self.db=self.root/'mock.json';self.db.write_text(json.dumps({'committed':{'dhcp.keep':'dnsmasq','dhcp.keep.server':'vpn-dns'},'pending':{},'table':None,'log':[]}))
        self.state=self.root/'journal';self.boot=self.root/'boot';self.boot.write_text('boot-A')
        self.env={**os.environ,'PATH':str(bin)+':'+os.environ['PATH'],'MOCK_DB':str(self.db),
                  'ALICE_ROUTER_STATE':str(self.state),'ALICE_DNSMASQ_INIT':str(bin/'dnsmasq'),'ALICE_ROUTER_BOOT_FILE':str(self.boot)}

    def run_script(self,*args,fail=None):
        env=dict(self.env)
        if fail:env['FAIL_ON']=fail
        return subprocess.run(['sh',str(SCRIPT),*args],capture_output=True,text=True,env=env,timeout=10)

    def data(self):return json.loads(self.db.read_text())
    def save(self,data):self.db.write_text(json.dumps(data))
    def apply(self,**kwargs):return self.run_script('apply','192.0.2.10','AA:BB:CC:DD:EE:FF',**kwargs)

    def test_apply_is_idempotent_and_cleanup_preserves_other_configuration(self):
        first=self.apply();self.assertEqual(first.returncode,0,first.stderr)
        self.assertIn('ROUTER_APPLIED',first.stdout)
        self.assertEqual(self.state.stat().st_mode&0o777,0o700)
        log=self.data()['log']
        self.assertLess(next(i for i,x in enumerate(log) if x[:2]==['nft','-c']),next(i for i,x in enumerate(log) if x[:2]==['uci','set']))
        self.assertIn('ROUTER_ALREADY_APPLIED',self.apply().stdout)
        self.assertIn('ROUTER_DNS_READY',self.run_script('status','192.0.2.10','AA:BB:CC:DD:EE:FF').stdout)
        result=self.run_script('cleanup','192.0.2.10','AA:BB:CC:DD:EE:FF')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIsNone(self.data()['table'])
        self.assertEqual(self.data()['committed'],{'dhcp.keep':'dnsmasq','dhcp.keep.server':'vpn-dns'})
        self.assertFalse(self.state.exists())
        self.assertFalse(Path(str(self.state)+'.lock').is_symlink())

    def test_foreign_section_or_table_is_preserved(self):
        for field in ('section','table'):
            d=self.data()
            if field=='section':d['committed']['dhcp.alice_root_dns']='foreign'
            else:d['table']='table inet alice_root_recovery { comment "foreign"; }'
            self.save(d);before=self.data()
            result=self.apply();self.assertNotEqual(result.returncode,0)
            self.assertEqual(self.data()['committed'],before['committed'])
            self.assertEqual(self.data()['table'],before['table'])
            self.assertFalse(self.state.exists())
            d=self.data();d['committed'].pop('dhcp.alice_root_dns',None);d['table']=None;self.save(d)

    def test_syntax_failure_has_no_network_configuration_mutations(self):
        result=self.apply(fail='nft:check');self.assertNotEqual(result.returncode,0)
        self.assertFalse(any(item[:2] in (['uci','set'],['dnsmasq','restart']) for item in self.data()['log']))
        self.assertFalse(self.state.exists())

    def test_partial_apply_failure_rolls_back_own_objects(self):
        for failure in ('uci:set:dhcp.alice_root_dns.ip','dnsmasq:restart','nft:-f'):
            with self.subTest(failure=failure):
                d=self.data();d.pop('failed',None);d['log']=[];self.save(d)
                result=self.apply(fail=failure);self.assertNotEqual(result.returncode,0)
                self.assertNotIn('dhcp.alice_root_dns',self.data()['committed'])
                self.assertIsNone(self.data()['table']);self.assertFalse(self.state.exists())

    def test_user_edits_block_cleanup_before_any_object_is_deleted(self):
        self.assertEqual(self.apply().returncode,0)
        d=self.data();d['table']+='\n# user edit';self.save(d)
        before=self.data();result=self.run_script('cleanup')
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(self.data()['committed'],before['committed'])
        self.assertEqual(self.data()['table'],before['table'])
        self.assertTrue(self.state.exists())

    def test_unsaved_uci_changes_and_wrong_cleanup_target_are_not_touched(self):
        d=self.data();d['pending']['dhcp.other']='user-change';self.save(d)
        self.assertNotEqual(self.apply().returncode,0);self.assertFalse(self.state.exists())
        d=self.data();d['pending']={};self.save(d)
        self.assertEqual(self.apply().returncode,0)
        before=self.data();result=self.run_script('cleanup','192.0.2.11','AA:BB:CC:DD:EE:FF')
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(self.data()['committed'],before['committed'])

    def test_cleanup_recovers_after_router_reboot_or_killed_process(self):
        self.assertEqual(self.apply().returncode,0)
        lock=Path(str(self.state)+'.lock')
        lock.symlink_to('alice-root-managed-v1:previous-boot:1')
        self.assertEqual(self.run_script('cleanup').returncode,0)
        self.assertEqual(self.apply().returncode,0)
        lock.symlink_to('alice-root-managed-v1:boot-A:99999999')
        (self.state/'phase').write_text('applying\n')
        self.assertEqual(self.run_script('cleanup').returncode,0)
        self.assertFalse(self.state.exists())

    def test_live_lock_and_invalid_arguments_do_not_mutate_configuration(self):
        lock=Path(str(self.state)+'.lock');lock.symlink_to('alice-root-managed-v1:boot-A:'+str(os.getpid()))
        self.assertNotEqual(self.apply().returncode,0);self.assertTrue(lock.is_symlink());lock.unlink()
        for ip,mac in [('bad','AA:BB:CC:DD:EE:FF'),('192.0.2.10','bad'),('127.0.0.1','AA:BB:CC:DD:EE:FF'),('192.0.2.10','AA:BB:CC:DD:EE:FF\nadd table inet foreign')]:
            self.assertNotEqual(self.run_script('apply',ip,mac).returncode,0)
        self.assertFalse(self.state.exists())

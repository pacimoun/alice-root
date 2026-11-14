#!/usr/bin/env python3
"""Пошаговый root Mini 2. Без команды запускается мастер; --dry-run только показывает план."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
sys.dont_write_bytecode=True
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from alice_root import rooting as r


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('step',nargs='?',default='wizard',choices=r.STEPS)
    parser.add_argument('--config',type=Path,default=Path('config.local.toml'))
    parser.add_argument('--dry-run',action='store_true')
    args=parser.parse_args(argv)
    os.umask(0o077)
    def stop(signum,frame):raise KeyboardInterrupt
    for name in ('SIGTERM','SIGHUP'):signal.signal(getattr(signal,name),stop)
    try:
        c=r.profile(args.config)
        if args.dry_run:
            result={'step':args.step,'firmware':str(c['firmware']),'sequence':['fetch','check','server','manual router/DNS/reset','prepare','backup','verify SSH','PC cleanup','manual router cleanup'],
                    'changes':False,'station_reset_automated':False}
        elif args.step=='wizard':result=r.wizard(c)
        elif args.step=='fetch':result=r.fetch_assets(c)
        elif args.step=='check':result=r.check(c)
        elif args.step=='router-script':result={'file':str(r.router_script(c))}
        elif args.step=='server':
            r.check(c)
            with r.server(c) as process:
                print('ROOT_SERVER_RUNNING; Ctrl+C — cleanup',flush=True);process.wait()
            result={'result':'ROOT_SERVER_STOPPED'}
        else:result=getattr(r,args.step)(c)
        print(json.dumps(result,ensure_ascii=False,indent=2));return 0
    except KeyboardInterrupt:
        print('Остановлено. При необходимости: rooting.py cleanup; на роутере: sh router-rooting.sh cleanup.',file=sys.stderr);return 130
    except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as error:
        print('ROOTING_ERROR: '+str(error),file=sys.stderr)
        print('Если DNS уже менялся, на роутере выполните: sh router-rooting.sh cleanup',file=sys.stderr)
        return 1


if __name__=='__main__':raise SystemExit(main())

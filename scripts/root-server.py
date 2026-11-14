#!/usr/bin/env python3
"""Локальный root-сервер Mini 2. DNS роутера настраивается отдельно."""
import argparse
from pathlib import Path
import json
import signal
import sys
sys.dont_write_bytecode=True
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from alice_root.root_server import run


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kit-dir',type=Path,required=True)
    parser.add_argument('--firmware-file',type=Path,required=True)
    parser.add_argument('--bind',required=True,help='LAN IPv4 ПК')
    parser.add_argument('--station-ip',required=True,help='зарезервированный DHCP IPv4 колонки')
    parser.add_argument('--upstream-file',type=Path,help='приватный файл реальных upstream IP до DNS override')
    parser.add_argument('--pid-file',type=Path,help=argparse.SUPPRESS)
    parser.add_argument('--check',action='store_true',help='проверить файлы, TLS и DNS без открытия портов')
    args=parser.parse_args()
    def stop(signum,frame):raise KeyboardInterrupt
    if not args.check:
        for name in ('SIGTERM','SIGHUP'):signal.signal(getattr(signal,name),stop)
    try:
        result=run(args.kit_dir.expanduser().absolute(),args.firmware_file.expanduser().absolute(),args.bind,args.station_ip,args.upstream_file,args.check,pid_file=args.pid_file)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 0
    except (OSError,ValueError,KeyError,RuntimeError) as error:
        print('ROOT_SERVER_ERROR: '+(str(error) if isinstance(error,ValueError) else type(error).__name__),file=sys.stderr)
        return 1


if __name__=='__main__':raise SystemExit(main())

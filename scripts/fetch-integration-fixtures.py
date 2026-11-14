#!/usr/bin/env python3
"""Загрузить исходники текущего релиза для тестов интеграции."""
from pathlib import Path
import argparse
import os
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from alice_root.artifacts import REPOSITORY, latest_release, download
from alice_root.voice.integration import INTEGRATION_FILES


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination',type=Path,default=Path(os.environ.get('XDG_CACHE_HOME',Path.home()/'.cache'))/'alice-root/integration-fixtures')
    args=parser.parse_args()
    tag=latest_release()['tag_name']
    for name in INTEGRATION_FILES:
        download(f'https://raw.githubusercontent.com/{REPOSITORY}/{tag}/{name}',args.destination/name,100000)
    print('INTEGRATION_FIXTURES_READY')


if __name__=='__main__':main()

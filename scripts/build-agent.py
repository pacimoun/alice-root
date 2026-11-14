#!/usr/bin/env python3
"""Сборка ARMhf-модуля Music. --fetch загружает исходники текущего релиза и Zig."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import struct
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from alice_root.artifacts import REPOSITORY, latest_release, download
from alice_root.transactions import safe
from alice_root.voice.integration import integrate_agent, require_agent

SOURCES = ('native_agent.c', 'custom_commands.h', 'tts_lease.h')
ZIG_VERSION = '0.14.1'
ZIG_URL = f'https://ziglang.org/download/{ZIG_VERSION}/zig-x86_64-linux-{ZIG_VERSION}.tar.xz'


def get(url, path, limit, fetch):
    path = safe(path)
    if fetch: return download(url, path, limit)
    if not path.is_file() or not path.stat().st_size:
        raise ValueError('Не найден '+path.name+'; повторите с --fetch')
    return path


def toolchain(work, fetch):
    destination = safe(work/f'zig-x86_64-linux-{ZIG_VERSION}')
    zig = safe(destination/'zig')
    if not zig.exists():
        archive = get(ZIG_URL, work/'zig-toolchain.tar.xz', 100*1024*1024, fetch)
        with tarfile.open(archive, 'r:xz') as source:
            members = source.getmembers()
            if len(members)>40000 or sum(m.size for m in members)>500*1024*1024:
                raise ValueError('STOP_TOOLCHAIN_SIZE')
            seen = set()
            for m in members:
                p = PurePosixPath(m.name)
                if (p.is_absolute() or '..' in p.parts or '\\' in m.name or not p.parts or
                        p.parts[0]!=destination.name or not(m.isfile() or m.isdir()) or m.name in seen):
                    raise ValueError('STOP_TOOLCHAIN_ARCHIVE')
                safe(work/m.name);seen.add(m.name)
            source.extractall(work, members=members, filter='data')
    if subprocess.check_output([str(zig),'version'],text=True,timeout=10).strip()!=ZIG_VERSION:
        raise ValueError('STOP_COMPILER_VERSION')
    return zig


def undefined_symbols(path):
    data = path.read_bytes()
    if data[:6] != b"\x7fELF\x01\x01":
        raise ValueError("SYMBOLS_REQUIRE_ELF32_LE")
    offset = struct.unpack_from("<I", data, 32)[0]
    stride, count = struct.unpack_from("<HH", data, 46)
    if stride != 40 or offset + stride * count > len(data):
        raise ValueError("INVALID_ELF_SECTIONS")
    sections = [struct.unpack_from("<10I", data, offset + i * stride) for i in range(count)]
    result = set()
    for section in sections:
        if section[1] != 11:
            continue
        table = sections[section[6]]
        strings = data[table[4]:table[4] + table[5]]
        if section[9] != 16:
            raise ValueError("INVALID_ELF_SYMBOLS")
        for position in range(section[4], section[4] + section[5], section[9]):
            name, value, size, info, other, index = struct.unpack_from("<IIIBBH", data, position)
            if name and index == 0:
                result.add(strings[name:].split(b"\0", 1)[0].decode("ascii"))
    return result



def build(work, fetch=False, host=False):
    work = safe(work)
    work.mkdir(parents=True, exist_ok=True)
    tag = latest_release()['tag_name'] if fetch else None
    for name in SOURCES:
        url = f'https://raw.githubusercontent.com/{REPOSITORY}/{tag}/engines/bridge/station/{name}'
        get(url, work/name, 100000, fetch)
    safe(work/'native_agent_music.c').write_bytes(integrate_agent((work/'native_agent.c').read_bytes()))
    for name in ('verity_music.h','agent_harness.c'):
        safe(work/name)
        shutil.copyfile(ROOT/'station'/name, work/name)
    zig = toolchain(work, fetch)
    env = {**os.environ,'ZIG_GLOBAL_CACHE_DIR':str(work/'zig-cache'),'ZIG_LOCAL_CACHE_DIR':str(work/'zig-local')}
    binary = work/'native_agent_music.so'
    for output,source in ((binary,'native_agent_music.c'),(work/'native_agent.base.so','native_agent.c')):
        safe(output)
        subprocess.run([str(zig),'cc','-target','arm-linux-gnueabihf','-mcpu=cortex_a7',
            '-O2','-fPIC','-shared','-nostdlib','-Wl,--hash-style=both','-Wl,-s',
            '-fno-stack-protector','-o',str(output),str(work/source)],check=True,env=env)
    require_agent(binary.read_bytes())
    introduced = undefined_symbols(binary)-undefined_symbols(work/'native_agent.base.so')
    if introduced: raise ValueError('STOP_UNRESOLVED_SYMBOLS: '+','.join(sorted(introduced)))
    result = {'abi':'ELF32 ARM EABI5 hard-float','binary':str(binary)}
    if host:
        for out,source,args in [('native_agent_music.host.so','native_agent_music.c',['-fPIC','-shared']),
                                ('agent-harness','agent_harness.c',['-rdynamic'])]:
            safe(work/out)
            subprocess.run([str(zig),'cc','-O2','-fno-stack-protector',*args,'-o',str(work/out),str(work/source)],check=True,env=env)
        result['host_harness'] = str(work/'agent-harness')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-dir',type=Path,default=Path(os.environ.get('XDG_CACHE_HOME',Path.home()/'.cache'))/'alice-root/build-agent')
    parser.add_argument('--fetch',action='store_true')
    parser.add_argument('--host-tests',action='store_true')
    args = parser.parse_args()
    print(json.dumps(build(args.work_dir.absolute(),args.fetch,args.host_tests),ensure_ascii=False,indent=2))


if __name__=='__main__':
    try: main()
    except (OSError,ValueError,subprocess.SubprocessError,tarfile.TarError) as error:
        raise SystemExit(str(error))

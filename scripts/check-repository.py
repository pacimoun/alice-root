#!/usr/bin/env python3
"""Локальный allowlist/secret/syntax check; ничего не отправляет в сеть."""
import ast
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
os.chdir(ROOT)
names=subprocess.check_output(['git','ls-files','--cached','--others','--exclude-standard','-z']).decode().split('\0')
errors=[];count=0
for name in sorted(set(names)-{''}):
    p=Path(name)
    if not p.is_file():continue
    if any(part in {'_handoff','node_modules','backup','backups','__pycache__','browser-profile','soundcloud-profile','.venv'} for part in p.parts) or name=='config.local.toml':
        errors.append(name+': private/generated path');continue
    if p.suffix in {'.zip','.gz','.xz','.so','.key','.pem','.log','.pyc'}:
        errors.append(name+': forbidden artifact');continue
    data=p.read_bytes();count+=1
    if b'\x00' in data:errors.append(name+': binary');continue
    try:text=data.decode('utf-8')
    except UnicodeError:errors.append(name+': encoding');continue
    # Patterns only; never print matching values.
    patterns=[r'-----BEGIN (?:OPENSSH |RSA |EC )?PRIVATE KEY-----',
        r'(?i)(?:access_token|refresh_token|session_token|authorization)\s*[=:]\s*[\"\x27]?[A-Za-z0-9_+/.-]{30,}',
        r'(?i)github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{30,}',
        r'\b192\.168\.\d{1,3}\.\d{1,3}\b',r'(?i)\b(?:[0-9A-F]{2}[:_]){5}[0-9A-F]{2}\b']
    if str(Path.home()) in text:errors.append(name+': private home path')
    scantext=re.sub(r'(?i)AA[:_]BB[:_]CC[:_]DD[:_]EE[:_][0-9A-F]{2}','',text).replace('00:11:22:33:44:55','').replace('00_11_22_33_44_55','')
    if any(re.search(pattern,scantext) for pattern in patterns):errors.append(name+': possible private identifier/secret')
    if p.suffix=='.py':
        try:ast.parse(data,filename=name)
        except SyntaxError:errors.append(name+': syntax')
# Staged contents may differ from the working tree; scan them independently.
for name in subprocess.check_output(['git','diff','--cached','--name-only','-z']).decode().split('\0'):
    if not name:continue
    r=subprocess.run(['git','show',':'+name],capture_output=True)
    staged=r.stdout.decode(errors='replace')
    scantext=re.sub(r'(?i)AA[:_]BB[:_]CC[:_]DD[:_]EE[:_][0-9A-F]{2}','',staged).replace('00:11:22:33:44:55','').replace('00_11_22_33_44_55','')
    if r.returncode==0 and (str(Path.home()) in staged or any(re.search(pattern,scantext) for pattern in patterns)):
        errors.append(name+': staged private content')
if errors:
    print('\n'.join(errors),file=sys.stderr);sys.exit(1)
print(f'REPOSITORY_CHECK_OK: {count} public/staged files; local pattern scanner (not proof of absence of all secrets)')

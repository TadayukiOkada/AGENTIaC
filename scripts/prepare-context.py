#!/usr/bin/env python3
"""Pack real tracked repository source/docs; count with worker tokenizer, never pad."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import urllib.request

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--count-mode', choices=['raw', 'chat'], default='raw')
p.add_argument('repository',type=Path);p.add_argument('--worker-base',required=True)
p.add_argument('--target-tokens',type=int,default=120000);p.add_argument('--max-tokens',type=int,default=125000)
p.add_argument('--api-key-file',type=Path,required=True)
p.add_argument('--output',type=Path,required=True);p.add_argument('--prioritize',nargs='*',default=[])
a=p.parse_args();os.umask(0o077)
repo=a.repository.resolve();revision=subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD'],text=True).strip()
if subprocess.check_output(['git','-C',str(repo),'status','--porcelain'],text=True):raise ValueError('context corpus must be a clean pinned checkout')
files=subprocess.check_output(['git','-C',str(repo),'ls-files','-z']).decode().split('\0')
allowed={'.c','.cc','.cpp','.h','.hpp','.py','.rs','.md','.txt','.log','.toml','.yaml','.json'}
files=[f for f in files if f and Path(f).suffix in allowed and not any(x in Path(f).parts for x in ['secrets','private','.git'])]
files=sorted(set(files),key=lambda f:(f not in a.prioritize,f))
parts=[];manifest=[]
for name in files:
    path=(repo/name).resolve()
    if not path.is_relative_to(repo) or path.stat().st_size>1024**2:continue
    try:text=path.read_text()
    except (UnicodeError,OSError):continue
    parts.append(f'\nFILE: {name}\n{text}\n')
    manifest.append({'path':name,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
    if sum(map(len,parts))>a.max_tokens*8:break
corpus=''.join(parts)
def count(text):
    payload = {'messages': [{'role': 'user', 'content': text}]} if a.count_mode == 'chat' else {'content': text, 'add_special': False}
    endpoint = '/v1/chat/completions/input_tokens' if a.count_mode == 'chat' else '/tokenize'
    req=urllib.request.Request(a.worker_base.rstrip('/')+endpoint,data=json.dumps(payload).encode(),headers={'Content-Type':'application/json','Authorization':'Bearer '+a.api_key_file.read_text().strip()})
    with urllib.request.urlopen(req,timeout=120) as r:d=json.load(r)
    return d['input_tokens'] if a.count_mode == 'chat' else len(d['tokens'])
lo,hi=0,len(corpus);selected='';tokens=0
# Binary-search an actual source prefix; at most16 tokenizer requests. No repeated filler.
for _ in range(16):
    mid=(lo+hi)//2;n=count(corpus[:mid])
    if a.target_tokens<=n<=a.max_tokens:selected=corpus[:mid];tokens=n;break
    if n>a.max_tokens:hi=mid-1
    else:selected=corpus[:mid];tokens=n;lo=mid+1
if tokens<a.target_tokens:raise RuntimeError(f'insufficient corpus: {tokens} tokens; choose larger real repository')
a.output.parent.mkdir(parents=True,exist_ok=True)
a.output.write_text(json.dumps({'repository_revision':revision,'tokenizer_endpoint':a.worker_base,'input_tokens':tokens,'token_count_mode':a.count_mode,'content':selected,'candidate_files':manifest,'content_sha256':hashlib.sha256(selected.encode()).hexdigest()},indent=2)+'\n')
print(json.dumps({'path':str(a.output),'input_tokens':tokens,'content_sha256':hashlib.sha256(selected.encode()).hexdigest()}))

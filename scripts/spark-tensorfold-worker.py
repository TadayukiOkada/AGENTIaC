import datetime,json,pathlib,signal,socket,subprocess,sys,time,urllib.request
R=pathlib.Path(__file__).resolve().parent
M=json.loads((R/'manifest.json').read_text()); N=M['container']; PORT=M['port']; S={'phase':'starting'}; started=False
def save():
 S['updated_at']=datetime.datetime.now(datetime.timezone.utc).isoformat(); tmp=R/'status.json.tmp';tmp.write_text(json.dumps(S,indent=2)+'\n');tmp.replace(R/'status.json')
def cmd(a): return subprocess.run(a,text=True,capture_output=True,timeout=30)
STOP='service termination requested'
def terminate(sig,frame): raise SystemExit(0)  # a requested stop (idle unload, release) is a clean exit
signal.signal(signal.SIGTERM,terminate)
def sample():
 d={x.split(':')[0]:int(x.split()[1])*1024 for x in pathlib.Path('/proc/meminfo').read_text().splitlines() if len(x.split())>1 and x.split()[1].isdigit()}; row={'at':time.time(),**{x:d[x] for x in ['MemAvailable','SwapTotal','SwapFree']}}
 with (R/'memory.jsonl').open('a') as f:f.write(json.dumps(row)+'\n')
 return d['MemAvailable']
def guard():
 if cmd(['docker','inspect','--format','{{.State.Running}}',N]).stdout.strip()!='true':raise RuntimeError('backend exited')
 top=cmd(['docker','top',N,'-eo','pid']); gpu=cmd(['nvidia-smi','--query-compute-apps=pid','--format=csv,noheader'])
 if top.returncode or gpu.returncode:raise RuntimeError('GPU/ownership check failed')
 if set(gpu.stdout.split())-{x.strip() for x in top.stdout.splitlines()[1:]}:raise RuntimeError('foreign GPU workload appeared')
try:
 save()
 if sample()<100*(1<<30):raise RuntimeError('admission less than 100 GiB available')
 gpu=cmd(['nvidia-smi','--query-compute-apps=pid','--format=csv,noheader'])
 if gpu.returncode or gpu.stdout.strip():raise RuntimeError('foreign GPU workload or GPU check failure')
 with socket.socket() as sock:
  if sock.connect_ex(('127.0.0.1',PORT))==0:raise RuntimeError('port occupied')
 existing=cmd(['docker','inspect',N])
 if existing.returncode==0:
  item=json.loads(existing.stdout)[0]
  if item['State']['Running'] or item['Config'].get('Labels',{}).get('io.agent-infra.owner')!='agent-infra' or item['Image']!='sha256:3442529299de2bed2c38c6c17abe50638ba152d7b3655150e9fcdca9570ef09f':
   raise RuntimeError('container name is in use by an unrecognized or running container')
  removed=cmd(['docker','rm',N])
  if removed.returncode:raise RuntimeError('could not remove stopped owned container')
 r=cmd(json.loads((R/'launch-args.json').read_text()))
 if r.returncode:raise RuntimeError('docker launch failed: '+r.stderr)
 started=True;S['phase']='loading';save()
 deadline=time.monotonic()+1300
 while time.monotonic()<deadline:
  sample();guard()
  try:
   with urllib.request.urlopen('http://127.0.0.1:'+str(PORT)+'/v1/models',timeout=3) as response:
    if json.load(response).get('data'):break
  except Exception:time.sleep(5)
 else:raise TimeoutError('readiness timeout')
 body={'model':'spark-tf114-bf16','messages':[{'role':'user','content':'Reply with OK.'}],'max_tokens':32,'temperature':0,'chat_template_kwargs':{'enable_thinking':False}}
 req=urllib.request.Request('http://127.0.0.1:'+str(PORT)+'/v1/chat/completions',data=json.dumps(body).encode(),headers={'Content-Type':'application/json'})
 with urllib.request.urlopen(req,timeout=120) as response:answer=json.load(response)
 if 'OK' not in (answer['choices'][0]['message'].get('content') or ''):raise RuntimeError('smoke check failed')
 S['phase']='ready';save()
 while True:
  sample();guard();time.sleep(10)
except BaseException as e:
 stopping=isinstance(e,SystemExit)
 if stopping:print(STOP,file=sys.stderr,flush=True)
 S['phase']='stopped' if stopping else 'failed';S['reason']=STOP if stopping else str(e);save();raise
finally:
 if started:
  stop=cmd(['docker','stop','-t','10',N]);S['stop_returncode']=stop.returncode;S['container_stopped']=stop.returncode==0;save()
  if stop.returncode:raise RuntimeError('container did not stop')

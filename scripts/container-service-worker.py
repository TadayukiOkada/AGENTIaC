"""The worker a declared service runs: launch the pinned container, admit it, wait for its readiness, hold it.

`<root>/service.json` (what the manifest declares), `<root>/launch-args.json` (the argv this site launches the engine
with, image reference included) and `<root>/backend-manifest.json` (the pin the launched container must match) are
written by `borrow.prepare_backend`. Nothing here names an engine: the container name, the port, the readiness route and
the launch command all come from those three files, which is what makes a second container-served engine a declaration
rather than a second script.

Deliberately not here yet, each because it is a measurement or a decision this checkout does not have:

- no memory admission threshold. Spark TensorFold's worker requires 100 GiB `MemAvailable`, a number that batch measured
  for its own engine; this engine's envelope is Stage 1c's measurement (§2.5 of the evaluation plan), and a guessed
  threshold on a 128 GB unified-memory host would refuse or admit by luck. Memory *is* sampled, so the measurement
  exists when the window runs.
- no inline smoke completion. TensorFold's worker asks its own model for "OK"; the generic readiness answer here is the
  declared route answering, and the text check is `scripts/smoke.py`'s, which the harness runs against the served front.
- no API key handling. `prepare_backend` writes `<root>/api.key`; how a given engine is *given* that key is an engine
  flag question, recorded as Stage 1c's (§2.3 of the evaluation plan).
"""
import datetime,json,pathlib,signal,socket,subprocess,sys,time,urllib.request
R=pathlib.Path(__file__).resolve().parent
SERVICE=json.loads((R/'service.json').read_text()); MANIFEST=json.loads((R/'backend-manifest.json').read_text())
N=SERVICE['container']; PORT=SERVICE['port']; STATUS_PATH=SERVICE.get('status_path') or '/v1/models'; LABEL='io.agent-infra.owner'
PIN=(MANIFEST.get('image') or {}).get('id'); S={'phase':'starting'}; started=False
def save():
 S['updated_at']=datetime.datetime.now(datetime.timezone.utc).isoformat(); tmp=R/'status.json.tmp'; tmp.write_text(json.dumps(S,indent=2)+'\n'); tmp.replace(R/'status.json')
def cmd(a): return subprocess.run(a,text=True,capture_output=True,timeout=30)
STOP='service termination requested'
def terminate(sig,frame): raise SystemExit(0)  # an idle unload or a release is a clean exit, not a failed unit
signal.signal(signal.SIGTERM,terminate)
def sample():
 d={x.split(':')[0]:int(x.split()[1])*1024 for x in pathlib.Path('/proc/meminfo').read_text().splitlines() if len(x.split())>1 and x.split()[1].isdigit()}
 with (R/'memory.jsonl').open('a') as f:f.write(json.dumps({'at':time.time(),**{k:d[k] for k in ('MemAvailable','SwapTotal','SwapFree')}})+'\n')
 return d['MemAvailable']
def guard():
 if cmd(['docker','inspect','--format','{{.State.Running}}',N]).stdout.strip()!='true':raise RuntimeError('backend exited')
 top=cmd(['docker','top',N,'-eo','pid']); gpu=cmd(['nvidia-smi','--query-compute-apps=pid','--format=csv,noheader'])
 if top.returncode or gpu.returncode:raise RuntimeError('GPU/ownership check failed')
 if set(gpu.stdout.split())-{x.strip() for x in top.stdout.splitlines()[1:]}:raise RuntimeError('foreign GPU workload appeared')
def ready():
 try:
  with urllib.request.urlopen('http://127.0.0.1:'+str(PORT)+STATUS_PATH,timeout=3) as response:return 200<=response.status<300
 except Exception:return False
try:
 save(); sample()
 gpu=cmd(['nvidia-smi','--query-compute-apps=pid','--format=csv,noheader'])
 if gpu.returncode or gpu.stdout.strip():raise RuntimeError('foreign GPU workload or GPU check failure')
 existing=cmd(['docker','inspect',N])
 if existing.returncode==0:
  item=json.loads(existing.stdout)[0]
  # The name is ours only if we labelled it and it runs the image the manifest pins: an unrecognized container holding
  # the name is not something this worker may delete, and a pinned image that is not the pinned image is a different engine.
  if item['State']['Running'] or item['Config'].get('Labels',{}).get(LABEL)!='agent-infra':raise RuntimeError('container name is in use by a running or unowned container')
  if PIN and item['Image']!=PIN:raise RuntimeError(f'container holds {item["Image"]}, the manifest pins {PIN}')
  if cmd(['docker','rm',N]).returncode:raise RuntimeError('could not remove stopped owned container')
 else:
  with socket.socket() as sock:
   if sock.connect_ex(('127.0.0.1',PORT))==0:raise RuntimeError('port occupied')
 r=cmd(json.loads((R/'launch-args.json').read_text()))
 if r.returncode:raise RuntimeError('docker launch failed: '+r.stderr)
 started=True; S['phase']='loading'; save()
 deadline=time.monotonic()+int(SERVICE.get('start_timeout_s') or 1300)
 while time.monotonic()<deadline:
  sample(); guard()
  if ready():break
  time.sleep(5)
 else:raise TimeoutError('readiness timeout on '+STATUS_PATH)
 S['phase']='ready'; save()
 while True:
  sample(); guard(); time.sleep(10)
except BaseException as e:
 stopping=isinstance(e,SystemExit)
 if stopping:print(STOP,file=sys.stderr,flush=True)
 S['phase']='stopped' if stopping else 'failed'; S['reason']=STOP if stopping else str(e); save(); raise
finally:
 if started:
  stop=cmd(['docker','stop','-t','10',N]); S['stop_returncode']=stop.returncode; S['container_stopped']=stop.returncode==0; save()
  if stop.returncode:raise RuntimeError('container did not stop')

"""Deterministic Stage A/B tasks. No paid judges or arbitrary host shell tools."""
import json
import os
import subprocess
from pathlib import Path
from . import declared
from .client import request


def tool(name, description, properties, required):
    return {'type':'function','function':{'name':name,'description':description,
            'parameters':{'type':'object','properties':properties,'required':required,'additionalProperties':False}}}


def invoke(base,key,model,messages,tools=None,max_tokens=2048,stream=True,reasoning_effort=None):
    payload={'model':model,'messages':messages,'max_tokens':max_tokens,'temperature':0,'seed':42,
             'stream':stream}
    if reasoning_effort is not None:
        if reasoning_effort != 'none':raise ValueError('only explicit reasoning_effort=none is supported')
        payload['reasoning_effort']=reasoning_effort
    if stream:
        payload['stream_options']={'include_usage':True}
    if tools:
        payload['tools']=tools
        payload['tool_choice']='auto'
    result=request(base,key,payload)
    result['request_payload']=json.loads(json.dumps(payload))
    return result


def tool_case(base,key,model,variant='single'):
    schema=tool('lookup_register','Read an embedded ECU register by name.',
                {'name':{'type':'string','enum':['CAN_STATUS','CAN_ERROR']},'bus':{'type':'integer','minimum':0,'maximum':3}},['name','bus'])
    prompt='Use lookup_register to read CAN_STATUS on bus 2. Do not guess the register value.'
    if variant=='parallel':
        prompt='Read CAN_STATUS and CAN_ERROR on bus 2 using lookup_register, then report their sum. Call both tools in one response if supported.'
    if variant=='error-recovery':
        prompt+=' If the tool fails, retry once as directed by its error.'
    messages=[{'role':'system','content':'Use the supplied tools. Respect JSON schemas. Do not invent results.'},{'role':'user','content':prompt}]
    trace=[]; seen=[]; error_sent=False
    for turn in range(5):
        r=invoke(base,key,model,messages,[schema]);trace.append(r);msg=r['message'];messages.append(msg)
        calls=msg.get('tool_calls') or []
        if not calls:
            target='43' if variant=='parallel' else '41'
            passed=bool(seen) and target in (msg.get('content') or '')
            if variant=='parallel':passed=passed and {'CAN_STATUS','CAN_ERROR'}.issubset(set(seen))
            if variant=='error-recovery':passed=passed and error_sent and len(seen)>=2
            return {'passed':passed,'trace':trace,'turns':turn+1,'tool_calls':len(seen),
                    'parallel_observed':any(len(x['message'].get('tool_calls',[]))>1 for x in trace)}
        for c in calls:
            if not c.get('id') or c.get('function',{}).get('name')!='lookup_register':
                return {'passed':False,'reason':'invalid tool identity','trace':trace}
            try:
                args=json.loads(c['function']['arguments'])
                assert set(args)=={'name','bus'} and type(args['bus']) is int and args['bus']==2
                assert args['name'] in ['CAN_STATUS','CAN_ERROR']
            except (ValueError,KeyError,AssertionError,TypeError):
                return {'passed':False,'reason':'invalid JSON/schema arguments','trace':trace}
            seen.append(args['name'])
            result={'value':41 if args['name']=='CAN_STATUS' else 2}
            if variant=='error-recovery' and not error_sent:
                result={'error':'temporary register read failure; retry the exact call once'};error_sent=True
            messages.append({'role':'tool','tool_call_id':c['id'],'content':json.dumps(result)})
    return {'passed':False,'reason':'tool turn budget exceeded','trace':trace}


FIXTURES={
'python':('parser.py', '''def decode(data):\n    """Decode little-endian unsigned 16-bit CAN payload; reject odd lengths."""\n    return [data[i] * 256 + data[i+1] for i in range(0,len(data),2)]\n''', ['python3','-c',"from parser import decode; assert decode(bytes([1,2,255,0]))==[513,255]; assert decode(b'')==[];\ntry: decode(b'x')\nexcept ValueError: pass\nelse: raise AssertionError('expected ValueError')"]),
'cpp':('ring.cpp', '''#include <array>\nstruct Ring {\n std::array<int,3> values{}; unsigned head=0, tail=0, count=0;\n bool push(int x){ if(count==values.size())return false; values[head]=x; head=(head+1)%values.size(); ++count;return true;}\n bool pop(int &x){ if(!count)return false; x=values[tail]; ++tail; --count; return true;}\n};\n\n''',['sh','-c','g++ -std=c++17 -fsanitize=address,undefined -g -I/work /grader/test.cpp -o /tmp/test && /tmp/test']),
'rust':('lib.rs', '''pub fn scale(raw: i16, factor: i16) -> i16 { raw * factor }\n\n''',['sh','-c','rustc --crate-name task --crate-type lib lib.rs -o /tmp/libtask.rlib && rustc --test /grader/test.rs --extern task=/tmp/libtask.rlib -o /tmp/test && /tmp/test'])}


def sandbox(work,command):
    return subprocess.run(['docker','run','--rm','--network=none','--read-only','--cap-drop=ALL',
            '--security-opt=no-new-privileges','--memory=512m','--memory-swap=512m','--cpus=2','--pids-limit=64',
            '--user','1000:1000','--tmpfs','/tmp:rw,exec,nosuid,size=128m','-v',str(work.resolve())+':/work:rw',
            '-v',str((work.parent/'grading').resolve())+':/grader:ro',
            '-e','PYTHONDONTWRITEBYTECODE=1','-e','PYTHONPATH=/work','-w','/work',os.environ.get('AGENTIAC_EVAL_IMAGE') or declared.resource('eval:baseline')]+command,capture_output=True,text=True,timeout=90)


def coding_case(base,key,model,language,work):
    filename,source,test=FIXTURES[language]
    work.mkdir(parents=True,exist_ok=True)
    (work/filename).write_text(source)
    grader=work.parent/'grading';grader.mkdir(exist_ok=True)
    (grader/'test.cpp').write_text('#include <cassert>\n#include "ring.cpp"\n'+'int main(){Ring r;int x;assert(!r.pop(x));for(int i=0;i<100;i++){assert(r.push(i));assert(r.pop(x));assert(x==i);}for(int i=0;i<3;i++)assert(r.push(i));assert(!r.push(9));for(int i=0;i<3;i++){assert(r.pop(x));assert(x==i);}assert(!r.pop(x));}')
    (grader/'test.rs').write_text('extern crate task;\n'+'#[cfg(test)] mod tests { use task::*; #[test] fn saturation(){assert_eq!(scale(30000,2),32767);assert_eq!(scale(-30000,2),-32768);assert_eq!(scale(123,2),246);assert_eq!(scale(-32768,-1),32767);}}')
    def safe(name):
        p=(work/name).resolve()
        if not p.is_relative_to(work.resolve()) or p==work.resolve():raise ValueError('path outside task workspace')
        return p
    tools=[tool('read_file','Read UTF-8 source',{'path':{'type':'string'}},['path']),
           tool('write_file','Replace UTF-8 source',{'path':{'type':'string'},'content':{'type':'string'}},['path','content']),
           tool('run_tests','Run the deterministic test suite in an isolated container',{},[])]
    messages=[{'role':'system','content':'Fix the bug using tools. Preserve tests and public API. Read the source, edit, then run tests. Only files in this task workspace are available.'},
              {'role':'user','content':f'Fix {filename}. '+{'python':'Decode unsigned little-endian 16-bit values. Reject odd payload lengths with ValueError.','cpp':'The fixed-size FIFO ring fails under repeated wraparound. Preserve bounded capacity.','rust':'scale must saturate to the i16 range instead of overflow.'}[language]}]
    trace=[];test_pass=False;wrote=False;reads=0
    for turn in range(12):
        r=invoke(base,key,model,messages,tools,max_tokens=4096);trace.append(r);msg=r['message'];messages.append(msg)
        if not msg.get('tool_calls'):
            final=sandbox(work,test)
            return {'passed':final.returncode==0 and test_pass and wrote and reads>0,'trace':trace,
                    'test_returncode':final.returncode,'test_output':(final.stdout+final.stderr)[-12000:],'turns':turn+1}
        for c in msg['tool_calls']:
            try:
                args=json.loads(c['function']['arguments']);name=c['function']['name']
                if name=='read_file':
                    result={'content':safe(args['path']).read_text()};reads+=1
                elif name=='write_file':
                    if args['path']!=filename:raise ValueError('only source file is editable')
                    safe(args['path']).write_text(args['content']);wrote=True;result={'ok':True}
                elif name=='run_tests':
                    t=sandbox(work,test);test_pass=t.returncode==0
                    result={'exit_code':t.returncode,'output':(t.stdout+t.stderr)[-12000:]}
                else:raise ValueError('unknown tool')
            except (ValueError,KeyError,OSError,TypeError,subprocess.TimeoutExpired) as exc:
                result={'error':str(exc)[:1000]}
            messages.append({'role':'tool','tool_call_id':c['id'],'content':json.dumps(result)})
    return {'passed':False,'reason':'turn budget exceeded','trace':trace}


def structured_case(base,key,model,stream=True):
    definition=tool('configure_can','Configure the CAN controller.',{'config':{'type':'object','properties':{
        'mode':{'type':'string','enum':['normal','listen-only']},'bitrate':{'type':'integer','enum':[125000,500000]},
        'filters':{'type':'array','items':{'type':'integer'}}},'required':['mode','bitrate','filters'],'additionalProperties':False}},['config'])
    messages=[{'role':'user','content':'Use configure_can with config mode listen-only, bitrate 500000, filters [291,1110]. Then report the applied configuration ID returned by the tool.'}]
    r=invoke(base,key,model,messages,[definition],stream=stream)
    trace=[r];calls=r['message'].get('tool_calls',[])
    if len(calls)!=1:return {'passed':False,'reason':'expected exactly one tool call','trace':trace}
    c=calls[0]
    try:
        args=json.loads(c['function']['arguments'])
        valid=c['function']['name']=='configure_can' and bool(c.get('id')) and args=={'config':{'mode':'listen-only','bitrate':500000,'filters':[291,1110]}}
        valid=valid and type(args['config']['bitrate']) is int and all(type(i) is int for i in args['config']['filters'])
    except (ValueError,TypeError,KeyError):valid=False
    if not valid:return {'passed':False,'reason':'nested schema or values invalid','trace':trace}
    messages += [r['message'],{'role':'tool','tool_call_id':c['id'],'content':'{"applied_configuration_id":73}'}]
    final=invoke(base,key,model,messages,[definition],stream=stream);trace.append(final)
    return {'passed':'73' in (final['message'].get('content') or '') and not final['message'].get('tool_calls'),'trace':trace,'tool_calls':1,'turns':2}


def abstention_case(base,key,model):
    definition=tool('lookup_register','Read an ECU register.',{'name':{'type':'string'}},['name'])
    r=invoke(base,key,model,[{'role':'user','content':'What is 2 + 2? Answer directly; do not use tools or read any registers.'}],[definition],max_tokens=256)
    return {'passed':not r['message'].get('tool_calls') and '4' in (r['message'].get('content') or ''),'trace':[r],'tool_calls':len(r['message'].get('tool_calls',[])),'turns':1}

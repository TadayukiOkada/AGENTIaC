"""OpenAI-compatible client with streaming tool-call assembly and latency metadata."""
import json
import time
import urllib.request


def request(base, key, payload, timeout=900):
    started = time.monotonic()
    req = urllib.request.Request(base.rstrip('/')+'/chat/completions', data=json.dumps(payload).encode(),
                                 headers={'Content-Type':'application/json','Authorization':'Bearer '+key})
    first = None
    calls, content, reasoning, usage, finish, timings = {}, [], [], {}, None, None
    with urllib.request.urlopen(req, timeout=timeout) as response:
        if not payload.get('stream'):
            result=json.load(response)
            elapsed=time.monotonic()-started
            msg=result['choices'][0]['message']
            return {'message':msg,'usage':result.get('usage',{}),'elapsed_s':elapsed,'ttft_s':None,
                    'finish_reason':result['choices'][0].get('finish_reason'),'timings':result.get('timings')}
        for line in response:
            if not line.startswith(b'data:'):
                continue
            data=line[5:].strip()
            if data==b'[DONE]':
                break
            obj=json.loads(data)
            if obj.get('error'):
                raise RuntimeError('stream returned API error')
            if obj.get('usage'):
                usage=obj['usage']
            if obj.get('timings'):
                timings=obj['timings']
            for choice in obj.get('choices',[]):
                delta=choice.get('delta',{})
                meaningful=delta.get('content') or delta.get('reasoning_content') or delta.get('tool_calls')
                if meaningful and first is None:
                    first=time.monotonic()-started
                content.append(delta.get('content') or '')
                reasoning.append(delta.get('reasoning_content') or '')
                finish=choice.get('finish_reason') or finish
                for tool in delta.get('tool_calls',[]):
                    i=tool.get('index',0)
                    call=calls.setdefault(i,{'id':'','type':'function','function':{'name':'','arguments':''}})
                    if tool.get('id'):
                        call['id']+=tool['id']
                    for field in ('name','arguments'):
                        call['function'][field]+=tool.get('function',{}).get(field) or ''
    if finish is None:
        raise RuntimeError('stream ended without finish reason')
    elapsed=time.monotonic()-started
    message={'role':'assistant','content':''.join(content)}
    if reasoning:
        message['reasoning_content']=''.join(reasoning)
    if calls:
        message['tool_calls']=[calls[i] for i in sorted(calls)]
    count=usage.get('completion_tokens')
    # SSE token chunks are not tokenizer tokens. Never estimate tok/s by counting chunks.
    tps=(count-1)/(elapsed-first) if count and count>1 and first is not None and elapsed>first else None
    return {'message':message,'usage':usage,'elapsed_s':elapsed,'ttft_s':first,
            'generation_tokens_s':tps,'finish_reason':finish,'timings':timings}

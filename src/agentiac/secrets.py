"""Protected local credential parsing and redaction; never print values."""
from pathlib import Path


def read_env(path):
    values={}
    for line in Path(path).read_text().splitlines():
        line=line.strip()
        if line and not line.startswith('#') and '=' in line:
            k,v=line.split('=',1);values[k]=v
    return values


def read_key(path, name='LITELLM_MASTER_KEY'):
    values=read_env(path)
    if name not in values or not values[name] or any(c in values[name] for c in '\r\n'):
        raise ValueError('invalid protected credential file')
    return values[name]


def redact(text, values):
    for value in sorted(set(values),key=len,reverse=True):
        if value:text=text.replace(value,'[REDACTED]')
    return text

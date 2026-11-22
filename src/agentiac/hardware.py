"""Hardware facts with confidence: PA §9.1 (T10.1), and the only source of `hw_fp` (PA §6).

`scripts/inventory.py` already reads every host with read-only probes, but it stores raw
command output: nothing can ask it what GPU a node has, and a value that could not be read
is indistinguishable from a value nobody looked for. This module turns those probe outputs
into the PA §9.1 record — one fact per key, each with a `value`, a `source` and a
`confidence` — and keeps the raw output in the timestamped snapshot for the human who has
to check a weird one.

Three rules hold the record honest:

* A fact that could not be read stays `unknown` with `value: null`. It is never dropped
  (the key is always present) and never filled from a similar host. `memory_bandwidth` is
  `unknown` on every host today because nothing here measures it; inventing it would be the
  exact failure PA §9.1 exists to prevent.
* `hw_fp` is computed by `identities.hardware_fingerprint` from the facts in this record,
  so the hash can never claim a machine the facts do not support. `validate` re-derives it
  and refuses a record whose hash does not follow from its own facts.
* Facts the operator declared in `infra/inventory.yml` (`node_class`, `inference_cpus`,
  `memory_limit`) are recorded with confidence `manual` and that file as their source. They
  are never mixed into the detected ones: a declared `memory_limit` of 97G is a cap, not
  the 120G of RAM `free -b` reports, and `hw_fp` reads only the detected facts.

`confidence` is one of `detected`, `measured`, `estimated`, `manual`, `unknown`. Only
`detected` and `manual` are produced here: `measured` and `estimated` belong to a probe
that runs a workload (T10.4 territory), and reserving the names now keeps a later
benchmark from being recorded as if it were a read.
"""
import datetime
import json
import os
from pathlib import Path
import re
import tempfile

from agentiac import identities

CONFIDENCE = ('detected', 'measured', 'estimated', 'manual', 'unknown')

#: facts read from the host; every key is always present, unknown ones as {'value': None}
DETECTED = ('cpu', 'cpu_count', 'arch', 'gpu', 'gpu_count', 'gpu_vram_mib', 'driver',
            'memory_total', 'memory_bandwidth', 'os', 'kernel', 'cuda')

#: facts the operator declared; kept apart from the detected ones because they are intentions, not readings
DECLARED = ('node_class', 'inference_cpus', 'memory_limit')

#: a record older than this is reported as stale: hardware does not change often, but a
#: month-old reading of a host someone re-imaged is worse than no reading
MAX_AGE_S = 30 * 24 * 3600


def fact(value, source, confidence='detected'):
    """One PA §9.1 fact entry."""
    return {'value': value, 'source': source, 'confidence': confidence}


def unknown_fact():
    """The one way a missing value may be written: present, null, and saying so."""
    return {'value': None, 'source': None, 'confidence': 'unknown'}


def _lines(probes, name):
    """The stdout of one probe, or '' when the probe did not run or failed to return."""
    return ((probes or {}).get(name) or {}).get('stdout') or ''


ARCHES = ('x86_64', 'aarch64', 'armv7l', 'riscv64', 'ppc64le', 's390x')


def parse_os(text):
    """`hostname; uname -a; cat /etc/os-release` -> os, kernel, arch.

    The arch is any known token of the line, not its last field: `uname -a` ends in
    `GNU/Linux` on glibc and repeats the machine three times on some kernels.
    """
    out = {}
    for line in text.splitlines():
        if line.startswith('PRETTY_NAME='):
            out['os'] = line.split('=', 1)[1].strip().strip('"')
        parts = line.split()
        if len(parts) > 2 and parts[0] == 'Linux':
            out['kernel'] = parts[2]
            out['arch'] = next((part for part in parts if part in ARCHES), None)
    return out


def parse_memory(text):
    """Total RAM in bytes: the `Mem:` row of `free -b`, or `MemTotal:` of /proc/meminfo.

    `free -b` has no `MemTotal:` label (that is /proc/meminfo), so reading only one of the
    two reports no memory on every host.
    """
    for line in text.splitlines():
        label, _, rest = line.partition(':')
        if label.strip() in ('Mem', 'MemTotal'):
            fields = rest.split()
            if fields and fields[0].isdigit():
                return int(fields[0])
    return None


def parse_cpu(text):
    """The cpu_facts probe -> arch, cpu_count and a CPU/SoC name, each or None.

    The probe labels its own lines, so a missing file cannot shift a value into the wrong
    fact. The name prefers what the machine calls itself (the device tree on a Jetson),
    then /proc/cpuinfo's `model name` on x86, then the SoC id; `CPU part` is deliberately
    not a name — a big.LITTLE host has two of them and picking one would be a guess.
    """
    out = {'arch': None, 'cpu_count': None, 'cpu': None}
    labelled, named = {}, {}
    for line in text.splitlines():
        key, _, value = line.partition(':')
        key, value = key.strip().lower(), value.strip()
        if not key:
            continue
        if key in ('arch', 'cpus', 'dtmodel', 'socid', 'socfamily'):
            if value:
                labelled[key] = value
        elif key in ('model name', 'model', 'hardware', 'soc', 'chip') and value and 'unknown' not in value.lower():
            named.setdefault(key, value)
    if labelled.get('arch') in ARCHES:
        out['arch'] = labelled['arch']
    if labelled.get('cpus', '').isdigit():
        out['cpu_count'] = int(labelled['cpus'])
    for key in ('dtmodel', 'socid', 'socfamily'):
        if labelled.get(key):
            out['cpu'] = labelled[key]
            break
    if out['cpu'] is None:
        for key in ('model name', 'model', 'hardware', 'soc', 'chip'):
            if named.get(key):
                out['cpu'] = named[key]
                break
    return out


def cpu_source(text):
    """Which file the CPU name came from, so the fact can name it."""
    for key, source in (('dtmodel', '/proc/device-tree/model'), ('socid', '/sys/devices/soc0/soc_id'),
                        ('socfamily', '/sys/devices/soc0/family')):
        for line in text.splitlines():
            if line.lower().startswith(key + ':') and line.partition(':')[2].strip():
                return source
    return '/proc/cpuinfo'


def parse_gpu(csv_text='', listing='', table=''):
    """GPU name, count, VRAM and driver, from the most reliable source that answered.

    `nvidia-smi --query-gpu` is preferred; the Orins' L4T does not always support it, so
    `nvidia-smi -L` and then the plain table are the fallbacks. A count of 0 is not a
    reading — it means no GPU was found, which is `unknown`, not a machine with no GPU.
    """
    out = {'gpu': None, 'gpu_count': None, 'gpu_vram_mib': None, 'driver': None}
    rows = [[cell.strip() for cell in line.split(',')] for line in csv_text.splitlines() if line.strip()]
    rows = [row for row in rows if len(row) == 3]
    if rows:
        names = sorted({row[0] for row in rows if row[0]})
        drivers = sorted({row[1] for row in rows if row[1]})
        memory = sorted({int(row[2]) for row in rows if row[2].isdigit()})
        out['gpu'] = ' + '.join(names) if names else None
        out['gpu_count'] = len(rows) or None
        out['driver'] = drivers[0] if len(drivers) == 1 else None
        out['gpu_vram_mib'] = min(memory) if memory else None
        return out
    listed = [line.split(':', 1)[1].split('(UUID:', 1)[0].strip() for line in listing.splitlines()
              if re.match(r'\s*GPU \d+:', line)]         # `GPU access blocked by ...` is a message, not a device
    if listed:
        out['gpu'] = ' + '.join(sorted({name for name in listed if name})) or None
        out['gpu_count'] = len(listed) or None
    for marker, key in (('Driver Version:', 'driver'),):
        for line in table.splitlines():
            if marker in line:
                value = line.split(marker, 1)[1].split()[0] if marker in line else ''
                if value:
                    out[key] = value
                break
    return out


def parse_cuda(text):
    """`nvcc --version` -> the CUDA release, or None."""
    for line in text.splitlines():
        if 'release' in line:
            return line.split('release', 1)[1].split(',')[0].strip() or None
    return None


def detected_facts(probes):
    """The DETECTED facts this probe set can support; anything else is simply absent here."""
    out = {}

    def add(name, value, source):
        if value not in (None, ''):
            out[name] = fact(value, source)

    system = parse_os(_lines(probes, 'os'))
    add('os', system.get('os'), '/etc/os-release')
    add('kernel', system.get('kernel'), 'uname -a')
    cpu_text = _lines(probes, 'cpu_facts')
    cpu = parse_cpu(cpu_text)
    add('cpu', cpu.get('cpu'), cpu_source(cpu_text))
    add('cpu_count', cpu.get('cpu_count'), 'nproc')
    add('arch', cpu.get('arch') or system.get('arch'), 'uname -m')
    gpu = parse_gpu(_lines(probes, 'gpu_facts'), _lines(probes, 'gpu_list'), _lines(probes, 'gpu'))
    for name in ('gpu', 'gpu_count', 'gpu_vram_mib', 'driver'):
        if gpu.get(name):
            out[name] = fact(gpu[name], 'nvidia-smi --query-gpu' if _lines(probes, 'gpu_facts') else 'nvidia-smi')
    add('memory_total', parse_memory(_lines(probes, 'resources') + '\n' + _lines(probes, 'meminfo')), 'free -b / /proc/meminfo')
    add('cuda', parse_cuda(_lines(probes, 'cuda')), 'nvcc --version')
    return out


def declared_facts(node):
    """The operator-declared facts of one inventory host, as confidence `manual`."""
    return {name: fact(node[name], 'infra/inventory.yml', 'manual')
            for name in DECLARED if node.get(name) is not None}


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')


GIB = 2 ** 30


def fingerprint_inputs(values):
    """The five facts `hw_fp` hashes, in the form it hashes them.

    RAM is rounded to whole GiB first. `free -b` reports what the kernel has left after
    firmware reservations, which is not a property of the machine: two identical Orins
    differ by 63 MiB, and an identity that calls them two machines cannot group a result
    set. The exact reading stays in the fact with its source; only the hash is coarse, and
    the cost of that coarseness is two hosts within half a GiB sharing an `hw_fp` while
    their records still show the bytes they each reported.
    """
    inputs = {name: values.get(name) for name in identities.HW_FP_FACTS}
    if isinstance(inputs.get('memory_total'), int):
        inputs['memory_total'] = round(inputs['memory_total'] / GIB) * GIB
    return inputs


def fingerprint_of(facts):
    """hw_fp of a facts record — computed in one place, so a record can never disagree with itself."""
    return identities.hardware_fingerprint(fingerprint_inputs(
        {name: entry['value'] if isinstance(entry, dict) else entry for name, entry in facts.items()}))


def record(host, probes, node=None, collected_at=None):
    """The PA §9.1 record of one host: every fact, its source and confidence, and hw_fp."""
    facts = detected_facts(probes)
    facts.update(declared_facts(node or {}))
    for name in (*DETECTED, *DECLARED):
        facts.setdefault(name, unknown_fact())
    return {'host': host, 'collected_at': collected_at or now_iso(),
            'hw_fp': fingerprint_of(facts), 'facts': facts,
            'unknowns': sorted(name for name, entry in facts.items() if entry['confidence'] == 'unknown')}


def validate(candidate):
    """Every way a record can be dishonest, as strings; empty means it may be written."""
    problems = []
    if not isinstance(candidate, dict):
        return ['record is not an object']
    if not candidate.get('host'):
        problems.append('host is missing')
    try:
        datetime.datetime.fromisoformat(candidate.get('collected_at') or '')
    except ValueError:
        problems.append(f'collected_at {candidate.get("collected_at")!r} is not an ISO timestamp')
    facts = candidate.get('facts')
    if not isinstance(facts, dict):
        problems.append('facts is missing')
        return problems
    for name in (*DETECTED, *DECLARED):
        if name not in facts:
            problems.append(f'fact {name} is missing (an unreadable fact is written as unknown, never left out)')
    for name, entry in facts.items():
        if not isinstance(entry, dict) or set(entry) != {'value', 'source', 'confidence'}:
            problems.append(f'fact {name} is not a value/source/confidence entry')
            continue
        if entry['confidence'] not in CONFIDENCE:
            problems.append(f'fact {name} has confidence {entry["confidence"]!r}, not one of {CONFIDENCE}')
        if entry['confidence'] == 'unknown':
            if entry['value'] is not None:
                problems.append(f'fact {name} is unknown but carries a value')
        elif entry['value'] is None:
            problems.append(f'fact {name} claims {entry["confidence"]} confidence with no value')
        elif not entry['source']:
            problems.append(f'fact {name} has a value and no source')
    unknowns = sorted(name for name, entry in facts.items()
                      if isinstance(entry, dict) and entry.get('confidence') == 'unknown')
    if candidate.get('unknowns') != unknowns:
        problems.append('unknowns does not list exactly the facts with confidence unknown')
    if candidate.get('hw_fp') != fingerprint_of(facts):
        problems.append('hw_fp does not follow from the facts of this record')
    return problems


def directory(root):
    """The canonical inventory directory of a checkout: PA §9.1's `artifacts/inventory/`."""
    return Path(root) / 'artifacts' / 'inventory'


def write(where, candidate):
    """Write one host's latest record into the inventory directory, refusing a dishonest one; returns the path."""
    problems = validate(candidate)
    if problems:
        raise ValueError(f'{candidate.get("host")}: ' + '; '.join(problems))
    target = Path(where) / f"{candidate['host']}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=target.parent, suffix='.tmp')
    with os.fdopen(handle, 'w') as out:
        json.dump(candidate, out, indent=2, sort_keys=True)
        out.write('\n')
    os.replace(temporary, target)  # a reader never sees a half-written record
    return target


def latest(where, host):
    """The last record written for a host, or None: missing and unreadable are not the same as empty."""
    path = Path(where) / f'{host}.json'
    try:
        candidate = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return None if validate(candidate) else candidate


def fingerprint(where, host, now=None, max_age_s=MAX_AGE_S):
    """hw_fp for one host with its provenance (T10.3): the value, when it was read, and whether to trust it.

    Never raises and never guesses. A host nobody has inventoried, a record that will not
    validate, and a record too old to be believed all report `hw_fp: unknown` with a reason,
    so a result that carries no hardware identity says so instead of borrowing another host's.
    """
    newest = latest(where, host)
    if newest is None:
        reason = 'no inventory record' if not (Path(where) / f'{host}.json').exists() \
            else 'inventory record does not validate'
        return {'hw_fp': identities.UNKNOWN, 'reason': reason}
    collected = datetime.datetime.fromisoformat(newest['collected_at'])
    age = (now or datetime.datetime.now(collected.tzinfo)) - collected
    age_s = round(age.total_seconds())
    entry = {'hw_fp': newest['hw_fp'], 'collected_at': newest['collected_at'], 'age_s': age_s}
    if age_s > max_age_s:
        entry['hw_fp'] = identities.UNKNOWN
        entry['stale'] = True
        entry['reason'] = f'recorded {age_s} s ago, older than {max_age_s} s'
    elif age_s < -300:  # a clock that jumped forward writes a record nobody read
        entry['hw_fp'] = identities.UNKNOWN
        entry['reason'] = f'collected_at {newest["collected_at"]} is in the future: the host clock is wrong'
    return entry

#!/usr/bin/env python3
"""`agentiac ops memory HOST`: print one stored reading; the logic is agentiac.telemetry."""
import argparse
from pathlib import Path
import sys

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE / 'src'))
from agentiac import paths  # noqa: E402
ROOT = paths.root()
from agentiac import declared, telemetry  # noqa: E402

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('host')
parser.add_argument('--floor', type=float, default=10)
parser.add_argument('--max-age', type=int, default=300)
args = parser.parse_args()
former = [old for old, host in declared.former_names(declared.hosts(ROOT)).items() if host == args.host]
code, line = telemetry.report(ROOT / 'artifacts/telemetry', args.host, args.floor, args.max_age, former=former)
print(line)
sys.exit(code)

from pathlib import Path
import tempfile
import unittest
from agentiac.secrets import read_key,read_env,redact

class SecretsTests(unittest.TestCase):
    def test_multiline_env_and_redaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'env';p.write_text('LITELLM_MASTER_KEY=fake-master\nORIN1_API_KEY=fake-worker\n')
            self.assertEqual(read_key(p),'fake-master')
            self.assertEqual(redact('bad Bearer fake-master and fake-worker',read_env(p).values()),'bad Bearer [REDACTED] and [REDACTED]')

"""pi compaction guard: its decisions (here) and the settings that size a session's compaction.

tests/test_compaction_e2e.py is the proof the guard works: the pinned pi binary against a backend
that fails the way the model did. An earlier version of this file drove the hook by hand and passed
for a guard whose hook pi never called on a summary request.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'src'))
from agentiac import paths  # noqa: E402
SITE = paths.root()
import agent  # noqa: E402


@unittest.skipUnless(agent.NODE.exists() and agent.PI.exists(), 'the pinned pi install is not here')
class CompactionGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory(dir=SITE / 'artifacts') as tmp:
            # Inside the checkout's artifacts/, so the guard's import of pi resolves to the pinned one.
            guard = Path(tmp) / 'guard.ts'
            guard.write_text(agent.COMPACTION_GUARD.read_text().replace(
                'from "@earendil-works/pi-coding-agent";\nimport type',
                f'from "{agent.PI.parents[1] / "@earendil-works/pi-coding-agent/dist/index.js"}";\nimport type'))
            # The harness drives synthetic failures. Left to inherit, AGENTIAC_COMPACTION_GUARD_LOG
            # points at the production log — running the suite from inside a pi session appends the
            # harness's fake `boom` failures to the file an operator reads to learn whether the guard is
            # still summarising.
            log = Path(tmp) / 'guard-log.jsonl'
            result = subprocess.run([str(agent.NODE), '--experimental-strip-types',
                                     str(Path(__file__).with_name('compaction_guard_harness.mjs')), str(guard)],
                                    capture_output=True, text=True, timeout=120,
                                    env=dict(os.environ, NODE_NO_WARNINGS='1', PI_OFFLINE='1',
                                             AGENTIAC_COMPACTION_GUARD_LOG=str(log)))
            logged = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        if result.returncode:
            raise AssertionError(result.stderr)
        cls.out = json.loads(result.stdout)
        cls.logged = logged

    def test_every_failure_and_skip_the_harness_simulated_reaches_the_log_it_was_given(self):
        """A summary that silently stops appearing is the failure this guard exists to prevent, so the
        log is not decoration and the harness has to prove it is written — and prove it somewhere other
        than the production file. Running the suite from inside a pi session used to append the harness's
        fake `boom` failures to the file an operator reads to decide whether the guard is still working.

        No `summarized` line belongs here: the harness drives the failure and backoff hooks, and a
        summary is only logged by the path that actually summarises. So a `summarized` entry in the real
        log can only have come from a real session, which is what makes the file worth reading."""
        events = [entry['event'] for entry in self.logged]
        self.assertEqual(events.count('failed'), 3, events)
        self.assertEqual(events.count('skipped'), 6, events)
        self.assertNotIn('summarized', events, 'the harness cannot produce a summary; one here means the log is not the harness\'s')
        self.assertTrue(all(entry.get('error') == 'boom' for entry in self.logged if entry['event'] == 'failed'))

    def test_a_failed_compaction_skips_the_next_threshold_checks(self):
        backoff = self.out['backoff']
        self.assertEqual(backoff['healthy'], [False])
        self.assertEqual(backoff['afterOne'], [True, True, False])
        self.assertEqual(backoff['afterTwo'], [True, True, True, True, False])
        self.assertEqual(backoff['ladder'], [2, 4, 8, 16, 16, 16])

    def test_a_cancelled_compaction_is_not_a_failure_or_the_skip_would_feed_itself(self):
        self.assertEqual(self.out['backoff']['afterAbort'], [False])

    def test_the_last_resorts_are_never_skipped(self):
        self.assertFalse(self.out['backoff']['overflowDuringSkip'])
        self.assertFalse(self.out['backoff']['manualDuringSkip'])

    def test_a_success_clears_the_backoff(self):
        self.assertEqual(self.out['backoff']['afterSuccess'], [False])

    def test_every_failure_is_said_out_loud(self):
        self.assertEqual(self.out['notes'], ['error'] * 3)

    def test_only_a_chat_template_switch_model_is_summarized_here(self):
        # Both names pi sends chat_template_kwargs under; a guard that knows only one leaves
        # the other to pi's own path, which asks a thinking-high session to summarize itself
        # (local-coder with qwen-chat-template).
        self.assertEqual(self.out['summarizesHere'],
                         {'chatTemplate': True, 'qwenChatTemplate': True, 'otherFormat': False,
                          'noCompat': False, 'noModel': False})

    def test_file_lists_survive_a_compaction_this_extension_wrote(self):
        carry = self.out['carry']
        self.assertEqual(carry['none'], {'read': ['own.py'], 'edited': []})
        self.assertEqual(carry['ours'], {'read': ['a.py', 'own.py'], 'edited': ['b.py']})
        self.assertEqual(carry['pisOwn'], {'read': ['own.py'], 'edited': []})  # pi already carried these
        self.assertEqual(carry['latestWins'], {'read': ['c.py', 'own.py'], 'edited': []})
        self.assertEqual(carry['malformed'], {'read': ['own.py'], 'edited': []})


class CompactionSettingsTests(unittest.TestCase):
    """The other half of the fix: compact early enough to have somewhere to put the summary."""

    def test_a_reasoning_model_reserves_room_for_its_whole_summary(self):
        model = {'id': 'worker-a', 'reasoning': True, 'maxTokens': 16384, 'contextWindow': 131072}  # not the live route
        block = agent.compaction([model])
        self.assertEqual(block, {'enabled': True, 'reserveTokens': 32768, 'keepRecentTokens': 12000})
        # 0.8 of the reserve is more than maxTokens, so pi's clamp leaves the summary with maxTokens.
        self.assertGreater(block['reserveTokens'] * 8 // 10, model['maxTokens'])
        self.assertEqual(model['contextWindow'] - block['reserveTokens'], 98304)  # the session died past 114688

    def test_a_model_without_thinking_is_sized_too_because_the_settings_file_is_shared(self):
        # Left out, a smaller model inherited a larger model's 32768 and its trigger went below zero.
        tracked = json.loads((SITE / 'agents/pi/models.json').read_text())['providers']['local-infra']['models']
        non_reasoning = next((m['id'] for m in tracked if not m.get('reasoning') and m.get('contextWindow')), None)
        model = agent.model_entry(non_reasoning, True) if non_reasoning else agent.model_entry('local-coder', False)
        self.assertFalse(model.get('reasoning'))
        block = agent.compaction([model])
        self.assertGreater(model['contextWindow'] - block['reserveTokens'], 0)

    def test_a_non_thinking_model_still_counts_in_a_router_session(self):
        low = {'id': 'low', 'reasoning': False, 'maxTokens': 4096, 'contextWindow': 16384}
        high = {'id': 'high', 'reasoning': True, 'maxTokens': 32768, 'contextWindow': 200000}
        self.assertEqual(agent.compaction([low, high])['reserveTokens'], 4096)

    def test_a_small_window_is_never_asked_to_reserve_more_than_it_can_work_in(self):
        block = agent.compaction([{'id': 'small', 'reasoning': True, 'maxTokens': 16384, 'contextWindow': 65536}])
        self.assertLessEqual(block['reserveTokens'], 65536 // 4)
        self.assertLess(block['keepRecentTokens'], 65536)

    def test_the_tightest_model_wins_when_a_session_has_two(self):
        low = {'id': 'low', 'reasoning': True, 'maxTokens': 16384, 'contextWindow': 131072}
        high = {'id': 'high', 'reasoning': True, 'maxTokens': 32768, 'contextWindow': 200000}
        self.assertEqual(agent.compaction([low, high])['reserveTokens'], 32768)

    def test_the_http_idle_timeout_covers_a_cold_prefill_and_defers_to_the_gateway(self):
        # pi's 300000 default killed long cold-prefill local-coder turns over 100k tokens on
        # large inputs: the strata worker sends its first chunk at ~0.3 s and then nothing
        # for the whole cold prefill, and pi read 300 s of silence as `terminated`.
        self.assertGreater(agent.HTTP_IDLE_TIMEOUT_MS, 350_000)  # the worst silence measured (181k input)
        self.assertLess(agent.HTTP_IDLE_TIMEOUT_MS, 900 * 1000)  # gateway/config.yaml timeout: 900 - the gateway stays the backstop
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            (state / 'settings.json').write_text(json.dumps({'lastChangelogVersion': '1.5.0'}))
            agent.write_settings(state, {'enabled': True, 'reserveTokens': 32768, 'keepRecentTokens': 12000})
            written = json.loads((state / 'settings.json').read_text())
            self.assertEqual(written['httpIdleTimeoutMs'], agent.HTTP_IDLE_TIMEOUT_MS)
            self.assertEqual(written['lastChangelogVersion'], '1.5.0')  # pi's own key, not ours to drop

    def test_settings_are_written_into_the_agent_dir_and_other_keys_survive(self):
        block = {'enabled': True, 'reserveTokens': 32768, 'keepRecentTokens': 12000}
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            (state / 'settings.json').write_text(json.dumps({'lastChangelogVersion': '1.3.0'}))
            agent.write_settings(state, block)
            written = json.loads((state / 'settings.json').read_text())
            self.assertEqual(written['lastChangelogVersion'], '1.3.0')  # pi's own key, not ours to drop
            self.assertEqual(written['compaction'], block)
            agent.write_settings(state, dict(block, reserveTokens=4096))  # the last session's block does not stay
            self.assertEqual(json.loads((state / 'settings.json').read_text())['compaction']['reserveTokens'], 4096)
            (state / 'settings.json').write_text('{not json')
            with self.assertRaises(SystemExit):
                agent.write_settings(state, block)  # reported, never overwritten

    def test_the_guard_is_loaded_without_a_flag_because_its_absence_costs_the_whole_session(self):
        self.assertTrue(agent.COMPACTION_GUARD.exists())
        source = (ROOT / 'scripts/agent.py').read_text()
        self.assertIn("env['AGENTIAC_COMPACTION_GUARD_LOG']", source)
        self.assertIn("['-e', str(COMPACTION_GUARD)]", source)
        self.assertNotIn('--compaction-guard', source)  # not a flag: nothing to forget to pass


if __name__ == '__main__':
    unittest.main()

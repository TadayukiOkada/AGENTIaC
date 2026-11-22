import importlib.util
import json
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('context_probe', Path(__file__).resolve().parents[1] / 'benchmarks/context-probe.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ContextProbeTests(unittest.TestCase):
    def test_cross_file_answer_requires_all_evidence(self):
        answer = dict(module.EXPECTED)
        answer['sources'] = list(reversed(answer['sources']))
        answer['random_source'] = 'std::random_device {} ()'
        self.assertTrue(module.grade(json.dumps(answer))['passed'])
        answer['cli_conversion'] = 'std::stoi(value)'
        self.assertFalse(module.grade(json.dumps(answer))['passed'])
        del answer['sentinel']
        self.assertFalse(module.grade(json.dumps(answer))['passed'])

    def test_malformed_answers_fail_without_crashing(self):
        for value in (None, 'not JSON', '[]', '{}', json.dumps({**module.EXPECTED, 'sources': [1, 'x']})):
            with self.subTest(value=value):
                self.assertFalse(module.grade(value)['passed'])

    def test_fenced_json_keeps_format_failure_and_reports_content_separately(self):
        answer = '```json\n' + json.dumps(module.EXPECTED) + '\n```'
        result = module.grade(answer)
        self.assertFalse(result['passed'])
        self.assertFalse(result['strict_json'])
        self.assertTrue(result['content_passed'])

    def test_edited_source_rejects_a_stale_cached_answer(self):
        original = 'prefix params.sampling.seed = std::stoul(value); suffix'
        content, expected, edits = module.prepare_variant(original, 'edited')
        self.assertIn('std::stoull(value)', content)
        self.assertEqual(edits[0]['character_offset'], 7)
        self.assertFalse(module.grade(json.dumps(module.EXPECTED), expected)['content_passed'])
        self.assertTrue(module.grade(json.dumps(expected), expected)['passed'])
        self.assertEqual(module.prepare_variant(original, 'warm')[0], original)
        with self.assertRaises(ValueError):
            module.prepare_variant(original + original, 'edited')

    def test_followup_checks_only_its_requested_source_fields(self):
        expected = {'sentinel': module.EXPECTED['sentinel']}
        self.assertTrue(module.grade(json.dumps(expected), expected)['passed'])
        self.assertFalse(module.grade('{"sentinel":"0"}', expected)['passed'])
        self.assertFalse(module.grade('{}', expected)['passed'])

    def test_cpp_names_and_cli_flags_remain_case_sensitive(self):
        answer = dict(module.EXPECTED)
        answer['sentinel'] = '0xffffffff'
        self.assertTrue(module.grade(json.dumps(answer))['passed'])
        answer['cli_conversion'] = 'STD::STOUL(VALUE)'
        self.assertFalse(module.grade(json.dumps(answer))['passed'])
        answer['cli_conversion'] = module.EXPECTED['cli_conversion']
        answer['cli_option'] = '--SEED'
        self.assertFalse(module.grade(json.dumps(answer))['passed'])

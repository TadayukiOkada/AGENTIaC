"""What upstream offers for the declared components, what machines run, and what an update would take."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import urllib.error
import urllib.parse

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'scripts'))
from agentiac import command, declared, errors, paths, updates  # noqa: E402
import update as update_script  # noqa: E402

SITE = paths.root()

ENGINE_MANIFEST = {'id': 'engine-v1', 'engine': {'version': '1.1.4'}}
STRATA_MANIFEST = {'id': 'strata-v2', 'source_version': '0.2.8'}
MANIFESTS = {'engine-v1': (ENGINE_MANIFEST, 'hash-1'), 'strata-v2': (STRATA_MANIFEST, 'hash-2')}

NOW = '1970-01-02T10:00:00+00:00'
EARLIER = '1970-01-01T10:00:00+00:00'
OLD_COMMIT, NEW_COMMIT = 'a' * 40, 'b' * 40
OLD_DIGEST, NEW_DIGEST = 'sha256:' + 'c' * 64, 'sha256:' + 'd' * 64
ANONYMOUS_TOKEN = 'anonymous-registry-token-sentinel'


def write_synthetic_update_site(root):
    for rel in ('infra', 'profiles', 'backends', 'gateway', 'agents/pi', 'docs'):
        (root / rel).mkdir(parents=True, exist_ok=True)
    inventory = {
        'all': {
            'vars': {
                'ansible_user': 'operator', 'nodejs_binary': '/usr/bin/node',
                'rclone_config_dir': '/srv/agentiac/rclone', 'backup_destination': 'remote:backups',
                'gateway_port': 14000, 'chatgpt_port': 14001, 'resource_prefix': 'agentiac',
            },
            'children': {'workers': {'hosts': {
                'workera': {'node_class': 'orin'},
                'workerb': {'node_class': 'orin'},
                'workerc': {'node_class': 'rtx5090', 'borrowed': True},
                'workerd': {'node_class': 'rtx5090', 'borrowed': True},
                'workere': {'node_class': 'gb10', 'borrowed': True},
                'control': {'ansible_connection': 'local', 'node_class': 'laptop'},
            }}},
        },
    }
    (root / 'infra/inventory.yml').write_text(yaml.safe_dump(inventory, sort_keys=False))
    (root / 'profiles/small.json').write_text(json.dumps({'nodes': ['orin'], 'model': 'q6'}))
    (root / 'gateway/config.yaml').write_text(yaml.safe_dump({'model_list': [
        {'model_name': 'workera-worker', 'litellm_params': {'model': 'openai/m', 'api_base': 'http://127.0.0.1:18101/v1'}}
    ]}))
    (root / 'gateway/local-coder.json').write_text(json.dumps({
        'version': 2, 'alias': 'local-coder', 'default': 'workera-small',
        'targets': {'workera-small': {'source_alias': 'workera-worker', 'node': 'workera', 'context_window': 32768, 'max_tokens': 4096}},
    }))
    (root / 'agents/pi/models.json').write_text(json.dumps({'providers': {'local-infra': {'models': [{'id': 'local-coder'}]}}})
                                                )
    (root / 'agents/pi/router.json').write_text(json.dumps({'low': 'local-coder', 'high': 'local-coder'}))

    components = [
        {'id': 'pi', 'kind': 'agent', 'owner': 'project', 'hosts': ['control'], 'policy': 'notify',
         'gate': 'X19', 'source': {'type': 'npm', 'package': '@scope/pi'},
         'pin': {'version': '1.3.0', 'lock': 'agents/pi/package-lock.json'}},
        {'id': 'claude-code', 'kind': 'agent', 'owner': 'project', 'hosts': ['control'], 'policy': 'manual',
         'source': {'type': 'observed'}, 'pin': {'version': '2.1.316', 'lock': 'agents/claude-code/pin.json'}},
        {'id': 'codex', 'kind': 'agent', 'owner': 'project', 'hosts': ['control'], 'policy': 'manual',
         'source': {'type': 'observed'}, 'pin': {'version': '0.165.0', 'lock': 'agents/codex/pin.json'}},
        {'id': 'litellm', 'kind': 'python', 'owner': 'project', 'hosts': ['control'], 'policy': 'manual',
         'source': {'type': 'pypi', 'package': 'litellm'},
         'pin': {'version': '1.105.1', 'lock': 'requirements-gateway.lock'}},
        {'id': 'ansible', 'kind': 'python', 'owner': 'project', 'hosts': ['control'], 'policy': 'manual',
         'source': {'type': 'pypi', 'package': 'ansible-core'},
         'pin': {'version': '2.21.0', 'lock': 'requirements-control.lock'}},
        {'id': 'tensorfold', 'kind': 'engine', 'owner': 'project', 'hosts': ['workere'], 'policy': 'manual',
         'gate': 'T9.11', 'source': {'type': 'github', 'repository': 'o/tensorfold'},
         'pin': {'version': '1.1.4', 'lock': 'backends/tf.json'}},
        {'id': 'strata', 'kind': 'engine', 'owner': 'project', 'hosts': ['workerd'], 'policy': 'manual',
         'gate': '適用は T3.7 まで禁止（U2）', 'source': {'type': 'github', 'repository': 'synth/Strata'},
         'pin': {'version': '0.2.8', 'lock': 'backends/strata-0.2.8.json'}},
        {'id': 'strata-workera', 'kind': 'engine', 'owner': 'project', 'hosts': ['workera'], 'policy': 'manual',
         'gate': 'U2 was lifted for this host', 'source': {'type': 'github', 'repository': 'synth/Strata'},
         'pin': {'version': '0.2.12', 'lock': 'backends/strata-0.2.12.json'}},
        {'id': 'strata-workerb', 'kind': 'engine', 'owner': 'project', 'hosts': ['workerb'], 'policy': 'manual',
         'gate': 'U2 was lifted for this host', 'source': {'type': 'github', 'repository': 'synth/Strata'},
         'pin': {'version': '0.2.12', 'lock': 'backends/strata-0.2.12.json'}},
        {'id': 'flash-next-weights', 'kind': 'model', 'owner': 'project', 'hosts': ['workere'], 'policy': 'manual',
         'source': {'type': 'huggingface', 'repository': 'o/weights'},
         'pin': {'revision': OLD_COMMIT, 'lock': 'backends/tf.json'}},
        {'id': 'runtime-base', 'kind': 'base-image', 'owner': 'project', 'hosts': ['control'], 'policy': 'manual',
         'source': {'type': 'digest', 'image': 'ubuntu', 'tag': '24.04'},
         'pin': {'digest': OLD_DIGEST, 'lock': 'runtime/Dockerfile'}},
        {'id': 'github-actions-checkout', 'kind': 'ci', 'owner': 'project', 'hosts': ['control'], 'policy': 'manual',
         'source': {'type': 'github', 'repository': 'actions/checkout'},
         'pin': {'version': 'v7.0.1', 'lock': '.github/workflows/check.yml'}},
        {'id': 'nvidia-driver', 'kind': 'driver', 'owner': 'vendor', 'hosts': ['workera', 'workerd'],
         'policy': 'frozen', 'source': {'type': 'observed'}},
        {'id': 'jetpack', 'kind': 'os', 'owner': 'vendor', 'hosts': ['workera'],
         'policy': 'frozen', 'source': {'type': 'observed'}},
        {'id': 'claude-user', 'kind': 'agent', 'owner': 'user', 'hosts': ['control'],
         'policy': 'frozen', 'source': {'type': 'observed'}},
        {'id': 'codex-user', 'kind': 'agent', 'owner': 'user', 'hosts': ['control'],
         'policy': 'frozen', 'source': {'type': 'observed'}},
    ]
    (root / 'infra/components.yml').write_text(yaml.safe_dump(components, sort_keys=False))

    (root / 'docs/platform-architecture.md').write_text('''| pi | a | b | before section | not this |

### 8.1 流れ

```text
validate      隔離した版で検証する（部品ごとの検査。Suite は要承認）
verify        ops drift、check --no-pi
```

`ops deploy` は lock をインストールしない。LiteLLM や agentiac の再起動の判定に入っている。

| 種類 | 検出元 | 比較 | apply の経路 | rollback |
|---|---|---|---|---|
| pi | npm | SemVer | lock 更新 | lock を戻す |
| LiteLLM | PyPI | PEP 440 | lock 更新、`uv pip sync` で `.venv-gateway` に反映、gateway と ChatGPT gateway の再起動（P6） | revert、`uv pip sync`、再起動 |
| ほかの control の Python | PyPI | PEP 440 | lock 更新、`uv pip sync` で該当の venv に反映 | revert と `uv pip sync` |
| Strata、TensorFold recipe | GitHub | release | `ops prepare`、S §3.5 | route を N-1 へ |
| モデル | HuggingFace | revision | 新しい profile、昇格ルール | route を戻す |
| コンテナ base | registry | digest | image 再構築 | 前の digest |
| GitHub Actions | GitHub | tag | workflow の更新 | revert |

### 8.2 所有者

| pi | a | b | not after | not this |
''')
    (root / 'docs/execution-plan.md').write_text(
        '| P1 | 各バッチの commit、push、deploy |\n'
        '| P6 | gateway 再起動の時刻（D5 の指定） |\n'
        '| P11 | ソーク |\n'
    )
    (root / 'docs/operations.md').write_text('### Reinstalling pi\n')
    (root / 'docs/tensorfold-recipe.md').write_text('## Changing the recipe\n')
    (root / 'docs/usage.md').write_text('re-pin with `scripts/pin-agent.py` and committing the new digest\n')
    return root


_SITE_TMP = tempfile.TemporaryDirectory()
SYNTH_SITE = write_synthetic_update_site(Path(_SITE_TMP.name))


def pi_pin():
    return next(c for c in declared.load(SYNTH_SITE).components if c['id'] == 'pi')['pin']['version']


def one_past(version):
    parts = version.split('.')
    parts[-1] = str(int(parts[-1]) + 1)
    return '.'.join(parts)


NEWER_PI = one_past(pi_pin())

PI = {'id': 'pi', 'owner': 'project', 'policy': 'notify', 'source': {'type': 'npm', 'package': '@scope/pi'},
      'pin': {'version': '1.3.0', 'lock': 'x'}}
LITELLM = {'id': 'litellm', 'owner': 'project', 'policy': 'manual', 'source': {'type': 'pypi', 'package': 'litellm'},
           'pin': {'version': '1.105.1', 'lock': 'x'}}
ENGINE = {'id': 'engine', 'owner': 'project', 'policy': 'manual', 'source': {'type': 'github', 'repository': 'o/engine'},
          'pin': {'version': '1.1.4', 'revision': OLD_COMMIT, 'lock': 'x'}}
BARE = {'id': 'bare', 'owner': 'project', 'policy': 'manual', 'source': {'type': 'github', 'repository': 'o/bare'},
        'pin': {'revision': OLD_COMMIT, 'lock': 'x'}}
WEIGHTS = {'id': 'weights', 'owner': 'project', 'policy': 'manual', 'source': {'type': 'huggingface', 'repository': 'o/w'},
           'pin': {'revision': OLD_COMMIT, 'lock': 'x'}}
BASE = {'id': 'base', 'owner': 'project', 'policy': 'manual', 'source': {'type': 'digest', 'image': 'ubuntu', 'tag': '24.04'},
        'pin': {'digest': OLD_DIGEST, 'lock': 'x'}}
OBSERVED = {'id': 'copy', 'owner': 'project', 'policy': 'manual', 'source': {'type': 'observed'},
            'pin': {'version': '2.1.316', 'lock': 'x'}}
FROZEN = {'id': 'strata', 'owner': 'project', 'policy': 'frozen', 'source': {'type': 'github', 'repository': 'o/strata'},
          'pin': {'version': '0.2.8', 'lock': 'x'}}
DRIVER = {'id': 'driver', 'owner': 'vendor', 'policy': 'frozen', 'source': {'type': 'observed'}}

ANSWERS = {
    'https://registry.npmjs.org/@scope/pi/latest': {'name': '@scope/pi', 'version': '1.4.0'},
    'https://pypi.org/pypi/litellm/json': {'info': {'version': '1.106.6'}},
    'https://api.github.com/repos/o/engine/releases/latest': {'tag_name': 'v1.2.1'},
    'https://api.github.com/repos/o/bare/commits/HEAD': {'sha': NEW_COMMIT},
    'https://huggingface.co/api/models/o/w': {'sha': NEW_COMMIT},
    'https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/ubuntu:pull': {'token': ANONYMOUS_TOKEN},
}
DIGEST_URL = 'https://registry-1.docker.io/v2/library/ubuntu/manifests/24.04'


class Response(io.BytesIO):
    def __init__(self, body=b'{}', headers=None):
        super().__init__(body)
        self.headers = headers or {}


class Opener:
    def __init__(self, answers=ANSWERS, digest=NEW_DIGEST, failing=()):
        self.answers, self.digest, self.failing, self.requests = answers, digest, failing, []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        self.timeout = timeout
        url = request.full_url
        for fragment, error in dict(self.failing).items():
            if fragment in url:
                raise error
        if url == DIGEST_URL:
            return Response(headers={'Docker-Content-Digest': self.digest} if self.digest else {})
        if url not in self.answers:
            raise http_error(url, 404)
        return Response(json.dumps(self.answers[url]).encode())


def http_error(url, code):
    return urllib.error.HTTPError(url, code, 'error', {}, io.BytesIO(b''))


def run(*components, opener=None, previous=None):
    return updates.check(list(components), previous, opener or Opener(), NOW)


def entry(*components, **options):
    return run(*components, **options)['components'][0]


class ProviderTests(unittest.TestCase):
    def test_npm_reads_the_latest_dist_tag(self):
        result = entry(PI)
        self.assertEqual((result['latest'], result['status']), ('1.4.0', 'behind'))
        self.assertEqual(result['source_url'], 'https://registry.npmjs.org/@scope/pi/latest')

    def test_pypi_reads_the_info_version(self):
        result = entry(LITELLM)
        self.assertEqual((result['latest'], result['status']), ('1.106.6', 'behind'))

    def test_a_pinned_version_is_compared_with_the_latest_release(self):
        result = entry(ENGINE)
        self.assertEqual((result['latest'], result['status']), ('v1.2.1', 'behind'))

    def test_the_tip_of_a_branch_past_the_pinned_release_is_not_behind(self):
        answers = {'https://api.github.com/repos/o/engine/releases/latest': {'tag_name': 'v1.1.4'},
                   'https://api.github.com/repos/o/engine/commits/HEAD': {'sha': NEW_COMMIT}}
        self.assertEqual(entry(ENGINE, opener=Opener(answers))['status'], 'current')

    def test_a_repository_with_no_releases_falls_back_to_the_commit(self):
        answers = {'https://api.github.com/repos/o/engine/commits/HEAD': {'sha': OLD_COMMIT}}
        result = entry(ENGINE, opener=Opener(answers))
        self.assertEqual((result['latest'], result['status']), (OLD_COMMIT, 'current'))
        self.assertTrue(result['source_url'].endswith('/commits/HEAD'))

    def test_a_rate_limit_is_a_failure_and_not_a_repository_with_no_releases(self):
        opener = Opener(failing={'/releases/latest': http_error('x', 403)})
        result = entry(ENGINE, opener=opener)
        self.assertEqual(result['status'], 'unknown')
        self.assertFalse(any('commits/HEAD' in sent.full_url for sent in opener.requests))

    def test_a_commit_pin_with_no_version_compares_commits(self):
        result = entry(BARE)
        self.assertEqual((result['latest'], result['status']), (NEW_COMMIT, 'differs'))

    def test_hugging_face_compares_the_revision(self):
        self.assertEqual(entry(WEIGHTS)['status'], 'differs')
        answers = {'https://huggingface.co/api/models/o/w': {'sha': OLD_COMMIT}}
        self.assertEqual(entry(WEIGHTS, opener=Opener(answers))['status'], 'current')

    def test_a_digest_is_read_with_the_tag_and_compared_for_equality(self):
        result = entry(BASE)
        self.assertEqual((result['latest'], result['status']), (NEW_DIGEST, 'differs'))
        self.assertEqual(entry(BASE, opener=Opener(digest=OLD_DIGEST))['status'], 'current')

    def test_a_namespaced_image_is_not_put_under_library(self):
        component = copy.deepcopy(BASE)
        component['source']['image'] = 'owner/image'
        opener = Opener()
        entry(component, opener=opener)
        self.assertTrue(any('repository:owner/image:pull' in sent.full_url for sent in opener.requests))

    def test_a_registry_that_names_no_digest_is_a_failed_read(self):
        result = entry(BASE, opener=Opener(digest=None))
        self.assertEqual((result['latest'], result['status']), (None, 'unknown'))

    def test_every_request_has_a_timeout(self):
        opener = Opener()
        run(PI, opener=opener)
        self.assertEqual(opener.timeout, updates.TIMEOUT_S)


class ComparisonTests(unittest.TestCase):
    def status(self, component, pinned, latest):
        changed = copy.deepcopy(component)
        changed['pin']['version'] = pinned
        return updates.compare(changed, latest)

    def test_semver_compares_numbers_and_not_text(self):
        self.assertEqual(self.status(PI, '1.9.0', '1.10.0'), 'behind')
        self.assertEqual(self.status(PI, '1.10.0', '1.9.0'), 'ahead')
        self.assertEqual(self.status(PI, '1.3.0', '1.3.0'), 'current')

    def test_a_prerelease_is_below_its_release(self):
        self.assertEqual(self.status(PI, '1.4.0-rc.1', '1.4.0'), 'behind')
        self.assertEqual(self.status(PI, '1.4.0', '1.4.0-rc.1'), 'ahead')

    def test_semver_prereleases_sort_numbers_before_names_and_by_number(self):
        self.assertEqual(self.status(PI, '1.4.0-rc.2', '1.4.0-rc.10'), 'behind')
        self.assertEqual(self.status(PI, '1.4.0-1', '1.4.0-alpha'), 'behind')

    def test_build_metadata_does_not_order_a_version(self):
        self.assertEqual(self.status(PI, '1.4.0+a', '1.4.0+b'), 'current')

    def test_pep_440_is_not_read_as_semver(self):
        self.assertEqual(self.status(LITELLM, '1.0.0rc1', '1.0.0'), 'behind')
        self.assertEqual(self.status(LITELLM, '1.0', '1.0.0'), 'current')
        self.assertEqual(self.status(LITELLM, '1.0.0', '1.0.0.post1'), 'behind')

    def test_a_version_that_cannot_be_parsed_is_unknown_and_not_current(self):
        self.assertEqual(self.status(PI, '1.3.0', 'nightly'), 'unknown')
        self.assertEqual(self.status(LITELLM, '1.0.0', 'nightly'), 'unknown')
        self.assertEqual(self.status(ENGINE, '1.1.4', 'release-next'), 'unknown')


class NothingToReadTests(unittest.TestCase):
    def test_frozen_is_never_read(self):
        opener = Opener()
        result = entry(FROZEN, opener=opener)
        self.assertEqual(opener.requests, [])
        self.assertEqual((result['status'], result['latest'], result['fetched_at']), ('frozen', None, None))

    def test_frozen_still_shows_what_is_pinned(self):
        self.assertEqual(entry(FROZEN)['pinned'], '0.2.8')

    def test_an_observed_source_has_no_upstream_to_read(self):
        opener = Opener()
        result = entry(OBSERVED, opener=opener)
        self.assertEqual(opener.requests, [])
        self.assertEqual((result['status'], result['latest']), ('observed', None))

    def test_a_vendor_component_has_no_pin(self):
        self.assertIsNone(entry(DRIVER)['pinned'])


class FailureTests(unittest.TestCase):
    PREVIOUS = {'pi': {'latest': '1.3.9', 'source_url': 'https://old', 'fetched_at': EARLIER}}

    def failing(self, error=OSError('no route')):
        return Opener(failing={'registry.npmjs.org': error})

    def test_one_failure_does_not_stop_the_rest(self):
        report = run(PI, LITELLM, opener=self.failing())
        self.assertEqual([item['latest'] for item in report['components']], [None, '1.106.6'])

    def test_the_last_answer_stays_and_is_marked_stale(self):
        result = entry(PI, opener=self.failing(), previous=self.PREVIOUS)
        self.assertEqual((result['latest'], result['stale'], result['fetched_at']), ('1.3.9', True, EARLIER))
        self.assertEqual(result['source_url'], 'https://old')
        self.assertIn('no route', result['reason'])

    def test_a_stale_answer_is_compared_with_the_pin_of_today(self):
        self.assertEqual(entry(PI, opener=self.failing(), previous=self.PREVIOUS)['status'], 'behind')
        current = {'pi': {'latest': '1.3.0', 'source_url': 'u', 'fetched_at': EARLIER}}
        self.assertEqual(entry(PI, opener=self.failing(), previous=current)['status'], 'current')

    def test_no_last_answer_says_null_and_why(self):
        result = entry(PI, opener=self.failing())
        self.assertEqual((result['latest'], result['stale'], result['status']), (None, False, 'unknown'))
        self.assertIn('no route', result['reason'])

    def test_a_last_answer_that_also_failed_is_not_stale(self):
        result = entry(PI, opener=self.failing(), previous={'pi': {'latest': None}})
        self.assertEqual((result['status'], result['stale']), ('unknown', False))

    def test_a_good_read_is_not_stale(self):
        self.assertFalse(entry(PI, previous=self.PREVIOUS)['stale'])

    def test_every_kind_of_bad_answer_is_a_failed_read_and_not_a_crash(self):
        for answer in ({}, [], {'version': None}, 'text'):
            answers = {'https://registry.npmjs.org/@scope/pi/latest': answer}
            result = entry(PI, opener=Opener(answers))
            self.assertIn(result['status'], ('unknown',), answer)

    def test_a_body_that_is_not_json_is_a_failed_read(self):
        def opener(request, timeout=None):
            return Response(b'<html>')
        self.assertEqual(entry(PI, opener=opener)['status'], 'unknown')

    def test_a_reason_stays_short(self):
        self.assertLessEqual(len(entry(PI, opener=self.failing(OSError('x' * 500)))['reason']), 200)


class ExitCodeTests(unittest.TestCase):
    def test_everything_read_is_0(self):
        self.assertEqual(updates.exit_code(run(PI, LITELLM)), 0)

    def test_behind_is_not_a_problem(self):
        self.assertEqual(entry(PI)['status'], 'behind')
        self.assertEqual(updates.exit_code(run(PI)), 0)

    def test_nothing_read_is_3(self):
        report = run(PI, LITELLM, opener=Opener(failing={'': OSError('down')}))
        self.assertEqual(updates.exit_code(report), 3)

    def test_nothing_read_is_3_even_with_stale_answers_to_show(self):
        previous = {'pi': {'latest': '1.3.9', 'source_url': 'u', 'fetched_at': EARLIER}}
        report = run(PI, opener=Opener(failing={'': OSError('down')}), previous=previous)
        self.assertEqual(updates.exit_code(report), 3)

    def test_a_partial_read_is_0_because_a_stale_entry_is_a_note(self):
        report = run(PI, LITELLM, opener=Opener(failing={'npmjs': OSError('down')}))
        self.assertEqual(updates.exit_code(report), 0)

    def test_frozen_and_observed_alone_are_0_because_nothing_was_asked(self):
        self.assertEqual(updates.exit_code(run(FROZEN, OBSERVED, DRIVER)), 0)


class RecordTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'components/latest.json'

    def test_a_saved_report_is_the_next_check_s_previous(self):
        updates.save(self.path, run(PI))
        result = entry(PI, opener=Opener(failing={'': OSError('down')}), previous=updates.load_previous(self.path))
        self.assertEqual((result['latest'], result['stale'], result['fetched_at']), ('1.4.0', True, NOW))

    def test_every_entry_carries_its_source_and_time(self):
        record = run(PI, BASE)
        self.assertEqual(record['schema_version'], 1)
        for item in record['components']:
            self.assertEqual(sorted(set(item) & {'pinned', 'latest', 'source_url', 'fetched_at', 'stale'}),
                             ['fetched_at', 'latest', 'pinned', 'source_url', 'stale'])
            self.assertTrue(item['source_url'] and item['fetched_at'])

    def test_a_save_leaves_no_temporary_file(self):
        updates.save(self.path, run(PI))
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ['latest.json'])

    def test_a_failed_save_leaves_the_old_record(self):
        updates.save(self.path, run(PI))
        before = self.path.read_text()
        with mock.patch.object(updates.os, 'replace', side_effect=OSError('disk')), self.assertRaises(OSError):
            updates.save(self.path, run(LITELLM))
        self.assertEqual(self.path.read_text(), before)

    def test_no_record_means_nothing_to_fall_back_on(self):
        self.assertEqual(updates.load_previous(self.path), {})

    def test_a_record_that_cannot_be_used_is_not_a_crash(self):
        self.path.parent.mkdir(parents=True)
        for text in ('not json', '[]', '{}', '{"schema_version": 1}'):
            self.path.write_text(text)
            self.assertEqual(updates.load_previous(self.path), {}, text)

    def test_a_record_of_another_schema_is_not_read_as_this_one(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text('{"schema_version": 2, "components": [{"id": "pi", "latest": "9.9.9"}]}')
        self.assertEqual(updates.load_previous(self.path), {})


class PrivacyTests(unittest.TestCase):
    def shipped(self, root=SYNTH_SITE):
        opener = mock.MagicMock(side_effect=lambda request, timeout=None: self.answer(request))
        self.sent = []
        components = declared.load(root).components
        return updates.check(components, None, opener, NOW), components

    def answer(self, request):
        self.sent.append(request)
        if 'auth.docker.io' in request.full_url:
            return Response(json.dumps({'token': ANONYMOUS_TOKEN}).encode())
        return Response(b'{"version": "9.9.9", "tag_name": "v9.9.9", "sha": "%s", "info": {"version": "9.9.9"}}'
                        % NEW_COMMIT.encode(), {'Docker-Content-Digest': NEW_DIGEST})

    def test_the_shipped_file_reads_only_the_six_expected_hosts(self):
        self.shipped()
        hosts = {urllib.parse.urlsplit(sent.full_url).hostname for sent in self.sent}
        self.assertEqual(hosts, {'registry.npmjs.org', 'pypi.org', 'api.github.com', 'huggingface.co', 'auth.docker.io',
                                 'registry-1.docker.io'})

    def test_a_request_carries_a_declared_name_and_nothing_else(self):
        _report, components = self.shipped()
        names = [value for c in components for value in c['source'].values()]
        for sent in self.sent:
            self.assertTrue(any(str(name) in urllib.parse.unquote(sent.full_url) for name in names), sent.full_url)

    def test_no_request_carries_a_key_or_a_cookie(self):
        self.shipped()
        for sent in self.sent:
            headers = {key.lower(): value for key, value in sent.header_items()}
            self.assertNotIn('cookie', headers)
            if 'registry-1.docker.io' not in sent.full_url:
                self.assertNotIn('authorization', headers, sent.full_url)

    def test_the_only_authorization_is_the_anonymous_token_on_its_own_registry(self):
        self.shipped()
        carried = [sent for sent in self.sent if sent.has_header('Authorization')]
        self.assertEqual([sent.get_header('Authorization') for sent in carried], [f'Bearer {ANONYMOUS_TOKEN}'])

    def test_the_registry_token_never_reaches_the_record(self):
        report, _ = self.shipped()
        self.assertNotIn(ANONYMOUS_TOKEN, json.dumps(report))

    def test_the_personal_clis_are_not_asked_about(self):
        self.shipped()
        urls = ' '.join(sent.full_url for sent in self.sent)
        self.assertNotIn('claude', urls)
        self.assertNotIn('codex', urls)

    def test_strata_is_asked_about_by_its_repository_name_and_nothing_else(self):
        self.shipped()
        asked = [sent.full_url for sent in self.sent if 'Strata' in sent.full_url]
        self.assertEqual(asked, ['https://api.github.com/repos/synth/Strata/releases/latest'] * 3)


def done(stdout='', returncode=0, stderr=''):
    return mock.Mock(stdout=stdout, returncode=returncode, stderr=stderr)


class Probes:
    def __init__(self, remote=None, fronts=None, agents=None, personal=None):
        self.answers, self.fronts, self.agents_, self.personal_ = remote or {}, fronts or {}, agents or {}, personal or {}
        self.remote_calls, self.front_calls = [], []

    def remote(self, name, argv):
        self.remote_calls.append((name, tuple(argv)))
        answer = self.answers.get(name, done(returncode=255, stderr='ssh: connect to host timed out'))
        if isinstance(answer, Exception):
            raise answer
        return answer

    def front(self, name):
        self.front_calls.append(name)
        return self.fronts.get(name, (None, {'error': 'ConnectionRefusedError'}))

    def agent(self, name):
        answer = self.agents_[name]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def personal(self, program):
        return self.personal_[program]


DRIVERS = {'id': 'nvidia-driver', 'kind': 'driver', 'owner': 'vendor', 'hosts': ['workera', 'workerd'], 'policy': 'frozen',
           'source': {'type': 'observed'}}
JETPACK = {'id': 'jetpack', 'kind': 'os', 'owner': 'vendor', 'hosts': ['workera'], 'policy': 'frozen',
           'source': {'type': 'observed'}}
TENSORFOLD = {'id': 'tensorfold', 'kind': 'engine', 'owner': 'project', 'hosts': ['workere'], 'policy': 'manual',
              'source': {'type': 'github', 'repository': 'o/t'}, 'pin': {'version': '1.1.4', 'lock': 'x'}}
STRATA = {'id': 'strata', 'kind': 'engine', 'owner': 'project', 'hosts': ['workerc'], 'policy': 'frozen',
          'source': {'type': 'github', 'repository': 'o/s'}, 'pin': {'version': '0.2.8', 'lock': 'x'}}
PINNED_PI = {'id': 'pi', 'kind': 'agent', 'owner': 'project', 'hosts': ['control'], 'policy': 'notify',
             'source': {'type': 'npm', 'package': 'p'}, 'pin': {'version': '1.3.0', 'lock': 'x'}}
USER_CODEX = {'id': 'codex-user', 'kind': 'agent', 'owner': 'user', 'hosts': ['control'], 'policy': 'frozen',
              'source': {'type': 'observed'}}
L4T_LINE = '# R39 (release), REVISION: 2.1, GCID: 46758480, BOARD: generic, EABI: aarch64, DATE: Fri Aug 7 2026\n'


def workere_front(backend='engine-v1', digest='hash-1'):
    return {'workere': (200, {'state': 'unloaded', 'identity': {'backend_id': backend, 'manifest_hash': digest}})}


def observe(*components, probes=None, previous=None):
    return updates.observe(list(components), probes or Probes(), MANIFESTS, previous, NOW)


def seen(report, name, host):
    return next(item for item in report['components'] if item['id'] == name)['hosts'][host]


class ObserveTests(unittest.TestCase):
    def test_a_driver_is_read_per_host_with_the_declared_command(self):
        probes = Probes(remote={'workera': done('595.78\n')})
        report = observe(DRIVERS, probes=probes)
        self.assertEqual(seen(report, 'nvidia-driver', 'workera'), {'value': '595.78'})
        self.assertIn(('workera', updates.DRIVER), probes.remote_calls)

    def test_a_host_that_cannot_be_reached_is_unknown_with_the_reason_and_the_rest_is_read(self):
        report = observe(DRIVERS, probes=Probes(remote={'workera': done('595.78\n')}))
        self.assertEqual(seen(report, 'nvidia-driver', 'workerd')['value'], 'unknown')
        self.assertIn('timed out', seen(report, 'nvidia-driver', 'workerd')['reason'])
        self.assertEqual(updates.observe_exit_code(report), 0)

    def test_a_probe_that_times_out_is_unknown_and_not_a_crash(self):
        error = subprocess.TimeoutExpired('ssh', 30)
        self.assertEqual(seen(observe(DRIVERS, probes=Probes(remote={'workera': error})), 'nvidia-driver', 'workera')['value'],
                         'unknown')

    def test_nothing_readable_at_all_is_exit_3(self):
        self.assertEqual(updates.observe_exit_code(observe(DRIVERS)), 3)

    def test_no_component_to_read_is_not_a_failure(self):
        self.assertEqual(updates.observe_exit_code(observe()), 0)

    def test_jetpack_is_the_release_line(self):
        report = observe(JETPACK, probes=Probes(remote={'workera': done(L4T_LINE)}))
        self.assertEqual(seen(report, 'jetpack', 'workera'), {'value': 'R39.2.1'})

    def test_a_release_file_that_says_nothing_is_unknown(self):
        report = observe(JETPACK, probes=Probes(remote={'workera': done('nonsense\n')}))
        self.assertEqual(seen(report, 'jetpack', 'workera')['value'], 'unknown')

    def test_a_failing_command_s_output_is_not_a_version(self):
        answer = done('NVIDIA-SMI has failed because it could not communicate with the driver.\n', returncode=9)
        report = observe(DRIVERS, probes=Probes(remote={'workera': answer}))
        self.assertEqual(seen(report, 'nvidia-driver', 'workera')['value'], 'unknown')

    def test_an_empty_answer_is_unknown_and_not_an_empty_version(self):
        report = observe(DRIVERS, probes=Probes(remote={'workera': done('')}))
        self.assertEqual(seen(report, 'nvidia-driver', 'workera')['value'], 'unknown')

    def test_a_component_nothing_can_read_is_left_out(self):
        self.assertEqual(observe(LITELLM)['components'], [])

    def test_a_pinned_agent_is_read_from_the_pinned_copy(self):
        report = observe(PINNED_PI, probes=Probes(agents={'pi': '1.3.0\n'}))
        self.assertEqual(seen(report, 'pi', 'control'), {'value': '1.3.0'})
        self.assertEqual(report['notes'], [])

    def test_an_agent_refused_by_its_pin_keeps_one_sentence_as_the_reason(self):
        error = updates.AdapterError('pi is not pinned at /x\n  cause: gone\n  check: a\n  next:  b')
        report = observe(PINNED_PI, probes=Probes(agents={'pi': error}))
        self.assertEqual(seen(report, 'pi', 'control'), {'value': 'unknown', 'reason': 'pi is not pinned at /x'})

    def test_a_pinned_agent_that_differs_from_its_pin_is_a_note(self):
        report = observe(PINNED_PI, probes=Probes(agents={'pi': '1.4.0\n'}))
        self.assertEqual(report['notes'], ['pi on control is 1.4.0, and the pin is 1.3.0'])


class EngineTests(unittest.TestCase):
    def engine(self, fronts):
        return seen(observe(TENSORFOLD, probes=Probes(fronts=fronts)), 'tensorfold', 'workere')

    def test_a_front_whose_manifest_matches_this_checkout_gives_the_version(self):
        self.assertEqual(self.engine(workere_front()), {'value': '1.1.4', 'backend': 'engine-v1'})

    def test_strata_reads_its_version_from_where_its_manifest_keeps_it(self):
        fronts = {'workerc': (200, {'identity': {'backend_id': 'strata-v2', 'manifest_hash': 'hash-2'}})}
        self.assertEqual(seen(observe(STRATA, probes=Probes(fronts=fronts)), 'strata', 'workerc')['value'], '0.2.8')

    def test_a_manifest_that_differs_on_the_worker_is_not_an_observation(self):
        result = self.engine(workere_front(digest='other'))
        self.assertEqual(result['value'], 'unknown')
        self.assertIn('differs from this checkout', result['reason'])

    def test_a_manifest_this_checkout_does_not_declare_is_not_an_observation(self):
        result = self.engine(workere_front(backend='elsewhere'))
        self.assertEqual(result['value'], 'unknown')
        self.assertIn('does not declare', result['reason'])

    def test_a_front_that_names_no_manifest_or_could_not_read_it(self):
        self.assertIn('names no manifest', self.engine({'workere': (200, {'state': 'ready'})})['reason'])
        self.assertIn('could not read', self.engine({'workere': (200, {'identity': {'error': 'OSError: x'}})})['reason'])

    def test_no_front_answering_is_unknown(self):
        self.assertIn('no front answered', self.engine({})['reason'])

    def test_an_engine_differing_from_its_pin_is_a_note(self):
        manifests = {'engine-v1': ({'id': 'engine-v1', 'engine': {'version': '1.2.1'}}, 'hash-1')}
        report = updates.observe([TENSORFOLD], Probes(fronts=workere_front()), manifests, None, NOW)
        self.assertEqual(report['notes'], ['tensorfold on workere is 1.2.1, and the pin is 1.1.4'])

    def test_the_hash_is_of_the_file_a_front_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'backends').mkdir()
            (root / 'backends/x.json').write_bytes(b'{"id": "x"}\n')
            _spec, digest = updates.manifests_by_id(root, {'x': {'id': 'x'}})['x']
        self.assertEqual(digest, updates.hashlib.sha256(b'{"id": "x"}\n').hexdigest())

    def test_the_engine_for_a_target_is_only_the_reading_of_its_own_manifest_on_its_own_node(self):
        report = observe(TENSORFOLD, probes=Probes(fronts=workere_front()))
        self.assertEqual(updates.observed_engine(report, 'workere', 'engine-v1'), '1.1.4')
        self.assertIsNone(updates.observed_engine(report, 'workere', 'other'))
        self.assertIsNone(updates.observed_engine(report, 'workerc', 'engine-v1'))
        self.assertIsNone(updates.observed_engine(None, 'workere', 'engine-v1'))
        self.assertIsNone(updates.observed_engine(report, 'workere', None))


class ChangeNoteTests(unittest.TestCase):
    def report(self, version, previous_value):
        previous = {'codex-user': {'hosts': {'control': {'value': previous_value}}}} if previous_value else None
        return observe(USER_CODEX, probes=Probes(personal={'codex': version}), previous=previous)

    def test_a_personal_cli_that_changed_is_a_note(self):
        self.assertEqual(self.report('codex-cli 0.167.0\n', 'codex-cli 0.166.0')['notes'],
                         ['codex-user on control changed from codex-cli 0.166.0 to codex-cli 0.167.0'])

    def test_the_same_version_is_quiet(self):
        self.assertEqual(self.report('codex-cli 0.166.0\n', 'codex-cli 0.166.0')['notes'], [])

    def test_the_first_reading_has_nothing_to_differ_from(self):
        self.assertEqual(self.report('codex-cli 0.166.0\n', None)['notes'], [])

    def test_recovering_from_unknown_is_not_a_change(self):
        self.assertEqual(self.report('codex-cli 0.166.0\n', 'unknown')['notes'], [])

    def test_a_host_that_went_dark_is_not_a_change(self):
        previous = {'nvidia-driver': {'hosts': {'workera': {'value': '595.78'}}}}
        self.assertEqual(observe(DRIVERS, previous=previous)['notes'], [])

    def test_a_vendor_change_is_a_note_too(self):
        previous = {'nvidia-driver': {'hosts': {'workera': {'value': '595.78'}}}}
        report = observe(DRIVERS, probes=Probes(remote={'workera': done('600.10\n')}), previous=previous)
        self.assertEqual(report['notes'], ['nvidia-driver on workera changed from 595.78 to 600.10'])

    def test_a_project_component_changing_is_the_pin_s_business_and_not_a_change_note(self):
        previous = {'pi': {'hosts': {'control': {'value': '1.2.0'}}}}
        report = observe(PINNED_PI, probes=Probes(agents={'pi': '1.3.0\n'}), previous=previous)
        self.assertEqual(report['notes'], [])


class ReadOnlyTests(unittest.TestCase):
    def everything(self, root=SYNTH_SITE):
        graph = declared.load(root)
        probes = Probes(agents=dict.fromkeys(updates.PINNED_AGENTS, '1\n'), personal={'claude': '1\n', 'codex': '1\n'})
        updates.observe(graph.components, probes, updates.manifests_by_id(root, graph.backends), None, NOW)
        return graph, probes

    def test_the_shipped_components_send_a_worker_only_the_two_reads(self):
        _, probes = self.everything()
        self.assertEqual({argv for _, argv in probes.remote_calls}, {updates.DRIVER, updates.L4T})

    def test_every_command_is_a_read_with_no_shell_in_it(self):
        for argv in updates.REMOTE_READS:
            self.assertFalse({'sudo', 'rm', 'tee', 'sh', 'bash', '>', '|', ';'} & set(' '.join(argv).split()), argv)

    def test_every_declared_host_of_the_driver_is_asked(self):
        graph, probes = self.everything()
        declared_hosts = next(c['hosts'] for c in graph.components if c['id'] == 'nvidia-driver')
        self.assertEqual({host for host, argv in probes.remote_calls if argv == updates.DRIVER}, set(declared_hosts))

    def test_the_real_probe_refuses_anything_else(self):
        probes = update_script.Probes.__new__(update_script.Probes)
        probes.borrow, probes.hosts = mock.Mock(), {'workera': {}}
        with self.assertRaises(ValueError):
            probes.remote('workera', ['rm', '-rf', '/'])
        probes.borrow.on_host.assert_not_called()

    def test_the_real_probe_passes_a_listed_read_with_a_timeout(self):
        probes = update_script.Probes.__new__(update_script.Probes)
        probes.borrow, probes.hosts = mock.Mock(), {'workera': {'x': 1}}
        probes.remote('workera', list(updates.DRIVER))
        probes.borrow.on_host.assert_called_once_with({'x': 1}, list(updates.DRIVER), timeout=update_script.PROBE_TIMEOUT_S)

    def test_a_front_is_only_asked_for_its_status_so_no_model_loads(self):
        probes = update_script.Probes.__new__(update_script.Probes)
        probes.borrow, probes.hosts = mock.Mock(), {'workere': {'x': 1}}
        probes.front('workere')
        probes.borrow.ondemand_call.assert_called_once_with('workere', {'x': 1}, 'GET', 'status', timeout=10)

    def test_a_personal_cli_is_found_on_path_or_in_the_local_bin_and_otherwise_said(self):
        probes = update_script.Probes.__new__(update_script.Probes)
        with mock.patch.object(update_script.shutil, 'which', return_value=None), self.assertRaises(ValueError):
            probes.personal('claude')
        with mock.patch.object(update_script.shutil, 'which', side_effect=[None, '/home/x/.local/bin/claude']), \
                mock.patch.object(update_script.subprocess, 'check_output', return_value='2.1\n') as ran:
            self.assertEqual(probes.personal('claude'), '2.1\n')
        self.assertEqual(ran.call_args.args[0], ['/home/x/.local/bin/claude', '--version'])


class ObservedScreenTests(unittest.TestCase):
    def test_the_screen_shows_each_host_its_reason_and_the_notes(self):
        report = observe(DRIVERS, probes=Probes(remote={'workera': done('600.10\n')}),
                         previous={'nvidia-driver': {'hosts': {'workera': {'value': '595.78'}}}})
        text = updates.screen_observed(report)
        self.assertRegex(text, r'nvidia-driver\s+workera\s+600\.10')
        self.assertRegex(text, r'nvidia-driver\s+workerd\s+unknown')
        self.assertIn('  ssh: connect to host timed out', text)
        self.assertIn('note: nvidia-driver on workera changed from 595.78 to 600.10', text)


class NoNetworkTests(unittest.TestCase):
    def test_a_check_with_a_fixture_opens_no_socket(self):
        with mock.patch('socket.socket', side_effect=AssertionError('a test opened a connection')):
            report = run(PI, LITELLM, ENGINE, BARE, WEIGHTS, BASE, OBSERVED, FROZEN)
        self.assertEqual(updates.exit_code(report), 0)


PLANNED = ('pi', 'litellm', 'tensorfold', 'flash-next-weights', 'runtime-base', 'github-actions-checkout', 'claude-code')
PLAN_DOC = '''| pi | a | b | a row before the section | not this one |

### 8.1 流れ

```text
validate      隔離して検証する
verify        ops drift
```

| 種類 | 検出元 | 比較 | apply の経路 | rollback |
|---|---|---|---|---|
| pi | npm | SemVer | lock 更新 | lock を戻す |

### 8.2 所有者

| pi | a | b | not this row | not this either |
'''


def shipped(name):
    return updates.find(declared.load(SYNTH_SITE).components, name)


def plan(name, latest=None, observed=None, root=SYNTH_SITE):
    return updates.plan(shipped(name), latest or {}, observed, root)


def behind(name='pi', version=None, **fields):
    return {name: {'id': name, 'latest': version or NEWER_PI, 'fetched_at': NOW, 'stale': False, **fields}}


class PlanTests(unittest.TestCase):
    def test_every_component_that_is_not_frozen_has_a_plan(self):
        for component in declared.load(SYNTH_SITE).components:
            if component['policy'] != 'frozen':
                shown = plan(component['id'])
                self.assertEqual(set(shown['steps']), {'validate', 'apply', 'verify', 'rollback'})
                self.assertTrue(all(shown['steps'].values()), component['id'])
                self.assertTrue(shown['capacity'] and shown['approvals'], component['id'])
                self.assertIn('P1', [approval['id'] for approval in shown['approvals']], component['id'])

    def test_each_kind_quotes_its_own_row_of_the_document(self):
        expected = {'pi': ('lock 更新', 'lock を戻す'),
                    'litellm': ('lock 更新、`uv pip sync` で `.venv-gateway` に反映、gateway と ChatGPT gateway の再起動（P6）',
                                'revert、`uv pip sync`、再起動'),
                    'ansible': ('lock 更新、`uv pip sync` で該当の venv に反映', 'revert と `uv pip sync`'),
                    'tensorfold': ('`ops prepare`、S §3.5', 'route を N-1 へ'),
                    'flash-next-weights': ('新しい profile、昇格ルール', 'route を戻す'),
                    'runtime-base': ('image 再構築', '前の digest'),
                    'github-actions-checkout': ('workflow の更新', 'revert')}
        for name, (apply, rollback) in expected.items():
            steps = plan(name)['steps']
            self.assertEqual((steps['apply'], steps['rollback']), (apply, rollback), name)

    def test_the_flow_steps_are_the_documents(self):
        steps = plan('pi')['steps']
        self.assertEqual(steps['validate'], '隔離した版で検証する（部品ごとの検査。Suite は要承認）')
        self.assertEqual(steps['verify'], 'ops drift、check --no-pi')

    def test_a_component_with_no_row_says_so_and_invents_no_rollback(self):
        shown = plan('claude-code')
        self.assertIsNone(shown['procedure'])
        self.assertIn('not written down', shown['steps']['rollback'])
        self.assertIn('scripts/pin-agent.py', shown['steps']['apply'])
        self.assertIn('steps (PA §8.1 has no row for this component)', updates.screen_plan(shown))

    def test_the_repin_it_points_at_is_the_one_usage_documents(self):
        target = SITE if (SITE / 'docs/usage.md').exists() else SYNTH_SITE
        self.assertIn('`scripts/pin-agent.py` and committing the new digest',
                      ' '.join((target / 'docs/usage.md').read_text().split()))

    def test_approvals_are_quoted_from_the_execution_plan(self):
        self.assertEqual(plan('litellm')['approvals'], [{'id': 'P1', 'what': '各バッチの commit、push、deploy'},
                                                        {'id': 'P6', 'what': 'gateway 再起動の時刻（D5 の指定）'}])

    def test_every_approval_is_printed(self):
        self.assertRegex(updates.screen_plan(plan('pi')), r'approval\s+P1 .+\n\s+P11 ソーク')

    def test_a_suite_in_the_validation_needs_p11(self):
        for name in ('pi', 'tensorfold', 'flash-next-weights'):
            self.assertIn('P11', [approval['id'] for approval in plan(name)['approvals']], name)

    def test_only_litellm_restarts_the_gateway(self):
        for name in (*PLANNED, 'ansible'):
            needs = 'P6' in [approval['id'] for approval in plan(name)['approvals']]
            self.assertEqual(needs, name == 'litellm', name)

    def test_the_gate_of_a_component_is_shown(self):
        self.assertIn('X19', plan('pi')['gate'])
        self.assertIn('X19', updates.screen_plan(plan('pi')))

    def test_no_gate_is_said_rather_than_left_blank(self):
        self.assertIsNone(plan('litellm')['gate'])
        self.assertRegex(updates.screen_plan(plan('litellm')), r'gate\s+none declared')

    def test_worker_disk_is_named_only_where_a_worker_is_touched(self):
        self.assertIn('none on a worker', plan('pi')['capacity'])
        self.assertIn('none on a worker', plan('litellm')['capacity'])
        self.assertIn('none: a workflow', plan('github-actions-checkout')['capacity'])
        self.assertIn('ops storage', plan('tensorfold')['capacity'])

    def test_capacity_claims_no_figure_it_does_not_have(self):
        for name in ('tensorfold', 'flash-next-weights'):
            capacity = plan(name)['capacity']
            self.assertIn('no figure here', capacity)
            self.assertNotRegex(capacity, r'\d+(\.\d+)? ?GiB')

    def test_a_weight_change_warns_about_thin_rollback(self):
        self.assertIn('薄型化の効果がなくなる', plan('flash-next-weights')['capacity'])
        self.assertNotIn('薄型化', plan('tensorfold')['capacity'])

    def test_the_document_says_deploy_installs_no_lock(self):
        target = SITE if (SITE / updates.PROCEDURES).exists() else SYNTH_SITE
        self.assertIn('`ops deploy` は lock をインストールしない', (target / updates.PROCEDURES).read_text())

    def test_nothing_the_deploy_runs_installs_a_python_lock(self):
        paths_list = sorted((ROOT / 'infra').rglob('*.yml')) + sorted((ROOT / 'infra/templates').iterdir())
        self.assertTrue(paths_list)
        for path in paths_list:
            if path.name != 'components.yml':
                self.assertNotRegex(path.read_text(), r'requirements-(gateway|control)\.lock', path)

    def test_the_document_says_a_lock_change_restarts_the_gateway(self):
        target = SITE if (SITE / updates.PROCEDURES).exists() else SYNTH_SITE
        self.assertIn('再起動の判定に入っている', (target / updates.PROCEDURES).read_text())

    def test_ansible_warns_that_its_candidate_needs_a_newer_python(self):
        self.assertIn('Python 3.13', plan('ansible')['note'])
        self.assertIn('uv venv --python 3.12 .venv', (ROOT / 'README.md').read_text())

    def test_tensorfold_is_held_until_its_direction_is_decided(self):
        self.assertIn('T9.11', plan('tensorfold')['gate'])

    def test_the_sections_a_note_names_exist(self):
        ops = SITE if (SITE / 'docs/operations.md').exists() else SYNTH_SITE
        tf = SITE if (SITE / 'docs/tensorfold-recipe.md').exists() else SYNTH_SITE
        self.assertIn('### Reinstalling pi', (ops / 'docs/operations.md').read_text())
        self.assertIn('## Changing the recipe', (tf / 'docs/tensorfold-recipe.md').read_text())
        self.assertIn('Reinstalling pi', updates.screen_plan(plan('pi')))
        self.assertIn('Changing the recipe', updates.screen_plan(plan('tensorfold')))

    def test_a_component_with_nothing_to_add_prints_no_note(self):
        self.assertIsNone(plan('runtime-base')['note'])
        self.assertNotRegex(updates.screen_plan(plan('runtime-base')), r'(?m)^note')

    def test_the_plan_says_it_changed_nothing(self):
        self.assertIn('Nothing was changed', updates.screen_plan(plan('pi')))

    def in_documents(self, text):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / 'docs').mkdir()
        (root / updates.PROCEDURES).write_text(text)
        (root / updates.APPROVALS).write_text('| P1 | commit |\n| P11 | suite |\n')
        return plan('pi', root=root)

    def test_the_row_inside_the_section_is_the_one_used(self):
        self.assertEqual(self.in_documents(PLAN_DOC)['steps'], {
            'validate': '隔離して検証する', 'apply': 'lock 更新', 'verify': 'ops drift', 'rollback': 'lock を戻す'})

    def test_a_row_after_the_section_does_not_stand_in_for_one_inside_it(self):
        with self.assertRaisesRegex(ValueError, 'no row for pi'):
            self.in_documents(PLAN_DOC.replace('| pi | npm | SemVer | lock 更新 | lock を戻す |\n', ''))

    def test_the_steps_come_from_the_document_as_it_is_now(self):
        self.assertEqual(self.in_documents(PLAN_DOC.replace('lock 更新', 'a changed step'))['steps']['apply'], 'a changed step')

    def test_a_row_that_left_the_document_is_an_error_not_a_blank(self):
        with self.assertRaisesRegex(ValueError, 'no row for pi'):
            self.in_documents('### 8.1\nvalidate  x\nverify  y\n### 8.2\n')

    def test_a_flow_step_that_left_the_document_is_an_error_not_a_blank(self):
        with self.assertRaisesRegex(ValueError, 'no verify step'):
            self.in_documents(PLAN_DOC.replace('verify        ops drift\n', ''))


class CandidateTests(unittest.TestCase):
    def test_the_candidate_is_what_check_saved(self):
        candidate = plan('pi', behind())['candidate']
        self.assertEqual((candidate['latest'], candidate['status'], candidate['stale']), (NEWER_PI, 'behind', False))

    def test_a_pin_that_moved_since_the_check_is_compared_as_it_is_now(self):
        latest = behind(version=pi_pin(), status='behind', pinned='1.2.0')
        self.assertEqual(plan('pi', latest)['candidate']['status'], 'current')

    def test_a_stale_answer_says_so(self):
        self.assertIn(f'read {NOW}, stale', updates.screen_plan(plan('pi', behind(stale=True))))

    def test_no_record_asks_for_a_check_and_the_steps_still_print(self):
        shown = plan('pi')
        self.assertIsNone(shown['candidate']['latest'])
        self.assertIn(f'run {errors.command()} ops update check', updates.screen_plan(shown))
        self.assertEqual(shown['steps']['apply'], 'lock 更新')

    def test_a_failed_read_keeps_its_reason(self):
        latest = {'pi': {'id': 'pi', 'latest': None, 'reason': 'URLError: down'}}
        self.assertIn('URLError: down', updates.screen_plan(plan('pi', latest)))

    def test_a_component_with_no_upstream_has_no_candidate(self):
        shown = plan('codex', behind('codex', '9.9.9'))
        self.assertEqual((shown['candidate']['latest'], shown['candidate']['status']), (None, 'observed'))

    def test_a_record_that_is_not_a_version_is_not_a_candidate(self):
        for junk in (5, '', None, ['1.4.0']):
            self.assertIsNone(plan('pi', {'pi': {'latest': junk}})['candidate']['latest'], junk)

    def test_a_commit_only_source_says_differs(self):
        latest = behind('flash-next-weights', 'c' * 40)
        self.assertEqual(plan('flash-next-weights', latest)['candidate']['status'], 'differs')

    def test_the_whole_value_is_shown_because_someone_will_copy_it(self):
        self.assertIn('c' * 40, updates.screen_plan(plan('flash-next-weights', behind('flash-next-weights', 'c' * 40))))


class PlanObservedTests(unittest.TestCase):
    REPORT = {'observed_at': NOW, 'components': [
        {'id': 'pi', 'hosts': {'control': {'value': '1.3.0'}}},
        {'id': 'strata', 'hosts': {'workerd': {'value': 'unknown', 'reason': 'no front answered'}}},
        {'id': 'tensorfold', 'hosts': {'workere': {'value': '1.1.4', 'backend': 'x'},
                                         'other': {'value': 'unknown', 'reason': 'no front answered'}}}]}

    def test_what_runs_is_shown_beside_the_pin(self):
        shown = plan('pi', observed=self.REPORT)
        self.assertEqual((shown['observed']['control']['value'], shown['observed_at']), ('1.3.0', NOW))
        self.assertRegex(updates.screen_plan(shown), r'observed\s+control 1\.3\.0')

    def test_every_host_is_listed_with_its_reason(self):
        text = updates.screen_plan(plan('tensorfold', observed=self.REPORT))
        self.assertRegex(text, r'observed\s+workere 1\.1\.4')
        self.assertRegex(text, r'\n\s+other unknown \(no front answered\)')

    def test_nothing_recorded_is_said(self):
        for report in (None, {'components': []}):
            self.assertRegex(updates.screen_plan(plan('pi', observed=report)), r'observed\s+none recorded')


class RefusalTests(unittest.TestCase):
    def refused(self, name):
        with self.assertRaises(updates.Refused) as caught:
            plan(name)
        return str(caught.exception)

    def test_every_frozen_component_is_refused(self):
        for component in declared.load(SYNTH_SITE).components:
            if component['policy'] == 'frozen':
                self.assertIn(f"{component['id']} is frozen", self.refused(component['id']))

    def test_the_shipped_strata_is_planned_and_its_plan_carries_its_gate(self):
        spec = plan('strata')
        self.assertEqual(spec['policy'], 'manual')
        self.assertIn('適用は T3.7 まで禁止（U2）', spec['gate'])
        self.assertIn('U2 was lifted for this host', plan('strata-workera')['gate'])
        self.assertIn('U2 was lifted for this host', plan('strata-workerb')['gate'])

    def test_vendor_says_who_may_replace_it(self):
        self.assertIn('vendor', self.refused('nvidia-driver'))
        self.assertIn('AGENTS.md', self.refused('jetpack'))

    def test_a_personal_cli_says_it_is_not_ours(self):
        self.assertIn('observed only', self.refused('claude-user'))

    def test_a_refusal_has_the_four_parts(self):
        for name in ('jetpack', 'nvidia-driver', 'codex-user'):
            for part in ('  cause: ', '  check: ', '  next:  '):
                self.assertIn(part, self.refused(name), name)

    def test_a_vendor_refusal_invents_no_gate(self):
        self.assertNotIn('gate:', self.refused('nvidia-driver'))

    def test_an_undeclared_component_is_refused(self):
        with self.assertRaisesRegex(updates.Refused, 'no component is named ghost'):
            updates.find(declared.load(SYNTH_SITE).components, 'ghost')


class PlanCommandTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)
        self.addCleanup(os.umask, os.umask(0o022))
        for item in (mock.patch.object(update_script, 'ROOT', SYNTH_SITE),
                     mock.patch.object(update_script, 'RECORD', self.folder / 'latest.json'),
                     mock.patch.object(update_script, 'OBSERVED', self.folder / 'observed.json')):
            item.start()
            self.addCleanup(item.stop)

    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, 'argv', ['update.py', 'plan', *argv]), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            return update_script.main(), out.getvalue(), err.getvalue()

    def records(self):
        (self.folder / 'latest.json').write_text(json.dumps({'schema_version': 1, 'components': [
            {'id': 'pi', 'latest': NEWER_PI, 'fetched_at': NOW, 'stale': False}]}))
        (self.folder / 'observed.json').write_text(json.dumps({'schema_version': 1, 'observed_at': NOW, 'components': [
            {'id': 'pi', 'hosts': {'control': {'value': pi_pin()}}}]}))

    def test_pi_gets_its_steps_from_the_saved_records(self):
        self.records()
        code, out, err = self.main('pi')
        self.assertEqual((code, err), (0, ''))
        self.assertRegex(out, rf'candidate\s+{NEWER_PI.replace(".", r"\.")}, behind')
        self.assertRegex(out, rf'observed\s+control {pi_pin().replace(".", r"\.")}')
        self.assertRegex(out, r'apply\s+lock 更新')
        self.assertRegex(out, r'rollback\s+lock を戻す')

    def test_every_field_the_plan_asks_for_is_printed(self):
        self.records()
        _, out, _ = self.main('pi')
        for label in ('pinned', 'observed', 'candidate', 'gate', 'capacity', 'approval', 'validate', 'apply', 'verify', 'rollback'):
            self.assertRegex(out, rf'(?m)^\s*{label}\s', label)

    def test_a_blocked_subprocess_and_network_change_nothing(self):
        self.records()
        blocked = AssertionError('a plan must not run anything')
        with mock.patch('subprocess.Popen', side_effect=blocked), mock.patch('subprocess.run', side_effect=blocked), \
                mock.patch('subprocess.check_output', side_effect=blocked), mock.patch('socket.socket', side_effect=blocked):
            code, out, _ = self.main('pi')
        self.assertEqual(code, 0)
        self.assertIn('lock 更新', out)

    def test_it_writes_nothing(self):
        self.records()
        before = {path.name: path.read_bytes() for path in self.folder.iterdir()}
        with mock.patch.object(updates, 'save', side_effect=AssertionError('a plan must not save')):
            self.main('pi')
        self.assertEqual({path.name: path.read_bytes() for path in self.folder.iterdir()}, before)

    def test_it_runs_with_no_records_at_all(self):
        code, out, _ = self.main('pi')
        self.assertEqual(code, 0)
        self.assertIn(f'run {errors.command()} ops update check', out)
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_a_frozen_component_is_exit_2_on_stderr_with_nothing_on_stdout(self):
        code, out, err = self.main('codex-user')
        self.assertEqual((code, out), (2, ''))
        self.assertIn('codex-user is frozen', err)

    def test_the_shipped_strata_plan_is_exit_0_and_names_its_gate_and_capacity(self):
        code, out, err = self.main('strata')
        self.assertEqual((code, err), (0, ''))
        for line in ('policy manual', 'gate       適用は T3.7 まで禁止（U2）', 'ops prepare', 'P11'):
            self.assertIn(line, out)

    def test_frozen_is_refused_with_json_too(self):
        code, out, err = self.main('nvidia-driver', '--json')
        self.assertEqual((code, out), (2, ''))
        self.assertIn('frozen', err)

    def test_an_undeclared_component_is_exit_2(self):
        code, out, err = self.main('ghost')
        self.assertEqual((code, out), (2, ''))
        self.assertIn('no component is named ghost', err)

    def test_json_is_one_document_with_a_schema_version(self):
        self.records()
        code, out, _ = self.main('pi', '--json')
        shown = json.loads(out)
        self.assertEqual((code, shown['schema_version'], shown['candidate']['latest']), (0, 1, NEWER_PI))
        self.assertIn('lock 更新', out)

    def test_a_document_that_lost_its_row_is_exit_3_with_advice(self):
        with mock.patch.object(update_script, 'ROOT', self.folder), \
                mock.patch.object(update_script.declared, 'load', return_value=mock.Mock(components=[shipped('pi')])):
            code, out, err = self.main('pi')
        self.assertEqual((code, out), (3, ''))
        self.assertIn('no plan for pi', err.splitlines()[0])
        self.assertIn('  next:  ', err)

    def test_the_cli_passes_the_id_and_the_flag(self):
        with mock.patch.object(command, 'run') as ran:
            command.main(['ops', 'update', 'plan', 'pi', '--json'])
        self.assertEqual([str(word) for word in ran.call_args.args[0]][2:], ['plan', 'pi', '--json'])

    def test_the_cli_needs_an_id(self):
        with self.assertRaises(SystemExit) as caught, contextlib.redirect_stderr(io.StringIO()):
            command.main(['ops', 'update', 'plan'])
        self.assertEqual(caught.exception.code, 2)

    def test_check_takes_no_id(self):
        with mock.patch.object(command, 'run') as ran:
            command.main(['ops', 'update', 'check'])
        self.assertEqual([str(word) for word in ran.call_args.args[0]][2:], ['check'])


AUDITED = {'schema_version': 1, 'checked_at': NOW, 'source': 'x', 'locks': [
    {'lock': 'a.lock', 'ecosystem': 'PyPI', 'packages': 2, 'stale': False, 'fetched_at': NOW,
     'advisories': [{'id': 'GHSA-1', 'package': 'p', 'version': '1', 'severity': 'high'}]}]}


class NewsTests(unittest.TestCase):
    ENTRY = {'id': 'pi', 'policy': 'notify', 'status': 'behind', 'latest': '1.5.0', 'stale': False}

    def news(self, previous=None, **change):
        return updates.news({'components': [{**self.ENTRY, **change}]}, previous or {})

    def test_a_new_candidate_of_a_notify_component_is_news(self):
        self.assertEqual(self.news({'pi': {'latest': '1.4.0'}}), ['pi 1.5.0'])

    def test_the_first_check_is_news(self):
        self.assertEqual(self.news(), ['pi 1.5.0'])

    def test_the_same_candidate_is_said_once(self):
        self.assertEqual(self.news({'pi': {'latest': '1.5.0'}}), [])

    def test_a_manual_component_is_not_news(self):
        self.assertEqual(self.news(policy='manual'), [])

    def test_a_stale_answer_is_not_news(self):
        self.assertEqual(self.news(stale=True), [])

    def test_a_pin_that_is_current_is_not_news(self):
        self.assertEqual(self.news(status='current'), [])

    def test_a_revision_that_differs_is_news(self):
        self.assertEqual(self.news(status='differs'), ['pi 1.5.0'])


class CommandTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.record = Path(directory.name) / 'components/latest.json'
        self.audit_record = self.record.parent / 'audit.json'
        self.addCleanup(os.umask, os.umask(0o022))
        graph = mock.Mock(components=[PI, FROZEN])
        self.audited = mock.Mock(return_value=AUDITED)
        for item in (mock.patch.object(update_script, 'RECORD', self.record),
                     mock.patch.object(update_script, 'AUDIT', self.audit_record),
                     mock.patch.object(update_script.audit, 'run', self.audited),
                     mock.patch.object(update_script.declared, 'load', return_value=graph)):
            item.start()
            self.addCleanup(item.stop)

    def main(self, *argv, opener=None):
        out, err = io.StringIO(), io.StringIO()
        real = updates.check
        check = lambda components, previous: real(components, previous, opener or Opener(), NOW)  # noqa: E731
        with mock.patch.object(sys, 'argv', ['update.py', *argv]), mock.patch.object(updates, 'check', check), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = update_script.main()
        return code, out.getvalue(), err.getvalue()

    def test_json_is_one_document_with_a_schema_version(self):
        code, out, _ = self.main('check', '--json')
        self.assertEqual((code, json.loads(out)['schema_version']), (0, 1))

    def test_the_table_names_each_component_and_its_state(self):
        _, out, _ = self.main('check')
        self.assertRegex(out, r'pi\s+1\.3\.0\s+1\.4\.0\s+behind')
        self.assertRegex(out, r'strata\s+0\.2\.8\s+-\s+frozen')

    def test_the_result_is_kept(self):
        self.main('check')
        self.assertEqual(json.loads(self.record.read_text())['checked_at'], NOW)

    def test_only_the_owner_can_read_the_record(self):
        self.main('check')
        self.assertEqual(self.record.stat().st_mode & 0o777, 0o600)

    def test_a_second_check_that_cannot_read_shows_the_first_as_stale(self):
        self.main('check')
        code, out, _ = self.main('check', opener=Opener(failing={'': OSError('down')}))
        self.assertEqual(code, 3)
        self.assertIn('stale', out)
        self.assertEqual(json.loads(self.record.read_text())['components'][0]['latest'], '1.4.0')

    def test_a_record_that_cannot_be_written_is_said_and_is_not_exit_0(self):
        self.record.parent.parent.mkdir(parents=True, exist_ok=True)
        self.record.parent.write_text('a file where the directory belongs')
        code, out, err = self.main('check', '--json')
        self.assertEqual(code, 3)
        json.loads(out)
        self.assertIn('the result was not kept', err.splitlines()[0])
        self.assertIn(f'  next:  fix that, then run {errors.command()} ops update check again', err)

    def test_the_audit_is_kept_beside_the_result(self):
        self.main('check')
        self.assertEqual(json.loads(self.audit_record.read_text())['locks'][0]['lock'], 'a.lock')
        self.assertEqual(self.audit_record.stat().st_mode & 0o777, 0o600)

    def test_the_last_audit_is_given_to_the_next(self):
        self.main('check')
        self.main('check')
        self.assertEqual(self.audited.call_args.args[1]['checked_at'], NOW)

    def test_the_table_ends_with_the_audit_counts(self):
        _, out, _ = self.main('check')
        self.assertIn('audit: 1 advisories in 1 packages (1 high)', out.splitlines()[-1])

    def test_json_carries_the_counts_and_the_record_stays_as_it_was(self):
        _, out, _ = self.main('check', '--json')
        self.assertEqual(json.loads(out)['audit']['advisories'], 1)
        self.assertNotIn('audit', json.loads(self.record.read_text()))

    def test_a_finding_does_not_change_the_exit_code(self):
        self.assertEqual(self.main('check')[0], 0)

    def test_an_audit_that_cannot_be_saved_is_not_exit_0(self):
        self.audit_record.parent.parent.mkdir(parents=True, exist_ok=True)
        self.audit_record.mkdir(parents=True)
        code, _, err = self.main('check')
        self.assertEqual(code, 3)
        self.assertIn('the result was not kept', err)

    def test_it_tells_the_desktop_only_when_asked_and_only_once(self):
        with mock.patch.object(update_script.evaluation_watch, 'desktop', return_value='sent') as desktop:
            self.main('check')
            desktop.assert_not_called()
            self.record.unlink()
            self.main('check', '--notify')
            self.main('check', '--notify')
        self.assertEqual(desktop.call_count, 1)
        self.assertIn('pi 1.4.0', desktop.call_args.args[1])

    def test_a_notification_that_fails_does_not_fail_the_check(self):
        with mock.patch.object(update_script.evaluation_watch, 'desktop', return_value='desktop unavailable'):
            code, _, err = self.main('check', '--notify')
        self.assertEqual(code, 0)
        self.assertIn('desktop unavailable', err)

    def test_the_cli_passes_notify_for_the_timer(self):
        with mock.patch.object(command, 'run') as ran:
            command.main(['ops', 'update', 'check', '--notify'])
        self.assertEqual([str(word) for word in ran.call_args.args[0]][2:], ['check', '--notify'])

    def test_the_cli_runs_the_script_and_passes_the_flag(self):
        with mock.patch.object(command, 'run') as ran:
            command.main(['ops', 'update', 'check', '--json'])
        words = [str(word) for word in ran.call_args.args[0]]
        self.assertTrue(words[1].endswith('update.py'), words)
        self.assertEqual(words[2:], ['check', '--json'])

    def test_observe_goes_through_the_same_script(self):
        with mock.patch.object(command, 'run') as ran:
            command.main(['ops', 'update', 'observe'])
        self.assertEqual([str(word) for word in ran.call_args.args[0]][2:], ['observe'])

    def observe_main(self, probes, component=USER_CODEX):
        graph = mock.Mock(components=[component], backends={})
        path = self.record.parent / 'observed.json'
        out = io.StringIO()
        with mock.patch.object(update_script, 'OBSERVED', path), mock.patch.object(update_script, 'Probes', return_value=probes), \
                mock.patch.object(update_script.declared, 'load', return_value=graph), \
                mock.patch.object(sys, 'argv', ['update.py', 'observe', '--json']), contextlib.redirect_stdout(out):
            code = update_script.main()
        return code, json.loads(out.getvalue()), path

    def test_observe_keeps_its_own_record(self):
        code, report, path = self.observe_main(Probes(personal={'codex': 'codex-cli 0.166.0\n'}))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(path.read_text()), report)
        self.assertFalse(self.record.exists())

    def test_a_second_observe_compares_with_the_first(self):
        self.observe_main(Probes(personal={'codex': 'codex-cli 0.166.0\n'}))
        _, report, _ = self.observe_main(Probes(personal={'codex': 'codex-cli 0.167.0\n'}))
        self.assertEqual(report['notes'], ['codex-user on control changed from codex-cli 0.166.0 to codex-cli 0.167.0'])

    def test_observe_exits_3_when_nothing_could_be_read(self):
        self.assertEqual(self.observe_main(Probes(), DRIVERS)[0], 3)

    def test_a_bare_update_is_a_usage_error(self):
        with mock.patch('sys.stderr', io.StringIO()), self.assertRaises(SystemExit) as raised:
            command.main(['ops', 'update'])
        self.assertEqual(raised.exception.code, 2)


if __name__ == '__main__':
    unittest.main()

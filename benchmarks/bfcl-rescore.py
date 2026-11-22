#!/usr/bin/env python3
"""Re-score completed BFCL subset responses offline with official name normalization."""
import argparse
import contextlib
import fcntl
import hashlib
import importlib.metadata
import json
import os
import subprocess
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path)
    parser.add_argument('--wait-unit')
    parser.add_argument('--allow-evaluation-failure', action='store_true', help='Require all saved responses, then score offline despite a failed original evaluation')
    args = parser.parse_args()
    if args.wait_unit:
        deadline = time.monotonic() + 7200
        while subprocess.check_output(['systemctl', 'show', args.wait_unit,
                                      '-p', 'ActiveState', '--value'], text=True).strip() in ('active', 'activating'):
            if time.monotonic() > deadline:
                raise TimeoutError('generation wait exceeded two hours')
            time.sleep(15)
    folder = args.run.resolve()
    os.umask(0o077)
    with (folder / 'controller.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        manifest = json.loads((folder / 'manifest.json').read_text())
        state = json.loads((folder / 'state.json').read_text())
        if state['stage'] != 'complete' and not (args.allow_evaluation_failure and state['stage'] == 'failed'):
            parser.error('generation and original evaluation must complete first')
        if importlib.metadata.version('bfcl-eval') != manifest['bfcl_version']:
            parser.error('BFCL package version differs')
        registry = 'local-' + manifest['model'] + '-FC'
        files = sorted((folder / 'result' / registry).glob('**/*_result.json'))
        hashes = {}; seen = set()
        for path in files:
            hashes[str(path.relative_to(folder))] = hashlib.sha256(path.read_bytes()).hexdigest()
            for line in path.read_text().splitlines():
                row = json.loads(line)
                if row['id'] in seen:
                    parser.error('duplicate result ID')
                seen.add(row['id'])
                if 'error' in row or 'traceback' in row or (isinstance(row.get('result'), str) and row['result'].startswith('Error during inference:')):
                    parser.error('generation error needs infrastructure classification')
        expected = {x for ids in manifest['selected_ids'].values() for x in ids}
        if seen != expected:
            parser.error('saved results do not match selected IDs exactly')
        os.environ.update(BFCL_PROJECT_ROOT=str(folder), HF_HUB_OFFLINE='1')
        from bfcl_eval.constants.model_config import MODEL_CONFIG_MAPPING, ModelConfig
        from bfcl_eval.model_handler.api_inference.openai_completion import OpenAICompletionsHandler

        class OfflineHandler(OpenAICompletionsHandler):
            def _build_client_kwargs(self):
                return {'base_url': 'http://127.0.0.1:1/v1', 'api_key': 'offline-no-requests'}

            def generate_with_backoff(self, **kwargs):
                raise RuntimeError('inference is disabled during offline rescoring')

        MODEL_CONFIG_MAPPING[registry] = ModelConfig(
            model_name=manifest['model'], display_name=registry, url='http://127.0.0.1',
            org='local', license=manifest['deployment']['profile'].get('upstream', {}).get('license', 'see pinned model repository'),
            model_handler=OfflineHandler, is_fc_model=True, underscore_to_dot=True)
        # BFCL decodes directory underscores as slashes, including native model IDs.
        # Register the same handler under that decoded name without renaming responses.
        MODEL_CONFIG_MAPPING[registry.replace('_', '/')] = MODEL_CONFIG_MAPPING[registry]
        from bfcl_eval.__main__ import cli
        with (folder / 'rescore.log').open('w') as log, contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            cli(args=['evaluate', '--model', registry, '--test-category',
                      ','.join(manifest['selected_ids']), '--partial-eval',
                      '--score-dir', 'score-corrected'], standalone_mode=False)
        counts = {}
        for path in (folder / 'score-corrected' / registry).glob('**/*_score.json'):
            first = json.loads(path.read_text().splitlines()[0])
            counts[path.stem] = {k: first[k] for k in ('correct_count', 'total_count', 'accuracy')}
        if sum(x['total_count'] for x in counts.values()) != len(expected):
            raise RuntimeError('score coverage differs from selected IDs')
        for name, digest in hashes.items():
            assert hashlib.sha256((folder / name).read_bytes()).hexdigest() == digest
        report = {'model': manifest['model'], 'generation_identity': folder.name,
                  'rescorer_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  'checker': {'underscore_to_dot': True}, 'result_sha256': hashes,
                  'categories': counts, 'scope': manifest['scope']}
        (folder / 'corrected-summary.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report))


if __name__ == '__main__':
    main()

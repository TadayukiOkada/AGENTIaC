#!/usr/bin/env python3
"""Sign the gateway's cloud-gpt-sol alias in to ChatGPT (ADR 0005), or show/remove it.

Runs LiteLLM's device-code login once, interactively, so the gateway never has
to: open the printed URL, sign in with the ChatGPT Plus account and enter the
code. Tokens stay in secrets/chatgpt/ (0700) and are never printed; a successful
sign-in (re)starts agentiac-chatgpt, the separate LiteLLM process that serves
the alias. Needs the gateway's LiteLLM environment:
agentiac chatgpt login|status|logout.
"""
import argparse
import os
from pathlib import Path
import subprocess
import sys

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE / 'src'))
from agentiac import declared, paths  # noqa: E402
ROOT = paths.root()
TOKENS = ROOT / 'secrets/chatgpt'
UNIT = declared.resource('chatgpt', paths.root())
os.environ['CHATGPT_TOKEN_DIR'] = str(TOKENS)  # the gateway unit sets the same directory
sys.path.insert(0, str(CODE / 'gateway'))
import chatgpt_auth  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--status', action='store_true', help='report the sign-in state only')
    action.add_argument('--logout', action='store_true', help='delete the stored tokens')
    action.add_argument('--preflight', action='store_true', help=f'{UNIT} start check: refresh or refuse, never prompt')
    args = parser.parse_args()
    os.umask(0o077)
    TOKENS.mkdir(mode=0o700, parents=True, exist_ok=True)
    if args.logout:
        subprocess.run(['sudo', '-n', 'systemctl', 'stop', UNIT], check=True)
        (TOKENS / 'auth.json').unlink(missing_ok=True)
        print(f'ChatGPT tokens removed and {UNIT} stopped; cloud-gpt-sol answers 503 until the next login.')
        return
    from litellm.llms.chatgpt.authenticator import Authenticator
    auth = Authenticator()
    if args.preflight:  # runs before the unit's LiteLLM starts, so this is the only refresher
        problem = chatgpt_auth.signin_problem(auth)
        print(problem or 'ChatGPT sign-in usable')
        sys.exit(1 if problem else 0)
    if args.status:
        # Only a running agentiac-chatgpt refreshes; a refresh here could rotate the token under it.
        running = subprocess.run(['systemctl', 'is-active', '--quiet', UNIT]).returncode == 0
        problem = chatgpt_auth.signin_problem(auth, refresh=not running)
        print(f"{problem or 'ChatGPT signed in'}; {UNIT} {'running' if running else 'not running'}")
        sys.exit(1 if problem or not running else 0)
    # Stop the unit (cuts in-flight cloud-gpt-sol requests) so this is the only refresher.
    subprocess.run(['sudo', '-n', 'systemctl', 'stop', UNIT], check=True)
    problem = chatgpt_auth.signin_problem(auth)
    if problem:
        if problem == chatgpt_auth.NOT_SIGNED_IN:
            # A token-less file only holds an abandoned login's timestamp, which would make
            # LiteLLM wait out a 5-min cooldown silently instead of printing a new code.
            (TOKENS / 'auth.json').unlink(missing_ok=True)
        auth.get_access_token()  # prints the device-code URL and code, then waits for the sign-in
        if chatgpt_auth.signin_problem(auth):
            sys.exit('ChatGPT sign-in did not complete')
    subprocess.run(['sudo', '-n', 'systemctl', 'start', UNIT], check=True)
    print(f'ChatGPT signed in and {UNIT} running; cloud-gpt-sol is usable through the gateway.')


if __name__ == '__main__':
    main()

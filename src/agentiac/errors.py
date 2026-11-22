"""Error text for the failures people hit most (T8.4, PA §13): what failed, why it may have, how to check, what next.

The first line stays the sentence the command always printed, so tests and habits that read it keep working.
A cause is written only if the code can produce it: a guess here sends someone to the wrong machine.
"""
import os
import re

COMMAND = 'agentiac'     # the distribution's command name; M9 renames it with the package (D6.8)
INVOKED = 'AGENTIAC_COMMAND'


def command():
    """The name the operator invoked the command as, so a hint can be typed back as printed (D6.8).

    `bin/agentiac` exports it and every process it starts inherits it; without it (a test, a timer that runs a
    script directly, the dashboard) it is the distribution's own name.
    """
    return os.environ.get(INVOKED) or COMMAND


def explain(what, causes, check, step):
    """The string for SystemExit or an exception: WHAT unchanged, then the causes, the check and the next step."""
    lines = [what, *(f'  cause: {cause}' for cause in causes), f'  check: {check}', f'  next:  {step}']
    return '\n'.join(lines)


def headline(error):
    """What explain() was given as WHAT: the advice is for a terminal, and a stored result keeps one sentence."""
    return re.split(r'\n  (?:cause|check): ', str(error), maxsplit=1)[0]

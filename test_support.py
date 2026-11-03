"""Explicit synthetic legacy fixtures; no production initialization path."""
import json
import sys


def _deny_actual_network(event, args):
    if event.startswith('socket.'):
        raise RuntimeError('Actual socket/DNS IO forbidden in offline regression tests')


sys.addaudithook(_deny_actual_network)
from pathlib import Path


def seed_test_state(path='state.json', state=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'_next_submit': 0} if state is None else state, indent=2))
    return path

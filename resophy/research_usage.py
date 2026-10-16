# SPDX-License-Identifier: MIT
"""Content-free, append-only provider usage journal persisted with each task."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path


TOKEN_FIELDS = ('prompt_tokens', 'completion_tokens', 'total_tokens',
                'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens')


def record_usage(path, event, call_id, usage=None):
    if not path:
        return
    entry = {'event': event, 'call_id': call_id,
             'at': datetime.now(timezone.utc).isoformat()}
    if usage is not None:
        entry['tokens'] = {name: usage[name] for name in TOKEN_FIELDS
                           if type(usage.get(name)) is int and usage[name] >= 0}
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, 'a', encoding='utf-8') as stream:
        stream.write(json.dumps(entry) + '\n')
        stream.flush()
        os.fsync(stream.fileno())


def usage_summary(path):
    summary = {'requests_started': 0, 'responses_received': 0, 'requests_failed': 0,
               'responses_without_usage': 0, 'cost': None, 'cost_status': 'not_provided'}
    if not Path(path).is_file():
        return summary
    tokens = {}
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            summary['incomplete_journal'] = True
            continue
        if entry['event'] == 'request_started':
            summary['requests_started'] += 1
        elif entry['event'] == 'request_failed':
            summary['requests_failed'] += 1
        elif entry['event'] == 'response_received':
            summary['responses_received'] += 1
            if not entry.get('tokens'):
                summary['responses_without_usage'] += 1
            for name, count in entry.get('tokens', {}).items():
                if name in TOKEN_FIELDS and type(count) is int and count >= 0:
                    tokens[name] = tokens.get(name, 0) + count
    summary['tokens'] = tokens
    summary['unresolved_requests'] = (summary['requests_started'] - summary['responses_received']
                                      - summary['requests_failed'])
    return summary

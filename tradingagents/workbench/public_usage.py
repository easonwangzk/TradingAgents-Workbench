"""Bounded encrypted operational metadata, independent of conversation history.

Modified for public distribution with Python 3.10-compatible UTC timestamps.
"""

import logging
import math
import time
from datetime import datetime, timezone
from uuid import UUID

from tradingagents.workbench.public_store import StoreConflict

MAX_EVENTS = 512
MAX_OPERATIONS = 2048
KINDS = frozenset({'research', 'assistant', 'research_provider', 'assistant_provider'})


def _metric(value, *, integer=False):
    if value is None:
        return None
    if type(value) not in ((int,) if integer else (int, float)) or value < 0 or not math.isfinite(value):
        raise ValueError('Invalid usage metric')
    return value


class UsageLedger:
    def __init__(self, store, *, clock=time.time, max_events=MAX_EVENTS):
        if type(max_events) is not int or not 1 <= max_events <= MAX_EVENTS:
            raise ValueError('Invalid retention bound')
        self.store = store
        self.clock = clock
        self.max_events = max_events

    @staticmethod
    def _key(account_id):
        if type(account_id) is not str or str(UUID(account_id)) != account_id:
            raise ValueError('Invalid account identity')
        return f'email-v1/users/{account_id}/usage'

    def read(self, account_id):
        stored = self.store.read(self._key(account_id))
        return stored[0] if stored else {'events': [], 'operations': [], 'since': None, 'truncated': False}

    def record(self, account_id, operation_id, kind, *, success, input_tokens=None,
               output_tokens=None, platform_cost_usd=None):
        if (type(operation_id) is not str or not 1 <= len(operation_id) <= 160
                or not operation_id.isascii() or not operation_id.isprintable()
                or kind not in KINDS or type(success) is not bool):
            raise ValueError('Invalid usage metadata')
        event = {'operation_id': operation_id, 'kind': kind,
                 'occurred_at': datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(), 'success': success,
                 'input_tokens': _metric(input_tokens, integer=True),
                 'output_tokens': _metric(output_tokens, integer=True),
                 'platform_cost_usd': _metric(platform_cost_usd)}
        key = self._key(account_id)
        for _ in range(8):
            stored = self.store.read(key)
            record, version = stored if stored else (self.read(account_id), None)
            if operation_id in record['operations']:
                return
            record['operations'].append(operation_id)
            record['events'].append(event)
            if len(record['events']) > self.max_events:
                del record['events'][:-self.max_events]
                record['truncated'] = True
            if len(record['operations']) > MAX_OPERATIONS:
                del record['operations'][:-MAX_OPERATIONS]
                record['truncated'] = True
            record['since'] = record['events'][0]['occurred_at']
            try:
                self.store.write(key, record, version)
                return
            except StoreConflict:
                continue
        raise StoreConflict()

    def capture(self, *args, **kwargs):
        try:
            self.record(*args, **kwargs)
        except Exception:
            logging.getLogger(__name__).warning('Usage metadata could not be persisted; upstream outcome is not retried.')

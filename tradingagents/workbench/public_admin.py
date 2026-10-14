"""Owner-only bounded metadata projections. Never load research report bodies.

Modified for public distribution with Python 3.10-compatible UTC timestamps.
"""

from datetime import datetime, timedelta, timezone
from uuid import UUID

from tradingagents.workbench.public_accounts import AccountError
from tradingagents.workbench.public_hosted_runs import _ERRORS
from tradingagents.workbench.public_usage import MAX_EVENTS, UsageLedger

MAX_SUMMARIES = 512


def _date(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None
    except (AttributeError, TypeError, ValueError):
        return None


class PublicAdmin:
    def __init__(self, *, auth, budget, usage=None):
        self.auth = auth
        self.budget = budget
        self.usage = usage if usage is not None else UsageLedger(auth.store, clock=auth.clock)

    def _metadata(self, session_id):
        accounts = self.auth.accounts(session_id)['accounts']
        summaries = []
        remaining = MAX_SUMMARIES
        truncated = False
        for account in accounts:
            if remaining <= 0:
                truncated = True
                break
            stored = self.auth.store.read(f"email-v1/users/{account['object_id']}/hosted-runs/index")
            if stored is None:
                continue
            entries = stored[0].get('runs', [])
            if type(entries) is not list:
                raise AccountError(status_code=503)
            truncated |= len(entries) > remaining
            for entry in entries[-remaining:]:
                summary = entry.get('summary', {})
                summaries.append((account, {key: summary.get(key) for key in ('id', 'status', 'created_at', 'error_code')}))
            remaining -= min(len(entries), remaining)
        return accounts, summaries, truncated

    @staticmethod
    def _coverage(since, truncated, notes=()):
        return {'since': since, 'notes': [
            'Research metadata is bounded to 512 index summaries; report bodies are never read.',
            'Usage includes newly recorded local events only; historical assistant tokens and costs are unknown.',
            'External provider cost is unknown. Usage persistence failures may leave gaps; requests are never replayed.',
            'Operation deduplication retains the most recent 2048 identifiers per account.', *notes],
            'truncated': truncated}

    def overview(self, session_id):
        accounts, summaries, truncated = self._metadata(session_id)
        statuses = [summary['status'] for _, summary in summaries]
        dates = [summary['created_at'] for _, summary in summaries if _date(summary['created_at'])]
        failures = [{'account_email': account['email'], 'run_id': summary['id'],
                     'code': summary['error_code'] if summary['error_code'] in _ERRORS else 'worker_error',
                     'occurred_at': summary['created_at'] if _date(summary['created_at']) else None}
                    for account, summary in summaries if summary['status'] == 'failed']
        starts = []
        failure_codes = {'assistant': 'assistant_failed', 'assistant_provider': 'provider_failed',
                         'research_provider': 'provider_failed'}
        for account in accounts:
            record = self.usage.read(account['object_id'])
            events = record['events']
            truncated |= record['truncated'] or len(events) > MAX_EVENTS
            if _date(record['since']):
                starts.append(record['since'])
            for event in events[-MAX_EVENTS:]:
                if event['success'] is False and event['kind'] in failure_codes:
                    failures.append({'account_email': account['email'], 'run_id': '',
                                     'code': failure_codes[event['kind']],
                                     'occurred_at': event['occurred_at'] if _date(event['occurred_at']) else None})
        return {'accounts': {'total': len(accounts), 'enabled': sum(not item['disabled'] for item in accounts),
                             'disabled': sum(item['disabled'] for item in accounts)},
                'runs': {'total': len(statuses), 'succeeded': statuses.count('succeeded'),
                         'failed': statuses.count('failed'),
                         'active': sum(status not in {'succeeded', 'failed', 'stopped'} for status in statuses)},
                'budget': self.budget.snapshot(),
                'recent_errors': sorted(failures, key=lambda item: item['occurred_at'] or '', reverse=True)[:20],
                'coverage': self._coverage(max(starts) if starts else min(dates) if dates else None, truncated,
                                           ('Usage failure reads are bounded to 512 retained events per account (at most 256 accounts).',
                                            'Research failure timestamps are run creation times; usage failures use actual event times.',
                                            'Usage coverage starts at the latest retained account start; earlier events may be unavailable.',))}

    def usage_report(self, session_id, *, days=30, account_id=None):
        self.auth.accounts(session_id)
        if type(days) is not int or days not in {7, 30, 90}:
            raise AccountError(status_code=400)
        if account_id is not None:
            try:
                if str(UUID(account_id)) != account_id:
                    raise ValueError
            except (TypeError, ValueError, AttributeError):
                raise AccountError(status_code=400) from None
        accounts, summaries, truncated = self._metadata(session_id)
        if account_id is not None and not any(item['object_id'] == account_id for item in accounts):
            raise AccountError(status_code=400)
        cutoff = datetime.fromtimestamp(self.auth.clock(), timezone.utc) - timedelta(days=days)
        rows = []
        starts = []
        for account in accounts:
            if account_id is not None and account['object_id'] != account_id:
                continue
            record = self.usage.read(account['object_id'])
            truncated |= record['truncated']
            if record['since']:
                starts.append(record['since'])
            events = [event for event in record['events'] if _date(event['occurred_at']) and _date(event['occurred_at']) >= cutoff]
            runs = {summary['id']: summary for owner, summary in summaries
                    if owner['object_id'] == account['object_id'] and _date(summary['created_at'])
                    and _date(summary['created_at']) >= cutoff}
            terminal = [event for event in events if event['kind'] == 'research']
            for event in terminal:
                identifier = event['operation_id'].removeprefix('research:')
                runs.setdefault(identifier, {'status': 'succeeded' if event['success'] else 'failed',
                                             'created_at': event['occurred_at']})
            metrics = [event for event in events if event['kind'] in {'research_provider', 'assistant_provider'}]
            if not metrics:
                metrics = [event for event in events if event['kind'] == 'research' and event['input_tokens'] is not None]
            activity = [event['occurred_at'] for event in events] + [summary['created_at'] for summary in runs.values()]
            row = {'account_id': account['object_id'], 'email': account['email'], 'research_runs': len(runs),
                   'research_failed': sum(item['status'] == 'failed' for item in runs.values()),
                   'assistant_calls': sum(event['kind'] == 'assistant' for event in events),
                   'external_cost_usd': None, 'last_activity': max(activity) if activity else None}
            for metric in ('input_tokens', 'output_tokens', 'platform_cost_usd'):
                row[metric] = (sum(event[metric] for event in metrics)
                               if metrics and all(event[metric] is not None for event in metrics) else None)
            rows.append(row)
        return {'days': days, 'rows': rows,
                'coverage': self._coverage(max(starts) if starts else None, truncated)}

"""Read scheduling protection before synthetic probes; never change account policy."""
from datetime import datetime
import math
import time


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else 0
    except (ValueError, TypeError):
        return 0


def flag(value):
    return value is True or str(value).strip().lower() in ('true', '1')


def timestamp(value):
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError, OverflowError):
        return 0


def probe_pause(account, defaults=None, now=None):
    """Mirror ordinary Codex quota pause without refreshing or spending credits.

    Credit auto-reset has a separate state machine. Leave its diagnostics to
    that controller instead of trying to duplicate its credit-spending policy.
    """
    if account.get('status', 'active') != 'active' or account.get('schedulable') is False:
        return {'reason': 'account_not_schedulable'}
    extra = account.get('extra') or {}
    if flag(extra.get('auto_reset_credit_enabled')):
        return {'reason': 'credit_policy_managed'}
    now = time.time() if now is None else now
    updated = timestamp(extra.get('codex_usage_updated_at'))
    if updated and now-updated >= 7200:
        return None  # Same two-hour freshness bound as the gateway scheduler.
    for window in ('5h', '7d'):
        if flag(extra.get('auto_pause_'+window+'_disabled')):
            continue
        threshold = max(0, min(1, number(extra.get('auto_pause_'+window+'_threshold'))))
        if threshold <= 0:
            threshold = max(0, min(1, number((defaults or {}).get('default_threshold_'+window))))
        used = number(extra.get('codex_'+window+'_used_percent'))/100
        reset = timestamp(extra.get('codex_'+window+'_reset_at'))
        remaining = number(extra.get('codex_'+window+'_reset_after_seconds'))
        if not reset and updated and remaining > 0:
            reset = updated+remaining
        if reset and now >= reset:
            continue
        if threshold > 0 and used >= threshold:
            return {'reason': 'quota_auto_pause_'+window, 'window': window,
                    'utilization': used, 'threshold': threshold, 'reset_at': reset or None}
    return None

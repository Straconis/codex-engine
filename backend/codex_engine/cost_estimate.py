"""Local-only estimates using a dated official pricing snapshot, never a billing guarantee."""
import math
import secrets
import threading
import time
from . import ai_format

PRICING_URL = "https://api-docs.deepseek.com/quick_start/pricing/"
CHECKED = "2026-10-12"
# USD per million tokens: peak, cache-miss input. Off-peak/cache savings are not assumed.
RATES = {"deepseek-flash": (0.30, 1.20), "deepseek-v4-pro": (1.32, 3.96)}
_lock = threading.Lock()
_approvals = {}
TTL = 600


def estimate(sections, model):
    rates = RATES.get(model)
    if rates is None:
        return {"estimated_usd": None, "higher_estimate_usd": None}
    typical_input = typical_output = high_input = high_output = 0
    system_bytes = len(ai_format.SYSTEM_PROMPT.encode("utf-8"))
    for section in sections:
        source_bytes = len(section.encode("utf-8"))
        limit = ai_format.max_output_tokens(section)
        typical_input += math.ceil((system_bytes + source_bytes) / 4) + 64
        typical_output += math.ceil(source_bytes / 4)
        # Up to two attempts, full capped replies, no cache discounts; allow ample retry-prompt overhead.
        high_input += 2 * (system_bytes + source_bytes + 24000 + 16000 + 512)
        high_output += 2 * limit
    incoming, outgoing = rates
    return {"estimated_usd": round((typical_input * incoming + typical_output * outgoing) / 1_000_000, 6),
            "higher_estimate_usd": round((high_input * incoming + high_output * outgoing) / 1_000_000, 6)}


def issue(identity, sections, model):
    token = secrets.token_urlsafe(24)
    with _lock:
        now = time.monotonic()
        for key in list(_approvals):
            if _approvals[key][1] <= now:
                del _approvals[key]
        if len(_approvals) >= 1000:
            _approvals.pop(next(iter(_approvals)))
        _approvals[token] = (identity, now + TTL)
    return {"required": bool(sections), "approval_token": token, **estimate(sections, model),
            "model": model, "sections_to_send": len(sections), "possible_attempts_per_section": 2,
            "pricing_checked": CHECKED, "pricing_url": PRICING_URL,
            "disclaimer": "Estimate only. Token counts, retries, rate changes, taxes and billing currency can change the actual charge. Higher estimate is not a guaranteed spending cap."}


def consume(token, identity):
    with _lock:
        item = _approvals.pop(token, None)
    return item is not None and item[0] == identity and item[1] > time.monotonic()

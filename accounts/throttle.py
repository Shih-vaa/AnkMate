"""Sign-in lockout. Counts failed attempts in the cache and refuses further tries for a while.

Two counters per sign-in identifier: one for (identifier, address), which locks the person typing from that device
quickly, and one for the identifier alone with a higher ceiling, so rotating addresses doesn't help an attacker but one
careless neighbour can't lock the real owner out after a single bad guess."""
import re

from django.conf import settings
from django.core.cache import cache

from .models import normalize_phone

_PHONE_LIKE = re.compile(r"[\d\s+\-]+")


def canonical(identifier):
    """The same account, however it is typed: '+91 98765-43210' and '9876543210' count as one."""
    ident = (identifier or "").strip().lower()
    return normalize_phone(ident) if ident and _PHONE_LIKE.fullmatch(ident) else ident


def _keys(identifier, address):
    ident = canonical(identifier)
    return f"login-fail:{ident}:{address}", f"login-fail:{ident}:*"


def is_locked(identifier, address):
    per_address, per_identifier = _keys(identifier, address)
    limit = settings.LOGIN_MAX_ATTEMPTS
    return cache.get(per_address, 0) >= limit or cache.get(per_identifier, 0) >= limit * 3


def record_failure(identifier, address):
    window = settings.LOGIN_LOCKOUT_SECONDS
    for key in _keys(identifier, address):
        cache.add(key, 0, window)  # starts the window on the first failure only
        try:
            cache.incr(key)
        except ValueError:  # expired between add() and incr()
            cache.set(key, 1, window)


def reset(identifier, address):
    per_address, _ = _keys(identifier, address)
    cache.delete(per_address)  # a successful sign-in clears this device's count (the broader count simply expires)

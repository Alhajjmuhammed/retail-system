"""
Slowing down password guessing.

Without this, a login form on the public internet is an open invitation: a
script can try a shop owner's email against a wordlist as fast as the server
will answer. Counted per email and per address, in the cache, so it costs
nothing and survives a restart losing only the counters.
"""

from django.core.cache import cache

MAX_ATTEMPTS = 8
# One address may not hammer many accounts either.
MAX_PER_IP = 20
# A last, loose net against a slow distributed guess at one account. High
# enough that nobody can lock a shop owner out by guessing at them on purpose.
MAX_PER_ACCOUNT = 50
LOCKOUT_SECONDS = 15 * 60


def _keys(email, ip):
    """
    Counted per (account, address) pair first.

    Per email alone was a way to lock an owner out at will; per address alone
    was defeated by a forged header, and by spelling the email with
    look-alike characters, because the database matches case-insensitively
    and Python normalises unicode. `email` is the canonical address of the
    account, resolved before we get here.
    """
    who = (email or "").lower()
    return (
        (f"login-fail:pair:{who}:{ip or 'unknown'}", MAX_ATTEMPTS),
        (f"login-fail:ip:{ip or 'unknown'}", MAX_PER_IP),
        (f"login-fail:email:{who}", MAX_PER_ACCOUNT),
    )


def is_locked(email, ip) -> bool:
    """
    Fails open on a cache outage.

    Failing closed would lock every shop out of the system over a Redis
    restart, which is a far worse outcome than briefly not counting attempts.
    """
    for key, ceiling in _keys(email, ip):
        try:
            if cache.get(key, 0) >= ceiling:
                return True
        except Exception:
            return False
    return False


def record_failure(email, ip) -> None:
    for key, _ceiling in _keys(email, ip):
        try:
            count = cache.get(key, 0) + 1
            cache.set(key, count, LOCKOUT_SECONDS)
        except Exception:
            # A cache outage must never stop people signing in.
            return


def clear(email, ip) -> None:
    for key, _ceiling in _keys(email, ip):
        try:
            cache.delete(key)
        except Exception:
            return


def seconds_remaining() -> int:
    return LOCKOUT_SECONDS


SIGNUPS_PER_ADDRESS = 5
SIGNUP_WINDOW = 60 * 60


def signups_exhausted(ip) -> bool:
    """One address opening shop after shop is a script, not a shopkeeper."""
    try:
        return cache.get(f"signup:{ip or 'unknown'}", 0) >= SIGNUPS_PER_ADDRESS
    except Exception:
        return False


def record_signup(ip) -> None:
    key = f"signup:{ip or 'unknown'}"
    try:
        cache.set(key, cache.get(key, 0) + 1, SIGNUP_WINDOW)
    except Exception:
        return

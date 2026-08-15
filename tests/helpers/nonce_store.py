"""InMemoryNonceStore test double (DRAFT 2026-08-13).

This file is a DRAFT for future Day 5+ implementation. It is NOT
executed today. The draft is the in-memory LRU nonce store used by
the v0.7 §1.4 verification. The production nonce store (Redis-backed
or DB-backed) will be a separate module that follows this same
interface so the tests are reusable.

Per v0.7 §1.4 (docs/proposals/orch-client-build-impl-plan-v0.7.md
line 327+) and the spec at docs/specs/orch-server-hmac-v0.7-alignment.md
§1.4 step 7: "Check the nonce has not been seen recently (in-memory
LRU with TTL matching the timestamp window; else 401 with
NONCE_REPLAY)".

The TTL matches the timestamp window (default 300s, per
src/hermes_orch/auth/hmac.py DEFAULT_HMAC_WINDOW_SEC) so nonces
older than the window are automatically evicted.
"""
from __future__ import annotations

import time
from typing import Optional


class InMemoryNonceStore:
    """In-memory LRU for nonce replay protection (v0.7 §1.4).

    The production nonce store (Redis-backed) will have the same
    interface so tests are reusable. The TTL matches the timestamp
    window (default 300s) so nonces older than the window are
    automatically evicted.
    """

    def __init__(self, ttl_seconds: int = 300):
        """Initialize the nonce store with the given TTL.

        Args:
            ttl_seconds: How long a nonce is considered "fresh" after
                first use. Default 300s (5 minutes, matching the
                HMAC timestamp window per
                src/hermes_orch/auth/hmac.py:DEFAULT_HMAC_WINDOW_SEC).
        """
        self._store: dict[str, float] = {}
        self._ttl = ttl_seconds

    def check_and_record(
        self, nonce: str, now: Optional[float] = None
    ) -> bool:
        """Check if the nonce has been seen within the TTL window.

        Returns:
            True  → nonce is fresh; record it and accept
            False → nonce is a replay; reject

        Side effect: on a fresh nonce, records the nonce with
        expiry = now + ttl_seconds. On a replay, does NOT update
        the existing record (preserves the original expiry).
        """
        now = now if now is not None else time.time()
        self._evict_expired(now)
        if nonce in self._store:
            return False  # replay
        self._store[nonce] = now + self._ttl
        return True

    def _evict_expired(self, now: float) -> None:
        """Remove nonces whose expiry is in the past."""
        self._store = {k: v for k, v in self._store.items() if v > now}

    def __len__(self) -> int:
        return len(self._store)

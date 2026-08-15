# coding: utf-8
"""In-process nonce store for v0.7 §1.4 HMAC replay protection.

Per spec §1 step 7, the verifier rejects requests whose nonce has
been seen within the timestamp window. This module provides a
simple in-process implementation; production deployments with
multiple uvicorn workers should swap in a Redis-backed store
(out of scope for v0.7; see impl plan §7 "Out of scope").

Design:
  - Per-nonce TTL = HMAC_WINDOW_SEC (default 300s). After TTL the
    nonce is evicted, so a replay of a request older than the
    window would already be rejected by the timestamp check.
  - Bounded memory: max_nonces (default 100k) prevents unbounded
    growth under attack. When full, oldest entries are evicted.
  - Thread-safe: a single lock guards the dict. uvicorn runs sync
    code in a thread pool; multiple workers would each have their
    own store (and would need Redis for cross-worker consistency).

Usage:
    store = InMemoryNonceStore(ttl_seconds=300, max_nonces=100_000)
    if store.is_seen(nonce):
        raise ...  # replay
    store.add(nonce)
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict


class InMemoryNonceStore:
    """Bounded in-memory nonce store with TTL-based eviction."""

    def __init__(self, ttl_seconds: int = 300, max_nonces: int = 100_000):
        self._ttl = ttl_seconds
        self._max = max_nonces
        self._lock = threading.Lock()
        # OrderedDict: insertion order is eviction order (FIFO).
        # value = (nonce, expiry_ts).
        self._seen: "OrderedDict[str, float]" = OrderedDict()

    def is_seen(self, nonce: str) -> bool:
        """True if the nonce has been seen within the TTL window."""
        with self._lock:
            entry = self._seen.get(nonce)
            if entry is None:
                return False
            expiry_ts = entry
            if time.time() > expiry_ts:
                # Expired; evict and treat as not seen
                del self._seen[nonce]
                return False
            return True

    def add(self, nonce: str) -> None:
        """Mark the nonce as seen with the current time + TTL.

        If the store is at capacity, evict the oldest entry first.
        Re-adding an already-seen nonce is allowed (it just refreshes
        the expiry, which is harmless for replay protection).
        """
        with self._lock:
            expiry = time.time() + self._ttl
            # If already present, remove first so the new entry
            # is at the end (most recent, evicted last).
            if nonce in self._seen:
                del self._seen[nonce]
            self._seen[nonce] = expiry
            # Bounded memory: evict oldest if over capacity
            while len(self._seen) > self._max:
                self._seen.popitem(last=False)

    def add_if_absent(self, nonce: str) -> bool:
        """Atomic check-and-record (Hardening Phase 2, 2026-08-15).

        Returns True iff the nonce was NOT already in the store
        (first to record it). Returns False if the nonce was
        already seen within the TTL window (replay detected).

        The check + record happens inside a single critical
        section, so concurrent callers racing to add the same
        nonce see deterministic behavior: exactly one caller
        returns True; all others return False. The previous
        `is_seen` + `add` two-call pattern had a race window
        between the lock release after `is_seen` and the lock
        acquisition for `add` — two concurrent callers could
        both see `is_seen() == False` and both proceed to `add`,
        effectively accepting two requests with the same nonce.

        The atomic version closes that window. The lock is held
        for the full check+insert, so the second caller always
        sees the nonce already present in the dict.

        Per-nonce TTL eviction: if the nonce is in the store
        but has expired (older than `ttl_seconds`), the expired
        entry is treated as absent and a fresh entry is recorded.
        This matches the original `is_seen` semantics (expired =
        not seen).
        """
        with self._lock:
            entry = self._seen.get(nonce)
            if entry is not None:
                expiry_ts = entry
                if time.time() <= expiry_ts:
                    # Not expired, already seen -> replay
                    return False
                # Expired: evict and proceed to record
                del self._seen[nonce]
            # Not present (or just evicted as expired): record it
            expiry = time.time() + self._ttl
            self._seen[nonce] = expiry
            # Bounded memory: evict oldest if over capacity
            while len(self._seen) > self._max:
                self._seen.popitem(last=False)
            return True

    def __len__(self) -> int:
        with self._lock:
            return len(self._seen)

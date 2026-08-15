# coding: utf-8
"""Nonce store for v0.7 §1.4 HMAC replay protection.

Per spec §1.14, the nonce store interface is abstracted behind a
`NonceStore` Protocol so the production backend can be swapped
without touching the v0.7 verifier. Two implementations ship in
v0.7:

  - `InMemoryNonceStore` (default): per-process, in-memory,
    `threading.Lock`-protected, atomic via `add_if_absent`. Used
    for single-process deployments and tests. Does NOT coordinate
    across multiple uvicorn workers.

  - `RedisNonceStore` (stub): cross-process via `SET nonce TTL NX`
    semantics. Stub implementation only in v0.7; raises
    `NotImplementedError` on every operation. The real Redis
    client integration is deferred to a follow-up PR.

Design (InMemoryNonceStore):
  - Per-nonce TTL = HMAC_WINDOW_SEC (default 300s). After TTL the
    nonce is evicted, so a replay of a request older than the
    window would already be rejected by the timestamp check.
  - Bounded memory: max_nonces (default 100k) prevents unbounded
    growth under attack. When full, oldest entries are evicted.
  - Thread-safe: a single lock guards the dict. uvicorn runs sync
    code in a thread pool; multiple workers would each have their
    own store (and would need Redis for cross-worker consistency).

Usage:
    store = make_nonce_store(backend="memory")  # or "redis"
    if not store.add_if_absent(nonce):
        raise ...  # replay detected

The factory reads `HERMES_NONCE_STORE_BACKEND` env var (default
`memory`) and instantiates the right implementation.
"""
from __future__ import annotations

import os
import threading
import time
from collections import OrderedDict
from typing import Protocol, runtime_checkable


@runtime_checkable
class NonceStore(Protocol):
    """Structural Protocol for nonce stores.

    Any class with `is_seen`, `add`, and `add_if_absent` methods
    satisfies this protocol. The `@runtime_checkable` decorator
    allows `isinstance(store, NonceStore)` checks (used in tests
    and at startup to verify the configured store implements the
    protocol before accepting requests).

    Implementations:
    - `InMemoryNonceStore`: per-process, default
    - `RedisNonceStore`: cross-process (stub in v0.7)
    """

    def is_seen(self, nonce: str) -> bool:
        """True iff the nonce has been seen within the TTL window."""
        ...

    def add(self, nonce: str) -> None:
        """Mark the nonce as seen (legacy interface; non-atomic)."""
        ...

    def add_if_absent(self, nonce: str) -> bool:
        """Atomic check+record. Returns True iff first to record."""
        ...


class InMemoryNonceStore:
    """Bounded in-memory nonce store with TTL-based eviction.

    Implements the NonceStore protocol (structural typing).
    """

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


class RedisNonceStore:
    """Cross-process nonce store via Redis `SET nonce TTL NX` (stub).

    Hardening Phase 7 (2026-08-15): the actual Redis client
    integration is deferred. The stub satisfies the
    `NonceStore` protocol structurally (has the right methods)
    so the factory + Protocol isinstance check + lifespan
    wiring all work, but every operation raises
    `NotImplementedError` with a clear production-deployment
    message.

    Per spec §1.14: when this stub is hit in production, the
    operator sees a clear error and knows to implement the
    real Redis client (separate follow-up PR).

    The constructor accepts a `redis_url` so the eventual
    implementation can use the same call site. The current
    stub does not actually connect.
    """

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379/0",
        ttl_seconds: int = 300,
    ):
        self._url = redis_url
        self._ttl = ttl_seconds
        # The real implementation would do:
        #   import redis
        #   self._client = redis.Redis.from_url(redis_url)
        # Stub: no client instantiation.

    def _stub_error(self, method: str) -> None:
        raise NotImplementedError(
            f"RedisNonceStore.{method} is a stub in v0.7. The "
            f"real Redis client integration is deferred to a "
            f"follow-up PR. The configured redis_url={self._url!r} "
            f"is recorded but not connected. Production deployment "
            f"with HERMES_NONCE_STORE_BACKEND=redis MUST NOT use "
            f"this stub — implement the real client first."
        )

    def is_seen(self, nonce: str) -> bool:
        self._stub_error("is_seen")

    def add(self, nonce: str) -> None:
        self._stub_error("add")

    def add_if_absent(self, nonce: str) -> bool:
        self._stub_error("add_if_absent")


def make_nonce_store(backend: str | None = None) -> NonceStore:
    """Factory: instantiate the right NonceStore based on the backend name.

    Reads `HERMES_NONCE_STORE_BACKEND` env var if `backend` is None.
    Default backend: `memory` (InMemoryNonceStore).

    Args:
        backend: one of `memory` / `redis`. None = read from env.

    Returns:
        A `NonceStore` instance. The `isinstance` Protocol check
        passes for both implementations.

    Raises:
        ValueError: if the backend name is unknown.
    """
    if backend is None:
        backend = os.environ.get("HERMES_NONCE_STORE_BACKEND", "memory").strip().lower()
    if not backend:
        backend = "memory"
    if backend == "memory":
        return InMemoryNonceStore(ttl_seconds=300)
    if backend == "redis":
        return RedisNonceStore(
            redis_url=os.environ.get("HERMES_NONCE_REDIS_URL", "redis://localhost:6379/0"),
            ttl_seconds=300,
        )
    raise ValueError(
        f"unknown nonce store backend: {backend!r}; "
        f"expected 'memory' or 'redis'"
    )

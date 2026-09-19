"""M022 — thread-safe cancellation token shared by every agent backend.

The backend contract already accepts an opaque ``cancel`` object exposing an
``is_set()`` predicate. ``CancellationToken`` is the canonical implementation:
one process-safe flag that a caller can set from any thread while a bounded
run is in flight. It never kills a process itself — a backend decides how to
observe it — so cancellation stays bounded and provider-neutral.
"""

from __future__ import annotations

import threading


class CancellationToken:
    """A lane-independent, thread-safe cancellation flag."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def is_set(self) -> bool:
        """The predicate consumed by the backend contract."""
        return self._event.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)

    def reset(self) -> None:
        """Clear the flag (only for reuse before a new bounded run)."""
        self._event.clear()

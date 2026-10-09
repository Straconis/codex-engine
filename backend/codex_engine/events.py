from __future__ import annotations

import asyncio
import threading


class EventBroker:
    """Fan-out of backend events to every connected SSE client.

    Each subscriber gets its own asyncio queue on its own event loop, so every client
    sees every event, and a client that disconnects just unsubscribes (nothing is left
    blocked waiting on a queue).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []

    def subscribe(self) -> tuple[asyncio.AbstractEventLoop, asyncio.Queue]:
        sub = (asyncio.get_running_loop(), asyncio.Queue())
        with self._lock:
            self._subscribers.append(sub)
        return sub

    def unsubscribe(self, sub) -> None:
        with self._lock:
            if sub in self._subscribers:
                self._subscribers.remove(sub)

    def publish(self, name: str, payload: dict) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for loop, q in subscribers:
            try:
                loop.call_soon_threadsafe(q.put_nowait, (name, payload))
            except RuntimeError:
                # Event loop already closed; drop the dead subscriber.
                self.unsubscribe((loop, q))

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

from __future__ import annotations

import asyncio


class Notifier:
    """Wakes every waiter once per notify(). Cheap broadcast for SSE and loops."""

    def __init__(self) -> None:
        self._event = asyncio.Event()

    def notify(self) -> None:
        self._event.set()

    async def wait(self, timeout: float | None = None) -> bool:
        try:
            await asyncio.wait_for(self._event.wait(), timeout)
        except asyncio.TimeoutError:
            return False
        finally:
            # allow the next notify() to wake waiters again
            self._event.clear()
        return True

from __future__ import annotations

import asyncio
from weakref import WeakKeyDictionary


class Notifier:
    """Wakes every waiter once per notify(). Cheap broadcast for SSE and loops.

    Each notify() bumps a sequence number and resolves every waiter that is
    parked right now. A waiting task also remembers the sequence it last saw, so
    a notify() that lands while that task is busy between two wait() calls is
    delivered on its next call instead of being swallowed.
    """

    def __init__(self) -> None:
        self._seq = 0
        self._waiters: set[asyncio.Future] = set()
        # per-task bookmark; entries disappear with the task
        self._seen: WeakKeyDictionary = WeakKeyDictionary()

    @property
    def seq(self) -> int:
        return self._seq

    def notify(self) -> None:
        self._seq += 1
        for fut in list(self._waiters):
            if not fut.done():
                fut.set_result(None)

    async def wait(self, timeout: float | None = None) -> bool:
        task = asyncio.current_task()
        seen = self._seen.get(task, self._seq) if task is not None else self._seq
        if self._seq > seen:  # notified while this task was busy
            self._seen[task] = self._seq
            return True

        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._waiters.add(fut)
        try:
            await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return False
        finally:
            self._waiters.discard(fut)
            if task is not None:
                self._seen[task] = self._seq
        return True

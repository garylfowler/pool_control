import asyncio

from app.notifier import Notifier


async def test_two_concurrent_waiters_both_wake_from_one_notify():
    n = Notifier()
    a = asyncio.create_task(n.wait(2))
    b = asyncio.create_task(n.wait(2))
    await asyncio.sleep(0.01)
    n.notify()
    assert await asyncio.wait_for(asyncio.gather(a, b), 1) == [True, True]


async def test_wait_returns_false_on_timeout():
    n = Notifier()
    assert await n.wait(0.01) is False


async def test_notify_reaches_a_waiter_that_was_busy_between_waits():
    n = Notifier()
    fast, slow = [], []

    async def fast_loop():
        for _ in range(2):
            fast.append(await n.wait(1))

    async def slow_loop():
        slow.append(await n.wait(1))
        await asyncio.sleep(0.1)  # busy building a frame while the next change lands
        slow.append(await n.wait(1))

    tasks = [asyncio.create_task(fast_loop()), asyncio.create_task(slow_loop())]
    await asyncio.sleep(0.01)
    n.notify()
    await asyncio.sleep(0.01)
    n.notify()  # fast is waiting again; slow has not called wait() yet
    await asyncio.wait_for(asyncio.gather(*tasks), 2)
    assert fast == [True, True]
    assert slow == [True, True]

"""Event-bus delivery semantics — the contract every transport is built on.

The interesting case is not the happy path but the **late subscriber**: a UI
that connects while the run is still executing. These tests pin down that such
a client receives the backlog *and* everything published while the backlog is
being delivered, exactly once and in publish order.
"""

from __future__ import annotations

import asyncio
from contextlib import aclosing

from os_core.events import Event, EventBus, EventType, replay_and_stream


async def _publish(bus: EventBus, type_: EventType, **data: object) -> Event:
    event = Event(type=type_, run_id=bus.run_id, data=data)
    await bus.publish(event)
    return event


async def test_history_is_delivered_before_live_events_in_publish_order() -> None:
    bus = EventBus(run_id="run_order")
    for i in range(3):
        await _publish(bus, EventType.LOG, i=i)

    received: list[int] = []

    async def late_client() -> None:
        async with aclosing(replay_and_stream(bus)) as stream:
            async for event in stream:
                received.append(event.data["i"])
                if len(received) == 5:
                    break

    async def run_task() -> None:
        for i in range(3, 5):
            await asyncio.sleep(0)
            await _publish(bus, EventType.LOG, i=i)

    await asyncio.wait_for(asyncio.gather(run_task(), late_client()), timeout=5)

    assert received == [0, 1, 2, 3, 4]


async def test_events_published_while_the_backlog_is_delivered_are_not_lost() -> None:
    """Regression guard for the replay/live handoff.

    Both transports used to snapshot ``bus.replay()`` and only *then* open
    ``bus.stream()``. Every ``await`` spent sending the backlog (a WebSocket
    frame, an SSE chunk) let the run publish further events, and those landed
    in neither the already-taken snapshot nor the not-yet-opened subscription:
    the original repro lost 4 of 16 events. Subscribing first closes the gap.
    """
    bus = EventBus(run_id="run_gap")
    published: list[str] = []
    received: list[Event] = []

    for i in range(5):  # the backlog a late client must be handed
        published.append((await _publish(bus, EventType.LOG, i=i)).id)

    async def run_task() -> None:  # the agent keeps executing meanwhile
        for i in range(5, 15):
            await asyncio.sleep(0)
            published.append((await _publish(bus, EventType.LOG, i=i)).id)
        await asyncio.sleep(0)
        published.append((await _publish(bus, EventType.RUN_FINISHED)).id)

    async def late_client() -> None:
        async with aclosing(replay_and_stream(bus)) as stream:
            async for event in stream:
                received.append(event)
                await asyncio.sleep(0)  # a socket write yields to the run task
                if event.type is EventType.RUN_FINISHED:
                    break

    await asyncio.wait_for(asyncio.gather(run_task(), late_client()), timeout=5)

    delivered = [event.id for event in received]
    assert delivered == published  # nothing lost, nothing duplicated, order kept
    assert len(delivered) == len(set(delivered))


async def test_an_event_in_both_history_and_live_feed_is_delivered_once() -> None:
    """The id-based de-duplication half of the fix.

    With the in-process bus an event cannot be both snapshotted and queued, but
    an external broker (Redis Streams / NATS — see ``docs/ROADMAP.md``) can hand
    the same event to both phases. ``Event.id`` makes the overlap idempotent.
    """

    class RacyBus(EventBus):
        """Claims ``claimed`` is already in the backlog, then publishes it live."""

        claimed: Event

        def replay(self, after_ts: float = 0.0) -> list[Event]:
            return [*super().replay(after_ts), self.claimed]

    claimed = Event(type=EventType.LOG, run_id="run_dupe", data={"text": "once"})
    bus = RacyBus(run_id="run_dupe", claimed=claimed)
    received: list[Event] = []

    async def run_task() -> None:
        await asyncio.sleep(0)
        await bus.publish(claimed)  # same id as the one already delivered
        await _publish(bus, EventType.RUN_FINISHED)

    async def late_client() -> None:
        async with aclosing(replay_and_stream(bus)) as stream:
            async for event in stream:
                received.append(event)
                if event.type is EventType.RUN_FINISHED:
                    break

    await asyncio.wait_for(asyncio.gather(run_task(), late_client()), timeout=5)

    assert [event.type for event in received] == [EventType.LOG, EventType.RUN_FINISHED]
    assert [event.id for event in received].count(claimed.id) == 1


async def test_after_ts_resumes_from_a_client_supplied_cursor() -> None:
    bus = EventBus(run_id="run_cursor")
    first = await _publish(bus, EventType.LOG, i=0)
    second = await _publish(bus, EventType.LOG, i=1)
    second.ts = first.ts + 1.0  # make the cursor unambiguous

    received: list[Event] = []

    async def late_client() -> None:
        async with aclosing(replay_and_stream(bus, after_ts=first.ts)) as stream:
            async for event in stream:
                received.append(event)
                break

    await asyncio.wait_for(late_client(), timeout=5)

    assert [event.id for event in received] == [second.id]


async def test_stopping_early_unsubscribes_from_the_bus() -> None:
    """Breaking at ``RUN_FINISHED`` must not leave a queue-feeding listener behind.

    The transports break out of the loop; ``aclosing`` turns that into a
    deterministic unsubscribe instead of waiting for garbage collection.
    """
    bus = EventBus(run_id="run_cleanup")
    await _publish(bus, EventType.RUN_STARTED)
    await _publish(bus, EventType.RUN_FINISHED)

    async with aclosing(replay_and_stream(bus)) as stream:
        async for _ in stream:
            break

    assert bus._listeners == []  # the subscription was released, not left feeding a queue

    # Publishing after the client left must still work and must not queue anywhere.
    await _publish(bus, EventType.LOG, i="after")
    assert bus.buffer[-1].data == {"i": "after"}

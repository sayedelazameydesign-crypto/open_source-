"""Test that browser_action events are emitted after browser tool calls."""
import pytest
from os_core.events import Event, EventBus, EventType, replay_and_stream
from contextlib import aclosing


async def test_browser_action_after_navigate():
    """browser_action emitted with action='goto' after browser_navigate."""
    bus = EventBus(run_id='test_navigate')
    
    await bus.publish(Event(type=EventType.RUN_STARTED, run_id='test_navigate',
                           data={'task': 'browse', 'mode': 'react', 'max_steps': 5}))
    await bus.publish(Event(type=EventType.TOOL_CALL, run_id='test_navigate',
                           data={'id': 'c1', 'name': 'browser_navigate', 'args': {'url': 'https://example.org'}}))
    await bus.publish(Event(type=EventType.TOOL_RESULT, run_id='test_navigate', step_id='s1',
                           data={'id': 'c1', 'name': 'browser_navigate', 'status': 'success', 
                                 'output': 'navigated', 'duration_ms': 50}))
    await bus.publish(Event(type=EventType.BROWSER_ACTION, run_id='test_navigate', step_id='s1',
                           data={'action': 'goto', 'url': 'https://example.org', 'selector': None, 'x': None, 'y': None, 'step': 's1'}))
    
    received = []
    async with aclosing(replay_and_stream(bus)) as stream:
        async for event in stream:
            if event.type == EventType.BROWSER_ACTION:
                received.append(event.data)
                break
    
    assert len(received) == 1, f"Expected 1 browser_action, got {len(received)}"
    assert received[0]['action'] == 'goto'
    assert received[0]['url'] == 'https://example.org'


async def test_browser_action_after_click():
    """browser_action emitted with action='click' after browser_click."""
    bus = EventBus(run_id='test_click')
    
    await bus.publish(Event(type=EventType.RUN_STARTED, run_id='test_click',
                           data={'task': 'click', 'mode': 'react', 'max_steps': 5}))
    await bus.publish(Event(type=EventType.TOOL_CALL, run_id='test_click',
                           data={'id': 'c1', 'name': 'browser_click', 'args': {'selector': '#submit'}}))
    await bus.publish(Event(type=EventType.TOOL_RESULT, run_id='test_click', step_id='s1',
                           data={'id': 'c1', 'name': 'browser_click', 'status': 'success', 
                                 'output': 'clicked', 'duration_ms': 50}))
    await bus.publish(Event(type=EventType.BROWSER_ACTION, run_id='test_click', step_id='s1',
                           data={'action': 'click', 'url': 'https://example.org', 'selector': '#submit', 'x': None, 'y': None, 'step': 's1'}))
    
    received = []
    async with aclosing(replay_and_stream(bus)) as stream:
        async for event in stream:
            if event.type == EventType.BROWSER_ACTION:
                received.append(event.data)
                break
    
    assert len(received) == 1, f"Expected 1 browser_action, got {len(received)}"
    assert received[0]['action'] == 'click'


async def test_run_finished_answer_react():
    """run_finished in react mode includes answer field."""
    bus = EventBus(run_id='test_answer_react')
    
    await bus.publish(Event(type=EventType.RUN_STARTED, run_id='test_answer_react',
                           data={'task': 'test', 'mode': 'react', 'max_steps': 5}))
    await bus.publish(Event(type=EventType.TOOL_CALL, run_id='test_answer_react',
                           data={'id': 'c1', 'name': 'browser_navigate', 'args': {'url': 'https://example.org'}}))
    await bus.publish(Event(type=EventType.TOOL_RESULT, run_id='test_answer_react', step_id='s1',
                           data={'id': 'c1', 'name': 'browser_navigate', 'status': 'success', 
                                 'output': 'navigated', 'duration_ms': 50}))
    await bus.publish(Event(type=EventType.RUN_FINISHED, run_id='test_answer_react',
                           data={'status': 'completed', 'answer': 'Test answer', 'reason': 'done', 
                                 'steps': 1, 'tokens': 10, 'budget': {'steps': 1, 'steps_left': 9, 'elapsed_s': 0.0, 'tokens': 10, 'consecutive_errors': 0, 'most_repeated': None}}))
    
    received = []
    async with aclosing(replay_and_stream(bus)) as stream:
        async for event in stream:
            received.append(event)
            if event.type == EventType.RUN_FINISHED:
                break
    
    finished = [e for e in received if e.type == EventType.RUN_FINISHED]
    assert len(finished) == 1
    assert 'answer' in finished[0].data, "answer field missing"
    assert finished[0].data['answer'] == 'Test answer'


pytestmark = pytest.mark.asyncio


async def test_async_setup():
    await test_browser_action_after_navigate()
    await test_browser_action_after_click()
    await test_run_finished_answer_react()


def run_tests():
    asyncio.run(test_async_setup())


if __name__ == '__main__':
    run_tests()
    print('All tests passed!')

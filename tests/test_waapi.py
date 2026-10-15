"""WaAPI integration foundations: secret isolation, response semantics, durable outbox."""

from __future__ import annotations

import asyncio
import json
import sqlite3

import httpx
import pytest
from os_server.waapi import (
    WAAPI_API_BASE_URL,
    WaapiClient,
    WaapiConfig,
    WaapiDisabled,
    WaapiNotConfigured,
    WaapiRequestError,
    WaapiService,
    WaapiWebhookRateLimiter,
)
from os_server.waapi_inbox import InboxIdempotencyConflict, InboxStatus, SQLiteWaapiInbox
from os_server.waapi_outbox import (
    ActionResult,
    DeliveryOutcome,
    IdempotencyConflict,
    InvalidOutboxTransition,
    OutboxStatus,
    SQLiteWaapiOutbox,
    WaapiOutboxWorker,
)

from conftest import wait_until

fastapi_testclient = pytest.importorskip("fastapi.testclient")
TestClient = fastapi_testclient.TestClient

from os_server.app import create_app  # noqa: E402
from os_server.demo import build_demo_platform_for  # noqa: E402

SECRET = "waapi-test-secret-do-not-leak"


def test_waapi_env_defaults_closed_and_hides_secrets() -> None:
    config = WaapiConfig.from_env(
        {
            "WAAPI_BEARER_TOKEN": SECRET,
            "WAAPI_INSTANCE_ID": "instance-1",
            "WAAPI_NOTIFY_CHAT_ID": "12345@c.us",
        }
    )

    assert config.enabled is False
    assert config.api_configured is False
    assert config.safe_status()["enabled"] is False
    assert SECRET not in repr(config)
    assert SECRET not in json.dumps(config.safe_status())


def test_waapi_config_requires_explicit_enable_and_reads_outbox_path() -> None:
    config = WaapiConfig.from_env(
        {
            "CELA_WAAPI_ENABLE": "1",
            "WAAPI_BEARER_TOKEN": f" {SECRET} ",
            "WAAPI_INSTANCE_ID": "42",
            "WAAPI_NOTIFY_CHAT_ID": "15551234567@c.us",
            "CELA_WAAPI_OUTBOX_PATH": "/tmp/waapi-test.db",
        }
    )

    assert config.enabled is True
    assert config.api_configured is True
    assert config.message_delivery_configured is True
    assert config.bearer_token == SECRET
    assert config.outbox_path == "/tmp/waapi-test.db"


def test_webhook_auth_configuration_requires_a_strong_path_secret() -> None:
    weak = WaapiConfig(
        enabled=True,
        bearer_token=SECRET,
        instance_id="42",
        webhook_path_secret="too-short",
    )
    partial_header = WaapiConfig(
        enabled=True,
        bearer_token=SECRET,
        instance_id="42",
        webhook_path_secret="S" * 43,
        webhook_header_name="X-WaAPI-Webhook",
    )
    valid = WaapiConfig(
        enabled=True,
        bearer_token=SECRET,
        instance_id="42",
        webhook_path_secret="S" * 43,
        webhook_header_name="X-WaAPI-Webhook",
        webhook_header_value="H" * 43,
    )

    assert weak.inbound_webhook_configured is False
    assert partial_header.inbound_webhook_configured is False
    assert valid.inbound_webhook_configured is True
    assert "S" * 43 not in json.dumps(valid.safe_status())
    assert "H" * 43 not in repr(valid)


def test_webhook_rate_limiter_caps_per_client_burst() -> None:
    limiter = WaapiWebhookRateLimiter(limit=2, window=60.0)
    assert limiter.allow("192.0.2.1") is True
    assert limiter.allow("192.0.2.1") is True
    assert limiter.allow("192.0.2.1") is False
    assert limiter.allow("192.0.2.2") is True


async def test_list_instances_uses_bearer_and_returns_only_allowlisted_fields() -> None:
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "status": "success",
                "instances": [
                    {
                        "id": 71,
                        "name": "Business sender",
                        "owner": "private@example.com",
                        "webhook_url": "https://private.example/webhook",
                        "webhook_events": ["message"],
                        "is_trial": True,
                    }
                ],
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    api = WaapiClient(WaapiConfig(enabled=True, bearer_token=SECRET), http_client=http)
    try:
        instances = await api.list_instances(filter="Business")
    finally:
        await http.aclose()

    assert len(seen) == 1
    assert str(seen[0].url) == f"{WAAPI_API_BASE_URL}/instances?filter=Business"
    assert seen[0].headers["authorization"] == f"Bearer {SECRET}"
    assert instances[0].model_dump() == {
        "id": 71,
        "name": "Business sender",
        "is_trial": True,
    }
    assert "private@example.com" not in json.dumps(instances[0].model_dump())
    assert "private.example" not in json.dumps(instances[0].model_dump())


async def test_list_instances_fails_closed_when_disabled_or_missing_token() -> None:
    disabled = WaapiClient(WaapiConfig(enabled=False, bearer_token=SECRET))
    with pytest.raises(WaapiDisabled):
        await disabled.list_instances()

    missing = WaapiClient(WaapiConfig(enabled=True))
    with pytest.raises(WaapiNotConfigured, match="WAAPI_BEARER_TOKEN"):
        await missing.list_instances()


async def test_list_instances_does_not_echo_provider_body_or_token_in_errors() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": SECRET})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    api = WaapiClient(WaapiConfig(enabled=True, bearer_token=SECRET), http_client=http)
    try:
        with pytest.raises(WaapiRequestError) as raised:
            await api.list_instances()
    finally:
        await http.aclose()

    assert SECRET not in str(raised.value)
    assert "HTTP 401" in str(raised.value)


async def test_action_uses_nested_data_status_as_authoritative() -> None:
    bodies = [
        {"status": "error", "data": {"status": "success"}},
        {"status": "success", "data": {"status": "error", "message": SECRET}},
        {"status": "success", "data": {"data": {"id": "x"}}},
    ]
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=bodies[len(seen) - 1])

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    api = WaapiClient(
        WaapiConfig(enabled=True, bearer_token=SECRET, instance_id="instance-1"),
        http_client=http,
    )
    try:
        accepted = await api.call_action("send-message", {"example": "payload"})
        rejected = await api.call_action("send-message", {"example": "payload"})
        unknown = await api.call_action("send-message", {"example": "payload"})
    finally:
        await http.aclose()

    assert accepted.outcome is DeliveryOutcome.ACCEPTED
    assert rejected.outcome is DeliveryOutcome.REJECTED
    assert rejected.error_code == "provider_rejected"
    assert unknown.outcome is DeliveryOutcome.UNKNOWN
    assert all(
        request.url.path == "/api/v1/instances/instance-1/client/action/send-message"
        for request in seen
    )
    assert all(request.headers["authorization"] == f"Bearer {SECRET}" for request in seen)
    assert json.loads(seen[0].content) == {"example": "payload"}
    assert SECRET not in repr(rejected)


async def test_send_text_message_uses_documented_payload() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path.endswith("/client/action/send-message")
        assert json.loads(request.content) == {
            "chatId": "201126212452@c.us",
            "message": "Hello from CELIA",
        }
        return httpx.Response(200, json={"status": "success", "data": {"status": "success"}})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    api = WaapiClient(
        WaapiConfig(enabled=True, bearer_token=SECRET, instance_id="42"), http_client=http
    )
    try:
        result = await api.send_text_message(
            chat_id="201126212452@c.us", message="Hello from CELIA"
        )
    finally:
        await http.aclose()
    assert result.outcome is DeliveryOutcome.ACCEPTED


async def test_action_transport_failure_is_unknown_not_retryable() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("request timed out with " + SECRET)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    api = WaapiClient(
        WaapiConfig(enabled=True, bearer_token=SECRET, instance_id="instance-1"),
        http_client=http,
    )
    try:
        result = await api.call_action("send-message", {"body": "hello"})
    finally:
        await http.aclose()

    assert result.outcome is DeliveryOutcome.UNKNOWN
    assert result.error_code == "transport_outcome_unknown"
    assert SECRET not in repr(result)


def test_sqlite_outbox_is_durable_idempotent_and_stores_no_bearer_token(tmp_path) -> None:
    path = tmp_path / "waapi-outbox.sqlite3"
    outbox = SQLiteWaapiOutbox(path)
    first, inserted = outbox.enqueue(
        instance_id="42",
        idempotency_key="event-123",
        chat_id="15551234567@c.us",
        text="hello from the agent",
    )
    duplicate, inserted_again = outbox.enqueue(
        instance_id="42",
        idempotency_key="event-123",
        chat_id="15551234567@c.us",
        text="hello from the agent",
    )
    assert inserted is True
    assert inserted_again is False
    assert duplicate.message_id == first.message_id
    assert outbox.pending_count() == 1
    outbox.close()

    reopened = SQLiteWaapiOutbox(path)
    persisted = reopened.get(first.message_id)
    assert persisted is not None
    assert persisted.status is OutboxStatus.QUEUED
    assert persisted.text == "hello from the agent"
    with sqlite3.connect(path) as db:
        blob = " ".join(
            str(item) for row in db.execute("SELECT * FROM waapi_outbox") for item in row
        )
    assert SECRET not in blob
    reopened.close()


def test_outbox_preserves_multiline_message_text(tmp_path) -> None:
    outbox = SQLiteWaapiOutbox(tmp_path / "waapi-outbox.sqlite3")
    body = "  first line\nsecond line  "
    queued, _ = outbox.enqueue(
        instance_id="42", idempotency_key="multiline", chat_id="1@c.us", text=body
    )
    assert queued.text == body
    outbox.close()


def test_sqlite_inbox_persists_and_deduplicates_original_message_id(tmp_path) -> None:
    path = tmp_path / "waapi.sqlite3"
    inbox = SQLiteWaapiInbox(path)
    first, inserted = inbox.enqueue(
        instance_id="42",
        provider_message_id="wamid.original-1",
        chat_id="201126212452@c.us",
        text="please help",
        timestamp=1_760_000_000,
    )
    duplicate, inserted_again = inbox.enqueue(
        instance_id="42",
        provider_message_id="wamid.original-1",
        chat_id="201126212452@c.us",
        text="please help",
        timestamp=None,
    )
    assert inserted is True
    assert inserted_again is False
    assert first.inbox_id == duplicate.inbox_id
    with pytest.raises(InboxIdempotencyConflict):
        inbox.enqueue(
            instance_id="42",
            provider_message_id="wamid.original-1",
            chat_id="201126212452@c.us",
            text="different body",
            timestamp=1_760_000_000,
        )
    inbox.close()

    reopened = SQLiteWaapiInbox(path)
    claimed = reopened.claim_next()
    assert claimed is not None
    assert claimed.status is InboxStatus.PROCESSING
    assert claimed.attempts == 1
    assert claimed.text == "please help"
    assert "please help" not in json.dumps(claimed.public_status())
    reopened.mark_run_started(claimed.inbox_id, "run_123")
    reopened.mark_reply_queued(claimed.inbox_id, "wam_reply_1")
    finished = reopened.get(claimed.inbox_id)
    assert finished is not None and finished.status is InboxStatus.REPLY_QUEUED
    assert finished.run_id == "run_123"
    assert finished.reply_message_id == "wam_reply_1"
    reopened.close()


def test_sqlite_inbox_marks_crashed_processing_unknown_without_replay(tmp_path) -> None:
    inbox = SQLiteWaapiInbox(tmp_path / "waapi.sqlite3")
    item, _ = inbox.enqueue(
        instance_id="42",
        provider_message_id="wamid.original-2",
        chat_id="201126212452@c.us",
        text="question",
        timestamp=None,
    )
    assert inbox.claim_next() is not None
    assert inbox.mark_stale_processing_unknown(older_than_seconds=0) == 1
    unknown = inbox.get(item.inbox_id)
    assert unknown is not None and unknown.status is InboxStatus.UNKNOWN
    assert inbox.claim_next() is None
    inbox.close()


def test_outbox_rejects_reused_idempotency_key_with_different_content(tmp_path) -> None:
    outbox = SQLiteWaapiOutbox(tmp_path / "waapi-outbox.sqlite3")
    outbox.enqueue(instance_id="42", idempotency_key="same-key", chat_id="1@c.us", text="first")
    with pytest.raises(IdempotencyConflict):
        outbox.enqueue(
            instance_id="42", idempotency_key="same-key", chat_id="1@c.us", text="different"
        )
    assert outbox.pending_count() == 1
    outbox.close()


def test_outbox_claim_and_terminal_transition_are_single_use(tmp_path) -> None:
    outbox = SQLiteWaapiOutbox(tmp_path / "waapi-outbox.sqlite3")
    queued, _ = outbox.enqueue(
        instance_id="42", idempotency_key="k1", chat_id="1@c.us", text="hello"
    )

    claimed = outbox.claim_next()
    assert claimed is not None and claimed.message_id == queued.message_id
    assert claimed.status is OutboxStatus.SENDING
    assert claimed.attempts == 1
    assert outbox.claim_next() is None

    outbox.mark_sent(queued.message_id, provider_message_id="msg-1")
    sent = outbox.get(queued.message_id)
    assert sent is not None and sent.status is OutboxStatus.SENT
    assert sent.provider_message_id == "msg-1"
    with pytest.raises(InvalidOutboxTransition):
        outbox.mark_sent(queued.message_id)
    outbox.close()


def test_outbox_marks_crashed_in_flight_delivery_unknown_without_retry(tmp_path) -> None:
    outbox = SQLiteWaapiOutbox(tmp_path / "waapi-outbox.sqlite3")
    message, _ = outbox.enqueue(
        instance_id="42", idempotency_key="k1", chat_id="1@c.us", text="hello"
    )
    assert outbox.claim_next() is not None

    assert outbox.mark_stale_sending_unknown(older_than_seconds=0) == 1
    unresolved = outbox.get(message.message_id)
    assert unresolved is not None
    assert unresolved.status is OutboxStatus.UNKNOWN
    assert unresolved.error_code == "worker_lease_expired"
    assert outbox.claim_next() is None
    outbox.close()


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (ActionResult(DeliveryOutcome.ACCEPTED), OutboxStatus.SENT),
        (ActionResult(DeliveryOutcome.REJECTED), OutboxStatus.REJECTED),
        (ActionResult(DeliveryOutcome.UNKNOWN), OutboxStatus.UNKNOWN),
    ],
)
async def test_worker_persists_provider_outcome(tmp_path, result, expected) -> None:
    class Sender:
        async def send_message(self, _message):
            return result

    outbox = SQLiteWaapiOutbox(tmp_path / "waapi-outbox.sqlite3")
    message, _ = outbox.enqueue(
        instance_id="42", idempotency_key="k1", chat_id="1@c.us", text="hello"
    )

    finished = await WaapiOutboxWorker(outbox, Sender()).deliver_one()
    assert finished is not None and finished.message_id == message.message_id
    assert finished.status is expected
    assert outbox.pending_count() == 0
    outbox.close()


async def test_worker_does_not_persist_exception_text_or_retry_unknown(tmp_path) -> None:
    class Sender:
        async def send_message(self, _message):
            raise RuntimeError(f"network failure with token {SECRET}")

    outbox = SQLiteWaapiOutbox(tmp_path / "waapi-outbox.sqlite3")
    _message, _ = outbox.enqueue(
        instance_id="42", idempotency_key="k1", chat_id="1@c.us", text="hello"
    )

    finished = await WaapiOutboxWorker(outbox, Sender()).deliver_one()
    assert finished is not None and finished.status is OutboxStatus.UNKNOWN
    assert finished.error_code == "delivery_outcome_unknown"
    assert SECRET not in repr(finished)
    assert outbox.claim_next() is None
    outbox.close()


async def test_service_status_and_queue_never_include_secret(tmp_path) -> None:
    config = WaapiConfig(
        enabled=True,
        bearer_token=SECRET,
        instance_id="42",
        notify_chat_id="1@c.us",
        outbox_path=str(tmp_path / "outbox.db"),
    )
    service = WaapiService(config)
    try:
        status = service.status()
        assert status["enabled"] is True
        assert status["api_configured"] is True
        assert status["outbox_available"] is True
        assert status["inbox_available"] is True
        assert status["message_delivery_enabled"] is True
        assert status["inbound_webhook_enabled"] is False
        assert SECRET not in json.dumps(status)

        queued, inserted = service.enqueue_notification(text="hello", idempotency_key="event-1")
        assert inserted is True
        assert queued.status is OutboxStatus.QUEUED
        assert SECRET not in json.dumps(queued.public_status())
    finally:
        await service.aclose()


def test_waapi_server_routes_require_auth_and_never_expose_secrets(tmp_path) -> None:
    sent_payloads: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {SECRET}"
        if request.method == "POST":
            sent_payloads.append(json.loads(request.content))
            return httpx.Response(200, json={"status": "success", "data": {"status": "success"}})
        return httpx.Response(
            200,
            json={
                "status": "success",
                "instances": [
                    {
                        "id": 42,
                        "name": "main",
                        "owner": "private@example.com",
                        "webhook_url": "https://internal.example/hook",
                        "is_trial": False,
                    }
                ],
            },
        )

    config = WaapiConfig(
        enabled=True,
        bearer_token=SECRET,
        instance_id="42",
        notify_chat_id="201126212452@c.us",
        outbox_path=str(tmp_path / "waapi.db"),
    )
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = WaapiService(config, client=WaapiClient(config, http_client=http))
    factory = lambda mode, max_steps: build_demo_platform_for(mode)  # noqa: E731
    app = create_app(factory, api_token="platform-token", waapi_service=service)
    try:
        with TestClient(app) as client:
            assert client.get("/api/waapi/status").status_code == 401
            headers = {"Authorization": "Bearer platform-token"}
            status = client.get("/api/waapi/status", headers=headers)
            instances = client.get("/api/waapi/instances", headers=headers)
            queued = client.post(
                "/api/waapi/notifications",
                headers=headers,
                json={"idempotency_key": "notify-test-1", "text": "Queue a test alert"},
            )
            duplicate = client.post(
                "/api/waapi/notifications",
                headers=headers,
                json={"idempotency_key": "notify-test-1", "text": "Queue a test alert"},
            )

            def delivered_record():
                item = client.app.state.waapi.outbox.get(queued.json()["message_id"])
                return item if item is not None and item.status is OutboxStatus.SENT else None

            delivered = wait_until(delivered_record, what="WaAPI notification delivery")

        assert status.status_code == 200
        assert status.json()["inbound_webhook_enabled"] is False
        assert status.json()["message_delivery_enabled"] is True
        assert instances.status_code == 200
        assert instances.json()["instances"] == [{"id": 42, "name": "main", "is_trial": False}]
        assert queued.status_code == 202 and queued.json()["result"] == "queued"
        assert duplicate.status_code == 202 and duplicate.json()["result"] == "duplicate"
        assert delivered.status is OutboxStatus.SENT
        assert sent_payloads == [{"chatId": "201126212452@c.us", "message": "Queue a test alert"}]
        combined = status.text + instances.text + queued.text + duplicate.text
        assert SECRET not in combined
        assert "private@example.com" not in combined
        assert "internal.example" not in combined
    finally:
        asyncio.run(http.aclose())


def test_webhook_auth_dedup_and_agent_reply_flow(tmp_path) -> None:
    path_secret = "A" * 43
    header_secret = "H" * 43
    sent_payloads: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        sent_payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "success", "data": {"status": "success"}})

    config = WaapiConfig(
        enabled=True,
        bearer_token=SECRET,
        instance_id="42",
        webhook_path_secret=path_secret,
        webhook_header_name="X-WaAPI-Webhook",
        webhook_header_value=header_secret,
        outbox_path=str(tmp_path / "waapi.sqlite3"),
    )
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = WaapiService(config, client=WaapiClient(config, http_client=http))
    factory = lambda mode, max_steps: build_demo_platform_for(mode)  # noqa: E731
    app = create_app(factory, api_token="platform-token", waapi_service=service)
    event = {
        "event": "message",
        "instanceId": "42",
        "data": {
            "message": {
                "id": {"_serialized": "wamid.original-abc"},
                "type": "chat",
                "timestamp": 1_760_000_000,
                "from": "201126212452@c.us",
                "body": "What is the task status?",
                "fromMe": False,
            }
        },
    }
    try:
        with TestClient(app) as client:
            base = "/api/webhooks/waapi"
            wrong_secret = client.post(f"{base}/{'B' * 43}", json=event)
            missing_header = client.post(f"{base}/{path_secret}", json=event)
            wrong_instance_event = {**event, "instanceId": "other-instance"}
            wrong_instance = client.post(
                f"{base}/{path_secret}",
                json=wrong_instance_event,
                headers={"X-WaAPI-Webhook": header_secret},
            )
            missing_id_event = json.loads(json.dumps(event))
            del missing_id_event["data"]["message"]["id"]
            missing_id = client.post(
                f"{base}/{path_secret}",
                json=missing_id_event,
                headers={"X-WaAPI-Webhook": header_secret},
            )
            outgoing_event = json.loads(json.dumps(event))
            outgoing_event["data"]["message"]["fromMe"] = True
            outgoing = client.post(
                f"{base}/{path_secret}",
                json=outgoing_event,
                headers={"X-WaAPI-Webhook": header_secret},
            )
            accepted = client.post(
                f"{base}/{path_secret}",
                json=event,
                headers={"X-WaAPI-Webhook": header_secret},
            )
            duplicate = client.post(
                f"{base}/{path_secret}",
                json=event,
                headers={"X-WaAPI-Webhook": header_secret},
            )

            inbox_id = accepted.json()["inbox_id"]

            def reply_sent():
                inbox_item = client.app.state.waapi.inbox.get(inbox_id)
                if inbox_item is None or inbox_item.status is not InboxStatus.REPLY_QUEUED:
                    return None
                reply = client.app.state.waapi.outbox.get(inbox_item.reply_message_id or "")
                return (inbox_item, reply) if reply and reply.status is OutboxStatus.SENT else None

            completed = wait_until(reply_sent, what="WaAPI webhook agent reply")

        assert wrong_secret.status_code == 401
        assert missing_header.status_code == 401
        assert wrong_instance.status_code == 404
        assert missing_id.status_code == 422
        assert outgoing.status_code == 200 and outgoing.json()["status"] == "ignored"
        assert accepted.status_code == 202 and accepted.json()["status"] == "queued"
        assert duplicate.status_code == 200 and duplicate.json()["status"] == "duplicate"
        assert completed[0].attempts == 1
        assert sent_payloads and sent_payloads[0]["chatId"] == "201126212452@c.us"
        assert isinstance(sent_payloads[0]["message"], str)
        assert (
            path_secret
            not in accepted.text + duplicate.text + completed[0].public_status().__str__()
        )
    finally:
        asyncio.run(http.aclose())


def test_disabled_waapi_instances_route_fails_closed() -> None:
    factory = lambda mode, max_steps: build_demo_platform_for(mode)  # noqa: E731
    app = create_app(
        factory,
        api_token="platform-token",
        waapi_service=WaapiService(WaapiConfig()),
    )
    with TestClient(app) as client:
        response = client.get(
            "/api/waapi/instances",
            headers={"Authorization": "Bearer platform-token"},
        )
    assert response.status_code == 503
    assert "WAAPI" in response.text

"""Protocol and fail-closed tests for the external Oracle advisor platform plugin."""

import asyncio
import base64
import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from plugins.platforms.oracle_advisor import adapter as adapter_module
from plugins.platforms.oracle_advisor import protocol


OWNER = "oracle-owner-test"
SUBSCRIPTION_ID = "sub_" + "a" * 40
CURRENT_KEY = bytes(range(32))
PREVIOUS_KEY = bytes(range(32, 64))


def _whsec(key=CURRENT_KEY):
    return "whsec_" + base64.b64encode(key).decode("ascii")


def _event_id(owner, request_id):
    return "advisor:request:" + hashlib.sha256(owner.encode()).hexdigest()[:20] + ":" + request_id


def _signature(body, event_id, timestamp, key=CURRENT_KEY):
    signed = event_id.encode() + b"." + timestamp.encode() + b"." + body
    return "v1," + base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()


def _event(request_id="request-0001", **updates):
    data = {
        "requestId": request_id,
        "originAgent": "agent-1",
        "originSession": "session-1",
        "question": "Review this bounded advice request.",
        "evidenceRefs": [],
    }
    data.update(updates)
    return {
        "eventId": _event_id(OWNER, request_id),
        "name": "advisor.requested",
        "timestamp": "2026-10-19T12:00:00.000Z",
        "data": data,
        "cursor": "1",
    }


def _headers(body, event_id, *, key=CURRENT_KEY, timestamp=None, subscription=SUBSCRIPTION_ID):
    timestamp = str(int(time.time())) if timestamp is None else str(timestamp)
    return {
        "webhook-id": event_id,
        "webhook-timestamp": timestamp,
        "webhook-signature": _signature(body, event_id, timestamp, key),
        "X-MCP-Subscription-Id": subscription,
        "Content-Type": "application/json",
    }


def _make_adapter(monkeypatch, *, owner=OWNER, subscription=SUBSCRIPTION_ID):
    monkeypatch.setattr(
        adapter_module,
        "Platform",
        lambda name: SimpleNamespace(value=name),
    )
    monkeypatch.setattr(
        adapter_module,
        "_read_scoped_secret",
        lambda name: {
            "ORACLE_ADVISOR_WEBHOOK_SECRET": _whsec(),
            "ORACLE_ADVISOR_WEBHOOK_PREVIOUS_SECRET": _whsec(PREVIOUS_KEY),
        }.get(name, ""),
    )
    config = PlatformConfig(
        enabled=True,
        extra={
            "owner": owner,
            "subscription_id": subscription,
            "host": "127.0.0.1",
            "port": 8645,
        },
    )
    adapter = adapter_module.OracleAdvisorAdapter(config)
    adapter.handle_message = AsyncMock()
    return adapter


def _app(adapter):
    app = web.Application()
    app.router.add_post("/oracle-advisor/events", adapter._handle_callback)
    return app


async def _post(client, payload, *, event_id=None, raw_body=None, headers=None):
    if raw_body is None:
        raw_body = json.dumps(payload, separators=(",", ":")).encode()
    event_id = event_id or payload.get("eventId", "msg_verification_" + "b" * 36)
    request_headers = _headers(raw_body, event_id)
    if headers:
        request_headers.update(headers)
    return await client.post("/oracle-advisor/events", data=raw_body, headers=request_headers)


class TestStandardWebhooks:
    def test_raw_bytes_and_valid_secret(self):
        body, event_id, timestamp = b'{ "raw" : true }', "event-1", "1000"
        headers = {
            "webhook-id": event_id,
            "webhook-timestamp": timestamp,
            "webhook-signature": _signature(body, event_id, timestamp),
        }
        assert protocol.verify_standard_webhook(headers, body, [_whsec()], now=1000)
        assert not protocol.verify_standard_webhook(headers, body + b" ", [_whsec()], now=1000)

    @pytest.mark.parametrize("delta", [-300, 300])
    def test_five_minute_timestamp_boundary_is_inclusive(self, delta):
        now = 10_000
        body, event_id, timestamp = b"{}", "event-bound", str(now + delta)
        headers = {
            "webhook-id": event_id,
            "webhook-timestamp": timestamp,
            "webhook-signature": _signature(body, event_id, timestamp),
        }
        assert protocol.verify_standard_webhook(headers, body, [_whsec()], now=now)

    @pytest.mark.parametrize("delta", [-301, 301])
    def test_stale_and_future_timestamps_reject(self, delta):
        now = 10_000
        body, event_id, timestamp = b"{}", "event-stale", str(now + delta)
        headers = {
            "webhook-id": event_id,
            "webhook-timestamp": timestamp,
            "webhook-signature": _signature(body, event_id, timestamp),
        }
        assert not protocol.verify_standard_webhook(headers, body, [_whsec()], now=now)

    def test_current_and_rotated_signatures_are_accepted(self):
        body, event_id, timestamp = b"{}", "event-rotate", "1000"
        headers = {
            "webhook-id": event_id,
            "webhook-timestamp": timestamp,
            "webhook-signature": "v1," + "A" * 44 + " " + _signature(body, event_id, timestamp, PREVIOUS_KEY),
        }
        assert protocol.verify_standard_webhook(
            headers, body, [_whsec(), _whsec(PREVIOUS_KEY)], now=1000
        )

    @pytest.mark.parametrize(
        "secret",
        ["", "INSECURE_NO_AUTH", "whsec_not-base64", "whsec_" + base64.b64encode(b"tiny").decode()],
    )
    def test_bad_secret_rejects(self, secret):
        headers = {"webhook-id": "e", "webhook-timestamp": "1000", "webhook-signature": "v1,x"}
        assert not protocol.verify_standard_webhook(headers, b"{}", [secret], now=1000)


class TestPinnedEnvelope:
    def test_signed_challenge_shape_and_no_agent_dispatch(self):
        challenge = {"type": "verification", "challenge": "A" * 43}
        raw = json.dumps(challenge, separators=(",", ":")).encode()
        event_id = "msg_verification_" + "c" * 36
        assert protocol.parse_callback(
            protocol.parse_json_body(raw), header_event_id=event_id, owner=OWNER
        ) == protocol.VerificationChallenge("A" * 43)

    def test_valid_request_preserves_work_and_correlation_identity(self):
        payload = _event(workId="work-123")
        parsed = protocol.parse_callback(payload, header_event_id=payload["eventId"], owner=OWNER)
        assert isinstance(parsed, protocol.AdvisorRequest)
        assert parsed.request_id == parsed.correlation_id == "request-0001"
        assert parsed.work_id == "work-123"

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda value: value.update(extra=True),
            lambda value: value["data"].update(extra=True),
            lambda value: value["data"].update(question="q" * 2_001),
            lambda value: value["data"].update(evidenceRefs=["r"] * 17),
            lambda value: value.update(name="advisor.answered"),
            lambda value: value["data"].update(originAgent="a" * 161),
            lambda value: value["data"].update(workId="w" * 161),
            lambda value: value["data"].update(evidenceRefs=["token=synthetic"]),
        ],
    )
    def test_invalid_contract_fields_reject(self, mutate):
        payload = _event(workId="work-123")
        mutate(payload)
        with pytest.raises(protocol.CallbackError):
            protocol.parse_callback(payload, header_event_id=payload["eventId"], owner=OWNER)

    def test_upper_schema_bounds_are_retained(self):
        payload = _event(
            originAgent="a" * 160,
            originSession="s" * 160,
            workId="w" * 160,
            question="q" * 2_000,
            evidenceRefs=["r" * 512] * 16,
        )
        parsed = protocol.parse_callback(payload, header_event_id=payload["eventId"], owner=OWNER)
        assert isinstance(parsed, protocol.AdvisorRequest)

    def test_event_header_owner_and_duplicate_json_keys_are_bound(self):
        payload = _event()
        with pytest.raises(protocol.CallbackError, match="does not match"):
            protocol.parse_callback(payload, header_event_id="wrong", owner=OWNER)
        payload["eventId"] = _event_id("other-owner", "request-0001")
        with pytest.raises(protocol.CallbackError) as error:
            protocol.parse_callback(payload, header_event_id=payload["eventId"], owner=OWNER)
        assert error.value.status == 403
        with pytest.raises(protocol.CallbackError, match="duplicate"):
            protocol.parse_json_body(b'{"name":"first","name":"second"}')

    def test_body_limit_is_bounded(self):
        with pytest.raises(protocol.CallbackError) as error:
            protocol.parse_json_body(b" " * (protocol.MAX_BODY_BYTES + 1))
        assert error.value.status == 413
        assert protocol.MAX_BODY_BYTES == 256 * 1024


class TestPluginAdapter:
    @pytest.mark.asyncio
    async def test_challenge_echoes_and_valid_request_fails_closed(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        app = _app(adapter)
        async with TestClient(TestServer(app)) as client:
            challenge = {"type": "verification", "challenge": "A" * 43}
            raw = json.dumps(challenge, separators=(",", ":")).encode()
            challenge_id = "msg_verification_" + "d" * 36
            response = await client.post(
                "/oracle-advisor/events", data=raw, headers=_headers(raw, challenge_id)
            )
            assert response.status == 200
            assert await response.json() == {"challenge": "A" * 43}

            event = _event(workId="work-123")
            response = await _post(client, event)
            assert response.status == 501
            assert await response.json() == {
                "status": "unsupported",
                "stage": "received",
                "dispatch": "unsupported",
                "result_submission": "unsupported",
            }
        adapter.handle_message.assert_not_awaited()
        assert not hasattr(adapter, "_seen_deliveries")
        send_result = await adapter.send("request-0001", "answer")
        assert not send_result.success
        assert "unsupported" in send_result.error.lower()

    @pytest.mark.asyncio
    async def test_signature_and_subscription_binding_fail_closed(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        challenge = {"type": "verification", "challenge": "A" * 43}
        raw = json.dumps(challenge).encode()
        event_id = "msg_verification_" + "c" * 36
        app = _app(adapter)
        async with TestClient(TestServer(app)) as client:
            invalid = _headers(raw, event_id)
            invalid["webhook-signature"] = "v1,invalid"
            response = await client.post("/oracle-advisor/events", data=raw, headers=invalid)
            assert response.status == 401

            response = await client.post(
                "/oracle-advisor/events",
                data=raw,
                headers=_headers(raw, event_id, subscription="sub_" + "b" * 40),
            )
            assert response.status == 403
        adapter.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_duplicate_conflict_concurrency_and_restart_never_claim(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        app = _app(adapter)
        async with TestClient(TestServer(app)) as client:
            first = _event("request-0001")
            conflict = _event("request-0001", question="changed request")
            responses = await asyncio.gather(
                _post(client, first), _post(client, first), _post(client, conflict)
            )
            assert [response.status for response in responses] == [501, 501, 501]
        adapter.handle_message.assert_not_awaited()

        restarted = _make_adapter(monkeypatch)
        app = _app(restarted)
        async with TestClient(TestServer(app)) as client:
            assert (await _post(client, first)).status == 501
        restarted.handle_message.assert_not_awaited()

    def test_plugin_registration_uses_platform_extension_seam(self):
        from plugins.platforms.oracle_advisor import register

        calls = []

        class Context:
            def register_platform(self, **kwargs):
                calls.append(kwargs)

        register(Context())
        assert len(calls) == 1
        assert calls[0]["name"] == "oracle_advisor"
        assert calls[0]["trusted_inbound"] is False

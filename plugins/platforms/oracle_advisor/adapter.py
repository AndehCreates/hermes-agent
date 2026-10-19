"""Hermes platform plugin for the Oracle advisor callback contract.

The adapter terminates the signed transport and validates the event. It does
not dispatch, claim Work, enqueue advice, or submit a result because this
source slice has no durable, authoritative binding for those operations.
"""

from __future__ import annotations

import contextlib
import logging
import re
from typing import Any, Dict, Optional

from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, SendResult

from .protocol import (
    MAX_BODY_BYTES,
    AdvisorRequest,
    CallbackError,
    VerificationChallenge,
    decode_secret,
    parse_callback,
    parse_json_body,
    verify_standard_webhook,
)

logger = logging.getLogger(__name__)
_SUBSCRIPTION_ID = re.compile(r"sub_[a-f0-9]{40}")
_OWNER_CONTROLS = re.compile(r"[\x00-\x1f\x7f]")


def _read_scoped_secret(name: str) -> str:
    """Read secrets through Hermes' profile-aware API; never read os.environ."""
    try:
        from gateway.platforms._shared import get_scoped_secret
    except ImportError:
        # The pinned Hermes fork predates the plugin platform seam and this
        # module. Fail closed until it adopts the upstream registry API.
        return ""
    value = get_scoped_secret(name)
    return value if isinstance(value, str) else ""


def _route_config(config: Any) -> dict[str, Any]:
    extra = getattr(config, "extra", {}) or {}
    return extra if isinstance(extra, dict) else {}


def _valid_owner(owner: Any) -> bool:
    if not isinstance(owner, str) or owner != owner.strip() or _OWNER_CONTROLS.search(owner):
        return False
    try:
        length = len(owner.encode("utf-16-le")) // 2
    except UnicodeEncodeError:
        return False
    return 1 <= length <= 512


def validate_config(config: Any) -> bool:
    """Require explicit binding and a scoped, canonical Standard Webhooks key."""
    extra = _route_config(config)
    host = extra.get("host")
    port = extra.get("port")
    owner = extra.get("owner")
    subscription_id = extra.get("subscription_id")
    body_limit = extra.get("max_body_bytes", MAX_BODY_BYTES)
    secret = _read_scoped_secret("ORACLE_ADVISOR_WEBHOOK_SECRET")
    previous_secret = _read_scoped_secret("ORACLE_ADVISOR_WEBHOOK_PREVIOUS_SECRET")
    return (
        isinstance(host, str)
        and bool(host.strip())
        and isinstance(port, int)
        and not isinstance(port, bool)
        and 1 <= port <= 65_535
        and isinstance(body_limit, int)
        and not isinstance(body_limit, bool)
        and 1 <= body_limit <= MAX_BODY_BYTES
        and _valid_owner(owner)
        and isinstance(subscription_id, str)
        and _SUBSCRIPTION_ID.fullmatch(subscription_id) is not None
        and decode_secret(secret) is not None
        and (
            not previous_secret
            or decode_secret(previous_secret) is not None
        )
    )


class OracleAdvisorAdapter(BasePlatformAdapter):
    """Opt-in single-binding Standard Webhooks server."""

    def __init__(self, config):
        super().__init__(config=config, platform=Platform("oracle_advisor"))
        self._extra = _route_config(config)
        self._owner = self._extra.get("owner")
        self._subscription_id = self._extra.get("subscription_id")
        self._current_secret = _read_scoped_secret("ORACLE_ADVISOR_WEBHOOK_SECRET")
        self._previous_secret = _read_scoped_secret(
            "ORACLE_ADVISOR_WEBHOOK_PREVIOUS_SECRET"
        )
        self._host = self._extra.get("host")
        self._port = self._extra.get("port")
        self._max_body_bytes = self._extra.get("max_body_bytes", MAX_BODY_BYTES)
        self._runner = None

    async def connect(self) -> bool:
        if not validate_config(self.config):
            self._set_fatal_error(
                "invalid_config",
                "Oracle advisor requires explicit owner, subscription, host, port, and scoped webhook secret",
                retryable=False,
            )
            return False
        from aiohttp import web

        app = web.Application(client_max_size=self._max_body_bytes)
        app.router.add_post("/oracle-advisor/events", self._handle_callback)
        self._runner = web.AppRunner(app)
        try:
            await self._runner.setup()
            site = web.TCPSite(self._runner, self._host, self._port)
            await site.start()
        except Exception as exc:
            if self._runner is not None:
                with contextlib.suppress(Exception):
                    await self._runner.cleanup()
            self._runner = None
            self._set_fatal_error("bind_failed", f"Oracle advisor bind failed: {exc}", retryable=isinstance(exc, OSError))
            return False
        self._mark_connected()
        logger.info("Oracle advisor callback adapter listening on %s:%s", self._host, self._port)
        return True

    async def disconnect(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        self._mark_disconnected()

    async def _handle_callback(self, request):
        from aiohttp import web

        if request.content_length is not None and request.content_length > self._max_body_bytes:
            return web.json_response({"error": "Payload too large"}, status=413)
        try:
            raw_body = await request.read()
            if len(raw_body) > self._max_body_bytes:
                return web.json_response({"error": "Payload too large"}, status=413)
        except Exception:
            return web.json_response({"error": "Bad request"}, status=400)

        secrets = [self._current_secret]
        if self._previous_secret:
            secrets.append(self._previous_secret)
        if not verify_standard_webhook(request.headers, raw_body, secrets):
            return web.json_response({"error": "Invalid signature"}, status=401)

        try:
            payload = parse_json_body(raw_body)
            supplied_subscription = request.headers.get("X-MCP-Subscription-Id", "")
            if supplied_subscription != self._subscription_id:
                raise CallbackError("subscription binding mismatch", 403)
            callback = parse_callback(
                payload,
                header_event_id=request.headers.get("webhook-id", ""),
                owner=self._owner,
            )
        except CallbackError as exc:
            return web.json_response({"error": str(exc)}, status=exc.status)

        if isinstance(callback, VerificationChallenge):
            return web.json_response({"challenge": callback.challenge}, status=200)

        assert isinstance(callback, AdvisorRequest)
        # Keep the producer's request/correlation and optional Work identity
        # intact for a future durable bridge. No generic message dispatch.
        logger.info(
            "Validated Oracle advisor request %s for bound owner; dispatch unavailable",
            callback.request_id,
        )
        return web.json_response(
            {
                "status": "unsupported",
                "stage": "received",
                "dispatch": "unsupported",
                "result_submission": "unsupported",
            },
            status=501,
        )

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        return SendResult(success=False, error="Oracle advisor result submission is unsupported")

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": "Oracle advisor callback", "type": "service", "chat_id": chat_id}

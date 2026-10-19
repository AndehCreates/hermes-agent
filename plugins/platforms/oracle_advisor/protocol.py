"""Pinned Oracle advisor webhook protocol validation.

This module contains no Hermes dispatch, Work state, queue, or result writer.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence

EVENT_NAME = "advisor.requested"
MAX_TIMESTAMP_SKEW_SECONDS = 300
MAX_SAFE_INTEGER = 9_007_199_254_740_991
# Keep the receiver bound equal to the pinned producer's callback ceiling.
MAX_BODY_BYTES = 256 * 1024

_ALL_CONTROLS = re.compile(r"[\x00-\x1f\x7f]")
_QUESTION_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_CREDENTIAL_REFERENCE = re.compile(
    r"(?:bearer\s+[a-z0-9._~+/-]+=*|(?:api[_-]?key|token|secret|password)\s*[:=])",
    re.IGNORECASE,
)


class CallbackError(ValueError):
    """A bounded validation error safe to return to the webhook producer."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class VerificationChallenge:
    challenge: str


@dataclass(frozen=True)
class AdvisorRequest:
    """Validated wire fields retained without inventing profile or effect IDs."""

    event_id: str
    request_id: str
    correlation_id: str
    origin_agent: str
    origin_session: str
    work_id: str | None
    question: str
    evidence_refs: tuple[str, ...]
    occurred_at: str
    cursor: str


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise CallbackError("duplicate JSON object key")
        obj[key] = value
    return obj


def _reject_constant(value: str) -> None:
    raise CallbackError(f"invalid JSON constant: {value}")


def parse_json_body(raw_body: bytes) -> Any:
    if len(raw_body) > MAX_BODY_BYTES:
        raise CallbackError("payload too large", 413)
    try:
        return json.loads(
            raw_body.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except CallbackError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CallbackError("cannot parse Oracle advisor body") from exc


def decode_secret(secret: Any) -> bytes | None:
    """Decode the producer's canonical ``whsec_`` base64 key format."""
    if not isinstance(secret, str) or not secret.startswith("whsec_"):
        return None
    encoded = secret[len("whsec_") :]
    if not re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", encoded):
        return None
    try:
        key = base64.b64decode(encoded, validate=True)
    except (ValueError, base64.binascii.Error):
        return None
    if not 24 <= len(key) <= 64 or base64.b64encode(key).decode("ascii") != encoded:
        return None
    return key


def verify_standard_webhook(
    headers: Mapping[str, str],
    raw_body: bytes,
    secrets: Sequence[str],
    *,
    now: float | None = None,
) -> bool:
    """Verify ``webhook-id.timestamp.rawBody`` using current/overlap secrets."""
    if not isinstance(secrets, (list, tuple)) or not 1 <= len(secrets) <= 2:
        return False
    event_id = headers.get("webhook-id", "")
    timestamp_text = headers.get("webhook-timestamp", "")
    signature_header = headers.get("webhook-signature", "")
    if (
        not isinstance(event_id, str)
        or not event_id
        or len(event_id) > 512
        or not re.fullmatch(r"[0-9]{1,12}", timestamp_text)
        or not isinstance(signature_header, str)
        or not signature_header
        or len(signature_header) > 4096
    ):
        return False
    timestamp = int(timestamp_text)
    if abs((time.time() if now is None else now) - timestamp) > MAX_TIMESTAMP_SKEW_SECONDS:
        return False
    keys = [decode_secret(secret) for secret in secrets]
    if any(key is None for key in keys):
        return False

    signed_content = (
        event_id.encode("utf-8")
        + b"."
        + timestamp_text.encode("ascii")
        + b"."
        + raw_body
    )
    candidates = []
    for item in signature_header.split():
        version, separator, encoded = item.partition(",")
        if version == "v1" and separator and re.fullmatch(r"[A-Za-z0-9+/]{43}=", encoded):
            candidates.append(encoded)
    for key in keys:
        expected = base64.b64encode(hmac.new(key, signed_content, hashlib.sha256).digest()).decode("ascii")
        if any(hmac.compare_digest(candidate, expected) for candidate in candidates):
            return True
    return False


def _js_length(value: str) -> int:
    try:
        return len(value.encode("utf-16-le")) // 2
    except UnicodeEncodeError:
        return -1


def _bounded_text(value: Any, minimum: int, maximum: int, controls: re.Pattern) -> bool:
    return (
        isinstance(value, str)
        and value == value.strip()
        and minimum <= _js_length(value) <= maximum
        and not controls.search(value)
    )


def _event_id_for_owner(owner: str, request_id: str) -> str:
    owner_hash = hashlib.sha256(owner.encode("utf-8")).hexdigest()[:20]
    return f"advisor:request:{owner_hash}:{request_id}"


def parse_callback(payload: Any, *, header_event_id: str, owner: str) -> VerificationChallenge | AdvisorRequest:
    if not isinstance(payload, dict):
        raise CallbackError("Oracle advisor payload must be an object")

    if payload.get("type") == "verification":
        if set(payload) != {"type", "challenge"}:
            raise CallbackError("invalid Oracle advisor verification envelope")
        challenge = payload.get("challenge")
        if (
            not isinstance(challenge, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{43}", challenge)
            or not re.fullmatch(r"msg_verification_[a-f0-9]{36}", header_event_id)
        ):
            raise CallbackError("invalid Oracle advisor verification challenge")
        return VerificationChallenge(challenge)

    if set(payload) != {"eventId", "name", "timestamp", "data", "cursor"}:
        raise CallbackError("invalid Oracle advisor event envelope")
    if payload.get("name") != EVENT_NAME:
        raise CallbackError("invalid Oracle advisor event name")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise CallbackError("invalid Oracle advisor request data")
    required = {"requestId", "originAgent", "originSession", "question", "evidenceRefs"}
    allowed = required | {"workId"}
    if not required.issubset(data) or not set(data).issubset(allowed):
        raise CallbackError("invalid Oracle advisor request fields")

    request_id = data.get("requestId")
    if (
        not isinstance(request_id, str)
        or not 8 <= len(request_id) <= 128
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", request_id)
    ):
        raise CallbackError("invalid Oracle advisor request ID")
    origin_agent = data.get("originAgent")
    origin_session = data.get("originSession")
    work_id = data.get("workId")
    question = data.get("question")
    if (
        not _bounded_text(origin_agent, 1, 160, _ALL_CONTROLS)
        or not _bounded_text(origin_session, 1, 160, _ALL_CONTROLS)
        or ("workId" in data and not _bounded_text(work_id, 1, 160, _ALL_CONTROLS))
        or not _bounded_text(question, 1, 2_000, _QUESTION_CONTROLS)
    ):
        raise CallbackError("invalid Oracle advisor request text")
    evidence = data.get("evidenceRefs")
    if (
        not isinstance(evidence, list)
        or len(evidence) > 16
        or any(
            not _bounded_text(ref, 1, 512, _ALL_CONTROLS)
            or _CREDENTIAL_REFERENCE.search(ref)
            for ref in evidence
        )
    ):
        raise CallbackError("invalid Oracle advisor evidence references")

    body_event_id = payload.get("eventId")
    if not isinstance(body_event_id, str) or body_event_id != header_event_id:
        raise CallbackError("eventId does not match webhook-id")
    if body_event_id != _event_id_for_owner(owner, request_id):
        raise CallbackError("Oracle advisor event is outside the bound owner", 403)

    timestamp = payload.get("timestamp")
    cursor = payload.get("cursor")
    if (
        not isinstance(timestamp, str)
        or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z", timestamp)
        or not isinstance(cursor, str)
        or not re.fullmatch(r"[1-9][0-9]{0,15}", cursor)
        or int(cursor) > MAX_SAFE_INTEGER
    ):
        raise CallbackError("invalid Oracle advisor timestamp or cursor")
    try:
        datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError as exc:
        raise CallbackError("invalid Oracle advisor timestamp") from exc

    return AdvisorRequest(
        event_id=body_event_id,
        request_id=request_id,
        correlation_id=request_id,
        origin_agent=origin_agent,
        origin_session=origin_session,
        work_id=work_id if isinstance(work_id, str) else None,
        question=question,
        evidence_refs=tuple(evidence),
        occurred_at=timestamp,
        cursor=cursor,
    )

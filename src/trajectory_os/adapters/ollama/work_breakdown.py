"""Ollama work-breakdown proposal adapter (V1.5-A configuration seam,
V1.5-B deterministic /api/chat request serialization, V1.5-C strict
/api/chat response parsing).

This module provides the adapter package's public types:

* :class:`OllamaWorkBreakdownProposalError` — the adapter-specific error,
  used for every invalid configuration value;
* :class:`OllamaWorkBreakdownProposalProducer` — holds the validated
  model id, base URL, and timeout, plus the V1.5-B private helpers for
  deterministic request-context serialization and the Ollama
  ``/api/chat`` payload. It deliberately has no ``propose`` method yet:
  response parsing and proposal construction are later micro-steps.

V1.5-B serializes exactly
``request.model_dump(mode="json")`` of the frozen
:class:`~trajectory_os.domain.work_breakdown_production.WorkBreakdownProposalRequest`
with ``sort_keys=True``, ``separators=(",", ":")``, and
``ensure_ascii=True``, and builds the ``/api/chat`` payload with the
:class:`~trajectory_os.domain.work_breakdown_proposals.WorkBreakdownProposal`
JSON schema as the Ollama ``format``. No HTTP call is made here; no
response parsing, retries, or error translation occur.

It also defines one private, minimal HTTP seam :func:`_post_json` built
solely on ``urllib`` from the standard library. That seam:

* issues one exact-URL, exact-payload JSON ``POST`` with explicit
  ``Content-Type``/``Accept`` headers and a timeout;
* returns the complete response body as bytes;
* does not parse JSON, retry, stream, or translate transport errors —
  error translation belongs to the propose/runtime boundary.

V1.5-C adds :func:`_parse_chat_response`, the private strict parser for
raw ``/api/chat`` response bytes: UTF-8 decode, outer JSON object,
``message`` object, non-blank string ``content``, then strict Pydantic
construction of the existing
:class:`~trajectory_os.domain.work_breakdown_proposals.WorkBreakdownProposal`
from ``content``. JSON/schema-shape construction is adapter-level only:
no markdown-fence stripping, no JSON-substring extraction, no coercion or
repair of malformed model output, and no V1.2/V1.4 semantic
validation (``validate_work_breakdown_proposal`` / ``build_work_breakdown``
/ ``accept_work_breakdown_proposal`` do not run here). Every malformed
response raises :class:`OllamaWorkBreakdownProposalError` preserving the
underlying ``UnicodeDecodeError``, ``json.JSONDecodeError``, or
``pydantic.ValidationError`` as the cause.

Only the two frozen domain request/proposal types above are imported from
domain; no new dependencies are introduced.
"""

from __future__ import annotations

import json
import math
from http.client import HTTPException
from typing import Any, cast
from urllib import error, parse, request
from uuid import UUID

import pydantic

from trajectory_os.domain.entities import EntityType
from trajectory_os.domain.work_breakdown_production import (
    WorkBreakdownProposalRequest,
)
from trajectory_os.domain.work_breakdown_proposals import (
    WorkBreakdownProposal,
)

__all__ = [
    "OllamaWorkBreakdownProposalError",
    "OllamaWorkBreakdownProposalProducer",
]


class OllamaWorkBreakdownProposalError(ValueError):
    """Raised for invalid Ollama work-breakdown producer configuration or input."""


def _require_nonempty_str(value: object, name: str) -> str:
    """Validate that ``value`` is a non-blank string and return it stripped."""

    if not isinstance(value, str):
        raise OllamaWorkBreakdownProposalError(
            f"{name} must be a str, got {type(value).__name__}"
        )
    stripped = value.strip()
    if not stripped:
        raise OllamaWorkBreakdownProposalError(f"{name} must not be empty or blank")
    return stripped


def _normalize_base_url(value: object) -> str:
    """Validate a base URL and return it without surrounding whitespace.

    The scheme must be exactly ``http`` or ``https`` and the URL must
    carry a non-empty authority (netloc); any trailing ``/`` characters
    are stripped. Any other form raises
    :class:`OllamaWorkBreakdownProposalError`.
    """

    raw = _require_nonempty_str(value, "base_url")
    parsed = parse.urlparse(raw)
    if parsed.scheme not in {"http", "https"}:
        raise OllamaWorkBreakdownProposalError(
            "base_url scheme must be http or https, "
            f"got {parsed.scheme!r}"
        )
    if not parsed.netloc:
        raise OllamaWorkBreakdownProposalError(
            "base_url must include a host authority, got "
            f"{raw!r}"
        )
    if parsed.query or parsed.fragment:
        raise OllamaWorkBreakdownProposalError(
            "base_url must not carry a query or fragment, got "
            f"{raw!r}"
        )
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def _validate_timeout(value: object) -> float:
    """Validate ``timeout`` and return it as a finite float greater than 0."""

    if isinstance(value, bool):
        raise OllamaWorkBreakdownProposalError(
            "timeout must be a positive number; bool is not accepted"
        )
    if not isinstance(value, (int, float)):
        raise OllamaWorkBreakdownProposalError(
            "timeout must be an int or float, got "
            f"{type(value).__name__}"
        )
    timeout = float(value)
    if not math.isfinite(timeout):
        raise OllamaWorkBreakdownProposalError("timeout must be finite")
    if timeout <= 0:
        raise OllamaWorkBreakdownProposalError("timeout must be greater than 0")
    return timeout


_PROPOSAL_SYSTEM_MESSAGE = (
    "You propose missing work beneath the selected anchor only. "
    "Preserve project_id and anchor_id exactly as given in the request. "
    "Treat existing_work as read-only context and never modify it. "
    "Do not claim to mutate, persist, accept, or execute any work. "
    "Respond with only JSON that matches the required proposal structure."
)

_PROPOSAL_USER_INSTRUCTION = (
    "Propose the missing work that belongs beneath the selected anchor. "
    "Request context (JSON): "
)


def _deterministic_json(value: Any) -> str:
    """Serialize ``value`` to canonical, deterministic JSON text."""

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def _convert_uuid_wire_value(value: object) -> object:
    """Convert a valid JSON UUID string; preserve invalid values for validation."""

    if not isinstance(value, str):
        return value
    try:
        return UUID(value)
    except ValueError:
        return value


def _convert_entity_type_wire_value(value: object) -> object:
    """Convert a valid JSON entity-type string; preserve invalid values."""

    if not isinstance(value, str):
        return value
    try:
        return EntityType(value)
    except ValueError:
        return value


def _convert_node(parsed: object) -> object:
    """Mechanically convert JSON-native node representations for the domain."""

    if not isinstance(parsed, dict):
        return parsed

    converted = dict(parsed)

    if "entity_type" in converted:
        converted["entity_type"] = _convert_entity_type_wire_value(
            converted["entity_type"]
        )

    children = converted.get("children")
    if isinstance(children, list):
        converted["children"] = tuple(_convert_node(child) for child in children)

    return converted


def _convert_proposal(parsed: object) -> object:
    """Mechanically convert JSON-native proposal representations for the domain."""

    if not isinstance(parsed, dict):
        return parsed

    converted = dict(parsed)

    for field in ("project_id", "anchor_id"):
        if field in converted:
            converted[field] = _convert_uuid_wire_value(converted[field])

    children = converted.get("children")
    if isinstance(children, list):
        converted["children"] = tuple(_convert_node(child) for child in children)

    return converted


class OllamaWorkBreakdownProposalProducer:
    """Ollama-backed producer configuration for work-breakdown proposals.

    V1.5-A validates and stores configuration. V1.5-B adds the private
    deterministic request-context serialization and the Ollama
    ``/api/chat`` payload builder/encoder (with the
    :class:`~trajectory_os.domain.work_breakdown_proposals.WorkBreakdownProposal`
    JSON schema as the ``format``) plus the exact ``/api/chat`` endpoint.

    There is still no ``propose`` method, no HTTP call, no response
    parsing, and no
    :class:`~trajectory_os.domain.work_breakdown_proposals.WorkBreakdownProposal`
    construction; those arrive in later micro-steps. No mutable domain or
    canonical state is exposed.
    """

    def __init__(
        self,
        *,
        model: str,
        base_url: str = "http://localhost:11434",
        timeout: float = 120.0,
    ) -> None:
        self._model = _require_nonempty_str(model, "model")
        self._base_url = _normalize_base_url(base_url)
        self._timeout = _validate_timeout(timeout)

    @property
    def chat_endpoint(self) -> str:
        """Exact Ollama ``/api/chat`` endpoint for the normalized base URL."""

        return f"{self._base_url}/api/chat"

    def _serialize_request_context(
        self, request: WorkBreakdownProposalRequest
    ) -> str:
        """Deterministically serialize the frozen request's JSON state."""

        return _deterministic_json(request.model_dump(mode="json"))

    def _build_chat_payload(
        self, request: WorkBreakdownProposalRequest
    ) -> dict[str, Any]:
        """Build the Ollama ``/api/chat`` payload for ``request``.

        Structured output is enforced with the
        :class:`~trajectory_os.domain.work_breakdown_proposals.WorkBreakdownProposal`
        JSON schema as the Ollama ``format``; no hand-written second
        schema, no other model-specific options.
        """

        return {
            "model": self._model,
            "messages": [
                {"role": "system", "content": _PROPOSAL_SYSTEM_MESSAGE},
                {
                    "role": "user",
                    "content": (
                        _PROPOSAL_USER_INSTRUCTION
                        + self._serialize_request_context(request)
                    ),
                },
            ],
            "stream": False,
            "format": WorkBreakdownProposal.model_json_schema(),
            "options": {"temperature": 0},
        }

    def _encode_chat_payload(
        self, request: WorkBreakdownProposalRequest
    ) -> bytes:
        """Return the deterministic UTF-8 request body for ``request``."""

        return _deterministic_json(self._build_chat_payload(request)).encode(
            "utf-8"
        )

    def propose(
        self,
        request: WorkBreakdownProposalRequest,
    ) -> WorkBreakdownProposal:
        """Produce a work-breakdown proposal for ``request``.

        The proposal is constructed by:
        1. encoding the request with :meth:`_encode_chat_payload`
        2. POSTing to the chat endpoint with :func:`_post_json`
        3. parsing the response with :func:`_parse_chat_response`

        Transport errors from the HTTP adapter are translated into
        :class:`OllamaWorkBreakdownProposalError`. Malformed responses
        are parsed by the existing parser and translated into
        :class:`OllamaWorkBreakdownProposalError`.

        Canonical semantic validation (V1.2) is not performed here.
        """
        payload = self._encode_chat_payload(request)

        try:
            response_body = _post_json(
                self.chat_endpoint,
                payload,
                self._timeout,
            )
        except error.HTTPError as exc:
            raise OllamaWorkBreakdownProposalError(
                f"Ollama HTTP error: status {exc.code}"
            ) from exc
        except error.URLError as exc:
            raise OllamaWorkBreakdownProposalError(
                "Ollama transport error"
            ) from exc
        except TimeoutError as exc:
            raise OllamaWorkBreakdownProposalError(
                "Ollama request timed out"
            ) from exc
        except OSError as exc:
            raise OllamaWorkBreakdownProposalError(
                "Ollama transport error"
            ) from exc
        except HTTPException as exc:
            raise OllamaWorkBreakdownProposalError(
                "Ollama HTTP transport error"
            ) from exc

        return _parse_chat_response(response_body)


def _post_json(
    url: str,
    payload: bytes,
    timeout: float,
) -> bytes:
    """POST ``payload`` to ``url`` as JSON and return the full body bytes.

    Minimal stdlib seam: constructs one
    :class:`urllib.request.Request` with method ``POST``, the exact
    payload bytes, ``Content-Type: application/json`` and
    ``Accept: application/json`` headers, and calls
    ``urllib.request.urlopen`` with the given ``timeout``. The complete
    response body is returned unchanged as bytes.

    Transport errors are not caught or translated here, and no retries,
    streaming, or JSON parsing occur.
    """

    req = request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    with request.urlopen(req, timeout=timeout) as response:
        return cast("bytes", response.read())


def _parse_chat_response(body: bytes) -> WorkBreakdownProposal:
    """Strictly parse raw ``/api/chat`` response bytes into a proposal.

    ``body`` is the exact response body returned by :func:`_post_json`.
    The parse is strict and deliberately minimal:

    1. ``body`` decodes as UTF-8;
    2. the decoded text is a JSON document whose root is an object;
    3. ``message`` exists and is an object;
    4. ``content`` exists, is a ``str``, and is non-empty after strip;
    5. ``content`` parses into JSON and constructs a
       :class:`~trajectory_os.domain.work_breakdown_proposals.WorkBreakdownProposal`
       through Pydantic (schema-shape construction only).

    There is no markdown-fence stripping, no JSON-substring extraction,
    no quote repair, and no coercion of malformed model output; every
    malformed response raises :class:`OllamaWorkBreakdownProposalError`
    preserving the underlying ``UnicodeDecodeError``,
    ``json.JSONDecodeError``, or ``pydantic.ValidationError`` as the
    cause.

    Canonical semantic validation (V1.2/V1.4) is intentionally out of
    scope: this boundary constructs the proposal JSON shape and nothing
    more; :func:`~trajectory_os.domain.work_breakdown_proposals.validate_work_breakdown_proposal`,
    :func:`~trajectory_os.domain.work_breakdown.build_work_breakdown`, and
    accept-side operations do not run here.
    """

    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OllamaWorkBreakdownProposalError(
            "response body is not valid UTF-8"
        ) from exc

    try:
        outer = json.loads(text)
    except json.JSONDecodeError as exc:
        raise OllamaWorkBreakdownProposalError(
            "response body is not valid JSON"
        ) from exc

    if not isinstance(outer, dict):
        raise OllamaWorkBreakdownProposalError(
            "response root is not an object, "
            f"got {type(outer).__name__}"
        )

    message = outer.get("message")
    if not isinstance(message, dict):
        raise OllamaWorkBreakdownProposalError(
            f"response message must be an object, got {type(message).__name__}"
        )

    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        if not isinstance(content, str):
            raise OllamaWorkBreakdownProposalError(
                "response content must be a str, got "
                f"{type(content).__name__}"
            )
        raise OllamaWorkBreakdownProposalError(
            "response content must not be empty or blank"
        )

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise OllamaWorkBreakdownProposalError(
            "response content is not valid JSON"
        ) from exc

    # Convert JSON-native representations required by domain
    converted = _convert_proposal(parsed)

    try:
        return WorkBreakdownProposal.model_validate(
            converted,
            strict=True,
        )
    except pydantic.ValidationError as exc:
        raise OllamaWorkBreakdownProposalError(
            "response content is not a valid WorkBreakdownProposal"
        ) from exc
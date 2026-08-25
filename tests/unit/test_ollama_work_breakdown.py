"""Unit tests for V1.5-A configuration/HTTP seam and V1.5-B deterministic
/api/chat request serialization of the Ollama work-breakdown adapter."""

from __future__ import annotations

import ast
import inspect
import json
import urllib.error
import urllib.request
from http.client import HTTPException
from typing import get_type_hints
from uuid import uuid4

import pydantic
import pytest

import trajectory_os.adapters.ollama.work_breakdown as ollama_work_breakdown
from trajectory_os.adapters.ollama.work_breakdown import (
    OllamaWorkBreakdownProposalError,
    OllamaWorkBreakdownProposalProducer,
    _parse_chat_response,
    _post_json,
)
from trajectory_os.domain import (
    Portfolio,
    RelationType,
    TrajectoryEntity,
    TrajectoryRelation,
)
from trajectory_os.domain.entities import EntityType
from trajectory_os.domain.work_breakdown_production import (
    WorkBreakdownProposalContextItem,
    WorkBreakdownProposalProducer,
    WorkBreakdownProposalProductionError,
    WorkBreakdownProposalRequest,
    propose_work_breakdown,
)
from trajectory_os.domain.work_breakdown_proposals import (
    ProposedWorkNode,
    WorkBreakdownProposal,
)

URL = "http://localhost:11434/api/chat"

# --- V1.5-B fixed identifiers / request fixture -----------------------------

PROJECT_ID = uuid4()
ANCHOR_ID = uuid4()
DELIVERABLE_ID = uuid4()
OTHER_PACKAGE_ID = uuid4()
SECOND_PACKAGE_ID = uuid4()

EXPECTED_CONTEXT_KEYS = {
    "entity_id",
    "parent_id",
    "entity_type",
    "title",
    "description",
}


def _make_request() -> WorkBreakdownProposalRequest:
    """Build a request directly (no Portfolio) with deterministic content."""

    return WorkBreakdownProposalRequest(
        project_id=PROJECT_ID,
        anchor_id=ANCHOR_ID,
        existing_work=(
            WorkBreakdownProposalContextItem(
                entity_id=PROJECT_ID,
                parent_id=None,
                entity_type=EntityType.PROJECT,
                title="Project Alpha",
                description="Build the thing",
            ),
            WorkBreakdownProposalContextItem(
                entity_id=DELIVERABLE_ID,
                parent_id=PROJECT_ID,
                entity_type=EntityType.DELIVERABLE,
                title="Delivery B",
                description=None,
            ),
            WorkBreakdownProposalContextItem(
                entity_id=OTHER_PACKAGE_ID,
                parent_id=PROJECT_ID,
                entity_type=EntityType.WORK_PACKAGE,
                title="Pkg C",
            ),
        ),
    )


def _make_producer() -> OllamaWorkBreakdownProposalProducer:
    return OllamaWorkBreakdownProposalProducer(model="  mistral  ")


def _context_json(request: WorkBreakdownProposalRequest) -> str:
    return (
        _make_producer()
        ._serialize_request_context(request)  # noqa: SLF001 — private seam under test
    )


class _FakeResponse:
    """Minimal stand-in for urllib's response context manager."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


def _capture_post(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Patch urllib.request.urlopen and capture the Request and timeout."""

    captured: dict[str, object] = {}

    def fake_urlopen(
        request: object, timeout: float | None = None, **kwargs: object
    ) -> _FakeResponse:
        captured["request"] = request
        captured["timeout"] = timeout
        return _FakeResponse(b'{"ok": true}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return captured


def _sent_request(captured: dict[str, object]) -> urllib.request.Request:
    request = captured["request"]
    assert isinstance(request, urllib.request.Request)
    return request


def _post_once(monkeypatch: pytest.MonkeyPatch, payload: bytes, timeout: float) -> bytes:
    _capture_post(monkeypatch)
    return _post_json(URL, payload, timeout)


# --- constructor -----------------------------------------------------------


def test_constructor_valid_defaults() -> None:
    producer = OllamaWorkBreakdownProposalProducer(model="llama3.2")

    assert producer._model == "llama3.2"
    assert producer._base_url == "http://localhost:11434"
    assert producer._timeout == 120.0


def test_constructor_model_whitespace_normalized() -> None:
    producer = OllamaWorkBreakdownProposalProducer(model="  mistral  ")

    assert producer._model == "mistral"


@pytest.mark.parametrize("raw", ["", " \t\n "])
def test_constructor_empty_model_rejected(raw: str) -> None:
    with pytest.raises(OllamaWorkBreakdownProposalError, match="model"):
        OllamaWorkBreakdownProposalProducer(model=raw)


@pytest.mark.parametrize("raw", [123, None, b"llama", 4.2])
def test_constructor_non_string_model_rejected(raw: object) -> None:
    with pytest.raises(OllamaWorkBreakdownProposalError, match="model"):
        OllamaWorkBreakdownProposalProducer(model=raw)  # type: ignore[arg-type]


def test_constructor_base_url_trailing_slash_normalized() -> None:
    producer = OllamaWorkBreakdownProposalProducer(
        model="m", base_url="http://localhost:11434/api/"
    )

    assert producer._base_url == "http://localhost:11434/api"


def test_constructor_base_url_whitespace_normalized() -> None:
    producer = OllamaWorkBreakdownProposalProducer(
        model="m", base_url="  https://ollama.example:443  "
    )

    assert producer._base_url == "https://ollama.example:443"


@pytest.mark.parametrize("raw", ["", "   "])
def test_constructor_empty_base_url_rejected(raw: str) -> None:
    with pytest.raises(OllamaWorkBreakdownProposalError, match="base_url"):
        OllamaWorkBreakdownProposalProducer(model="m", base_url=raw)


@pytest.mark.parametrize("raw", ["ftp://host:11434", "localhost:11434", "gopher://h"])
def test_constructor_unsupported_scheme_rejected(raw: str) -> None:
    with pytest.raises(OllamaWorkBreakdownProposalError, match="scheme"):
        OllamaWorkBreakdownProposalProducer(model="m", base_url=raw)


@pytest.mark.parametrize("raw", ["http://", "https:///path"])
def test_constructor_missing_authority_rejected(raw: str) -> None:
    with pytest.raises(OllamaWorkBreakdownProposalError, match="authority"):
        OllamaWorkBreakdownProposalProducer(model="m", base_url=raw)


def test_constructor_timeout_zero_rejected() -> None:
    with pytest.raises(OllamaWorkBreakdownProposalError, match="timeout"):
        OllamaWorkBreakdownProposalProducer(model="m", timeout=0)


@pytest.mark.parametrize("raw", [-1, -0.5])
def test_constructor_negative_timeout_rejected(raw: float) -> None:
    with pytest.raises(OllamaWorkBreakdownProposalError, match="timeout"):
        OllamaWorkBreakdownProposalProducer(model="m", timeout=raw)


@pytest.mark.parametrize("raw", [True, False])
def test_constructor_bool_timeout_rejected(raw: bool) -> None:
    with pytest.raises(OllamaWorkBreakdownProposalError, match="bool"):
        OllamaWorkBreakdownProposalProducer(model="m", timeout=raw)


def test_constructor_nan_timeout_rejected() -> None:
    with pytest.raises(OllamaWorkBreakdownProposalError, match="timeout"):
        OllamaWorkBreakdownProposalProducer(model="m", timeout=float("nan"))


@pytest.mark.parametrize("raw", [float("inf"), float("-inf")])
def test_constructor_infinite_timeout_rejected(raw: float) -> None:
    with pytest.raises(OllamaWorkBreakdownProposalError, match="finite"):
        OllamaWorkBreakdownProposalProducer(model="m", timeout=raw)


def test_constructor_integer_timeout_stored_as_float() -> None:
    producer = OllamaWorkBreakdownProposalProducer(model="m", timeout=30)

    assert producer._timeout == 30.0
    assert isinstance(producer._timeout, float)


# --- _post_json seam -------------------------------------------------------


def test_post_json_uses_post_method(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_post(monkeypatch)

    _post_json(URL, b"{}", 10.0)

    assert _sent_request(captured).get_method() == "POST"


def test_post_json_url_exact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_post(monkeypatch)

    _post_json(URL, b"{}", 10.0)

    assert _sent_request(captured).full_url == URL


def test_post_json_payload_bytes_exact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_post(monkeypatch)
    payload = b'{"model": "m", "stream": false}'

    _post_json(URL, payload, 10.0)

    assert _sent_request(captured).data == payload


def test_post_json_content_type_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_post(monkeypatch)

    _post_json(URL, b"{}", 10.0)

    assert _sent_request(captured).get_header("Content-type") == "application/json"


def test_post_json_accept_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_post(monkeypatch)

    _post_json(URL, b"{}", 10.0)

    assert _sent_request(captured).get_header("Accept") == "application/json"


def test_post_json_timeout_exact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_post(monkeypatch)

    _post_json(URL, b"{}", 12.5)

    assert captured["timeout"] == 12.5


def test_post_json_returns_read_bytes_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _post_once(monkeypatch, b"{}", 10.0) == b'{"ok": true}'


# --- V1.5-B: request-context serialization ----------------------------------


def test_context_serialization_is_deterministic_json() -> None:
    request = _make_request()

    parsed = json.loads(_context_json(request))

    assert parsed["project_id"] == str(PROJECT_ID)
    assert parsed["anchor_id"] == str(ANCHOR_ID)
    assert len(parsed["existing_work"]) == 3


def test_context_serialization_identical_twice() -> None:
    request = _make_request()
    producer = _make_producer()

    first = producer._serialize_request_context(request)  # noqa: SLF001
    second = producer._serialize_request_context(request)  # noqa: SLF001

    assert first == second
    assert first == _context_json(_make_request())


def test_context_project_id_exact() -> None:
    parsed = json.loads(_context_json(_make_request()))

    assert parsed["project_id"] == str(PROJECT_ID)
    assert isinstance(parsed["project_id"], str)


def test_context_anchor_id_exact() -> None:
    parsed = json.loads(_context_json(_make_request()))

    assert parsed["anchor_id"] == str(ANCHOR_ID)
    assert isinstance(parsed["anchor_id"], str)


def test_context_existing_work_order_preserved_exactly() -> None:
    request = _make_request()
    parsed = json.loads(_context_json(request))

    assert [item["entity_id"] for item in parsed["existing_work"]] == [
        str(PROJECT_ID),
        str(DELIVERABLE_ID),
        str(OTHER_PACKAGE_ID),
    ]
    assert [item["title"] for item in parsed["existing_work"]] == [
        "Project Alpha",
        "Delivery B",
        "Pkg C",
    ]


def test_context_item_keys_exactly_five() -> None:
    parsed = json.loads(_context_json(_make_request()))

    for item in parsed["existing_work"]:
        assert set(item) == EXPECTED_CONTEXT_KEYS


# --- V1.5-B: /api/chat payload -------------------------------------------------


def _payload() -> dict[str, object]:
    producer = _make_producer()
    return producer._build_chat_payload(_make_request())  # noqa: SLF001


def test_payload_bytes_deterministic() -> None:
    request = _make_request()
    producer = _make_producer()

    first = producer._encode_chat_payload(request)  # noqa: SLF001
    second = producer._encode_chat_payload(request)  # noqa: SLF001

    assert first == second
    assert first == json.dumps(
        producer._build_chat_payload(request),  # noqa: SLF001
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def test_payload_model_uses_normalized_constructor_model() -> None:
    assert _payload()["model"] == "mistral"


def test_payload_stream_false() -> None:
    assert _payload()["stream"] is False


def test_payload_options_exactly_temperature_zero() -> None:
    assert _payload()["options"] == {"temperature": 0}


def test_payload_format_is_domain_proposal_schema() -> None:
    assert _payload()["format"] == WorkBreakdownProposal.model_json_schema()


def test_payload_exactly_two_messages_with_roles_system_then_user() -> None:
    messages = _payload()["messages"]
    assert isinstance(messages, list)
    assert len(messages) == 2
    assert [message["role"] for message in messages] == ["system", "user"]


def test_payload_user_message_contains_exact_context_json() -> None:
    request = _make_request()
    payload = _make_producer()._build_chat_payload(request)  # noqa: SLF001
    messages = payload["messages"]
    user = messages[1]

    assert _context_json(request) in user["content"]
    assert "Project Alpha" not in messages[0]["content"]


def test_payload_system_message_safety_instructions_without_domain_state() -> None:
    system = _payload()["messages"][0]["content"]

    assert "propose missing work" in system
    assert "project_id" in system
    assert "anchor_id" in system
    assert "read-only" in system
    assert "Do not claim to mutate, persist, accept, or execute" in system
    assert "Return" in system or "Respond" in system
    assert "JSON" in system
    assert str(PROJECT_ID) not in system
    assert str(ANCHOR_ID) not in system


# --- V1.5-B: endpoint ----------------------------------------------------------


def test_endpoint_is_normalized_base_url_plus_api_chat() -> None:
    producer = OllamaWorkBreakdownProposalProducer(
        model="m", base_url="  https://ollama.example:443/v1/  "
    )

    assert producer.chat_endpoint == "https://ollama.example:443/v1/api/chat"


def test_endpoint_default_base_url() -> None:
    assert _make_producer().chat_endpoint == URL


# --- V1.5-C: strict /api/chat response parsing -------------------------------

TASK_A_ID = uuid4()
TASK_B_ID = uuid4()


def _task(title: str, confidence: float) -> ProposedWorkNode:
    return ProposedWorkNode(
        entity_type=EntityType.TASK,
        title=title,
        description=None,
        confidence=confidence,
    )


def _make_proposal() -> WorkBreakdownProposal:
    """Build a valid proposal independently (no adapter involved)."""

    return WorkBreakdownProposal(
        project_id=PROJECT_ID,
        anchor_id=ANCHOR_ID,
        children=(
            ProposedWorkNode(
                entity_type=EntityType.DELIVERABLE,
                title="Delivery X",
                description="Deliver the thing",
                confidence=0.5,
                children=(_task("Task A", 0.8), _task("Task B", 0.25)),
            ),
            ProposedWorkNode(
                entity_type=EntityType.WORK_PACKAGE,
                title="Package W",
                description=None,
                confidence=0.9,
            ),
        ),
    )


def _proposal_content() -> str:
    """Valid proposal content built independently via serialization."""

    return _make_proposal().model_dump_json()


def _chat_body(outer: object) -> bytes:
    """Serialize an outer /api/chat response object to UTF-8 bytes."""

    return json.dumps(outer).encode("utf-8")


def _content_body(content: object) -> bytes:
    """Build a chat envelope whose message.content is ``content``."""

    return _chat_body({"message": {"content": content}})


# --- happy path -----------------------------------------------------------


def test_v15c_valid_envelope_returns_proposal() -> None:
    parsed = _parse_chat_response(_content_body(_proposal_content()))

    assert isinstance(parsed, WorkBreakdownProposal)


def test_v15c_project_id_preserved_exactly() -> None:
    parsed = _parse_chat_response(_content_body(_proposal_content()))

    assert parsed.project_id == PROJECT_ID
    assert isinstance(parsed.project_id, type(PROJECT_ID))


def test_v15c_anchor_id_preserved_exactly() -> None:
    parsed = _parse_chat_response(_content_body(_proposal_content()))

    assert parsed.anchor_id == ANCHOR_ID
    assert isinstance(parsed.anchor_id, type(ANCHOR_ID))


def test_v15c_children_ordering_preserved() -> None:
    parsed = _parse_chat_response(_content_body(_proposal_content()))

    assert [child.title for child in parsed.children] == ["Delivery X", "Package W"]
    assert [child.title for child in parsed.children[0].children] == [
        "Task A",
        "Task B",
    ]


def test_v15c_child_content_preserved() -> None:
    parsed = _parse_chat_response(_content_body(_proposal_content()))

    first, second = parsed.children
    assert first == ProposedWorkNode(
        entity_type=EntityType.DELIVERABLE,
        title="Delivery X",
        description="Deliver the thing",
        confidence=0.5,
        children=(_task("Task A", 0.8), _task("Task B", 0.25)),
    )
    assert second.entity_type is EntityType.WORK_PACKAGE
    assert second.title == "Package W"
    assert second.description is None
    assert second.confidence == 0.9
    assert second.children == ()


# --- decode step -----------------------------------------------------------


def test_v15c_invalid_utf8_rejected_with_cause() -> None:
    with pytest.raises(OllamaWorkBreakdownProposalError) as excinfo:
        _parse_chat_response(b"\xff\xfe\x00{\"message\":")

    assert isinstance(excinfo.value.__cause__, UnicodeDecodeError)


# --- outer JSON document ---------------------------------------------------


def test_v15c_outer_non_json_rejected_with_cause() -> None:
    with pytest.raises(OllamaWorkBreakdownProposalError) as excinfo:
        _parse_chat_response(b"not json at all")

    assert isinstance(excinfo.value.__cause__, json.JSONDecodeError)


@pytest.mark.parametrize(
    ("outer", "name"),
    [([1, 2, 3], "list"), ("a string", "str"), ([], "list"), (42, "int")],
)
def test_v15c_outer_root_not_object_rejected(outer: object, name: str) -> None:
    with pytest.raises(
        OllamaWorkBreakdownProposalError, match="root is not an object"
    ) as excinfo:
        _parse_chat_response(_chat_body(outer))

    assert excinfo.value.__cause__ is None


# --- message object --------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [None, [{"role": "assistant"}], "assistant", 42],
    ids=["none", "list", "string", "integer"],
)
def test_v15c_message_not_object_rejected(message: object) -> None:
    with pytest.raises(
        OllamaWorkBreakdownProposalError, match="message must be an object"
    ):
        _parse_chat_response(_chat_body({"message": message}))


def test_v15c_missing_message_rejected() -> None:
    with pytest.raises(
        OllamaWorkBreakdownProposalError, match="message must be an object"
    ):
        _parse_chat_response(_chat_body({}))


# --- content field ----------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "match"),
    [
        (None, "content must be a str"),
        (42, "content must be a str"),
        ([1, 2], "content must be a str"),
        ("", "content must not be empty or blank"),
        ("   \t\n  ", "content must not be empty or blank"),
    ],
    ids=["none", "integer", "list", "empty", "whitespace"],
)
def test_v15c_content_invalid_rejected(content: object, match: str) -> None:
    with pytest.raises(OllamaWorkBreakdownProposalError, match=match):
        _parse_chat_response(_content_body(content))


def test_v15c_missing_content_rejected() -> None:
    with pytest.raises(
        OllamaWorkBreakdownProposalError, match="content must be a str"
    ):
        _parse_chat_response(_chat_body({"message": {}}))


# --- schema-shape construction ----------------------------------------------


@pytest.mark.parametrize(
    "content",
    [
        "this is definitely not json",
        "```json\n" + _proposal_content() + "\n```",
        "Here is the proposal:\n" + _proposal_content() + "\nEnjoy!",
    ],
    ids=["prose", "markdown-fenced", "surrounding-prose"],
)
def test_v15c_non_json_content_rejected_with_json_cause(content: str) -> None:
    with pytest.raises(OllamaWorkBreakdownProposalError) as excinfo:
        _parse_chat_response(_content_body(content))

    assert isinstance(excinfo.value.__cause__, json.JSONDecodeError)


@pytest.mark.parametrize(
    "content",
    [
        "[1, 2, 3]",
        '"a string"',
        json.dumps(
            {
                "anchor_id": str(ANCHOR_ID),
                "children": [],
            }
        ),
        json.dumps(
            {
                "project_id": "not-a-uuid",
                "anchor_id": str(ANCHOR_ID),
                "children": [],
            }
        ),
    ],
    ids=[
        "wrong-root-list",
        "wrong-root-string",
        "missing-project-id",
        "malformed-uuid",
    ],
)
def test_v15c_valid_json_invalid_proposal_rejected_with_pydantic_cause(
    content: str,
) -> None:
    with pytest.raises(OllamaWorkBreakdownProposalError) as excinfo:
        _parse_chat_response(_content_body(content))

    assert isinstance(excinfo.value.__cause__, pydantic.ValidationError)


# --- deterministic JSON-wire → domain conversion ----------------------------


def test_v15c_json_entity_type_string_converts_to_enum() -> None:
    content = json.dumps(
        {
            "project_id": str(PROJECT_ID),
            "anchor_id": str(ANCHOR_ID),
            "children": [
                {
                    "entity_type": "deliverable",
                    "title": "Delivery X",
                    "description": None,
                    "confidence": 0.5,
                    "children": [],
                }
            ],
        }
    )

    parsed = _parse_chat_response(_content_body(content))

    assert parsed.children[0].entity_type is EntityType.DELIVERABLE


def test_v15c_invalid_entity_type_string_is_not_repaired() -> None:
    content = json.dumps(
        {
            "project_id": str(PROJECT_ID),
            "anchor_id": str(ANCHOR_ID),
            "children": [
                {
                    "entity_type": "not-a-real-entity-type",
                    "title": "Delivery X",
                    "description": None,
                    "confidence": 0.5,
                    "children": [],
                }
            ],
        }
    )

    with pytest.raises(OllamaWorkBreakdownProposalError) as excinfo:
        _parse_chat_response(_content_body(content))

    assert isinstance(excinfo.value.__cause__, pydantic.ValidationError)


def test_v15c_confidence_string_is_not_coerced() -> None:
    content = json.dumps(
        {
            "project_id": str(PROJECT_ID),
            "anchor_id": str(ANCHOR_ID),
            "children": [
                {
                    "entity_type": "deliverable",
                    "title": "Delivery X",
                    "description": None,
                    "confidence": "0.5",
                    "children": [],
                }
            ],
        }
    )

    with pytest.raises(OllamaWorkBreakdownProposalError) as excinfo:
        _parse_chat_response(_content_body(content))

    assert isinstance(excinfo.value.__cause__, pydantic.ValidationError)


def test_v15c_integer_confidence_is_valid_domain_input() -> None:
    content = json.dumps(
        {
            "project_id": str(PROJECT_ID),
            "anchor_id": str(ANCHOR_ID),
            "children": [
                {
                    "entity_type": "deliverable",
                    "title": "Delivery X",
                    "description": None,
                    "confidence": 1,
                    "children": [],
                }
            ],
        }
    )

    parsed = _parse_chat_response(_content_body(content))

    assert parsed.children[0].confidence == 1.0
    assert isinstance(parsed.children[0].confidence, float)
# --- V1.5-D2: propose runtime boundary -------------------------------------


def test_v15d2_propose_calls_transport_exactly_and_preserves_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _make_request()
    producer = OllamaWorkBreakdownProposalProducer(
        model="mistral",
        base_url="http://localhost:11434",
        timeout=37.5,
    )
    expected_payload = producer._encode_chat_payload(request)  # noqa: SLF001
    captured: dict[str, object] = {}

    def fake_post_json(url: str, payload: bytes, timeout: float) -> bytes:
        captured["url"] = url
        captured["payload"] = payload
        captured["timeout"] = timeout
        return _content_body(_proposal_content())

    monkeypatch.setattr(ollama_work_breakdown, "_post_json", fake_post_json)

    result = producer.propose(request)

    assert isinstance(result, WorkBreakdownProposal)
    assert captured == {
        "url": producer.chat_endpoint,
        "payload": expected_payload,
        "timeout": 37.5,
    }
    assert result.project_id == PROJECT_ID
    assert result.anchor_id == ANCHOR_ID
    assert [child.title for child in result.children] == [
        "Delivery X",
        "Package W",
    ]


def test_v15d2_propose_translates_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer = _make_producer()
    exc = urllib.error.HTTPError(
        "http://localhost:11434/api/chat",
        503,
        "Unavailable",
        None,
        None,
    )

    def fake_post_json(url: str, payload: bytes, timeout: float) -> bytes:
        raise exc

    monkeypatch.setattr(ollama_work_breakdown, "_post_json", fake_post_json)

    with pytest.raises(OllamaWorkBreakdownProposalError) as excinfo:
        producer.propose(_make_request())

    assert "503" in str(excinfo.value)
    assert excinfo.value.__cause__ is exc


def test_v15d2_propose_translates_url_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer = _make_producer()
    exc = urllib.error.URLError("connection refused")

    def fake_post_json(url: str, payload: bytes, timeout: float) -> bytes:
        raise exc

    monkeypatch.setattr(ollama_work_breakdown, "_post_json", fake_post_json)

    with pytest.raises(OllamaWorkBreakdownProposalError) as excinfo:
        producer.propose(_make_request())

    assert "transport" in str(excinfo.value).lower()
    assert excinfo.value.__cause__ is exc


def test_v15d2_propose_translates_timeout_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer = _make_producer()
    exc = TimeoutError("deadline exceeded")

    def fake_post_json(url: str, payload: bytes, timeout: float) -> bytes:
        raise exc

    monkeypatch.setattr(ollama_work_breakdown, "_post_json", fake_post_json)

    with pytest.raises(OllamaWorkBreakdownProposalError) as excinfo:
        producer.propose(_make_request())

    assert "timed out" in str(excinfo.value).lower()
    assert excinfo.value.__cause__ is exc


def test_v15d2_propose_translates_os_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer = _make_producer()
    exc = OSError("socket failed")

    def fake_post_json(url: str, payload: bytes, timeout: float) -> bytes:
        raise exc

    monkeypatch.setattr(ollama_work_breakdown, "_post_json", fake_post_json)

    with pytest.raises(OllamaWorkBreakdownProposalError) as excinfo:
        producer.propose(_make_request())

    assert "transport" in str(excinfo.value).lower()
    assert excinfo.value.__cause__ is exc


def test_v15d2_propose_translates_http_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer = _make_producer()
    exc = HTTPException("broken HTTP response")

    def fake_post_json(url: str, payload: bytes, timeout: float) -> bytes:
        raise exc

    monkeypatch.setattr(ollama_work_breakdown, "_post_json", fake_post_json)

    with pytest.raises(OllamaWorkBreakdownProposalError) as excinfo:
        producer.propose(_make_request())

    assert "transport" in str(excinfo.value).lower()
    assert excinfo.value.__cause__ is exc


def test_v15d2_real_parser_error_is_not_rewrapped_as_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer = _make_producer()

    def fake_post_json(url: str, payload: bytes, timeout: float) -> bytes:
        return b"not json"

    monkeypatch.setattr(ollama_work_breakdown, "_post_json", fake_post_json)

    with pytest.raises(OllamaWorkBreakdownProposalError) as excinfo:
        producer.propose(_make_request())

    assert "response body is not valid JSON" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, json.JSONDecodeError)


def test_v15d2_parser_error_object_propagates_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer = _make_producer()
    sentinel = OllamaWorkBreakdownProposalError("parser sentinel")

    def fake_post_json(url: str, payload: bytes, timeout: float) -> bytes:
        return b"irrelevant"

    def fake_parse(body: bytes) -> WorkBreakdownProposal:
        raise sentinel

    monkeypatch.setattr(ollama_work_breakdown, "_post_json", fake_post_json)
    monkeypatch.setattr(
        ollama_work_breakdown,
        "_parse_chat_response",
        fake_parse,
    )

    with pytest.raises(OllamaWorkBreakdownProposalError) as excinfo:
        producer.propose(_make_request())

    assert excinfo.value is sentinel


def test_v15d2_propose_signature_matches_v14_port() -> None:
    signature = inspect.signature(
        OllamaWorkBreakdownProposalProducer.propose
    )
    hints = get_type_hints(
        OllamaWorkBreakdownProposalProducer.propose
    )

    assert list(signature.parameters) == ["self", "request"]
    assert hints["request"] is WorkBreakdownProposalRequest
    assert hints["return"] is WorkBreakdownProposal

    producer: WorkBreakdownProposalProducer = _make_producer()
    assert callable(producer.propose)


# --- V1.5-D2: real composition with V1.4 ----------------------------------


def _v15d2_portfolio() -> tuple[
    Portfolio,
    TrajectoryEntity,
    TrajectoryEntity,
    TrajectoryEntity,
    TrajectoryEntity,
]:
    project = TrajectoryEntity(
        entity_type=EntityType.PROJECT,
        title="Primary Project",
    )
    anchor = TrajectoryEntity(
        entity_type=EntityType.DELIVERABLE,
        title="Primary Deliverable",
    )
    other_project = TrajectoryEntity(
        entity_type=EntityType.PROJECT,
        title="Other Project",
    )
    other_anchor = TrajectoryEntity(
        entity_type=EntityType.DELIVERABLE,
        title="Other Deliverable",
    )

    portfolio = Portfolio(
        name="V1.5-D2",
        entities=[
            project,
            anchor,
            other_project,
            other_anchor,
        ],
        relations=[
            TrajectoryRelation(
                source_id=anchor.id,
                target_id=project.id,
                relation_type=RelationType.BELONGS_TO,
            ),
            TrajectoryRelation(
                source_id=other_anchor.id,
                target_id=other_project.id,
                relation_type=RelationType.BELONGS_TO,
            ),
        ],
    )

    return portfolio, project, anchor, other_project, other_anchor


def test_v15d2_real_v14_composition_returns_safe_proposal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    portfolio, project, anchor, _, _ = _v15d2_portfolio()
    producer = _make_producer()

    proposal = WorkBreakdownProposal(
        project_id=project.id,
        anchor_id=anchor.id,
        children=(
            ProposedWorkNode(
                entity_type=EntityType.TASK,
                title="Generated task",
                confidence=0.8,
            ),
        ),
    )

    def fake_post_json(url: str, payload: bytes, timeout: float) -> bytes:
        return _content_body(proposal.model_dump_json())

    monkeypatch.setattr(ollama_work_breakdown, "_post_json", fake_post_json)

    result = propose_work_breakdown(
        portfolio,
        project.id,
        anchor.id,
        producer,
    )

    assert isinstance(result, WorkBreakdownProposal)
    assert result.project_id == project.id
    assert result.anchor_id == anchor.id
    assert [child.title for child in result.children] == ["Generated task"]


def test_v15d2_v14_rejects_valid_project_redirection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    portfolio, project, anchor, other_project, other_anchor = _v15d2_portfolio()
    producer = _make_producer()

    redirected = WorkBreakdownProposal(
        project_id=other_project.id,
        anchor_id=other_anchor.id,
        children=(),
    )

    def fake_post_json(url: str, payload: bytes, timeout: float) -> bytes:
        return _content_body(redirected.model_dump_json())

    monkeypatch.setattr(ollama_work_breakdown, "_post_json", fake_post_json)

    with pytest.raises(WorkBreakdownProposalProductionError):
        propose_work_breakdown(
            portfolio,
            project.id,
            anchor.id,
            producer,
        )


def test_v15d2_v14_rejects_valid_anchor_redirection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    portfolio, project, anchor, _, _ = _v15d2_portfolio()
    producer = _make_producer()

    redirected = WorkBreakdownProposal(
        project_id=project.id,
        anchor_id=project.id,
        children=(),
    )

    def fake_post_json(url: str, payload: bytes, timeout: float) -> bytes:
        return _content_body(redirected.model_dump_json())

    monkeypatch.setattr(ollama_work_breakdown, "_post_json", fake_post_json)

    with pytest.raises(WorkBreakdownProposalProductionError):
        propose_work_breakdown(
            portfolio,
            project.id,
            anchor.id,
            producer,
        )


# --- V1.5-D2: executable-authority scope guard -----------------------------


def test_v15d2_adapter_has_no_forbidden_executable_authority() -> None:
    tree = ast.parse(inspect.getsource(ollama_work_breakdown))

    forbidden_symbols = {
        "Portfolio",
        "TrajectoryEntity",
        "TrajectoryRelation",
        "build_work_breakdown",
        "validate_work_breakdown_proposal",
        "accept_work_breakdown_proposal",
    }

    executable_names: set[str] = set()
    imported_modules: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            executable_names.add(node.id)
        elif isinstance(node, ast.Attribute):
            executable_names.add(node.attr)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported_modules.add(alias.name)
                executable_names.add(
                    alias.asname or alias.name.split(".")[0]
                )
        elif isinstance(node, ast.ImportFrom):
            imported_modules.add(node.module or "")
            for alias in node.names:
                executable_names.add(alias.asname or alias.name)

    assert forbidden_symbols.isdisjoint(executable_names)
    assert not any(
        "persistence" in module.split(".")
        for module in imported_modules
    )

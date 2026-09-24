"""Unit tests for Codex ``/side`` follow-up routing.

Covers the two pure-ish seams that carry a user's follow-up turn into the side
chat's Codex thread: the server redirect (`_forward_codex_side_chat_turn`) and
the runner's message-text extraction (`_side_chat_text_from_content`).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

_JsonObject = dict[str, Any]


def test_side_chat_text_from_content_joins_text_blocks() -> None:
    from omnigent.runner.app import _side_chat_text_from_content

    assert _side_chat_text_from_content([{"type": "input_text", "text": "and why?"}]) == "and why?"
    assert (
        _side_chat_text_from_content(
            [
                {"type": "text", "text": "a"},
                {"type": "image", "url": "x"},  # non-text block ignored
                {"type": "input_text", "text": "b"},
            ]
        )
        == "a\nb"
    )
    assert _side_chat_text_from_content("nope") == ""  # not a list
    assert _side_chat_text_from_content([]) == ""


class _FakeResp:
    def __init__(self, status_code: int = 202, payload: _JsonObject | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> _JsonObject:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeRunnerClient:
    def __init__(self, resp: _FakeResp | None = None) -> None:
        self.posts: list[tuple[str, _JsonObject]] = []
        self._resp = resp or _FakeResp()

    async def post(self, url: str, *, json: _JsonObject) -> _FakeResp:
        self.posts.append((url, json))
        return self._resp


class _LabelStore:
    def __init__(self) -> None:
        self.writes: list[tuple[str, dict[str, str]]] = []

    def set_labels(self, conversation_id: str, updates: dict[str, str]) -> None:
        self.writes.append((conversation_id, updates))


@pytest.mark.asyncio
async def test_forward_side_chat_turn_redirects_to_parent_with_thread_id() -> None:
    from omnigent.server.routes._sessions import orchestration as orch
    from omnigent.server.routes._sessions.common import _CODEX_NATIVE_SUBAGENT_THREAD_ID_LABEL_KEY

    conv = SimpleNamespace(
        parent_conversation_id="conv_parent",
        kind="sub_agent",
        labels={_CODEX_NATIVE_SUBAGENT_THREAD_ID_LABEL_KEY: "thread_side"},
    )
    body = SimpleNamespace(data={"content": [{"type": "input_text", "text": "and why?"}]})
    client = _FakeRunnerClient()

    result = await orch._forward_codex_side_chat_turn(conv, body, client, _LabelStore())

    assert result is not None and result.item_id is None  # no AP-side persist
    assert len(client.posts) == 1
    url, payload = client.posts[0]
    assert "conv_parent" in url and url.endswith("/events")  # forwarded to the PARENT
    assert payload["codex_side_thread_id"] == "thread_side"  # tagged with the child thread
    assert payload["content"] == [{"type": "input_text", "text": "and why?"}]


@pytest.mark.asyncio
async def test_forward_side_chat_turn_falls_through_without_thread_or_parent() -> None:
    from omnigent.server.routes._sessions import orchestration as orch
    from omnigent.server.routes._sessions.common import _CODEX_NATIVE_SUBAGENT_THREAD_ID_LABEL_KEY

    body = SimpleNamespace(data={"content": []})
    client = _FakeRunnerClient()

    conv_no_label = SimpleNamespace(
        parent_conversation_id="conv_parent", kind="sub_agent", labels={}
    )
    assert (
        await orch._forward_codex_side_chat_turn(conv_no_label, body, client, _LabelStore())
        is None
    )

    conv_no_parent = SimpleNamespace(
        parent_conversation_id=None,
        kind="sub_agent",
        labels={_CODEX_NATIVE_SUBAGENT_THREAD_ID_LABEL_KEY: "thread_side"},
    )
    assert (
        await orch._forward_codex_side_chat_turn(conv_no_parent, body, client, _LabelStore())
        is None
    )

    assert client.posts == []  # nothing forwarded when it should fall through


@pytest.mark.asyncio
async def test_forward_side_chat_turn_seals_child_when_fork_is_gone() -> None:
    from omnigent.errors import ErrorCode, OmnigentError
    from omnigent.harnesses.codex_native.side_chat import SIDE_CHAT_GONE_ERROR
    from omnigent.server.routes._sessions import orchestration as orch
    from omnigent.server.routes._sessions.common import (
        _CODEX_NATIVE_SUBAGENT_THREAD_ID_LABEL_KEY,
        _CODEX_SIDE_CHAT_GONE_LABEL_KEY,
    )

    conv = SimpleNamespace(
        id="conv_side",
        parent_conversation_id="conv_parent",
        kind="sub_agent",
        labels={_CODEX_NATIVE_SUBAGENT_THREAD_ID_LABEL_KEY: "thread_side"},
    )
    body = SimpleNamespace(data={"content": [{"type": "input_text", "text": "and why?"}]})
    client = _FakeRunnerClient(_FakeResp(410, {"error": SIDE_CHAT_GONE_ERROR}))
    store = _LabelStore()

    with pytest.raises(OmnigentError) as exc_info:
        await orch._forward_codex_side_chat_turn(conv, body, client, store)

    assert exc_info.value.code == ErrorCode.CONFLICT
    assert store.writes == [("conv_side", {_CODEX_SIDE_CHAT_GONE_LABEL_KEY: "1"})]


def test_side_thread_gone_error_matches_only_thread_not_found() -> None:
    from omnigent.harnesses.codex_native.app_server import CodexAppServerResponseError
    from omnigent.harnesses.codex_native.side_chat import is_side_thread_gone_error

    gone = CodexAppServerResponseError({"code": -32600, "message": "thread not found: abc"})
    busy = CodexAppServerResponseError({"code": -32600, "message": "turn already in progress"})
    assert is_side_thread_gone_error(gone) is True
    assert is_side_thread_gone_error(busy) is False
    assert is_side_thread_gone_error(RuntimeError("thread not found")) is False


@pytest.mark.asyncio
async def test_parent_thread_child_detection_skips_only_thread_children() -> None:
    from omnigent.harnesses.codex_native.side_chat import CODEX_SUBAGENT_THREAD_ID_LABEL_KEY
    from omnigent.runner.native import orchestration as native_orch

    thread_labels = {CODEX_SUBAGENT_THREAD_ID_LABEL_KEY: "thread_side"}
    assert await native_orch._is_codex_parent_thread_child(None, "c", thread_labels) is True
    # A sys_session_send codex sub-agent has no thread label and must still launch.
    assert await native_orch._is_codex_parent_thread_child(None, "c", {}) is False


@pytest.mark.asyncio
async def test_parent_thread_child_detection_reads_snapshot_on_legacy_init(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.harnesses.codex_native.side_chat import CODEX_SUBAGENT_THREAD_ID_LABEL_KEY
    from omnigent.runner.native import orchestration as native_orch

    async def _payload(_client: object, _session_id: str) -> _JsonObject:
        return {"labels": {CODEX_SUBAGENT_THREAD_ID_LABEL_KEY: "thread_side"}}

    monkeypatch.setattr(native_orch, "_session_payload_for_host_spawn_check", _payload)
    assert await native_orch._is_codex_parent_thread_child(None, "c", None) is True


@pytest.mark.asyncio
async def test_forward_side_chat_turn_keeps_chat_open_when_bridge_lookup_fails() -> None:
    """A missing bridge can be a failed label lookup, not a gone fork: stay retryable."""
    from omnigent.server.routes._sessions import orchestration as orch
    from omnigent.server.routes._sessions.common import _CODEX_NATIVE_SUBAGENT_THREAD_ID_LABEL_KEY

    conv = SimpleNamespace(
        id="conv_side",
        parent_conversation_id="conv_parent",
        kind="sub_agent",
        labels={_CODEX_NATIVE_SUBAGENT_THREAD_ID_LABEL_KEY: "thread_side"},
    )
    body = SimpleNamespace(data={"content": [{"type": "input_text", "text": "and why?"}]})
    client = _FakeRunnerClient(_FakeResp(503, {"error": "codex_side_chat_no_bridge"}))
    store = _LabelStore()

    with pytest.raises(RuntimeError):
        await orch._forward_codex_side_chat_turn(conv, body, client, store)

    assert store.writes == []  # never sealed on an indeterminate lookup

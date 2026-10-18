"""OllamaChatModel — the ollama client is faked, structured JSON output validated.

Same convention as tests/unit/test_ollama_enricher.py: a small hand-rolled
fake standing in for ollama.AsyncClient, not a mocking library.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from acamar.adapters.llm.ollama_chat_model import OllamaChatModel
from acamar.ports.chat_model import ChatModelError


class _Verdict(BaseModel):
    ok: bool
    reason: str


class _FakeMessage:
    def __init__(self, content: str | None) -> None:
        self.content = content


class _FakeResponse:
    def __init__(self, content: str | None) -> None:
        self.message = _FakeMessage(content)


class _FakeClient:
    def __init__(self, response: object = None, error: Exception | None = None) -> None:
        self._response = response
        self._error = error
        self.calls: list[dict[str, Any]] = []

    async def chat(self, **kwargs: Any) -> object:
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._response


async def test_structured_parses_a_valid_response() -> None:
    client = _FakeClient(_FakeResponse('{"ok": true, "reason": "looks good"}'))
    model = OllamaChatModel(model="llama3", client=client)  # type: ignore[arg-type]

    result = await model.structured("check this", schema=_Verdict)

    assert result == _Verdict(ok=True, reason="looks good")


async def test_structured_sends_the_json_schema_as_the_format() -> None:
    client = _FakeClient(_FakeResponse('{"ok": true, "reason": "x"}'))
    model = OllamaChatModel(model="llama3", client=client)  # type: ignore[arg-type]

    await model.structured("check this", schema=_Verdict)

    assert client.calls[0]["format"] == _Verdict.model_json_schema()
    assert client.calls[0]["model"] == "llama3"


async def test_transport_failure_raises_chat_model_error() -> None:
    client = _FakeClient(error=RuntimeError("connection refused"))
    model = OllamaChatModel(model="llama3", client=client)  # type: ignore[arg-type]

    with pytest.raises(ChatModelError, match="connection refused"):
        await model.structured("check this", schema=_Verdict)


async def test_empty_content_raises_chat_model_error() -> None:
    client = _FakeClient(_FakeResponse(None))
    model = OllamaChatModel(model="llama3", client=client)  # type: ignore[arg-type]

    with pytest.raises(ChatModelError, match="no content"):
        await model.structured("check this", schema=_Verdict)


async def test_unparseable_content_raises_chat_model_error() -> None:
    client = _FakeClient(_FakeResponse("not json"))
    model = OllamaChatModel(model="llama3", client=client)  # type: ignore[arg-type]

    with pytest.raises(ChatModelError, match="unparseable"):
        await model.structured("check this", schema=_Verdict)

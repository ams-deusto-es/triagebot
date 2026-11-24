"""Unit tests for app.classifier. The LLM is always faked: no network calls."""

from types import SimpleNamespace

import pytest

from app import classifier

FALLBACK = {"category": "question", "priority": "P3", "tags": []}


class FakeClient:
    """Mimics client.chat.completions.create; each item in `replies` is a str or an exception."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls += 1
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        message = SimpleNamespace(content=reply)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


@pytest.fixture()
def fake_client(monkeypatch):
    def install(*replies):
        client = FakeClient(*replies)
        monkeypatch.setattr(classifier, "_get_client", lambda: client)
        return client

    return install


def test_valid_llm_response_is_returned(fake_client):
    client = fake_client(
        '{"category": "bug", "priority": "P1", "tags": ["login", "Blank Screen "]}'
    )

    result = classifier.classify_ticket("La app no carga", "Pantalla en blanco al iniciar sesión")

    assert result == {"category": "bug", "priority": "P1", "tags": ["login", "blank screen"]}
    assert client.calls == 1


def test_json_inside_code_fence_is_parsed(fake_client):
    fake_client('```json\n{"category": "feature_request", "priority": "P2", "tags": []}\n```')

    result = classifier.classify_ticket("Exportar a PDF", "Querría exportar informes")

    assert result == {"category": "feature_request", "priority": "P2", "tags": []}


@pytest.mark.parametrize(
    "bad_reply",
    [
        '{"category": "URGENT", "priority": "P1", "tags": []}',  # wrong case
        '{"category": "incident", "priority": "P1", "tags": []}',  # invented category
        '{"category": "bug", "priority": "p1", "tags": []}',  # invalid priority
        '{"category": "bug", "priority": "P1", "tags": "login"}',  # tags not a list
        "no es JSON",
        "",
    ],
)
def test_invalid_output_falls_back(fake_client, bad_reply):
    client = fake_client(bad_reply, bad_reply)

    assert classifier.classify_ticket("t", "d") == FALLBACK
    assert client.calls == 2  # one retry, then fallback


def test_sdk_error_retries_once_then_succeeds(fake_client):
    client = fake_client(
        RuntimeError("timeout"), '{"category": "urgent", "priority": "P1", "tags": ["demo"]}'
    )

    result = classifier.classify_ticket("Demo el viernes", "Urge")

    assert result == {"category": "urgent", "priority": "P1", "tags": ["demo"]}
    assert client.calls == 2


def test_sdk_error_twice_returns_fallback_without_raising(fake_client):
    client = fake_client(RuntimeError("down"), RuntimeError("still down"))

    assert classifier.classify_ticket("t", "d") == FALLBACK
    assert client.calls == 2  # never more than one retry


def test_missing_api_key_returns_fallback(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    assert classifier.classify_ticket("t", "d") == FALLBACK


def test_fallback_is_a_fresh_copy(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    classifier.classify_ticket("t", "d")["tags"].append("mutated")

    assert classifier.FALLBACK_CLASSIFICATION["tags"] == []

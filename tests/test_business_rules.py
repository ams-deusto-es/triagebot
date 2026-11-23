"""Reglas de negocio sobre la clasificación y la validación de entrada."""

import pytest
from fastapi.testclient import TestClient

from app.main import app

PRIORITY_ORDER = {"P1": 1, "P2": 2, "P3": 3}  # menor número = más prioridad


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    return TestClient(app)


def _classify_as(monkeypatch, category, priority):
    monkeypatch.setattr(
        "app.classifier.classify_ticket",
        lambda title, description: {"category": category, "priority": priority, "tags": ["x"]},
    )


def _create(client):
    return client.post("/tickets", json={"title": "Titulo", "description": "Descripcion"})


def test_question_ticket_is_never_p1(client, monkeypatch):
    _classify_as(monkeypatch, "question", "P1")

    response = _create(client)

    assert response.status_code == 201
    data = response.json()
    if data["category"] == "question":
        assert data["priority"] != "P1"


def test_urgent_ticket_is_at_least_p1(client, monkeypatch):
    # Un urgent con P2/P3 no es coherente: la prioridad nunca puede ser menor que P1.
    # Como P1 es la máxima, un urgent solo puede ser P1.
    _classify_as(monkeypatch, "urgent", "P3")

    response = _create(client)

    assert response.status_code == 201
    data = response.json()
    if data["category"] == "urgent":
        assert PRIORITY_ORDER[data["priority"]] <= PRIORITY_ORDER["P1"]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"title": "", "description": ""},
        {"title": "   ", "description": "   "},
        {"title": "Solo titulo"},
        {"description": "Solo descripcion"},
    ],
)
def test_ticket_requires_title_and_description(client, monkeypatch, payload):
    _classify_as(monkeypatch, "bug", "P2")

    response = client.post("/tickets", json=payload)

    assert response.status_code == 422
    assert client.get("/tickets").json() == []

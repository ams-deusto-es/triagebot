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


def _seed_for_sorting():
    from app import db

    db.create_ticket("a", "d", "bug", "P3", [], created_at="2026-01-01T10:00:00Z")
    t2 = db.create_ticket("b", "d", "bug", "P1", [], created_at="2026-01-02T10:00:00Z")
    t3 = db.create_ticket("c", "d", "bug", "P2", [], created_at="2026-01-03T10:00:00Z")
    db.update_ticket(t2["id"], {"status": "closed"})
    db.update_ticket(t3["id"], {"status": "in_progress"})


def _titles(response):
    import re

    return re.findall(r'<div class="font-medium">(\w)</div>', response.text)


def test_table_default_sort_is_open_before_closed(client):
    _seed_for_sorting()
    assert _titles(client.get("/ui/tickets")) == ["a", "c", "b"]


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("sort=priority&order=asc", ["b", "c", "a"]),
        ("sort=priority&order=desc", ["a", "c", "b"]),
        ("sort=created_at&order=asc", ["a", "b", "c"]),
        ("sort=created_at&order=desc", ["c", "b", "a"]),
        ("sort=status&order=desc", ["b", "c", "a"]),
        ("sort=evil;drop&order=x", ["a", "c", "b"]),
    ],
)
def test_table_sorting(client, query, expected):
    _seed_for_sorting()
    assert _titles(client.get(f"/ui/tickets?{query}")) == expected

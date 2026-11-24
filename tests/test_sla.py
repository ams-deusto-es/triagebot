"""Fecha límite por prioridad y estado de vencimiento."""

import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app

FMT = "%Y-%m-%dT%H:%M:%SZ"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    return TestClient(app)


def _ago(**delta) -> str:
    return (datetime.now(UTC) - timedelta(**delta)).strftime(FMT)


@pytest.mark.parametrize(
    ("priority", "expected"),
    [
        ("P1", "2026-01-01T14:00:00Z"),
        ("P2", "2026-01-02T10:00:00Z"),
        ("P3", "2026-01-08T10:00:00Z"),
    ],
)
def test_due_at_depends_on_priority(client, priority, expected):
    ticket = db.create_ticket("t", "d", "bug", priority, [], created_at="2026-01-01T10:00:00Z")
    assert ticket["due_at"] == expected
    assert client.get("/tickets").json()[0]["due_at"] == expected


def test_priority_change_recalculates_due_at(client):
    ticket = db.create_ticket("t", "d", "bug", "P3", [], created_at="2026-01-01T10:00:00Z")
    patched = client.patch(f"/tickets/{ticket['id']}", json={"priority": "P1"})
    assert patched.json()["due_at"] == "2026-01-01T14:00:00Z"


def test_overdue_flag(client):
    late = db.create_ticket("late", "d", "bug", "P1", [], created_at=_ago(hours=5))
    on_time = db.create_ticket("ok", "d", "bug", "P1", [], created_at=_ago(hours=3))
    p3_old = db.create_ticket("p3", "d", "bug", "P3", [], created_at=_ago(days=6))
    closed = db.create_ticket("closed", "d", "bug", "P1", [], created_at=_ago(hours=5))
    db.update_ticket(closed["id"], {"status": "closed"})

    overdue = {t["title"]: t["overdue"] for t in client.get("/tickets").json()}
    assert overdue == {"late": True, "ok": False, "p3": False, "closed": False}
    assert db.get_ticket(late["id"])["overdue"] is True
    assert db.get_ticket(on_time["id"])["overdue"] is False
    assert db.get_ticket(p3_old["id"])["overdue"] is False


def test_filter_by_overdue(client):
    db.create_ticket("late", "d", "bug", "P1", [], created_at=_ago(hours=5))
    db.create_ticket("ok", "d", "bug", "P2", [], created_at=_ago(hours=5))

    assert [t["title"] for t in client.get("/tickets?overdue=true").json()] == ["late"]
    assert [t["title"] for t in client.get("/tickets?overdue=false").json()] == ["ok"]
    assert len(client.get("/tickets").json()) == 2


def _titles(response):
    return re.findall(r'<div class="font-medium">(\w+)</div>', response.text)


def test_ui_marks_overdue_with_red_flag_and_filters(client):
    db.create_ticket("late", "d", "bug", "P1", [], created_at=_ago(hours=5))
    db.create_ticket("ok", "d", "bug", "P3", [], created_at=_ago(hours=5))

    html = client.get("/ui/tickets").text
    assert html.count('aria-label="Vencido"') == 1
    assert _titles(client.get("/ui/tickets?overdue=true")) == ["late"]
    assert _titles(client.get("/ui/tickets?overdue=false")) == ["ok"]
    assert len(_titles(client.get("/ui/tickets?overdue=garbage"))) == 2


def test_ui_sort_by_due(client):
    db.create_ticket("soon", "d", "bug", "P2", [], created_at=_ago(hours=1))  # vence en 23 h
    db.create_ticket("late", "d", "bug", "P1", [], created_at=_ago(hours=5))  # vencido
    db.create_ticket("far", "d", "bug", "P3", [], created_at=_ago(hours=1))  # vence en ~7 d

    assert _titles(client.get("/ui/tickets?sort=due&order=asc")) == ["late", "soon", "far"]
    assert _titles(client.get("/ui/tickets?sort=due&order=desc")) == ["far", "soon", "late"]


def test_existing_database_gets_due_at_backfilled(tmp_path, monkeypatch):
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE tickets (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
        description TEXT NOT NULL, category TEXT NOT NULL, priority TEXT NOT NULL,
        tags TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'open',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"""
    )
    conn.execute(
        "INSERT INTO tickets (title, description, category, priority, created_at, updated_at) "
        "VALUES ('t', 'd', 'bug', 'P2', '2026-01-01T10:00:00Z', '2026-01-01T10:00:00Z')"
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path}")

    [ticket] = db.list_tickets()
    assert ticket["due_at"] == "2026-01-02T10:00:00Z"
    assert ticket["overdue"] is True

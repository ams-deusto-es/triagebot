"""Estados open / in-progress / resolved / closed y fecha del último cambio de estado."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app, time_since


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    return TestClient(app)


def _create(created_at="2026-01-01T10:00:00Z", priority="P3"):
    return db.create_ticket("t", "d", "bug", priority, [], created_at=created_at)


@pytest.mark.parametrize("status", ["open", "in_progress", "resolved", "closed"])
def test_all_statuses_are_accepted(client, status):
    ticket = _create()
    response = client.patch(f"/tickets/{ticket['id']}", json={"status": status})
    assert response.status_code == 200
    assert response.json()["status"] == status


def test_in_progress_with_hyphen_is_an_alias(client):
    ticket = _create()
    response = client.patch(f"/tickets/{ticket['id']}", json={"status": "in-progress"})
    assert response.json()["status"] == "in_progress"
    assert len(client.get("/tickets?status=in-progress").json()) == 1


@pytest.mark.parametrize("status", ["IN_PROGRESS", "done", "Resolved", ""])
def test_unknown_status_is_422(client, status):
    ticket = _create()
    assert client.patch(f"/tickets/{ticket['id']}", json={"status": status}).status_code == 422


def test_new_ticket_status_changed_at_is_created_at(client):
    ticket = _create()
    assert ticket["status_changed_at"] == "2026-01-01T10:00:00Z"
    assert client.get("/tickets").json()[0]["status_changed_at"] == "2026-01-01T10:00:00Z"


def test_status_change_updates_status_changed_at(client, monkeypatch):
    ticket = _create()
    monkeypatch.setattr(db, "utc_now", lambda: "2026-02-01T08:00:00Z")
    patched = client.patch(f"/tickets/{ticket['id']}", json={"status": "resolved"})
    assert patched.json()["status_changed_at"] == "2026-02-01T08:00:00Z"


def test_other_changes_keep_status_changed_at(client, monkeypatch):
    ticket = _create()
    monkeypatch.setattr(db, "utc_now", lambda: "2026-02-01T08:00:00Z")
    # Mismo estado o solo prioridad: no cuenta como cambio de estado.
    client.patch(f"/tickets/{ticket['id']}", json={"status": "open", "priority": "P2"})
    assert db.get_ticket(ticket["id"])["status_changed_at"] == "2026-01-01T10:00:00Z"
    assert db.get_ticket(ticket["id"])["updated_at"] == "2026-02-01T08:00:00Z"


def test_resolved_ticket_is_not_overdue(client):
    ticket = _create(priority="P1")
    assert db.get_ticket(ticket["id"])["overdue"] is True
    db.update_ticket(ticket["id"], {"status": "resolved"})
    assert db.get_ticket(ticket["id"])["overdue"] is False


def test_status_sort_follows_lifecycle(client):
    ids = {}
    for status in ["closed", "open", "resolved", "in_progress"]:
        ticket = db.create_ticket(status, "d", "bug", "P3", [])
        db.update_ticket(ticket["id"], {"status": status})
        ids[ticket["id"]] = status
    html = client.get("/ui/tickets?sort=status&order=asc").text
    import re

    titles = re.findall(r'<div class="font-medium">(\w+)</div>', html)
    assert titles == ["open", "in_progress", "resolved", "closed"]


def test_ui_shows_labels_and_time_in_status(client, monkeypatch):
    _create(created_at="2026-01-01T10:00:00Z")
    html = client.get("/ui/tickets").text
    assert ">in-progress</option>" in html
    assert ">resolved</option>" in html
    assert "desde hace" in html


@pytest.mark.parametrize(
    ("timestamp", "expected"),
    [
        ("2026-03-10T11:59:30Z", "< 1 min"),
        ("2026-03-10T11:15:00Z", "45 min"),
        ("2026-03-10T07:00:00Z", "5 h"),
        ("2026-03-08T09:00:00Z", "2 d 3 h"),
        ("2026-03-09T12:00:00Z", "1 d"),
        ("2026-02-28T12:00:00Z", "10 d"),
        ("2026-03-10T13:00:00Z", "< 1 min"),  # reloj desajustado: nunca negativo
    ],
)
def test_time_since(timestamp, expected):
    now = datetime(2026, 3, 10, 12, 0, 0, tzinfo=UTC)
    assert time_since(timestamp, now) == expected


def test_existing_database_gets_status_changed_at_backfilled(tmp_path, monkeypatch):
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
        "INSERT INTO tickets (title, description, category, priority, status, created_at, "
        "updated_at) VALUES ('t', 'd', 'bug', 'P2', 'closed', '2026-01-01T10:00:00Z', "
        "'2026-01-03T09:00:00Z')"
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path}")

    [ticket] = db.list_tickets()
    assert ticket["status_changed_at"] == "2026-01-03T09:00:00Z"

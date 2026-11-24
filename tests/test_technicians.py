"""Técnicos responsables: alta y asignación a tickets."""

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setattr(
        "app.classifier.classify_ticket",
        lambda title, description: {"category": "bug", "priority": "P2", "tags": []},
    )
    return TestClient(app)


def _create_ticket(client):
    response = client.post("/tickets", json={"title": "Titulo", "description": "Descripcion"})
    assert response.status_code == 201
    return response.json()


def test_new_ticket_has_no_assignee(client):
    ticket = _create_ticket(client)
    assert ticket["assignee_id"] is None
    assert ticket["assignee"] is None


def test_create_and_list_technicians(client):
    response = client.post("/technicians", json={"name": "  Ane  "})
    assert response.status_code == 201
    assert response.json()["name"] == "Ane"

    client.post("/technicians", json={"name": "Jon"})
    names = [t["name"] for t in client.get("/technicians").json()]
    assert names == ["Ane", "Jon"]


@pytest.mark.parametrize("payload", [{}, {"name": ""}, {"name": "   "}, {"name": "x" * 101}])
def test_technician_name_is_validated(client, payload):
    assert client.post("/technicians", json=payload).status_code == 422


def test_duplicate_technician_is_rejected(client):
    client.post("/technicians", json={"name": "Ane"})
    assert client.post("/technicians", json={"name": "ane"}).status_code == 409


def test_assign_and_unassign_ticket(client):
    ticket = _create_ticket(client)
    technician = client.post("/technicians", json={"name": "Ane"}).json()

    assigned = client.patch(f"/tickets/{ticket['id']}", json={"assignee_id": technician["id"]})
    assert assigned.status_code == 200
    assert assigned.json()["assignee_id"] == technician["id"]
    assert assigned.json()["assignee"] == "Ane"

    # Cambiar otro campo sin mencionar assignee_id no lo toca.
    patched = client.patch(f"/tickets/{ticket['id']}", json={"priority": "P1"})
    assert patched.json()["assignee"] == "Ane"

    unassigned = client.patch(f"/tickets/{ticket['id']}", json={"assignee_id": None})
    assert unassigned.status_code == 200
    assert unassigned.json()["assignee_id"] is None


def test_assign_unknown_technician_is_422(client):
    ticket = _create_ticket(client)
    response = client.patch(f"/tickets/{ticket['id']}", json={"assignee_id": 999})
    assert response.status_code == 422


def test_status_cannot_be_set_to_null(client):
    ticket = _create_ticket(client)
    assert client.patch(f"/tickets/{ticket['id']}", json={"status": None}).status_code == 422


def test_ui_assigns_technician_via_dropdown(client):
    ticket = _create_ticket(client)
    created = client.post("/ui/technicians", data={"name": "Ane"})
    assert created.status_code == 200
    assert created.headers["HX-Trigger"] == "technicians-changed"
    technician_id = client.get("/technicians").json()[0]["id"]

    response = client.patch(
        f"/ui/tickets/{ticket['id']}",
        data={
            "ticket_status": "open",
            "ticket_priority": "P2",
            "ticket_assignee": str(technician_id),
        },
    )
    assert response.status_code == 200
    assert f'<option value="{technician_id}" selected>Ane</option>' in response.text
    assert client.get("/tickets").json()[0]["assignee"] == "Ane"


def test_ui_rejects_duplicate_technician_with_message(client):
    client.post("/ui/technicians", data={"name": "Ane"})
    response = client.post("/ui/technicians", data={"name": "Ane"})
    assert "Ya existe" in response.text
    assert "HX-Trigger" not in response.headers


def test_existing_database_is_migrated(tmp_path, monkeypatch):
    import sqlite3

    from app import db

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
        "VALUES ('t', 'd', 'bug', 'P1', '2026-01-01T10:00:00Z', '2026-01-01T10:00:00Z')"
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path}")

    [ticket] = db.list_tickets()
    assert ticket["assignee_id"] is None

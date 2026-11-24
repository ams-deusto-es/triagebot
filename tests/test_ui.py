"""Tests for the HTML board (GET /) and its HTMX fragments under /ui/."""

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setattr(
        "app.classifier.classify_ticket",
        lambda title, description: {"category": "bug", "priority": "P1", "tags": ["login"]},
    )
    return TestClient(app)


def _create(client, title="La app no carga", description="Pantalla en blanco"):
    return client.post("/tickets", json={"title": title, "description": description}).json()


def test_index_renders_board_with_tickets(client):
    _create(client)

    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "La app no carga" in response.text
    assert 'hx-post="/ui/tickets"' in response.text  # create form
    assert 'id="filters"' in response.text


def test_ui_table_filters(client):
    _create(client, title="Ticket abierto")
    closed = _create(client, title="Ticket cerrado")
    client.patch(f"/tickets/{closed['id']}", json={"status": "closed"})

    response = client.get("/ui/tickets", params={"status": "closed", "category": ""})

    assert response.status_code == 200
    assert "Ticket cerrado" in response.text
    assert "Ticket abierto" not in response.text


def test_ui_create_ticket_classifies_and_triggers_refresh(client):
    response = client.post("/ui/tickets", data={"title": "Nuevo", "description": "Algo falla"})

    assert response.status_code == 200
    assert response.headers["HX-Trigger"] == "tickets-changed"
    assert "creado" in response.text and "bug" in response.text
    assert client.get("/tickets").json()[0]["title"] == "Nuevo"


def test_ui_create_ticket_shows_validation_errors(client):
    response = client.post("/ui/tickets", data={"title": "   ", "description": "x" * 5001})

    assert response.status_code == 422
    assert "Título: no puede estar vacío." in response.text
    assert "Descripción: máximo 5000 caracteres." in response.text
    assert client.get("/tickets").json() == []


def test_ui_update_ticket_returns_updated_row(client):
    ticket = _create(client)

    response = client.post(
        f"/ui/tickets/{ticket['id']}", data={"status": "in_progress", "priority": "P3"}
    )

    assert response.status_code == 200
    assert response.text.lstrip().startswith("<tr")
    stored = client.get("/tickets").json()[0]
    assert (stored["status"], stored["priority"]) == ("in_progress", "P3")


@pytest.mark.parametrize(
    ("ticket_id", "data", "expected"),
    [
        (999, {"status": "open", "priority": "P1"}, 404),
        (None, {"status": "CLOSED", "priority": "P1"}, 422),
    ],
)
def test_ui_update_ticket_errors_do_not_swap(client, ticket_id, data, expected):
    ticket_id = ticket_id or _create(client)["id"]

    response = client.post(f"/ui/tickets/{ticket_id}", data=data)

    assert response.status_code == expected
    assert response.headers["HX-Reswap"] == "none"


def test_ticket_content_is_html_escaped(client):
    _create(client, title="<script>alert(1)</script>")

    html = client.get("/").text

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html

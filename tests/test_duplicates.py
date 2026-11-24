"""No se puede dar de alta dos veces el mismo ticket (mismo título y descripción)."""

import pytest
from fastapi.testclient import TestClient

from app.main import app

TICKET = {"title": "La app no carga", "description": "Pantalla en blanco al iniciar sesión"}


@pytest.fixture()
def calls(monkeypatch):
    """Registra las llamadas al clasificador para comprobar que un duplicado no lo invoca."""
    recorded = []

    def fake_classifier(title, description):
        recorded.append(title)
        return {"category": "bug", "priority": "P2", "tags": []}

    monkeypatch.setattr("app.classifier.classify_ticket", fake_classifier)
    return recorded


@pytest.fixture()
def client(tmp_path, monkeypatch, calls):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    return TestClient(app)


def test_duplicate_ticket_is_rejected_with_409(client, calls):
    first = client.post("/tickets", json=TICKET)
    assert first.status_code == 201

    second = client.post("/tickets", json=TICKET)
    assert second.status_code == 409
    assert f"#{first.json()['id']}" in second.json()["detail"]

    assert len(client.get("/tickets").json()) == 1
    assert len(calls) == 1  # el duplicado no llega a clasificarse


@pytest.mark.parametrize(
    "duplicate",
    [
        {"title": "  La app no carga  ", "description": TICKET["description"] + "   "},
        {"title": "LA APP NO CARGA", "description": TICKET["description"].upper()},
    ],
)
def test_duplicate_ignores_surrounding_spaces_and_case(client, duplicate):
    assert client.post("/tickets", json=TICKET).status_code == 201
    assert client.post("/tickets", json=duplicate).status_code == 409


@pytest.mark.parametrize(
    "other",
    [
        {"title": TICKET["title"], "description": "Otra descripción distinta"},
        {"title": "Otro título", "description": TICKET["description"]},
    ],
)
def test_same_title_or_description_alone_is_not_a_duplicate(client, other):
    assert client.post("/tickets", json=TICKET).status_code == 201
    assert client.post("/tickets", json=other).status_code == 201
    assert len(client.get("/tickets").json()) == 2


def test_ui_rejects_duplicate_ticket_with_message(client):
    assert client.post("/ui/tickets", data=TICKET).status_code == 200

    response = client.post("/ui/tickets", data=TICKET)
    assert response.status_code == 200
    assert "Ya existe el ticket" in response.text
    assert response.headers["HX-Retarget"] == "#form-error"
    assert "HX-Trigger" not in response.headers
    assert len(client.get("/tickets").json()) == 1


def test_concurrent_duplicates_create_only_one_ticket(tmp_path, monkeypatch):
    """Altas simultáneas del mismo ticket: todas pasan la comprobación previa mientras el
    LLM clasifica, pero solo una debe insertarse; el resto acaba en duplicado."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from app.main import DuplicateTicketError, create_classified_ticket
    from app.models import TicketCreate

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    workers = 8
    # Nadie sale del «LLM» hasta que todos han pasado la comprobación previa.
    barrier = threading.Barrier(workers, timeout=10)

    def slow_classifier(title, description):
        barrier.wait()
        return {"category": "bug", "priority": "P2", "tags": []}

    monkeypatch.setattr("app.classifier.classify_ticket", slow_classifier)

    def create(_):
        try:
            return create_classified_ticket(TicketCreate(**TICKET))["id"]
        except DuplicateTicketError as exc:
            return ("dup", exc.existing["id"])

    with ThreadPoolExecutor(workers) as pool:
        results = list(pool.map(create, range(workers)))

    created = [r for r in results if not isinstance(r, tuple)]
    duplicates = [r[1] for r in results if isinstance(r, tuple)]
    assert len(created) == 1
    assert duplicates == [created[0]] * (workers - 1)
    client = TestClient(app)
    assert len(client.get("/tickets").json()) == 1

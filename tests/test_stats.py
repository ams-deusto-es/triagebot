"""GET /tickets/stats: conteos por categoría, prioridad y estado."""

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    return TestClient(app)


def test_stats_on_empty_database_are_all_zero(client):
    response = client.get("/tickets/stats")
    assert response.status_code == 200
    assert response.json() == {
        "total": 0,
        "by_category": {"bug": 0, "feature_request": 0, "question": 0, "urgent": 0},
        "by_priority": {"P1": 0, "P2": 0, "P3": 0},
        "by_status": {"open": 0, "in_progress": 0, "resolved": 0, "closed": 0},
    }


def test_stats_count_tickets(client):
    db.create_ticket("a", "d", "bug", "P1", [])
    db.create_ticket("b", "d", "bug", "P2", [])
    db.create_ticket("c", "d", "question", "P3", [])
    db.create_ticket("e", "d", "urgent", "P1", [])
    client.patch("/tickets/1", json={"status": "in-progress"})
    client.patch("/tickets/2", json={"status": "closed"})

    stats = client.get("/tickets/stats").json()

    assert stats["total"] == 4
    assert stats["by_category"] == {"bug": 2, "feature_request": 0, "question": 1, "urgent": 1}
    assert stats["by_priority"] == {"P1": 2, "P2": 1, "P3": 1}
    assert stats["by_status"] == {"open": 2, "in_progress": 1, "resolved": 0, "closed": 1}
    for key in ("by_category", "by_priority", "by_status"):
        assert sum(stats[key].values()) == stats["total"]

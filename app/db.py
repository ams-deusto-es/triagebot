import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

DEFAULT_DATABASE_URL = "sqlite:///triagebot.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    category TEXT NOT NULL,
    priority TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""


def database_path() -> str:
    # Se lee en cada conexión para que DATABASE_URL pueda cambiar en tiempo de ejecución (tests).
    url = os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
    return url.removeprefix("sqlite:///")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(database_path())
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row_to_dict(row: sqlite3.Row) -> dict:
    ticket = dict(row)
    ticket["tags"] = json.loads(ticket["tags"])
    return ticket


def create_ticket(
    title: str,
    description: str,
    category: str,
    priority: str,
    tags: list[str],
    created_at: str | None = None,
) -> dict:
    created_at = created_at or utc_now()
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO tickets
                (title, description, category, priority, tags, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'open', ?, ?)
            """,
            (title, description, category, priority, json.dumps(tags), created_at, created_at),
        )
        ticket_id = cursor.lastrowid
    return get_ticket(ticket_id)


def get_ticket(ticket_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    return _row_to_dict(row) if row else None


def list_tickets(
    category: str | None = None,
    priority: str | None = None,
    status: str | None = None,
) -> list[dict]:
    filters = {"category": category, "priority": priority, "status": status}
    clauses = [f"{column} = ?" for column, value in filters.items() if value]
    params = [value for value in filters.values() if value]
    query = "SELECT * FROM tickets"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY created_at DESC, id DESC"
    with connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return [_row_to_dict(row) for row in rows]


def update_ticket(ticket_id: int, changes: dict) -> dict | None:
    ticket = get_ticket(ticket_id)
    if ticket is None:
        return None
    # Solo se toca updated_at si algún valor cambia de verdad.
    changes = {field: value for field, value in changes.items() if ticket[field] != value}
    if not changes:
        return ticket
    assignments = ", ".join(f"{field} = ?" for field in changes)
    with connect() as conn:
        conn.execute(
            f"UPDATE tickets SET {assignments}, updated_at = ? WHERE id = ?",
            (*changes.values(), utc_now(), ticket_id),
        )
    return get_ticket(ticket_id)

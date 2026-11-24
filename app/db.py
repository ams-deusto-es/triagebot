import json
import os
import sqlite3
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


def _db_path() -> str:
    """Resolve the SQLite file path from DATABASE_URL on every call (tests change it)."""
    url = os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL)
    prefix = "sqlite:///"
    return url[len(prefix) :] if url.startswith(prefix) else url


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute(SCHEMA)
    return conn


def _row_to_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["tags"] = json.loads(data["tags"])
    return data


def create_ticket(
    title: str,
    description: str,
    category: str,
    priority: str,
    tags: list[str],
    created_at: str | None = None,
) -> dict:
    """Insert a ticket. `created_at` (ISO 8601 UTC) is only for importing historical data."""
    now = created_at or datetime.now(UTC).isoformat().replace("+00:00", "Z")
    with get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO tickets
                (title, description, category, priority, tags, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'open', ?, ?)
            """,
            (title, description, category, priority, json.dumps(tags), now, now),
        )
        row = conn.execute("SELECT * FROM tickets WHERE id = ?", (cursor.lastrowid,)).fetchone()
    conn.close()
    return _row_to_dict(row)


# Columns allowed in filters/updates. Guards the f-string SQL below against injection.
FILTERABLE_COLUMNS = ("category", "priority", "status")
UPDATABLE_COLUMNS = ("status", "priority")


def list_tickets(**filters: str | None) -> list[dict]:
    """List tickets, newest first. Only non-None filters in FILTERABLE_COLUMNS apply."""
    active = {k: v for k, v in filters.items() if v is not None}
    unknown = set(active) - set(FILTERABLE_COLUMNS)
    if unknown:
        raise ValueError(f"Unknown filter(s): {sorted(unknown)}")

    query = "SELECT * FROM tickets"
    if active:
        query += " WHERE " + " AND ".join(f"{col} = ?" for col in active)
    query += " ORDER BY created_at DESC, id DESC"

    with get_connection() as conn:
        rows = conn.execute(query, tuple(active.values())).fetchall()
    conn.close()
    return [_row_to_dict(row) for row in rows]


def ticket_exists(title: str, created_at: str) -> bool:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM tickets WHERE title = ? AND created_at = ?", (title, created_at)
        ).fetchone()
    conn.close()
    return row is not None


def get_ticket(ticket_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    conn.close()
    return _row_to_dict(row) if row else None


def update_ticket(ticket_id: int, **changes: str) -> dict | None:
    """Update status/priority. Returns the updated ticket, or None if it doesn't exist.

    `updated_at` only changes when at least one field is provided.
    """
    unknown = set(changes) - set(UPDATABLE_COLUMNS)
    if unknown:
        raise ValueError(f"Field(s) not updatable: {sorted(unknown)}")
    if not changes:
        return get_ticket(ticket_id)

    assignments = ", ".join(f"{col} = ?" for col in changes)
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    with get_connection() as conn:
        cursor = conn.execute(
            f"UPDATE tickets SET {assignments}, updated_at = ? WHERE id = ?",
            (*changes.values(), now, ticket_id),
        )
        row = None
        if cursor.rowcount:
            row = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    conn.close()
    return _row_to_dict(row) if row else None

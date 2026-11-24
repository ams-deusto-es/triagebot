import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from app.models import SLA_BY_PRIORITY, SLA_STOPPED_STATUSES, STATUSES

TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
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
);
CREATE TABLE IF NOT EXISTS technicians (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    created_at TEXT NOT NULL
);
"""

# Columnas añadidas después de la primera versión del esquema: se crean con ALTER TABLE
# si la base de datos ya existía (p. ej. un triagebot.db anterior).
MIGRATIONS = {
    "assignee_id": "INTEGER REFERENCES technicians(id)",
    "due_at": "TEXT",
    "status_changed_at": "TEXT",
}

# Rellena las columnas nuevas en filas que ya existían antes de la migración.
_SLA_MODIFIER_SQL = (
    "CASE priority "
    + " ".join(
        f"WHEN '{priority}' THEN '+{int(sla.total_seconds())} seconds'"
        for priority, sla in SLA_BY_PRIORITY.items()
    )
    + " END"
)
BACKFILLS = {
    "due_at": f"UPDATE tickets SET due_at = strftime('{TIMESTAMP_FORMAT}', created_at, "
    f"{_SLA_MODIFIER_SQL})",
    # No sabemos cuándo cambió el estado; updated_at es la mejor aproximación disponible.
    "status_changed_at": "UPDATE tickets SET status_changed_at = updated_at",
}


def database_path() -> str:
    # Se lee en cada conexión para que DATABASE_URL pueda cambiar en tiempo de ejecución (tests).
    url = os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
    return url.removeprefix("sqlite:///")


def _migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(tickets)")}
    for column, definition in MIGRATIONS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE tickets ADD COLUMN {column} {definition}")
            if column in BACKFILLS:
                conn.execute(BACKFILLS[column])


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(database_path())
    conn.row_factory = sqlite3.Row
    try:
        _migrate(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


def utc_now() -> str:
    return datetime.now(UTC).strftime(TIMESTAMP_FORMAT)


def due_at_for(created_at: str, priority: str) -> str:
    created = datetime.strptime(created_at, TIMESTAMP_FORMAT)
    return (created + SLA_BY_PRIORITY[priority]).strftime(TIMESTAMP_FORMAT)


# --- Técnicos ----------------------------------------------------------------


class DuplicateTechnicianError(Exception):
    pass


def create_technician(name: str) -> dict:
    try:
        with connect() as conn:
            cursor = conn.execute(
                "INSERT INTO technicians (name, created_at) VALUES (?, ?)", (name, utc_now())
            )
            technician_id = cursor.lastrowid
    except sqlite3.IntegrityError as exc:
        raise DuplicateTechnicianError(name) from exc
    return get_technician(technician_id)


def get_technician(technician_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM technicians WHERE id = ?", (technician_id,)).fetchone()
    return dict(row) if row else None


def list_technicians() -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM technicians ORDER BY name COLLATE NOCASE").fetchall()
    return [dict(row) for row in rows]


# --- Tickets -----------------------------------------------------------------

# Vencido = sigue abierto y la fecha límite ya pasó. Depende de :now, por eso se calcula
# en cada consulta y no se guarda.
_STOPPED_SQL = ", ".join(f"'{status}'" for status in SLA_STOPPED_STATUSES)
OVERDUE_SQL = f"(t.status NOT IN ({_STOPPED_SQL}) AND t.due_at < :now)"

TICKET_SELECT = f"""
SELECT t.*, tech.name AS assignee, {OVERDUE_SQL} AS overdue
FROM tickets t
LEFT JOIN technicians tech ON tech.id = t.assignee_id
"""


def _row_to_dict(row: sqlite3.Row) -> dict:
    ticket = dict(row)
    ticket["tags"] = json.loads(ticket["tags"])
    ticket["overdue"] = bool(ticket["overdue"])
    return ticket


def create_ticket(
    title: str,
    description: str,
    category: str,
    priority: str,
    tags: list[str],
    created_at: str | None = None,
) -> dict:
    with connect() as conn:
        ticket_id = _insert_ticket(conn, title, description, category, priority, tags, created_at)
    return get_ticket(ticket_id)


def create_ticket_if_new(
    title: str, description: str, category: str, priority: str, tags: list[str]
) -> tuple[dict, bool]:
    """Crea el ticket salvo que ya exista uno igual. Devuelve (ticket, creado).

    La comprobación y la inserción van en la misma transacción BEGIN IMMEDIATE, que toma
    el bloqueo de escritura de SQLite: dos altas simultáneas del mismo ticket se
    serializan (también entre procesos) y la segunda ve la primera y no inserta.
    """
    with connect() as conn:
        conn.commit()  # cierra la transacción implícita que pudiera dejar la migración
        conn.execute("BEGIN IMMEDIATE")
        existing_id = _find_duplicate_id(conn, title, description)
        if existing_id is None:
            ticket_id = _insert_ticket(conn, title, description, category, priority, tags)
    if existing_id is not None:
        return get_ticket(existing_id), False
    return get_ticket(ticket_id), True


def _insert_ticket(
    conn: sqlite3.Connection,
    title: str,
    description: str,
    category: str,
    priority: str,
    tags: list[str],
    created_at: str | None = None,
) -> int:
    created_at = created_at or utc_now()
    cursor = conn.execute(
        """
        INSERT INTO tickets
            (title, description, category, priority, tags, status, status_changed_at,
             due_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)
        """,
        (
            title,
            description,
            category,
            priority,
            json.dumps(tags),
            created_at,
            due_at_for(created_at, priority),
            created_at,
            created_at,
        ),
    )
    return cursor.lastrowid


def find_duplicate_ticket(title: str, description: str) -> dict | None:
    """Ticket existente con el mismo título y descripción (sin distinguir mayúsculas)."""
    with connect() as conn:
        existing_id = _find_duplicate_id(conn, title, description)
    return get_ticket(existing_id) if existing_id is not None else None


def _find_duplicate_id(conn: sqlite3.Connection, title: str, description: str) -> int | None:
    # NOCASE de SQLite solo pliega ASCII; casefold también cubre «Ó»/«ó», «Ñ»/«ñ»…
    conn.create_function("casefold", 1, str.casefold, deterministic=True)
    row = conn.execute(
        "SELECT id FROM tickets WHERE casefold(title) = ? "
        "AND casefold(description) = ? ORDER BY id LIMIT 1",
        (title.casefold(), description.casefold()),
    ).fetchone()
    return row["id"] if row else None


def get_ticket(ticket_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            TICKET_SELECT + " WHERE t.id = :id", {"id": ticket_id, "now": utc_now()}
        ).fetchone()
    return _row_to_dict(row) if row else None


# Cada criterio es una lista de términos; la dirección (ASC/DESC) se aplica a todos.
SORT_EXPRESSIONS = {
    "priority": ["t.priority"],
    "status": [
        "CASE t.status "
        + " ".join(f"WHEN '{status}' THEN {index}" for index, status in enumerate(STATUSES))
        + " END"
    ],
    "created_at": ["t.created_at"],
    # asc: primero los vencidos, y dentro de cada grupo el que vence antes.
    "due": [f"NOT {OVERDUE_SQL}", "t.due_at"],
}


def list_tickets(
    category: str | None = None,
    priority: str | None = None,
    status: str | None = None,
    overdue: bool | None = None,
    sort: str | None = None,
    order: str = "asc",
) -> list[dict]:
    params: dict = {"now": utc_now()}
    clauses = []
    for column, value in {"category": category, "priority": priority, "status": status}.items():
        if value:
            clauses.append(f"t.{column} = :{column}")
            params[column] = value
    if overdue is not None:
        clauses.append(OVERDUE_SQL if overdue else f"NOT {OVERDUE_SQL}")
    query = TICKET_SELECT
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    if sort in SORT_EXPRESSIONS:
        direction = "DESC" if order == "desc" else "ASC"
        terms = ", ".join(f"{term} {direction}" for term in SORT_EXPRESSIONS[sort])
        query += f" ORDER BY {terms}, t.created_at DESC, t.id DESC"
    else:
        query += " ORDER BY t.created_at DESC, t.id DESC"
    with connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return [_row_to_dict(row) for row in rows]


def count_tickets_by(column: str) -> dict[str, int]:
    """Número de tickets por cada valor de `column` (category, priority o status)."""
    if column not in ("category", "priority", "status"):
        raise ValueError(column)
    with connect() as conn:
        rows = conn.execute(
            f"SELECT {column} AS value, COUNT(*) AS n FROM tickets GROUP BY {column}"
        ).fetchall()
    return {row["value"]: row["n"] for row in rows}


def update_ticket(ticket_id: int, changes: dict) -> dict | None:
    ticket = get_ticket(ticket_id)
    if ticket is None:
        return None
    # Solo se toca updated_at si algún valor cambia de verdad.
    changes = {field: value for field, value in changes.items() if ticket[field] != value}
    if not changes:
        return ticket
    if "priority" in changes:
        # El plazo se recalcula desde la creación con la nueva prioridad.
        changes["due_at"] = due_at_for(ticket["created_at"], changes["priority"])
    now = utc_now()
    if "status" in changes:
        changes["status_changed_at"] = now
    assignments = ", ".join(f"{field} = ?" for field in changes)
    with connect() as conn:
        conn.execute(
            f"UPDATE tickets SET {assignments}, updated_at = ? WHERE id = ?",
            (*changes.values(), now, ticket_id),
        )
    return get_ticket(ticket_id)

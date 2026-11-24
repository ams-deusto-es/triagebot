from datetime import datetime, timedelta
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, StringConstraints, model_validator

ALLOWED_CATEGORIES = {"bug", "feature_request", "question", "urgent"}
ALLOWED_PRIORITIES = {"P1", "P2", "P3"}
# Orden del ciclo de vida; también es el orden de la UI y de la ordenación por estado.
STATUSES = ("open", "in_progress", "resolved", "closed")
ALLOWED_STATUSES = set(STATUSES)
# El valor canónico es in_progress (lo exigen los tests de aceptación); en la UI se muestra
# como «in-progress» y la API acepta también esa grafía como alias de entrada.
STATUS_LABELS = {
    "open": "open",
    "in_progress": "in-progress",
    "resolved": "resolved",
    "closed": "closed",
}
STATUS_ALIASES = {"in-progress": "in_progress"}


def normalize_status(value):
    return STATUS_ALIASES.get(value, value) if isinstance(value, str) else value


# Plazo de resolución por prioridad, contado desde created_at.
SLA_BY_PRIORITY = {
    "P1": timedelta(hours=4),
    "P2": timedelta(hours=24),
    "P3": timedelta(days=7),
}
# Estados en los que el ticket ya no puede vencer: el plazo es de resolución.
SLA_STOPPED_STATUSES = ("resolved", "closed")

Category = Literal["bug", "feature_request", "question", "urgent"]
Priority = Literal["P1", "P2", "P3"]
Status = Literal["open", "in_progress", "resolved", "closed"]
StatusInput = Annotated[Status, BeforeValidator(normalize_status)]

Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Description = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=5000)
]
TechnicianName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)
]


class TicketCreate(BaseModel):
    title: Title
    description: Description


class TicketUpdate(BaseModel):
    # Solo status, priority y assignee_id son editables; cualquier otro campo da 422.
    model_config = ConfigDict(extra="forbid")

    status: StatusInput | None = None
    priority: Priority | None = None
    # null explícito = quitar el responsable; omitido = no tocarlo.
    assignee_id: int | None = None

    @model_validator(mode="after")
    def _status_and_priority_not_null(self) -> "TicketUpdate":
        for field in ("status", "priority"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} no puede ser null")
        return self

    def changes(self) -> dict:
        return self.model_dump(exclude_unset=True)


class TechnicianCreate(BaseModel):
    name: TechnicianName


class Technician(BaseModel):
    id: int
    name: str
    created_at: datetime


class Ticket(BaseModel):
    id: int
    title: str
    description: str
    category: Category
    priority: Priority
    tags: list[str]
    status: Status
    status_changed_at: datetime
    assignee_id: int | None = None
    assignee: str | None = None
    due_at: datetime
    overdue: bool
    created_at: datetime
    updated_at: datetime

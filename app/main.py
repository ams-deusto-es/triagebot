import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from dotenv import load_dotenv
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from app import classifier, db
from app.models import (
    ALLOWED_CATEGORIES,
    ALLOWED_PRIORITIES,
    STATUS_LABELS,
    STATUSES,
    Technician,
    TechnicianCreate,
    Ticket,
    TicketCreate,
    TicketUpdate,
    normalize_status,
)

load_dotenv()

logger = logging.getLogger(__name__)

app = FastAPI(title="TriageBot")
templates = Jinja2Templates(directory=Path(__file__).resolve().parent.parent / "templates")


def time_since(timestamp: str, now: datetime | None = None) -> str:
    """Duración legible desde `timestamp` (UTC) hasta ahora: «5 min», «3 h», «2 d 4 h»."""
    then = datetime.strptime(timestamp, db.TIMESTAMP_FORMAT).replace(tzinfo=UTC)
    minutes = max(0, int(((now or datetime.now(UTC)) - then).total_seconds()) // 60)
    if minutes < 1:
        return "< 1 min"
    if minutes < 60:
        return f"{minutes} min"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} h"
    days, hours = divmod(hours, 24)
    return f"{days} d {hours} h" if hours and days < 7 else f"{days} d"


templates.env.filters["since"] = time_since


def classify_safely(title: str, description: str) -> dict:
    """Llama al clasificador sin dejar escapar errores y revalida su salida."""
    try:
        result = classifier.classify_ticket(title, description)
        category, priority, tags = result["category"], result["priority"], result["tags"]
        if (
            category in ALLOWED_CATEGORIES
            and priority in ALLOWED_PRIORITIES
            and classifier.is_coherent(category, priority)
            and isinstance(tags, list)
            and all(isinstance(tag, str) for tag in tags)
        ):
            return {"category": category, "priority": priority, "tags": tags}
        logger.warning("El clasificador devolvió una salida inválida; se usa el fallback")
    except Exception:
        logger.exception("El clasificador falló; se usa el fallback")
    return dict(classifier.FALLBACK_CLASSIFICATION)


class DuplicateTicketError(Exception):
    def __init__(self, existing: dict) -> None:
        super().__init__(existing["id"])
        self.existing = existing


def create_classified_ticket(payload: TicketCreate) -> dict:
    # Se comprueba antes de clasificar para no gastar una llamada al LLM en un duplicado.
    existing = db.find_duplicate_ticket(payload.title, payload.description)
    if existing is not None:
        raise DuplicateTicketError(existing)
    classification = classify_safely(payload.title, payload.description)
    return db.create_ticket(payload.title, payload.description, **classification)


def apply_ticket_update(ticket_id: int, payload: TicketUpdate) -> dict:
    """Valida el responsable y aplica el cambio. Lanza 404/422 como HTTPException."""
    changes = payload.changes()
    assignee_id = changes.get("assignee_id")
    if assignee_id is not None and db.get_technician(assignee_id) is None:
        raise HTTPException(status_code=422, detail="El técnico no existe")
    ticket = db.update_ticket(ticket_id, changes)
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return ticket


# --- API JSON ---------------------------------------------------------------


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/tickets", status_code=201, response_model=Ticket)
def create_ticket(payload: TicketCreate) -> dict:
    try:
        return create_classified_ticket(payload)
    except DuplicateTicketError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"Ya existe un ticket con el mismo título y descripción (#{exc.existing['id']})",
        ) from exc


@app.get("/tickets", response_model=list[Ticket])
def list_tickets(
    category: str | None = None,
    priority: str | None = None,
    status: str | None = None,
    overdue: bool | None = None,
) -> list[dict]:
    return db.list_tickets(
        category=category, priority=priority, status=normalize_status(status), overdue=overdue
    )


@app.patch("/tickets/{ticket_id}", response_model=Ticket)
def update_ticket(ticket_id: int, payload: TicketUpdate) -> dict:
    return apply_ticket_update(ticket_id, payload)


@app.get("/technicians", response_model=list[Technician])
def list_technicians() -> list[dict]:
    return db.list_technicians()


@app.post("/technicians", status_code=201, response_model=Technician)
def create_technician(payload: TechnicianCreate) -> dict:
    try:
        return db.create_technician(payload.name)
    except db.DuplicateTechnicianError as exc:
        raise HTTPException(status_code=409, detail="Ya existe un técnico con ese nombre") from exc


# --- Interfaz web (HTML + HTMX) ---------------------------------------------


def board_context(
    category: str | None,
    priority: str | None,
    status: str | None,
    sort: str | None = None,
    order: str | None = None,
    overdue: str | None = None,
) -> dict:
    # Por defecto: por estado, con los tickets abiertos antes que los cerrados.
    sort = sort if sort in db.SORT_EXPRESSIONS else "status"
    order = order if order in ("asc", "desc") else "asc"
    # El filtro de la UI envía "true", "false" o "" (= todos); cualquier otro valor se ignora.
    overdue_filter = {"true": True, "false": False}.get(overdue or "")
    return {
        "tickets": db.list_tickets(
            category=category,
            priority=priority,
            status=normalize_status(status),
            overdue=overdue_filter,
            sort=sort,
            order=order,
        ),
        "technicians": db.list_technicians(),
        "sort": sort,
        "order": order,
        "categories": sorted(ALLOWED_CATEGORIES),
        "priorities": sorted(ALLOWED_PRIORITIES),
        "statuses": STATUSES,
        "status_labels": STATUS_LABELS,
    }


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "index.html", board_context(None, None, None))


@app.get("/ui/tickets", response_class=HTMLResponse)
def tickets_table(
    request: Request,
    category: str | None = None,
    priority: str | None = None,
    status: str | None = None,
    sort: str | None = None,
    order: str | None = None,
    overdue: str | None = None,
) -> HTMLResponse:
    context = board_context(category, priority, status, sort, order, overdue)
    return templates.TemplateResponse(request, "_tickets_table.html", context)


@app.post("/ui/tickets", response_class=HTMLResponse)
def create_ticket_from_form(
    request: Request,
    title: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    category: Annotated[str | None, Form()] = None,
    priority: Annotated[str | None, Form()] = None,
    status: Annotated[str | None, Form()] = None,
    sort: Annotated[str | None, Form()] = None,
    order: Annotated[str | None, Form()] = None,
    overdue: Annotated[str | None, Form()] = None,
) -> HTMLResponse:
    try:
        payload = TicketCreate(title=title, description=description)
        create_classified_ticket(payload)
    except (ValidationError, DuplicateTicketError) as exc:
        if isinstance(exc, DuplicateTicketError):
            error = f"Ya existe el ticket #{exc.existing['id']} con ese título y descripción."
        else:
            error = "El título (1–200) y la descripción (1–5000) son obligatorios."
        return templates.TemplateResponse(
            request,
            "_form_error.html",
            {"error": error},
            headers={"HX-Retarget": "#form-error", "HX-Reswap": "innerHTML"},
        )
    context = board_context(category, priority, status, sort, order, overdue)
    return templates.TemplateResponse(
        request, "_tickets_table.html", context, headers={"HX-Trigger": "ticket-created"}
    )


@app.patch("/ui/tickets/{ticket_id}", response_class=HTMLResponse)
def update_ticket_from_form(
    request: Request,
    ticket_id: int,
    ticket_status: Annotated[str, Form()],
    ticket_priority: Annotated[str, Form()],
    ticket_assignee: Annotated[str, Form()] = "",
    category: Annotated[str | None, Form()] = None,
    priority: Annotated[str | None, Form()] = None,
    status: Annotated[str | None, Form()] = None,
    sort: Annotated[str | None, Form()] = None,
    order: Annotated[str | None, Form()] = None,
    overdue: Annotated[str | None, Form()] = None,
) -> HTMLResponse:
    try:
        changes = TicketUpdate(
            status=ticket_status,
            priority=ticket_priority,
            # El desplegable envía "" para «Sin asignar».
            assignee_id=ticket_assignee or None,
        )
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="Valor no permitido") from exc
    apply_ticket_update(ticket_id, changes)
    context = board_context(category, priority, status, sort, order, overdue)
    return templates.TemplateResponse(request, "_tickets_table.html", context)


@app.post("/ui/technicians", response_class=HTMLResponse)
def create_technician_from_form(
    request: Request, name: Annotated[str, Form()] = ""
) -> HTMLResponse:
    error = None
    headers = {}
    try:
        payload = TechnicianCreate(name=name)
        db.create_technician(payload.name)
        # La tabla escucha este evento para refrescar los desplegables de responsable.
        headers["HX-Trigger"] = "technicians-changed"
    except ValidationError:
        error = "El nombre del técnico es obligatorio (máx. 100 caracteres)."
    except db.DuplicateTechnicianError:
        error = "Ya existe un técnico con ese nombre."
    context = {"technicians": db.list_technicians(), "error": error}
    return templates.TemplateResponse(request, "_technicians.html", context, headers=headers)

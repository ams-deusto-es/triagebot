import logging
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
    Ticket,
    TicketCreate,
    TicketUpdate,
)

load_dotenv()

logger = logging.getLogger(__name__)

app = FastAPI(title="TriageBot")
templates = Jinja2Templates(directory=Path(__file__).resolve().parent.parent / "templates")


def classify_safely(title: str, description: str) -> dict:
    """Llama al clasificador sin dejar escapar errores y revalida su salida."""
    try:
        result = classifier.classify_ticket(title, description)
        category, priority, tags = result["category"], result["priority"], result["tags"]
        if (
            category in ALLOWED_CATEGORIES
            and priority in ALLOWED_PRIORITIES
            and isinstance(tags, list)
            and all(isinstance(tag, str) for tag in tags)
        ):
            return {"category": category, "priority": priority, "tags": tags}
        logger.warning("El clasificador devolvió una salida inválida; se usa el fallback")
    except Exception:
        logger.exception("El clasificador falló; se usa el fallback")
    return dict(classifier.FALLBACK_CLASSIFICATION)


def create_classified_ticket(payload: TicketCreate) -> dict:
    classification = classify_safely(payload.title, payload.description)
    return db.create_ticket(payload.title, payload.description, **classification)


# --- API JSON ---------------------------------------------------------------


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/tickets", status_code=201, response_model=Ticket)
def create_ticket(payload: TicketCreate) -> dict:
    return create_classified_ticket(payload)


@app.get("/tickets", response_model=list[Ticket])
def list_tickets(
    category: str | None = None,
    priority: str | None = None,
    status: str | None = None,
) -> list[dict]:
    return db.list_tickets(category=category, priority=priority, status=status)


@app.patch("/tickets/{ticket_id}", response_model=Ticket)
def update_ticket(ticket_id: int, payload: TicketUpdate) -> dict:
    ticket = db.update_ticket(ticket_id, payload.model_dump(exclude_none=True))
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return ticket


# --- Interfaz web (HTML + HTMX) ---------------------------------------------


def board_context(category: str | None, priority: str | None, status: str | None) -> dict:
    return {
        "tickets": db.list_tickets(category=category, priority=priority, status=status),
        "categories": sorted(ALLOWED_CATEGORIES),
        "priorities": sorted(ALLOWED_PRIORITIES),
        "statuses": ["open", "in_progress", "closed"],
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
) -> HTMLResponse:
    context = board_context(category, priority, status)
    return templates.TemplateResponse(request, "_tickets_table.html", context)


@app.post("/ui/tickets", response_class=HTMLResponse)
def create_ticket_from_form(
    request: Request,
    title: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    category: Annotated[str | None, Form()] = None,
    priority: Annotated[str | None, Form()] = None,
    status: Annotated[str | None, Form()] = None,
) -> HTMLResponse:
    try:
        payload = TicketCreate(title=title, description=description)
    except ValidationError:
        errors = "El título (1–200) y la descripción (1–5000) son obligatorios."
        return templates.TemplateResponse(
            request,
            "_form_error.html",
            {"error": errors},
            headers={"HX-Retarget": "#form-error", "HX-Reswap": "innerHTML"},
        )
    create_classified_ticket(payload)
    context = board_context(category, priority, status)
    return templates.TemplateResponse(
        request, "_tickets_table.html", context, headers={"HX-Trigger": "ticket-created"}
    )


@app.patch("/ui/tickets/{ticket_id}", response_class=HTMLResponse)
def update_ticket_from_form(
    request: Request,
    ticket_id: int,
    ticket_status: Annotated[str, Form()],
    ticket_priority: Annotated[str, Form()],
    category: Annotated[str | None, Form()] = None,
    priority: Annotated[str | None, Form()] = None,
    status: Annotated[str | None, Form()] = None,
) -> HTMLResponse:
    try:
        changes = TicketUpdate(status=ticket_status, priority=ticket_priority)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="Valor no permitido") from exc
    if db.update_ticket(ticket_id, changes.model_dump(exclude_none=True)) is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    context = board_context(category, priority, status)
    return templates.TemplateResponse(request, "_tickets_table.html", context)

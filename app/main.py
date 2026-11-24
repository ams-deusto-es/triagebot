from pathlib import Path
from typing import Annotated, get_args

from dotenv import load_dotenv
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from app import classifier, db
from app.models import (
    ALLOWED_CATEGORIES,
    ALLOWED_PRIORITIES,
    Category,
    Priority,
    Status,
    Ticket,
    TicketCreate,
    TicketUpdate,
)

load_dotenv()  # OPENROUTER_API_KEY from .env (never committed); real env vars take precedence

app = FastAPI(title="TriageBot")
templates = Jinja2Templates(directory=Path(__file__).resolve().parent.parent / "templates")

# Ordered option lists for the UI selects.
UI_OPTIONS = {
    "categories": get_args(Category),
    "priorities": get_args(Priority),
    "statuses": get_args(Status),
}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _safe_classify(title: str, description: str) -> dict:
    """Call the classifier and guarantee a valid result; never raise."""
    fallback = dict(classifier.FALLBACK_CLASSIFICATION, tags=[])
    try:
        # Accessed through the module so tests can monkeypatch app.classifier.classify_ticket.
        result = classifier.classify_ticket(title, description)
    except Exception:
        return fallback

    if not isinstance(result, dict):
        return fallback
    tags = result.get("tags")
    if (
        result.get("category") not in ALLOWED_CATEGORIES
        or result.get("priority") not in ALLOWED_PRIORITIES
        or not isinstance(tags, list)
        or not all(isinstance(t, str) for t in tags)
    ):
        return fallback
    return {"category": result["category"], "priority": result["priority"], "tags": tags}


def _create_and_classify(payload: TicketCreate) -> dict:
    classification = _safe_classify(payload.title, payload.description)
    return db.create_ticket(title=payload.title, description=payload.description, **classification)


# ---------------------------------------------------------------- JSON API (SPEC §4)


@app.post("/tickets", response_model=Ticket, status_code=201)
def create_ticket(payload: TicketCreate) -> dict:
    return _create_and_classify(payload)


@app.get("/tickets", response_model=list[Ticket])
def list_tickets(
    category: str | None = None,
    priority: str | None = None,
    status: str | None = None,
) -> list[dict]:
    # Plain str on purpose: SPEC lists no errors for GET /tickets, so an unknown
    # filter value (e.g. "URGENT") just matches nothing and returns 200 [].
    return db.list_tickets(category=category, priority=priority, status=status)


@app.patch("/tickets/{ticket_id}", response_model=Ticket)
def update_ticket(ticket_id: int, payload: TicketUpdate) -> dict:
    changes = payload.model_dump(exclude_none=True)
    ticket = db.update_ticket(ticket_id, **changes)
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return ticket


# ---------------------------------------------------------------- HTML UI (SPEC §6)
# GET /tickets stays JSON; the HTMX fragments live under /ui/ so formats never mix.


def _filtered_tickets(category: str, priority: str, status: str) -> list[dict]:
    # Empty select value ("Todas") means "no filter".
    return db.list_tickets(
        category=category or None, priority=priority or None, status=status or None
    )


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "index.html", {"tickets": db.list_tickets(), **UI_OPTIONS}
    )


@app.get("/ui/tickets", response_class=HTMLResponse)
def ui_tickets_table(
    request: Request, category: str = "", priority: str = "", status: str = ""
) -> HTMLResponse:
    tickets = _filtered_tickets(category, priority, status)
    return templates.TemplateResponse(
        request, "_tickets_table.html", {"tickets": tickets, **UI_OPTIONS}
    )


@app.post("/ui/tickets", response_class=HTMLResponse)
def ui_create_ticket(
    request: Request,
    title: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
) -> HTMLResponse:
    try:
        payload = TicketCreate(title=title, description=description)
    except ValidationError as exc:
        errors = [_error_message(err) for err in exc.errors()]
        return templates.TemplateResponse(
            request,
            "_form_feedback.html",
            {"errors": errors, "title": title, "description": description},
            status_code=422,
        )

    ticket = _create_and_classify(payload)
    response = templates.TemplateResponse(request, "_form_feedback.html", {"created": ticket})
    response.headers["HX-Trigger"] = "tickets-changed"  # table reloads with current filters
    return response


@app.post("/ui/tickets/{ticket_id}", response_class=HTMLResponse)
def ui_update_ticket(
    request: Request,
    ticket_id: int,
    status: Annotated[str, Form()],
    priority: Annotated[str, Form()],
) -> HTMLResponse:
    try:
        changes = TicketUpdate(status=status, priority=priority).model_dump(exclude_none=True)
    except ValidationError:
        # Don't swap anything into the row; the select keeps the user's choice visible.
        raise HTTPException(422, "Valor no válido", headers={"HX-Reswap": "none"}) from None
    ticket = db.update_ticket(ticket_id, **changes)
    if ticket is None:
        raise HTTPException(404, "Ticket no encontrado", headers={"HX-Reswap": "none"})
    return templates.TemplateResponse(request, "_ticket_row.html", {"ticket": ticket, **UI_OPTIONS})


def _error_message(err: dict) -> str:
    field = {"title": "Título", "description": "Descripción"}.get(str(err["loc"][-1]), "Campo")
    if err["type"] == "string_too_short":
        return f"{field}: no puede estar vacío."
    if err["type"] == "string_too_long":
        return f"{field}: máximo {err['ctx']['max_length']} caracteres."
    return f"{field}: {err['msg']}"

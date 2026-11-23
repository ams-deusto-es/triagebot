"""Carga seed_tickets.json en la base de datos.

Uso (desde la raíz del repo):
    python -m scripts.seed            # clasifica cada ticket con el LLM
    python -m scripts.seed --no-llm   # usa la clasificación de fallback (rápido, sin coste)
    python -m scripts.seed --force    # carga aunque la base de datos ya tenga tickets
"""

import argparse
import json
from pathlib import Path

from dotenv import load_dotenv

from app import classifier, db
from app.models import TicketCreate

SEED_FILE = Path(__file__).resolve().parent.parent / "seed_tickets.json"


def main() -> None:
    parser = argparse.ArgumentParser(description="Carga los tickets de ejemplo en SQLite.")
    parser.add_argument("--no-llm", action="store_true", help="no llamar al LLM; usar fallback")
    parser.add_argument("--force", action="store_true", help="cargar aunque ya haya tickets")
    args = parser.parse_args()

    load_dotenv()

    if db.list_tickets() and not args.force:
        print(f"La base de datos ({db.database_path()}) ya tiene tickets. Usa --force para cargar.")
        return

    seeds = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    for number, seed in enumerate(seeds, start=1):
        ticket = TicketCreate(title=seed["title"], description=seed["description"])
        if args.no_llm:
            classification = dict(classifier.FALLBACK_CLASSIFICATION)
        else:
            classification = classifier.classify_ticket(ticket.title, ticket.description)
        db.create_ticket(
            ticket.title, ticket.description, created_at=seed.get("created_at"), **classification
        )
        print(
            f"[{number}/{len(seeds)}] {classification['category']:<15} "
            f"{classification['priority']}  {ticket.title}"
        )


if __name__ == "__main__":
    main()

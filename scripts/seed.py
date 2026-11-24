"""Load seed_tickets.json into the database, classifying each ticket with the LLM.

Usage (from the repo root):
    python -m scripts.seed                    # uses DATABASE_URL or triagebot.db
    python -m scripts.seed --workers 4

Idempotent: tickets already present (same title + created_at) are skipped,
so it can be re-run safely after an interruption.
"""

import argparse
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from dotenv import load_dotenv

from app import classifier, db
from app.models import TicketCreate

SEED_FILE = Path(__file__).resolve().parent.parent / "seed_tickets.json"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--file", type=Path, default=SEED_FILE)
    parser.add_argument("--workers", type=int, default=8, help="parallel LLM calls")
    args = parser.parse_args()

    load_dotenv()
    seeds = json.loads(args.file.read_text(encoding="utf-8"))

    pending = []
    for item in seeds:
        ticket = TicketCreate(title=item["title"], description=item["description"])
        if not db.ticket_exists(ticket.title, item["created_at"]):
            pending.append((ticket, item["created_at"]))
    print(f"{len(seeds)} tickets en el fichero, {len(pending)} por importar.")

    # Classify in parallel (network-bound); write to SQLite from this thread only.
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(classifier.classify_ticket, t.title, t.description): (t, created_at)
            for t, created_at in pending
        }
        for done, future in enumerate(as_completed(futures), start=1):
            ticket, created_at = futures[future]
            result = future.result()  # classify_ticket never raises
            db.create_ticket(ticket.title, ticket.description, **result, created_at=created_at)
            print(
                f"[{done}/{len(pending)}] {result['category']:<15} {result['priority']}  "
                f"{ticket.title[:60]}"
            )

    print("Hecho.")


if __name__ == "__main__":
    main()

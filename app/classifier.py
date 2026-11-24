"""Ticket classifier. The ONLY module that talks to the LLM (no FastAPI imports here)."""

import json
import logging
import os
import re

from openai import OpenAI

logger = logging.getLogger(__name__)

FALLBACK_CLASSIFICATION = {"category": "question", "priority": "P3", "tags": []}

ALLOWED_CATEGORIES = {"bug", "feature_request", "question", "urgent"}
ALLOWED_PRIORITIES = {"P1", "P2", "P3"}

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
MODEL = "openai/gpt-oss-120b"
MAX_ATTEMPTS = 2  # first call + at most one retry (SPEC §5)
REQUEST_TIMEOUT_SECONDS = 30
MAX_TAGS = 5
MAX_TAG_LENGTH = 30

SYSTEM_PROMPT = """\
Eres un asistente de triage de tickets de soporte. Clasifica el ticket que te dé el usuario.

Responde ÚNICAMENTE con un objeto JSON, sin texto adicional ni bloques de código, con esta forma:
{"category": "...", "priority": "...", "tags": ["...", "..."]}

Reglas:
- category: exactamente uno de "bug", "feature_request", "question", "urgent" (en minúsculas).
  - bug: algo que no funciona como debería.
  - feature_request: petición de funcionalidad nueva o mejora.
  - question: duda o petición de información.
  - urgent: bloqueo crítico o con plazo inminente (caída general, demo, pérdida de datos...).
- priority: exactamente uno de "P1" (crítico), "P2" (importante), "P3" (menor).
- tags: entre 0 y 5 etiquetas cortas, en minúsculas, con guiones en lugar de espacios
  (p. ej. "login", "export-pdf").
"""


def _fallback() -> dict:
    # Fresh copy so callers can't mutate the shared constant (e.g. its tags list).
    return {**FALLBACK_CLASSIFICATION, "tags": []}


def _get_client() -> OpenAI | None:
    """Build the OpenRouter client, or None if there is no API key."""
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        return None
    # max_retries=0: the retry policy lives in classify_ticket, not in the SDK.
    return OpenAI(
        base_url=OPENROUTER_BASE_URL,
        api_key=api_key,
        timeout=REQUEST_TIMEOUT_SECONDS,
        max_retries=0,
    )


def _extract_json(text: str) -> dict:
    """Parse the model's reply, tolerating ```json fences or stray text around the object."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("No JSON object in LLM response")
    data = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise ValueError("LLM response JSON is not an object")
    return data


def _validate(data: dict) -> dict:
    """Return a clean classification or raise ValueError. Values must match exactly."""
    category = data.get("category")
    priority = data.get("priority")
    tags = data.get("tags", [])

    if category not in ALLOWED_CATEGORIES:
        raise ValueError(f"Invalid category: {category!r}")
    if priority not in ALLOWED_PRIORITIES:
        raise ValueError(f"Invalid priority: {priority!r}")
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        raise ValueError(f"Invalid tags: {tags!r}")

    clean_tags: list[str] = []
    for tag in tags:
        tag = tag.strip().lower()[:MAX_TAG_LENGTH]
        if tag and tag not in clean_tags:
            clean_tags.append(tag)

    return {"category": category, "priority": priority, "tags": clean_tags[:MAX_TAGS]}


def _call_llm(client: OpenAI, title: str, description: str) -> str:
    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Título: {title}\n\nDescripción: {description}"},
        ],
        temperature=0,
    )
    content = response.choices[0].message.content
    if not content:
        raise ValueError("Empty LLM response")
    return content


def classify_ticket(title: str, description: str) -> dict:
    """Classify a support ticket. Never raises: any problem returns the fallback."""
    try:
        client = _get_client()
    except Exception:
        logger.exception("Could not create LLM client")
        return _fallback()
    if client is None:
        logger.warning("OPENROUTER_API_KEY not set; using fallback classification")
        return _fallback()

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            content = _call_llm(client, title, description)
            return _validate(_extract_json(content))
        except Exception as exc:  # SDK errors, bad JSON, invalid values...
            logger.warning("Classification attempt %d/%d failed: %s", attempt, MAX_ATTEMPTS, exc)

    return _fallback()

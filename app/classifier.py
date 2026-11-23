"""Clasificador de tickets: ÚNICO módulo que habla con el LLM."""

import json
import logging
import os

from openai import OpenAI

from app.models import ALLOWED_CATEGORIES, ALLOWED_PRIORITIES

logger = logging.getLogger(__name__)

FALLBACK_CLASSIFICATION = {"category": "question", "priority": "P3", "tags": []}

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
MODEL = "openai/gpt-oss-120b"
TIMEOUT_SECONDS = 20
MAX_ATTEMPTS = 2  # la llamada original + como mucho un reintento
MAX_TAGS = 5

SYSTEM_PROMPT = """Eres el sistema de triage del equipo de Soporte. Clasificas tickets de clientes.

Responde SOLO con un objeto JSON, sin texto adicional ni bloques de código, con esta forma exacta:
{"category": "...", "priority": "...", "tags": ["...", "..."]}

Reglas:
- category: exactamente uno de "bug", "feature_request", "question", "urgent" (en minúsculas).
  - bug: algo que funcionaba o debería funcionar está fallando.
  - feature_request: petición de funcionalidad nueva o mejora.
  - question: duda, consulta o petición de información o permisos.
  - urgent: bloqueo crítico o con plazo inminente (caída general, demo, pérdida de datos...).
- priority: exactamente uno de "P1", "P2", "P3".
  - P1: impacto alto o bloqueante, o plazo inminente.
  - P2: impacto moderado, hay alternativa temporal.
  - P3: impacto bajo, consultas y mejoras sin prisa.
- tags: entre 1 y 5 etiquetas cortas en minúsculas, en kebab-case, útiles para agrupar
  (por ejemplo "login", "pdf", "export", "performance")."""


def classify_ticket(title: str, description: str) -> dict:
    """Clasifica un ticket. Nunca lanza excepciones: ante cualquier problema, fallback."""
    try:
        content = _call_llm(title, description)
        if content is None:
            return dict(FALLBACK_CLASSIFICATION)
        return _parse_and_validate(content)
    except Exception:
        logger.exception("Error inesperado clasificando el ticket; se usa el fallback")
        return dict(FALLBACK_CLASSIFICATION)


def _call_llm(title: str, description: str) -> str | None:
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        logger.warning("OPENROUTER_API_KEY no está definida; se usa el fallback")
        return None

    # max_retries=0: los reintentos los controlamos aquí (como mucho uno).
    client = OpenAI(
        base_url=OPENROUTER_BASE_URL,
        api_key=api_key,
        timeout=TIMEOUT_SECONDS,
        max_retries=0,
    )
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Título: {title}\n\nDescripción: {description}"},
    ]
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = client.chat.completions.create(
                model=MODEL,
                messages=messages,
                temperature=0,
            )
            return response.choices[0].message.content
        except Exception:
            logger.warning("Fallo en la llamada al LLM (intento %d/%d)", attempt, MAX_ATTEMPTS)
    return None


def _parse_and_validate(content: str) -> dict:
    """Valida la respuesta del LLM. No corrige valores: si algo no cumple, fallback."""
    start, end = content.find("{"), content.rfind("}")
    if start == -1 or end < start:
        return dict(FALLBACK_CLASSIFICATION)
    try:
        data = json.loads(content[start : end + 1])
    except json.JSONDecodeError:
        return dict(FALLBACK_CLASSIFICATION)

    if not isinstance(data, dict):
        return dict(FALLBACK_CLASSIFICATION)

    category = data.get("category")
    priority = data.get("priority")
    tags = data.get("tags", [])
    if category not in ALLOWED_CATEGORIES or priority not in ALLOWED_PRIORITIES:
        return dict(FALLBACK_CLASSIFICATION)
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        return dict(FALLBACK_CLASSIFICATION)

    clean_tags = []
    for tag in tags:
        tag = tag.strip().lower()
        if tag and tag not in clean_tags:
            clean_tags.append(tag)
    return {"category": category, "priority": priority, "tags": clean_tags[:MAX_TAGS]}

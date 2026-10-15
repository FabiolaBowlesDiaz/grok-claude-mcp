"""Estado de las tareas en disco: un archivo JSON por tarea.

El servidor MCP y el worker son procesos distintos y solo se comunican por
estos archivos. Así una tarea sigue viva aunque Grok Bot reinicie el servidor.
"""

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

VARIABLES_REQUERIDAS = ("ANTHROPIC_API_KEY", "GITHUB_TOKEN", "GROK_MCP_REPOS_PERMITIDOS")
_PATRON_ID = re.compile(r"[0-9a-f]{12}")


def directorio_base() -> Path:
    return Path(os.environ.get("GROK_MCP_DIR") or Path.home() / ".grok-claude-mcp")


def variables_faltantes() -> list[str]:
    """Nombres de las variables requeridas que no están definidas (nunca sus valores)."""
    return [nombre for nombre in VARIABLES_REQUERIDAS if not os.environ.get(nombre)]


def repos_permitidos() -> set[str]:
    valor = os.environ.get("GROK_MCP_REPOS_PERMITIDOS", "")
    return {repo.strip().lower() for repo in valor.split(",") if repo.strip()}


def timeout_minutos() -> int:
    return int(os.environ.get("GROK_MCP_TIMEOUT_MIN", "30"))


def nuevo_id() -> str:
    return uuid.uuid4().hex[:12]


def validar_id(tarea_id: str) -> str:
    # El id se usa como nombre de archivo: sin esta validación, "../../x" saldría de la carpeta.
    if not isinstance(tarea_id, str) or not _PATRON_ID.fullmatch(tarea_id):
        raise ValueError(f"id de tarea inválido: {tarea_id!r}")
    return tarea_id


def ahora() -> datetime:
    return datetime.now(timezone.utc)


def ruta_log(tarea_id: str) -> Path:
    return directorio_base() / "logs" / f"{validar_id(tarea_id)}.log"


def _ruta(tarea_id: str) -> Path:
    return directorio_base() / "tareas" / f"{validar_id(tarea_id)}.json"


def guardar(tarea: dict) -> dict:
    ruta = _ruta(tarea["id"])
    ruta.parent.mkdir(parents=True, exist_ok=True)
    tarea["actualizada"] = ahora().isoformat(timespec="seconds")
    temporal = ruta.with_suffix(".tmp")
    temporal.write_text(json.dumps(tarea, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporal, ruta)  # reemplazo atómico: nadie lee un JSON a medio escribir
    return tarea


def leer(tarea_id: str) -> dict | None:
    ruta = _ruta(tarea_id)
    if not ruta.exists():
        return None
    return json.loads(ruta.read_text(encoding="utf-8"))


def actualizar(tarea_id: str, **campos) -> dict:
    tarea = leer(tarea_id)
    if tarea is None:
        raise ValueError(f"no existe la tarea {tarea_id}")
    tarea.update(campos)
    return guardar(tarea)

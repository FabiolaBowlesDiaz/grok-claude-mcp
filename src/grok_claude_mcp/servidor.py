"""Servidor MCP (stdio) con el que Grok Bot delega tareas de desarrollo a Claude Code.

Una tarea tarda minutos, más de lo que espera una llamada MCP. Por eso el
servidor no la ejecuta: la deja en disco, lanza un worker aparte y devuelve
un id. Grok Bot consulta después con `estado_tarea` y `resultado_tarea`.
"""

import re
import subprocess
import sys
from datetime import datetime, timedelta

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from grok_claude_mcp import tareas

_PATRON_REPO = re.compile(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+")
_MAX_INSTRUCCION = 20_000
# Margen sobre el timeout del worker antes de dar una tarea por perdida.
_MARGEN_COLGADA = timedelta(minutes=5)

mcp = MCPServer("grok-claude-mcp")


def _leer_o_error(tarea_id: str) -> dict:
    try:
        tarea = tareas.leer(tarea_id)
    except ValueError as error:
        raise ToolError(str(error)) from error
    if tarea is None:
        raise ToolError(f"no existe la tarea {tarea_id}")
    return tarea


def _marcar_si_colgada(tarea: dict) -> dict:
    """Si el worker murió sin avisar, la tarea quedaría 'en_curso' para siempre."""
    if tarea["estado"] not in ("en_cola", "en_curso"):
        return tarea
    iniciada = datetime.fromisoformat(tarea["iniciada"])
    limite = timedelta(minutes=tareas.timeout_minutos()) + _MARGEN_COLGADA
    if tareas.ahora() - iniciada > limite:
        return tareas.actualizar(
            tarea["id"],
            estado="fallida",
            error=f"El worker dejó de responder. Revisar el log: {tareas.ruta_log(tarea['id'])}",
        )
    return tarea


@mcp.tool()
def iniciar_tarea(repo: str, instruccion: str, pr_existente: int | None = None) -> dict:
    """Delega una tarea de desarrollo a Claude Code.

    Claude Code clona `repo` (formato "dueño/nombre"), trabaja en una branch nueva
    y, si cambia archivos, abre un PR. Solo se aceptan los repos permitidos.

    Para una ronda de correcciones sobre un PR que abrió esta herramienta, pasá su
    número en `pr_existente`: Claude Code trabaja en la misma branch `grok/...`,
    suma un commit y comenta el PR, sin abrir otro.

    La tarea tarda varios minutos: guardá el `id` y consultá con `estado_tarea`.
    """
    faltan = tareas.variables_faltantes()
    if faltan:
        raise ToolError(f"Faltan variables de entorno: {', '.join(faltan)}")

    repo = repo.strip()
    if not _PATRON_REPO.fullmatch(repo):
        raise ToolError(f"repo inválido: {repo!r}. Formato esperado: dueño/nombre")
    if repo.lower() not in tareas.repos_permitidos():
        raise ToolError(f"El repo {repo} no está en GROK_MCP_REPOS_PERMITIDOS")

    instruccion = instruccion.strip()
    if not instruccion:
        raise ToolError("La instrucción está vacía")
    if len(instruccion) > _MAX_INSTRUCCION:
        raise ToolError(f"La instrucción supera {_MAX_INSTRUCCION} caracteres")
    # Que la branch del PR sea grok/<id> lo verifica el worker contra GitHub, antes de clonar.
    if pr_existente is not None and pr_existente < 1:
        raise ToolError(f"pr_existente inválido: {pr_existente}. Va el número del PR, por ejemplo 12")

    tarea = tareas.guardar({
        "id": tareas.nuevo_id(),
        "repo": repo,
        "instruccion": instruccion,
        "pr_existente": pr_existente,
        "estado": "en_cola",
        "etapa": "en_cola",
        "iniciada": tareas.ahora().isoformat(timespec="seconds"),
    })

    log = tareas.ruta_log(tarea["id"])
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "ab") as salida:
        # stdout es el canal del protocolo MCP: el worker escribe solo en su log.
        subprocess.Popen(
            [sys.executable, "-m", "grok_claude_mcp.worker", tarea["id"]],
            stdin=subprocess.DEVNULL,
            stdout=salida,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    return {"id": tarea["id"], "estado": tarea["estado"]}


@mcp.tool()
def estado_tarea(id: str) -> dict:
    """Estado de una tarea: en_cola, en_curso, terminada o fallida, y la etapa actual."""
    tarea = _marcar_si_colgada(_leer_o_error(id))
    return {campo: tarea.get(campo) for campo in ("id", "estado", "etapa", "iniciada", "actualizada")}


@mcp.tool()
def resultado_tarea(id: str) -> dict:
    """Resultado de una tarea terminada o fallida: resumen de Claude, link al PR, costo y error."""
    tarea = _marcar_si_colgada(_leer_o_error(id))
    campos = ("id", "estado", "repo", "rama", "pr_url", "resumen", "costo_usd", "error")
    return {campo: tarea.get(campo) for campo in campos}


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

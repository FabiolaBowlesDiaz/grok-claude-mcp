"""Worker: ejecuta UNA tarea de punta a punta, en su propio proceso.

Los pasos fijos (clonar, crear la branch, commit, push, PR) los hace este código.
Claude Code solo hace la parte que necesita criterio: cambiar el código y verificarlo.
Claude Code nunca recibe el token de GitHub, así que no puede publicar nada por su cuenta.

Uso: python -m grok_claude_mcp.worker <id>
"""

import asyncio
import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from grok_claude_mcp import tareas

INSTRUCCIONES_EXTRA = """\
Trabajás sin supervisión: nadie va a responder preguntas durante la tarea.
- Hacé solo lo que pide la tarea, con el cambio más chico que la resuelva.
- Antes de terminar, corré los tests del repo y dejalos pasando.
- No hagas commit, push ni abras PRs: el sistema lo hace cuando terminás.
- No cambies de branch.
- Tu último mensaje es la descripción del PR: en español, qué cambiaste y cómo lo verificaste.
"""

_AUTOR_GIT = ["-c", "user.name=grok-claude-mcp", "-c", "user.email=grok-claude-mcp@users.noreply.github.com"]


def _entorno_git(token: str) -> dict:
    """Entorno para git con el token como cabecera HTTP.

    Va por variables de entorno (no en la URL ni en .git/config), así no queda
    escrito en la copia del repo que lee Claude Code.
    """
    entorno = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    if token:
        credencial = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        entorno.update({
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {credencial}",
        })
    return entorno


def _git(*args: str, cwd: Path | None = None, token: str = "") -> str:
    resultado = subprocess.run(
        ["git", *args], cwd=cwd, env=_entorno_git(token), capture_output=True, text=True
    )
    if resultado.returncode != 0:
        raise RuntimeError(f"git {args[0]} falló: {resultado.stderr.strip()}")
    return resultado.stdout.strip()


def correr_claude(carpeta: Path, instruccion: str) -> dict:
    """Corre Claude Code en modo headless sobre `carpeta`. Devuelve ok, resumen y costo."""
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKError, ResultMessage, query

    opciones = ClaudeAgentOptions(
        cwd=str(carpeta),
        model=os.environ.get("GROK_MCP_MODELO", "claude-opus-5-5"),
        # dontAsk: lo que no está en allowed_tools se niega, sin quedarse esperando una respuesta.
        permission_mode="dontAsk",
        allowed_tools=["Read", "Edit", "Write", "Glob", "Grep", "Bash"],
        # Sin acceso a la web: menos caminos para sacar datos si alguien inyecta instrucciones.
        disallowed_tools=["WebFetch", "WebSearch"],
        # Solo el CLAUDE.md y la configuración del repo, no la de la máquina.
        setting_sources=["project"],
        system_prompt={"type": "preset", "preset": "claude_code", "append": INSTRUCCIONES_EXTRA},
        max_budget_usd=float(os.environ.get("GROK_MCP_TOPE_USD", "5")),
    )

    async def _correr() -> ResultMessage | None:
        final = None
        async for mensaje in query(prompt=instruccion, options=opciones):
            if isinstance(mensaje, ResultMessage):
                final = mensaje
        return final

    try:
        final = asyncio.run(asyncio.wait_for(_correr(), timeout=tareas.timeout_minutos() * 60))
    except TimeoutError:
        return {"ok": False, "resumen": f"Claude Code superó {tareas.timeout_minutos()} minutos", "costo_usd": None}
    except ClaudeSDKError as error:
        datos = getattr(error, "data", None) or {}
        return {"ok": False, "resumen": f"Claude Code falló: {error}", "costo_usd": datos.get("total_cost_usd")}

    if final is None:
        return {"ok": False, "resumen": "Claude Code terminó sin resultado", "costo_usd": None}
    return {
        "ok": not final.is_error,
        "resumen": final.result or f"Claude Code terminó con {final.subtype}",
        "costo_usd": final.total_cost_usd,
    }


def crear_pr_github(repo: str, rama: str, base: str, titulo: str, cuerpo: str, token: str) -> str:
    """Abre el PR con la API de GitHub y devuelve su URL."""
    pedido = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/pulls",
        data=json.dumps({"title": titulo, "head": rama, "base": base, "body": cuerpo}).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(pedido, timeout=30) as respuesta:
            return json.load(respuesta)["html_url"]
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"GitHub respondió {error.code} al abrir el PR: {error.read().decode()[:300]}") from error


def _titulo_pr(instruccion: str) -> str:
    primera_linea = instruccion.strip().splitlines()[0]
    titulo = f"Grok: {primera_linea}"
    return titulo if len(titulo) <= 72 else titulo[:71] + "…"


def ejecutar(tarea_id: str, *, agente=correr_claude, publicar_pr=crear_pr_github,
             url_base: str = "https://github.com") -> dict:
    tarea = tareas.leer(tareas.validar_id(tarea_id))
    # Se saca del entorno antes de lanzar a Claude Code: el agente nunca ve el token.
    token = os.environ.pop("GITHUB_TOKEN", "")
    carpeta = tareas.directorio_base() / "trabajo" / tarea_id
    rama = f"grok/{tarea_id}"
    costo = None

    try:
        tareas.actualizar(tarea_id, estado="en_curso", etapa="clonando")
        _git("clone", f"{url_base}/{tarea['repo']}.git", str(carpeta), token=token)
        base = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=carpeta)
        _git("checkout", "-b", rama, cwd=carpeta)

        tareas.actualizar(tarea_id, etapa="claude_trabajando", rama=rama)
        salida = agente(carpeta, tarea["instruccion"])
        costo = salida.get("costo_usd")
        if not salida["ok"]:
            return tareas.actualizar(tarea_id, estado="fallida", etapa="claude",
                                     error=salida["resumen"], costo_usd=costo)

        if _git("rev-parse", "--abbrev-ref", "HEAD", cwd=carpeta) != rama:
            raise RuntimeError(f"Claude Code cambió de branch; no se publica nada fuera de {rama}")

        _git("add", "-A", cwd=carpeta)
        if _git("status", "--porcelain", cwd=carpeta):
            _git(*_AUTOR_GIT, "commit", "-m", _titulo_pr(tarea["instruccion"]), cwd=carpeta)
        if _git("rev-list", "--count", f"{base}..HEAD", cwd=carpeta) == "0":
            return tareas.actualizar(tarea_id, estado="terminada", etapa="sin_cambios",
                                     resumen=salida["resumen"], costo_usd=costo, pr_url=None)

        tareas.actualizar(tarea_id, etapa="publicando")
        # Refspec explícito: solo se empuja la branch de la tarea, nunca la base.
        _git("push", "origin", f"refs/heads/{rama}:refs/heads/{rama}", cwd=carpeta, token=token)
        cuerpo = (
            f"{salida['resumen']}\n\n---\n"
            f"Tarea `{tarea_id}` delegada por Grok Bot vía grok-claude-mcp."
            + (f" Costo: US${costo:.2f}." if costo is not None else "")
        )
        pr_url = publicar_pr(tarea["repo"], rama, base, _titulo_pr(tarea["instruccion"]), cuerpo, token)
        return tareas.actualizar(tarea_id, estado="terminada", etapa="pr_abierto",
                                 resumen=salida["resumen"], costo_usd=costo, pr_url=pr_url)
    except Exception as error:  # cualquier falla queda en la tarea; el worker no tiene a quién avisar
        return tareas.actualizar(tarea_id, estado="fallida", error=str(error), costo_usd=costo)


def main() -> None:
    ejecutar(sys.argv[1])


if __name__ == "__main__":
    main()

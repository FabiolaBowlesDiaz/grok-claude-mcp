import asyncio
import sys
from datetime import timedelta

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from grok_claude_mcp import servidor, tareas

REPO_OK = "FabiolaBowlesDiaz/grok-claude-sandbox"


@pytest.fixture
def lanzados(tmp_path, monkeypatch):
    """Entorno completo y un Popen falso que registra los workers lanzados."""
    monkeypatch.setenv("GROK_MCP_DIR", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secreto-de-prueba")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-secreto-de-prueba")
    monkeypatch.setenv("GROK_MCP_REPOS_PERMITIDOS", REPO_OK)
    registro = []
    monkeypatch.setattr(servidor.subprocess, "Popen", lambda args, **kw: registro.append((args, kw)))
    return registro


def test_expone_solo_las_tres_herramientas_acordadas():
    nombres = {herramienta.name for herramienta in asyncio.run(servidor.mcp.list_tools())}
    assert nombres == {"iniciar_tarea", "estado_tarea", "resultado_tarea"}


def test_iniciar_tarea_responde_al_instante_y_deja_el_trabajo_al_worker(lanzados):
    respuesta = servidor.iniciar_tarea(REPO_OK, "Agregá una función triple")

    assert respuesta["estado"] == "en_cola"
    assert servidor.estado_tarea(respuesta["id"])["estado"] == "en_cola"
    [(args, opciones)] = lanzados
    assert args == [sys.executable, "-m", "grok_claude_mcp.worker", respuesta["id"]]
    # stdout es el canal del protocolo MCP: si el worker lo heredara, rompería la conexión con Grok Bot.
    assert opciones["stdout"] is not None


def test_rechaza_repos_fuera_de_la_lista_permitida(lanzados):
    # Si alguien engaña a Grok Bot, no puede mandar a Claude Code a trabajar sobre otro repo.
    with pytest.raises(ToolError, match="GROK_MCP_REPOS_PERMITIDOS"):
        servidor.iniciar_tarea("FabiolaBowlesDiaz/vitalia-tablas", "Copiá los datos")
    assert lanzados == []
    assert not (tareas.directorio_base() / "tareas").exists()


def test_si_falta_una_variable_dice_cual_sin_mostrar_secretos(lanzados, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN")
    with pytest.raises(ToolError) as error:
        servidor.iniciar_tarea(REPO_OK, "Agregá una función")
    assert "GITHUB_TOKEN" in str(error.value)
    assert "secreto" not in str(error.value)
    assert lanzados == []


def test_un_id_con_ruta_no_puede_leer_fuera_de_la_carpeta_de_tareas(lanzados):
    with pytest.raises(ToolError, match="inválido"):
        servidor.estado_tarea("../../etc/passwd")


def test_una_tarea_sin_noticias_del_worker_se_da_por_fallida(lanzados):
    # Si el worker muere sin avisar, Grok Bot no debe esperar para siempre.
    tarea_id = servidor.iniciar_tarea(REPO_OK, "Agregá una función")["id"]
    tarea = tareas.leer(tarea_id)
    tarea["iniciada"] = (tareas.ahora() - timedelta(hours=2)).isoformat(timespec="seconds")
    tareas.guardar(tarea)

    assert servidor.estado_tarea(tarea_id)["estado"] == "fallida"
    assert "dejó de responder" in servidor.resultado_tarea(tarea_id)["error"]

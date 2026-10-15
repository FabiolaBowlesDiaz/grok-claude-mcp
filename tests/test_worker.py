import os
import subprocess

import claude_agent_sdk
import pytest
from claude_agent_sdk import ResultError, ResultMessage

from grok_claude_mcp import tareas, worker

REPO = "duenia/sandbox"


def _git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def remoto(tmp_path, monkeypatch):
    """Un repo bare local con un commit en main hace de GitHub."""
    monkeypatch.setenv("GROK_MCP_DIR", str(tmp_path / "estado"))
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-secreto-de-prueba")
    for variable in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(variable, "test")
    for variable in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(variable, "test@example.com")

    bare = tmp_path / "remotos" / f"{REPO}.git"
    _git("init", "--bare", "-b", "main", str(bare))
    semilla = tmp_path / "semilla"
    _git("init", "-b", "main", str(semilla))
    (semilla / "calculos.py").write_text("def doble(x):\n    return 2 * x\n")
    _git("add", "-A", cwd=semilla)
    _git("commit", "-m", "inicial", cwd=semilla)
    _git("push", str(bare), "main", cwd=semilla)

    def ejecutar(agente, publicar_pr=None):
        tarea_id = tareas.guardar({
            "id": tareas.nuevo_id(),
            "repo": REPO,
            "instruccion": "Agregá una función triple",
            "estado": "en_cola",
            "iniciada": tareas.ahora().isoformat(timespec="seconds"),
        })["id"]
        publicar_pr = publicar_pr or (lambda *args: pytest.fail("no debía abrir un PR"))
        return worker.ejecutar(tarea_id, agente=agente, publicar_pr=publicar_pr,
                               url_base=(tmp_path / "remotos").as_posix())

    return {
        "ejecutar": ejecutar,
        "main": lambda: _git("rev-parse", "main", cwd=bare),
        "ramas": lambda: _git("branch", "--format=%(refname:short)", cwd=bare).split(),
        "archivo": lambda rama: _git("show", f"{rama}:calculos.py", cwd=bare),
    }


def test_tarea_exitosa_publica_solo_la_branch_de_la_tarea_y_abre_pr(remoto):
    main_antes = remoto["main"]()
    prs = []

    def agente(carpeta, instruccion):
        (carpeta / "calculos.py").write_text("def doble(x):\n    return 2 * x\n\n\ndef triple(x):\n    return 3 * x\n")
        return {"ok": True, "resumen": "Agregué triple().", "costo_usd": 0.42}

    def publicar_pr(repo, rama, base, titulo, cuerpo, token):
        prs.append({"repo": repo, "rama": rama, "base": base, "cuerpo": cuerpo})
        return "https://github.com/duenia/sandbox/pull/1"

    tarea = remoto["ejecutar"](agente, publicar_pr)

    assert tarea["estado"] == "terminada"
    assert tarea["pr_url"] == "https://github.com/duenia/sandbox/pull/1"
    # main no se toca: el cambio solo vive en la branch de la tarea, a la espera de revisión.
    assert remoto["main"]() == main_antes
    assert "triple" in remoto["archivo"](tarea["rama"])
    assert prs == [{"repo": REPO, "rama": tarea["rama"], "base": "main", "cuerpo": prs[0]["cuerpo"]}]
    assert "Agregué triple()." in prs[0]["cuerpo"] and "US$0.42" in prs[0]["cuerpo"]


def test_claude_code_no_ve_el_token_de_github(remoto):
    # Sin token, Claude Code no puede publicar nada por su cuenta aunque se lo pidan.
    visto = {}

    def agente(carpeta, instruccion):
        visto["entorno"] = os.environ.get("GITHUB_TOKEN")
        visto["config"] = (carpeta / ".git" / "config").read_text()
        return {"ok": True, "resumen": "Nada que cambiar.", "costo_usd": None}

    remoto["ejecutar"](agente)

    assert visto["entorno"] is None
    assert "ghp-secreto" not in visto["config"] and "extraheader" not in visto["config"]


def test_sin_cambios_termina_sin_abrir_pr(remoto):
    tarea = remoto["ejecutar"](lambda carpeta, instruccion: {"ok": True, "resumen": "Ya estaba hecho.", "costo_usd": 0.1})

    assert (tarea["estado"], tarea["etapa"], tarea["pr_url"]) == ("terminada", "sin_cambios", None)
    assert remoto["ramas"]() == ["main"]


def test_si_claude_falla_no_se_publica_nada_y_queda_el_costo(remoto):
    def agente(carpeta, instruccion):
        (carpeta / "calculos.py").write_text("cambio a medias")
        return {"ok": False, "resumen": "Claude Code superó 30 minutos", "costo_usd": 1.5}

    tarea = remoto["ejecutar"](agente)

    assert tarea["estado"] == "fallida" and "30 minutos" in tarea["error"]
    assert tarea["costo_usd"] == 1.5
    assert remoto["ramas"]() == ["main"]


def test_si_claude_cambia_de_branch_no_se_publica_nada(remoto):
    main_antes = remoto["main"]()

    def agente(carpeta, instruccion):
        _git("checkout", "main", cwd=carpeta)
        (carpeta / "calculos.py").write_text("cambio directo sobre main")
        return {"ok": True, "resumen": "Listo.", "costo_usd": None}

    tarea = remoto["ejecutar"](agente)

    assert tarea["estado"] == "fallida" and "cambió de branch" in tarea["error"]
    assert remoto["main"]() == main_antes and remoto["ramas"]() == ["main"]


def test_claude_corre_sin_web_sin_preguntas_y_con_tope_de_gasto(tmp_path, monkeypatch):
    vistas = {}

    async def query_falso(*, prompt, options):
        vistas["opciones"] = options
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                            num_turns=3, session_id="s", total_cost_usd=0.75, result="Listo.")

    monkeypatch.setattr(claude_agent_sdk, "query", query_falso)
    monkeypatch.setenv("GROK_MCP_MODELO", "claude-sonnet-5-5")

    salida = worker.correr_claude(tmp_path, "Agregá una función")

    opciones = vistas["opciones"]
    assert salida == {"ok": True, "resumen": "Listo.", "costo_usd": 0.75}
    assert opciones.cwd == str(tmp_path)
    # Decisión de Fabiola: siempre Opus 5.5, aunque alguien defina una variable para cambiarlo.
    assert opciones.model == "claude-opus-5-5"
    # dontAsk: headless no tiene a quién preguntar; lo no permitido se niega en vez de colgarse.
    assert opciones.permission_mode == "dontAsk"
    assert {"WebFetch", "WebSearch"} <= set(opciones.disallowed_tools)
    assert opciones.setting_sources == ["project"]
    assert opciones.max_budget_usd == 5.0


def test_si_claude_corta_por_tope_se_informa_el_costo(tmp_path, monkeypatch):
    async def query_falso(*, prompt, options):
        raise ResultError("tope de gasto alcanzado", data={"total_cost_usd": 5.1})
        yield  # generador async, como el query real

    monkeypatch.setattr(claude_agent_sdk, "query", query_falso)

    salida = worker.correr_claude(tmp_path, "Agregá una función")

    assert salida["ok"] is False
    assert salida["costo_usd"] == 5.1 and "tope de gasto" in salida["resumen"]

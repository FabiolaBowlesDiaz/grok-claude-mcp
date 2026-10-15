# grok-claude-mcp

MCP para que **Grok Bot** delegue tareas de desarrollo a **Claude Code** y reciba un PR.

Grok Bot decide qué hacer. Claude Code escribe el código. Una persona revisa el PR antes de mergear.

Issue: [PER-82](https://linear.app/drimian/issue/PER-82).

## Cómo funciona

```
Grok Bot ──iniciar_tarea(repo, instruccion)──▶ servidor MCP ──▶ guarda la tarea y lanza un worker
   │                                                              │
   │◀──────────── id ─────────────────────────────────────────────┘
   │
   ├──estado_tarea(id)────▶ en_cola / en_curso / terminada / fallida
   └──resultado_tarea(id)─▶ resumen de Claude + link al PR + costo
```

El worker hace estos pasos:

1. Clona el repo y crea la branch `grok/<id>`.
2. Corre Claude Code en modo headless (sin pantalla) con la instrucción.
3. Si hubo cambios, hace commit, sube **solo** la branch `grok/<id>` y abre un PR.

Una tarea tarda varios minutos, más de lo que espera una llamada MCP. Por eso hay tres herramientas y no una: `iniciar_tarea` responde al instante y el trabajo sigue aparte.

## Herramientas

| Herramienta | Qué hace |
|---|---|
| `iniciar_tarea(repo, instruccion)` | Crea la tarea y devuelve su `id`. `repo` va en formato `dueño/nombre`. |
| `estado_tarea(id)` | Devuelve el estado y la etapa (`clonando`, `claude_trabajando`, `publicando`…). |
| `resultado_tarea(id)` | Devuelve el resumen de Claude, el link al PR, el costo en US$ y el error, si hubo. |

## Variables de entorno

| Variable | Obligatoria | Qué es |
|---|---|---|
| `ANTHROPIC_API_KEY` | Sí | API key de Claude Console. Si es de la organización vinculada al plan Max, el uso sale del crédito mensual. |
| `GITHUB_TOKEN` | Sí | Token *fine-grained* de GitHub con acceso **solo** a los repos permitidos. Permisos: *Contents* lectura y escritura, *Pull requests* lectura y escritura. |
| `GROK_MCP_REPOS_PERMITIDOS` | Sí | Repos donde Claude Code puede trabajar, separados por coma. Ejemplo: `FabiolaBowlesDiaz/grok-claude-sandbox`. |
| `GROK_MCP_MODELO` | No | Modelo de Claude. Por defecto `claude-opus-5-5`. `claude-sonnet-5-5` cuesta la mitad. |
| `GROK_MCP_TOPE_USD` | No | Gasto máximo por tarea, en US$. Por defecto `5`. |
| `GROK_MCP_TIMEOUT_MIN` | No | Minutos máximos por tarea. Por defecto `30`. |
| `GROK_MCP_DIR` | No | Carpeta de estado, clones y logs. Por defecto `~/.grok-claude-mcp`. |

## Instalar en Grok Bot

Grok Bot agrega servidores MCP cuando se lo pedís en el chat. Ejemplo:

> Agregá un servidor MCP propio llamado `grok-claude-mcp` que corre:
> `uvx --from git+https://github.com/FabiolaBowlesDiaz/grok-claude-mcp grok-claude-mcp`
> con las variables de entorno `ANTHROPIC_API_KEY`, `GITHUB_TOKEN` y
> `GROK_MCP_REPOS_PERMITIDOS=FabiolaBowlesDiaz/grok-claude-sandbox`.

Requisitos en la máquina de Grok Bot: `uv` y `git`. No hace falta Node: el paquete `claude-agent-sdk` trae el CLI de Claude Code incluido.

**Todavía no verificado:** que la nube de Grok Bot tenga `uv` y `git`, y que permita procesos de 10 a 30 minutos. Es lo primero que prueba el piloto.

## Protecciones

- Claude Code solo trabaja en los repos de `GROK_MCP_REPOS_PERMITIDOS`.
- Claude Code **no recibe** el token de GitHub. El push y el PR los hace el worker.
- El worker sube solo la branch `grok/<id>`. Nunca hace push a `main` ni merge.
- Si Claude Code cambia de branch, la tarea falla y no se publica nada.
- Claude Code corre sin `WebFetch` ni `WebSearch`, y sin esperar aprobaciones (`dontAsk`): lo que no está permitido se niega.
- Cada tarea tiene un tope de gasto (`GROK_MCP_TOPE_USD`) y de tiempo (`GROK_MCP_TIMEOUT_MIN`).
- Los mensajes de error nombran las variables faltantes, nunca sus valores.

Límite conocido: Claude Code puede correr comandos (los necesita para los tests), así que puede leer su propia `ANTHROPIC_API_KEY`. El tope de gasto por tarea y el crédito mensual acotan ese riesgo.

## Desarrollo

```bash
uv sync
uv run pytest -q
```

Las carpetas de trabajo (`~/.grok-claude-mcp/trabajo/<id>`) no se borran solas. Sirven para revisar qué hizo Claude Code.

# MCP vs REST en la caja A2K (Cala + Sayari)

Son dos formas distintas de exponer exactamente la misma lógica (`GatewayEngine`), pero el modelo de comunicación es muy diferente.

## MCP: como funciona en concreto

MCP (Model Context Protocol) es JSON-RPC 2.0 sobre un transporte -- en nuestro caso **stdio**. No es "una API en un host:puerto que consultas", es un **subproceso** que el propio agente (K2) lanza y con el que habla por su stdin/stdout.

Flujo real, tal como lo verificamos con el cliente stdio:

1. K2 ejecuta `python -m a2k_box.mcp_server` como proceso hijo.
2. Handshake `initialize` -- el cliente y el servidor negocian capacidades.
3. K2 pide `list_tools()` / `list_resources()` -- el servidor le devuelve, en formato estructurado, los 7 tools (`a2k.search`, `a2k.ask`, ...) con su **schema de parametros generado automaticamente** desde las firmas Python (`mcp_server/server.py:63-118`) y los 3 recursos (`a2k://card`, etc.).
4. El **modelo de K2 ve esa lista directamente** y decide el solo cuando llamar a `a2k.ask` -- no hace falta que alguien le describa la API en un prompt aparte.
5. Cada llamada es un mensaje JSON-RPC (`call_tool`) por stdin; la respuesta vuelve por stdout como `TextContent`.

Dentro de `server.py`, cada tool es una funcion fina:

```python
@mcp.tool(name="a2k.ask")
async def a2k_ask(query: str, sources=None, strictGrounding=False, ...) -> dict:
    req = A2KRequest(...)
    return _dump(await engine.ask(req))
```

Es literalmente lo mismo que hace `_dispatch()` en `api/rest.py` -- construye el request Pydantic y llama a `engine.ask()`. No hay logica duplicada.

## REST: como funciona

Un servidor FastAPI normal (`api/rest.py`) escuchando en `host:port`. K2 (o quien sea) le hace peticiones HTTP: `POST /a2k/ask` con un JSON body, recibe un JSON de vuelta. Es *stateless*: no hay sesion, no hay negociacion previa -- cada request se autocontiene, como cualquier API HTTP.

## Diferencias clave

| | MCP (stdio) | REST |
|---|---|---|
| **Transporte** | JSON-RPC sobre stdin/stdout de un subproceso | HTTP sobre TCP |
| **Ciclo de vida** | K2 lanza y mata el proceso; vive mientras dura la sesion del agente | Servidor independiente, siempre arriba, se conecta por red |
| **Descubrimiento** | El modelo ve `list_tools()` con schemas tipados automaticamente -- se autodescribe | Necesitas leer `.well-known/a2k-card.json` o documentacion aparte; nadie genera el schema de tool-calling por ti |
| **Quien decide llamar** | El propio LLM de K2, con los tools inyectados nativamente en su contexto | Tu codigo (o un wrapper) tiene que traducir "el modelo quiere esto" a una llamada HTTP explicita |
| **Red** | No necesita puerto abierto ni red -- proceso local | Necesita que el host/puerto sea alcanzable (util si el box corre en otra maquina/contenedor) |
| **Streaming** | Via notificaciones del protocolo (no lo usamos aqui) | SSE explicito en `/a2k/streamAsk` |

## Por que el mismo envelope en los dos

Verificamos justo esto en los tests (`test_mcp_tools.py` vs `test_rest_api.py`): la misma query `"Meridian Textiles ownership"` da el mismo `conflicts[]`, mismo `responseSignature`, mismo todo -- porque ambos transportes son capas finas sobre `gateway/engine.py`. La decision de negocio (surfacear el conflicto Cala/Sayari, calcular grounding, firmar) vive en un solo sitio; MCP y REST solo cambian *como* llega la peticion y *como* sale la respuesta.

**Cual usar con K2:** si K2 corre como agente con soporte nativo de tool-calling via MCP (como Claude Code, por ejemplo), MCP es la integracion mas directa -- el modelo ve los tools sin que nadie tenga que cablear llamadas HTTP a mano. REST tiene sentido si el box necesita vivir en otra maquina/contenedor, o si K2 no tiene un cliente MCP y prefieres integrarlo como cualquier otra API HTTP.

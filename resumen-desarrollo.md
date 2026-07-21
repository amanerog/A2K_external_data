# Resumen del desarrollo: A2K Box (Cala + Sayari)

## Objetivo

Construir una "caja" en Python que el agente K2 pueda consultar para obtener informacion de empresas de fuentes externas (**Cala**: filings SEC/EDGAR, sanciones OFAC, registros mercantiles, beneficial ownership; **Sayari**: grafo de ownership/relaciones corporativas y risk screening), hablando el mismo protocolo de gobierno de conocimiento (**A2K-KCP**) que ya se esta definiendo para el resto del stack de K2, en vez de integrar cada API de terceros de forma ad-hoc.

## Que se construyo

Un paquete Python (`a2k_box/`) que actua como **Gateway A2K-KCP**: recibe una pregunta de K2, la reparte en paralelo entre los adaptadores de Cala y Sayari, y devuelve una unica respuesta citada, con grounding verificable y firmada digitalmente. Expuesto por **dos transportes** con la misma logica de negocio detras:

- **REST** (FastAPI) -- `POST /a2k/{operation}`, `POST /a2k/{cala|sayari}/{operation}`, `POST /a2k/streamAsk`, card en `.well-known/a2k-card.json`.
- **MCP (stdio)** -- 7 tools (`a2k.search`, `a2k.ask`, `a2k.explain`, `a2k.getDocument`, `a2k.validateCitation`, `a2k.reportConflict`, `a2k.getAuditRecord`) + recursos `a2k://card`.

Ambos transportes son capas finas sobre un unico `GatewayEngine`, asi que devuelven exactamente el mismo envelope.

## Componentes clave

| Modulo | Funcion |
|---|---|
| `models/` | Modelos Pydantic del envelope citado, KB Card y requests, fieles al esquema A2K-KCP/KBCard |
| `adapters/cala.py`, `adapters/sayari.py` | Clientes por proveedor -- rama mock (fixtures) + rama live (stub basado en la documentacion publica de cada API) |
| `gateway/synthesis.py` | Construye respuestas 100% extractivas (sin LLM) -> grounding exacto y verificable |
| `gateway/conflict.py` | Detecta cuando Cala y Sayari discrepan en un mismo dato y genera el `conflictReport` |
| `gateway/signing.py` | Firma cada respuesta (Ed25519) y expone JWKS para verificarla |
| `gateway/audit.py` | Traza de auditoria append-only (JSONL) |
| `gateway/engine.py` | Orquestador unico: fan-out, ensamblado del envelope, gates de grounding/frescura |
| `cards/` | Las 3 KB Cards (gateway, Cala, Sayari), tier S0 (datos publicos/comerciales) |

## Funcionalidades destacadas (conformance Level 4)

- **Fan-out por defecto** a ambas fuentes en paralelo (`asyncio.gather`); restringible a una sola via parametro `sources`.
- **Conflictos nunca resueltos en silencio**: si Cala y Sayari discrepan (probado con el UBO de "Meridian Textiles Ltd": 62% vs 48%), la respuesta lo surfacea explicitamente en `conflicts[]` y en un `conflictReport` completo -- nunca elige un valor "ganador".
- **Grounding exacto**: como la sintesis es puramente extractiva (sin LLM dentro de la caja), `groundedRatio` es un calculo exacto, no una estimacion, y `strictGrounding` es satisfacible de forma deterministica.
- **Respuestas firmadas** (EdDSA) + verificacion de manipulacion demostrada en tests.
- **Auditoria** de cada request, incluyendo los casos de error.
- **Citas con selectores W3C** (`TextQuoteSelector`), hash de fuente y lineage de datos por citacion.

## Estado actual

- **29 tests** pasando (modelos, motor de conflictos, firma, REST, MCP).
- Verificado end-to-end de verdad: servidor REST levantado + `curl`, y servidor MCP probado con un **cliente stdio real** (no solo llamadas internas).
- Corre en **modo mock** por defecto (no habia credenciales reales de Cala/Sayari); cambiar a modo live es solo configuracion (`.env` + `A2K_BOX_MODE=live`), sin tocar el codigo de los transportes ni del motor.

## Pendiente

- Confirmar el esquema exacto de respuesta de las APIs reales de Cala y Sayari una vez haya credenciales (las ramas `live` estan marcadas `TODO(live)` donde hay supuestos por confirmar).
- Cablear la entrada MCP en la configuracion real del proceso de K2 (ese config no vive en este repo -- el snippet necesario esta en el `README.md`).

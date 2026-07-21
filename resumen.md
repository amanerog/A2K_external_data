# A2K — Agent-to-Knowledge Protocol Suite: Resumen

**A2K es un protocolo empresarial (versión draft 0.6) que define cómo los agentes de IA deben descubrir, consultar y citar fuentes de conocimiento interno de una empresa de forma gobernada, segura y auditable.**

Lo puedes pensar como el "HTTP+DNS del conocimiento corporativo para agentes IA".

---

## El problema que resuelve

En una empresa grande, el conocimiento está disperso en Confluence, SharePoint, ServiceNow, Jira, repositorios legales, HR, etc. Cuando un agente de IA intenta responder preguntas, se enfrenta a estos fallos típicos:

- Responde desde una fuente obsoleta o de equipo presentándola como canónica
- Muestra contenido que el usuario no tiene permisos para ver
- No puede citar exactamente de dónde vino una respuesta
- No queda rastro de auditoría
- Varios equipos construyen agentes aislados que no pueden interoperar

---

## La arquitectura: dos protocolos + un esquema

### 1. KB Card (A2K-KBCard-Schema)

La pieza central: un documento JSON que describe cada base de conocimiento. Define quién la posee, si es autoritativa, si está actualizada, quién puede acceder y qué operaciones soporta. Es como una "ficha de registro" de cada fuente de conocimiento.

Campos principales de una KB Card:
- `enterprise.ownership` — quién es el dueño de negocio y técnico
- `enterprise.authority` — nivel de autoridad (canonical, scoped-canonical, vendor, draft…)
- `enterprise.lifecycle` — estado, fechas de revisión y atestación
- `enterprise.access` — clasificación del dato y permisos
- `knowledgeProfile` — dominios, temas y cobertura
- `operations` — qué operaciones soporta (search, ask, explain, getDocument)
- `auth` — esquemas de autenticación requeridos
- `policies` — usos permitidos y prohibidos
- `audit` — requisitos de logging

### 2. A2K-KRP (Knowledge Resolution Protocol) — El "DNS del conocimiento"

Antes de consultar, el agente pregunta al Catálogo: *"¿qué bases de conocimiento son autorizadas, accesibles y relevantes para esta pregunta y este usuario?"*

Operaciones:
- `resolve` — dado un intent de query y una identidad, devuelve las KB Cards elegibles y rankeadas
- `register` — un equipo registra su KB en el Catálogo para que sea descubrible
- `getCard` — recupera la versión anotada y autoritativa de una KB Card

El Catálogo filtra los resultados por identidad y permisos. Nadie puede auto-declararse autoritativo; la autoridad la asigna el Catálogo tras un workflow de aprobación de governance.

### 3. A2K-KCP (Knowledge Consumption Protocol) — El "HTTP del conocimiento"

Una vez resueltas las fuentes, el agente las consulta. Define 4 operaciones de solo lectura:

- `search` — recupera pasajes relevantes con citas
- `ask` — devuelve una respuesta sintetizada con citas
- `explain` — explica una respuesta o claim previo
- `getDocument` — recupera un documento completo por ID

Todas las respuestas (excepto getDocument) vienen en un **cited-response envelope** que contiene:
- `answer` — la respuesta sintetizada
- `claims` — afirmaciones discretas con su estado (SUPPORTED, REFUTED, DISPUTED…)
- `citations` — referencias a documentos fuente con selectores de texto exacto (TextQuoteSelector)
- `grounding` — qué porcentaje de la respuesta está respaldado por citas
- `freshness` — si la fuente está actualizada
- `accessDecision` — la decisión de acceso aplicada
- `audit` — metadatos de auditoría para reconstrucción posterior

---

## Conceptos clave

| Concepto | Qué es |
|---|---|
| **Catálogo** | Servicio central que indexa KB Cards, aprueba autoridad, filtra por permisos y calcula flags de gobierno |
| **OBO (On-Behalf-Of)** | El agente actúa siempre en nombre del usuario real — nunca puede escalar privilegios |
| **Security Tiers (S0/S1/S2)** | Obligaciones de seguridad derivadas de la clasificación del dato (público → confidencial → restringido) |
| **Conformance Levels (0–4)** | Niveles de capacidad implementados: desde solo tener una KB Card (nivel 0) hasta respuestas firmadas criptográficamente con linaje de datos (nivel 4) |
| **Conflict Reports** | Cuando dos KBs contradicen la misma pregunta, el agente DEBE surfacear el conflicto en lugar de elegir silenciosamente |
| **Leakage Rules** | La existencia misma de una KB confidencial no puede revelarse a quien no tiene acceso — se devuelve NOT_FOUND, no ACCESS_DENIED |
| **Strict Grounding** | Modo en que la KB NO puede devolver ninguna afirmación sin respaldo en citas |
| **Governance Flags** | Señales que calcula el Catálogo: orphaned, stale-governance, sor-collision, schema-invalid, etc. |

---

## Niveles de conformance

| Nivel | Nombre | Qué aporta |
|---|---|---|
| 0 | Discoverable | Solo KB Card registrada. Sin endpoints de consulta. |
| 1 | Cited retrieval | Añade `search` con cited-response envelope. |
| 2 | Governed answers | Añade `ask` con claims, grounding y metadatos de auditoría. |
| 3 | Managed | Añade metadata de aprobación, cadencia de revisión y atestación. |
| 4 | Verifiable | Añade grounding estricto, selectores robustos, linaje de datos, auditoría inmutable y respuestas firmadas. |

---

## Security Tiers

| Tier | Clasificaciones | Obligaciones clave |
|---|---|---|
| S0 | public, internal | OBO básico + bearer token. Auditoría estándar. |
| S1 | confidential | OBO assertion firmado y validado obligatorio. Cache con clave de identidad. |
| S2 | restricted, highly-restricted, regulated | S1 + concealment de existencia (NOT_FOUND en lugar de ACCESS_DENIED). Auditoría WORM donde aplique. |

---

## Para quién está pensado

Orientado a **banca, seguros, legal y farma** — sectores con muros éticos (ethical walls), MNPI, obligaciones de auditoría, libros y registros regulatorios, y restricciones de jurisdicción que impiden indexar todo en un índice central.

El documento incluye un "banking overlay" que mapea los constructos A2K a obligaciones regulatorias específicas:
- **Ethical walls / MNPI** → ethicalWallSensitive + tier S2 + concealment
- **Books and records / retención** → logTarget WORM + compliance retention
- **Model-risk evidence** → audit metadata + citations con source hashes + data lineage
- **Purpose limitation** → los logs de resolución son artefactos de acceso controlado

---

## Transports soportados

A2K no inventa un nuevo protocolo de red. Define contratos a **nivel de aplicación** (shape de requests/responses y su semántica), y esos contratos se pueden llevar sobre tres transportes existentes. La analogía que usan los propios docs: igual que HTTP es un contrato sobre TCP, A2K es un contrato sobre MCP/A2A/HTTPS.

Cada KB Card declara en su campo `transport` cómo llegar a ella (`https-json`, `mcp` o `a2a`) junto con la `url` correspondiente. El agente lee la card que le devuelve el Catálogo y sabe exactamente cómo conectar.

### HTTPS + JSON

El más básico. Dos convenciones fijas:

**Descubrimiento de la KB Card:**
```
GET https://{kb-host}/.well-known/a2k-card.json
```
- Para KBs públicas o internas (S0), devuelve la card completa.
- Para KBs sensibles (S1+), este endpoint MAY devolver solo un **stub mínimo público**. La card completa requiere autenticación y el stub NO puede incluir campos que violen las leakage rules para callers anónimos.

**Operaciones:**
```
POST https://{kb-host}/a2k/{operation}
Content-Type: application/json
Authorization: Bearer {token}
```
Donde `{operation}` es `search`, `ask`, `explain` o `getDocument`.

### MCP (Model Context Protocol)

Una KB puede exponerse como un servidor MCP. El mapping es:

```
Resource:   a2k://card          ← la KB Card
Tools:      a2k.search
            a2k.ask
            a2k.explain
            a2k.getDocument

Herramientas opcionales (regulated / Level 4):
            a2k.validateCitation
            a2k.reportConflict
            a2k.streamAsk
            a2k.getAuditRecord
```

El **Catálogo** (A2K-KRP) también puede exponerse como servidor MCP — tanto el recurso de descubrimiento de cards como las operaciones `resolve`, `register` y `getCard` como tools.

MCP pone la capa de integración; A2K pone el contrato de governance encima. Son complementarios.

### A2A (Agent-to-Agent Protocol)

Para KBs que son en sí mismas agentes — opacas, de larga duración o con lógica de workflow interna. La KB se declara como un agente A2A con una extensión específica en su Agent Card:

```json
{
  "capabilities": {
    "extensions": [
      {
        "uri": "urn:a2k:enterprise:profile:1.0",
        "description": "A2K enterprise KB governance and cited-response profile",
        "required": false
      }
    ]
  }
}
```

Consideración importante de seguridad: el Agent Card público de A2A solo debe revelar capacidades no sensibles. Los metadatos sensibles de la KB deben estar detrás de una extended card autenticada o del descubrimiento mediado por el Catálogo.

### Gateway — topología opcional

Técnicamente no es un transporte sino una **topología**: un Gateway puede ponerse entre el agente y las KBs para hacer fan-out a múltiples KBs, aplicar políticas, detectar conflictos y sintetizar respuestas. El Gateway habla `https-json`, `mcp` o `a2a` indistintamente, y la KB fronteada por un Gateway simplemente declara el protocolo que el Gateway expone.

El Gateway tiene obligaciones normativas estrictas: debe loggear cada KB consultada, preservar los envelopes de respuesta individuales, y **nunca suprimir conflictos, citas, decisiones de acceso o fallos de frescura** del resultado visible al caller.

---

## Estado y roadmap

- Versión actual: **0.6-draft** (baseline para implementación, fechado 2026-07-07)
- No es releasable sin: schema JSON modular, corpus de conformance validado en CI y test vectors de firma
- Roadmap incluye: binding formal para MCP y A2A, OpenAPI profile, integración con Confluence/SharePoint/Jira, selectores para PDFs/imágenes/audio, federación cross-empresa

---

## En una frase

A2K es una **especificación de protocolo** que quiere ser el estándar para que agentes de IA empresariales accedan al conocimiento corporativo de forma gobernada, citada y auditable, funcionando sobre MCP, A2A o HTTPS sin reemplazarlos.

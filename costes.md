# Estimación de costes — Agente + AgentCore Gateway + a2k-box

**Fecha de la estimación:** 2026-08-07
**Actualizada:** 2026-08-21 — arquitectura de despliegue ya confirmada en producción (ver sección 0). Los precios en sí no se han vuelto a verificar; siguen siendo los de la fecha original.
**Precios:** estándar (sin promoción), pensado para planificación a largo plazo. Fuentes oficiales AWS verificadas en esa fecha; los precios de AWS cambian con el tiempo — antes de usar estas cifras para un presupuesto en firme, confirmarlas contra `aws.amazon.com/bedrock/agentcore/pricing` y `aws.amazon.com/bedrock/pricing/`.

---

## 0. Qué cambió desde la estimación original (2026-08-07)

Esta estimación se escribió en fase de planificación, antes de tener nada desplegado. Desde entonces la arquitectura se confirmó en vivo contra una cuenta AWS real (`deploy/agentcore/README.md`), lo que despeja varias suposiciones que en su momento eran solo hipótesis:

- **Identity confirmado gratis, no solo asumido.** El Gateway usa **IAM Role** como outbound auth hacia a2k-box (sección 4a del runbook, confirmado en vivo 2026-08-17) — no un credential provider OAuth2 de AgentCore Identity. La fila "gratis" de la sección 1 ya no es una suposición de diseño, es lo que realmente hay desplegado.
- **a2k-box en modo live desde 2026-08-18**, con credenciales reales de Cala/Sayari resueltas vía Secrets Manager (`a2k/config.py`) en vez de mock — la partida "CALA + Sayari, sin dato" de la sección 1 sigue sin cifra, pero ya no es hipotética: hay tráfico real facturable ahí fuera en cuanto haya volumen.
- **El agente router (`agent/core.py`) ya no deja la decisión de mirar el catálogo de vendors al modelo.** Se probó dejarlo como tool (`a2k.listVendors`) y el modelo no la llamaba de forma fiable (`agent/test_routing_behavior.py`, confirmado 2026-08-18). Ahora el catálogo se obtiene una vez por contenedor cálido y se inyecta directo en el system prompt de cada `ask()`, y `listVendors` se retira de las tools que ve el modelo — de las 8 tools que expone a2k-box, el modelo ve 7. Esto mueve algo de coste de "una tool call adicional" a "más tokens fijos en el primer turno" (el texto del catálogo), sin medir todavía cuánto exactamente.
- **Confirmado por diseño, no solo por suposición**, que el agente llama a `ask` como máximo una vez por pregunta y hace 2 turnos de modelo (decidir la llamada + redactar la respuesta final) — el system prompt de `agent/core.py` lo impone explícitamente. La asunción de la sección 2 seguía siendo correcta, solo que ahora está verificada.
- **Despliegue dual de a2k-box, y esta tabla solo cubre uno de los dos lados.** a2k-box corre en dos sitios distintos: transporte **MCP en AgentCore Runtime** (el que consume el agente router vía Gateway, y el único que genera los costes de Runtime/Gateway de este documento) y transporte **REST en EKS** (el que sigue usando K2, ver `deploy/deployment.yaml`). El EKS se factura como cómputo Kubernetes normal — fuera del alcance de AgentCore y de esta tabla.

---

## 1. Qué se factura por separado

Una sola llamada del agente ("pregúntale algo a a2k-box") toca cuatro medidores de coste AWS distintos, más el coste (desconocido por ahora) de CALA y Sayari:

| Componente | Precio oficial |
|---|---|
| **Inferencia del modelo** (Claude Sonnet 5 en Bedrock, precio estándar) | **$3 / $10^6 tokens de entrada**, **$15 / $10^6 tokens de salida** |
| **AgentCore Runtime** (cómputo, ×2: workload del agente + workload de a2k-box) | **$0.0895/vCPU-hora** + **$0.00945/GB-hora** — solo se factura mientras procesa activamente, no mientras espera al modelo o a una API externa |
| **AgentCore Gateway** | **$0.005 por 1.000 invocaciones de tool** ($0.000005/llamada). Si se activa semantic search: $0.025/1.000 |
| **AgentCore Identity** | $0.010/1.000 tokens/API keys — **$0, confirmado en vivo**: el Gateway usa IAM Role (sección 4a de `deploy/agentcore/README.md`), no un credential provider de Identity, así que este medidor no se activa en absoluto |
| **CALA + Sayari** | **Sin dato** — depende del contrato con cada proveedor. Puede ser la partida dominante; no incluido en los totales de este documento |

Componentes menores no incluidos por ser previsiblemente despreciables a este volumen: CloudWatch (Observability), S3 (paquete de despliegue), Cognito (autenticación, tanto la de prueba como la real que usan Gateway y el agente hoy), Secrets Manager (credenciales live de Cala/Sayari/agente, leídas una vez por contenedor cálido gracias al `lru_cache` de `config.py`/`agent/core.py`, no en cada request).

**Esta tabla cubre solo el camino AgentCore Runtime + Gateway** (agente router ↔ a2k-box vía MCP, ver sección 0). El despliegue REST de a2k-box en EKS para K2 no pasa por ningún medidor de AgentCore — se factura como cómputo EKS ordinario, fuera del alcance de este documento.

---

## 2. Coste por llamada (ejemplo trabajado)

### Asunciones (ajustables)

- El agente hace 2 turnos con el modelo por interacción: uno para decidir llamar a `a2k.ask`, otro para redactar la respuesta final con el resultado ya en contexto — **confirmado por diseño** en `agent/core.py`'s system prompt ("Call the ask tool at most once per question"), no solo una suposición.
- ~2.000 tokens de entrada en el primer turno (system prompt + 7 tool schemas de a2k-box + pregunta del usuario). El número de tools sigue siendo 7 pero por una razón distinta a la original: a2k-box expone 8 tools en total, y el agente ahora retira `a2k.listVendors` de lo que le pasa al modelo (sección 0) — así que el 7 es el mismo de antes por coincidencia, no porque a2k-box no haya cambiado. **No incluye** el texto del catálogo de vendors que el system prompt inyecta directamente (reemplaza a la tool call que antes se habría necesitado) — sin medir todavía cuánto añade, probablemente decenas-bajas-centenas de tokens con solo dos vendors activos.
- ~4.200 tokens de entrada en el segundo turno (incluye el `CitedResponseEnvelope` completo de vuelta — claims, citations, audit metadata).
- ~450 tokens de salida en total entre los dos turnos.
- 1 invocación de Gateway por interacción (una llamada a `a2k.ask`) — igualmente confirmado por diseño, no solo asunción.
- Cómputo de Runtime: activo unos cientos de ms en cada workload (agente + a2k-box) — la partida más incierta de esta estimación, ver nota al final.

### Desglose

| Partida | Cálculo | Coste |
|---|---|---|
| Modelo (Claude Sonnet 5, estándar) | 6.200 in × $3/M + 450 out × $15/M | **$0.0254** |
| Gateway | 1 invocación × $0.000005 | **$0.000005** |
| Runtime (ambos workloads, activo) | ~0.5 vCPU-seg + memoria | **~$0.00005–0.0002** |
| Identity | — | **$0** |
| **Total AWS por llamada** | | **≈ $0.026** |
| **+ CALA/Sayari** | | **? (falta dato)** |

El modelo es, con diferencia, la partida dominante (>95% del coste AWS) — Runtime y Gateway son ruido en comparación a este volumen de tokens.

---

## 3. Proyección mensual (solo lado AWS, sin CALA/Sayari)

| Llamadas/mes | Coste AWS estimado |
|---|---|
| 1.000 | ~$26 |
| 10.000 | ~$260 |
| 100.000 | ~$2.600 |
| 1.000.000 | ~$26.000 |

---

## 4. Pendiente para cerrar el número real

1. **Volumen esperado de llamadas/mes** (o al día) — es lo que más cambia el total.
2. **Tarifas de CALA y Sayari por llamada**, en cuanto se negocien/confirmen — sin esto el coste real puede ser bastante mayor que la tabla de la sección 3, según cómo facturen (muchos vendors de datos financieros/riesgo cobran por lookup, y podría superar con facilidad al coste del modelo).
3. **Confirmar el modelo real a usar** — si Claude Haiku es suficiente para el caso de uso, el coste de inferencia baja sustancialmente frente a Sonnet 5.

---

## 5. Advertencia sobre la precisión de la estimación

- El coste de Runtime se ha estimado con un margen muy amplio porque el modelo de facturación es "solo mientras procesa activamente" — la memoria, además, factura sobre el pico de uso mientras dura la sesión, no solo mientras hay CPU activa. Esto solo se conoce con precisión midiendo de verdad con CloudWatch una vez desplegado; no es grave porque de todas formas es la partida menos relevante del total.
- Los tokens de entrada/salida por turno son una asunción razonable pero no medida — el tamaño real del `CitedResponseEnvelope` (sección 5 de `A2K-KCP-Consumption 4.md`) varía según cuántas citas/claims devuelva cada pregunta, y el histórico de conversación (si el agente mantiene contexto multi-turno) hace crecer el coste de tokens de entrada en cada turno sucesivo.
- No se ha incluido el coste de **AgentCore Memory** por asumir que a2k-box es stateless (`stateless_http=True`, ver `deploy/agentcore/README.md`) y que la memoria de conversación del agente, si existe, es una decisión de diseño aún no tomada — añadir si se confirma su uso ($0.25/1.000 eventos nuevos + $0.75/1.000 registros almacenados/mes + $0.50/1.000 recuperaciones).
- **Ahora que el despliegue está en vivo, ya no hace falta seguir estimando el coste de Runtime a ciegas** — se puede medir de verdad en Cost Explorer (Billing and Cost Management → Cost Explorer, agrupando por Service/Usage Type, con ~24h de retraso en los datos; tags de asignación de coste por Runtime si se necesita separar agente vs. a2k-box). En cuanto haya volumen real de uso, sustituir las cifras de las secciones 2-3 por datos medidos en vez de la asunción de "unos cientos de ms".

---

## Fuentes

- [Amazon Bedrock AgentCore Pricing](https://aws.amazon.com/bedrock/agentcore/pricing)
- [Amazon Bedrock Pricing](https://aws.amazon.com/bedrock/pricing/)

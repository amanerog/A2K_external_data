# Estimación de costes — Agente + AgentCore Gateway + a2k-box

**Fecha de la estimación:** 2026-08-07
**Precios:** estándar (sin promoción), pensado para planificación a largo plazo. Fuentes oficiales AWS verificadas en esa fecha; los precios de AWS cambian con el tiempo — antes de usar estas cifras para un presupuesto en firme, confirmarlas contra `aws.amazon.com/bedrock/agentcore/pricing` y `aws.amazon.com/bedrock/pricing/`.

---

## 1. Qué se factura por separado

Una sola llamada del agente ("pregúntale algo a a2k-box") toca cuatro medidores de coste AWS distintos, más el coste (desconocido por ahora) de CALA y Sayari:

| Componente | Precio oficial |
|---|---|
| **Inferencia del modelo** (Claude Sonnet 5 en Bedrock, precio estándar) | **$3 / $10^6 tokens de entrada**, **$15 / $10^6 tokens de salida** |
| **AgentCore Runtime** (cómputo, ×2: workload del agente + workload de a2k-box) | **$0.0895/vCPU-hora** + **$0.00945/GB-hora** — solo se factura mientras procesa activamente, no mientras espera al modelo o a una API externa |
| **AgentCore Gateway** | **$0.005 por 1.000 invocaciones de tool** ($0.000005/llamada). Si se activa semantic search: $0.025/1.000 |
| **AgentCore Identity** | $0.010/1.000 tokens/API keys — **gratis si pasa por Runtime o Gateway** (nuestro caso) |
| **CALA + Sayari** | **Sin dato** — depende del contrato con cada proveedor. Puede ser la partida dominante; no incluido en los totales de este documento |

Componentes menores no incluidos por ser previsiblemente despreciables a este volumen: CloudWatch (Observability), S3 (paquete de despliegue), Cognito (autenticación de prueba).

---

## 2. Coste por llamada (ejemplo trabajado)

### Asunciones (ajustables)

- El agente hace 2 turnos con el modelo por interacción: uno para decidir llamar a `a2k.ask`, otro para redactar la respuesta final con el resultado ya en contexto.
- ~2.000 tokens de entrada en el primer turno (system prompt + 7 tool schemas de a2k-box + pregunta del usuario).
- ~4.200 tokens de entrada en el segundo turno (incluye el `CitedResponseEnvelope` completo de vuelta — claims, citations, audit metadata).
- ~450 tokens de salida en total entre los dos turnos.
- 1 invocación de Gateway por interacción (una llamada a `a2k.ask`).
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

---

## Fuentes

- [Amazon Bedrock AgentCore Pricing](https://aws.amazon.com/bedrock/agentcore/pricing)
- [Amazon Bedrock Pricing](https://aws.amazon.com/bedrock/pricing/)

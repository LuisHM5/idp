# Plan de Implementación — Sistema de Validación Inteligente de Documentos (IDP)

**Duración: 12 semanas núcleo (3 meses) + 4 semanas de integración (1 mes) = 16 semanas (~4 meses)** | **Equipo: 1 desarrollador** | **Fecha inicio estimada: Semana 1**

---

> **Cómo leer este plan**
> El proyecto corre en **dos líneas de trabajo paralelas**, no en una sola línea de tiempo:
>
> - **Núcleo IDP:** desarrollo secuencial del sistema. Entrega un avance semi-funcional al final de cada uno de los primeros 3 meses.
> - **Integración y Prototipado:** arranca desde la semana 1 en paralelo. Es bajo costo de tiempo al inicio (reuniones, contratos de API, prototipos) y se vuelve la prioridad completa en el mes 4, cuando el núcleo ya está listo para integrarse de verdad con **Portal (módulo de altas)** y **ERP (módulo de pedimentos)**.
>
> Esto es lo que hace posible comprimir el proyecto: mientras avanza el Núcleo IDP, en paralelo avanza la Integración y Prototipado — se negocian contratos, se consiguen muestras de documentos y se prueban prototipos — para que en el mes 4 la integración real no empiece de cero.

> **Nota sobre alcance vs. el plan de 6 meses (v2)**
> El plan anterior (`plan-desarrollo-v2.md`, 26 semanas) asumía construir el catálogo completo de documentos, endurecer seguridad a nivel auditoría externa formal, y hacer rollout gradual a 5 áreas distintas antes de cerrar. Este plan de 4 meses **no elimina ese trabajo, lo reordena y lo prioriza**: el catálogo crece de forma incremental como trabajo de soporte (no como entregable principal de cada mes), la seguridad cubre lo crítico para producción (no una auditoría externa formal completa), y el rollout se concentra en los dos consumidores que ya están comprometidos (Portal y ERP) en lugar de las 5 áreas del plan original. Lo que se aplaza explícitamente: auditoría de seguridad externa formal con firma de Seguridad/TI corporativa, fine-tuning custom de Bedrock si el baseline ya es suficiente, y la integración con Contabilidad/Pagos/Auditoría (quedan para una fase posterior, fuera de este plan).

> **Naturaleza del sistema**
> Backend puro. No expone UI propia. Expone APIs REST que sistemas externos consumen: (1) el sistema externo llama `POST /v1/documents/upload`; (2) el pipeline procesa de forma asíncrona (Bedrock clasificación/extracción + Textract fallback + validación determinista); (3) al terminar, hace webhook al sistema llamante con el `Canonical_Result`, o el consumidor hace polling con `GET /v1/documents/{id}/status`.

---

## Resumen Visual por Mes

| MES 1 — Semanas 1 a 4 | MES 2 — Semanas 5 a 8 |
|---|---|
| Núcleo IDP: API funcional para CSF<br>Integración y Prototipado: contrato preliminar con Portal y ERP | Núcleo IDP: seguridad, rate limiting y fine-tuning<br>Integración y Prototipado: prototipo en sandbox + diseño de PEDIMENTO |

| MES 3 — Semanas 9 a 12 | MES 4 — Semanas 13 a 16 |
|---|---|
| Núcleo IDP: catálogo ampliado + tests/CI-CD<br>Integración y Prototipado: pruebas externas con Portal y ERP | Núcleo IDP: soporte y estabilización<br>Integración y Prototipado: rollout a producción (Portal + ERP) |

> El primer tipo de documento (mes 1) es **Constancia de Situación Fiscal (CSF)**: ya tiene catálogo definido en el código actual (`CONSTANCIA_SITUACION_FISCAL`, validado contra `validators.fiscal`), por lo que el mes 1 es principalmente endurecerlo y exponerlo vía API, no construirlo desde cero. Es también el tipo más relevante para validación fiscal de proveedores, lo que lo conecta directamente con el trabajo de integración con Costos/ERP.

---

## MES 1 — Semanas 1 a 4

### Núcleo IDP

**Entregable del mes:** API funcional, end-to-end, para 1 tipo de documento (Constancia de Situación Fiscal). Un documento real sube por API, se clasifica, se extrae y se valida, y el `Canonical_Result` queda disponible por polling.

**Semana 1 — Setup e infraestructura**

- [ ] Accesos AWS: roles IAM, perfiles locales, habilitar Bedrock (Claude Haiku) y Textract en la región objetivo
- [ ] Verificar/ajustar estructura CDK existente (`bin/app.ts`, `src/document-validation-stack.ts`) y dependencias fijadas
- [ ] S3 con prefijos `raw/`, `processed/`, `results/`, versionado habilitado
- [ ] Deploy de API Gateway + Upload Lambda, EventBridge broker, políticas IAM mínimas
- [ ] Alarma de billing desde el día 1 (umbral bajo para detección temprana)
- [ ] Verificar conectividad real Lambda → Bedrock / Textract / S3

**Semana 2 — Pipeline core con CSF**

- [ ] Confirmar/ajustar `document_types.json` con `CONSTANCIA_SITUACION_FISCAL` (RFC, CURP, nombre/razón social, régimen fiscal, código postal, fecha de emisión, estatus en padrón)
- [ ] Validar `validators/fiscal.py`: regex RFC (12–13 chars), regex CURP, antigüedad máxima de la constancia (`date_max_age`)
- [ ] Prueba end-to-end con 10 CSF reales: accuracy campo por campo
- [ ] Ajustar `extraction_hints` donde el modelo confunda RFC con CURP, o régimen fiscal con actividad económica
- [ ] Documentar la estructura del `Canonical_Result` (este contrato alimenta a la Integración y Prototipado)

**Semana 3 — Endurecimiento del primer tipo**

- [ ] Respuestas de API consistentes: `202` al recibir, `200` al consultar, `400` archivo ilegible, `404` ID no existe
- [ ] Endpoint `GET /v1/documents/{id}/status` para polling
- [ ] Manejo de errores de pipeline (reintentos, mensajes claros)
- [ ] Dataset de prueba ampliado (20+ CSF reales), medir accuracy formalmente

**Semana 4 — Cierre del entregable 1**

- [ ] Demo funcional del flujo completo (upload → status → resultado)
- [ ] Smoke tests del pipeline completo
- [ ] Si el tiempo lo permite sin comprometer la fecha: activar 1–2 tipos ya maduros en el catálogo actual (INE, CURP) como adelanto, sin que sea el compromiso del mes

**Por qué un solo tipo de documento:** comprometerse con 5+ tipos en el primer mes (como en versiones anteriores del plan) es lo que genera retrasos en cascada. Un tipo bien probado y documentado es lo que necesita la Integración y Prototipado para negociar contratos reales con Portal y ERP.

### Integración y Prototipado (arranca este mes)

- [ ] Reunión de descubrimiento con **Portal — módulo de altas**: flujo actual de alta de empleado, stack técnico del portal, qué dato dispara la llamada al IDP
- [ ] Reunión de descubrimiento con **ERP — módulo de pedimentos**: flujo actual de registro de pedimentos en Costos, cómo se validan hoy, qué sistema/base de datos los recibe
- [ ] Borrador de contrato de API por cada consumidor: qué endpoint llaman, qué campos del `Canonical_Result` necesitan, webhook vs. polling
- [ ] Con el ERP: levantamiento de campos típicos de un pedimento aduanal (número de pedimento, aduana, RFC del importador, fecha de pago, valor en aduana, partidas) — esto alimenta el diseño del tipo `PEDIMENTO` en el mes 2
- [ ] Solicitar muestras reales (anonimizadas si aplica): pedimentos del ERP, expedientes de alta del Portal
- [ ] Entregable de Integración y Prototipado en este mes: documento de contrato preliminar por sistema (no integración funcional todavía)

---

## MES 2 — Semanas 5 a 8

### Núcleo IDP

**Entregable del mes:** Seguridad básica de producción, rate limiting por consumidor, y accuracy optimizada (fine-tuning si aplica).

**Semana 5 — Autenticación y autorización**

- [ ] API Keys por sistema consumidor (Portal, ERP) + Usage Plans con rate limiting diferenciado
- [ ] Roles `UPLOADER` / `REVIEWER` / `READER` separados por operación
- [ ] Registro de cada API Key en DynamoDB: sistema, responsable, fecha, estado

**Semana 6 — Protección de infraestructura**

- [ ] AWS WAF en API Gateway: rate limit por IP, reglas OWASP básicas, CORS explícito (sin `*`)
- [ ] Mover secretos a AWS Secrets Manager (credenciales de Portal/ERP, HMAC de webhooks)
- [ ] Cifrado en reposo confirmado: S3 (KMS), DynamoDB; CloudTrail habilitado con data events

**Semana 7 — Dataset y accuracy**

- [ ] Recolectar y etiquetar 50–80 documentos reales de CSF (y de los tipos activados en mes 1)
- [ ] Script `scripts/evaluate_accuracy.py`: accuracy por campo, falsos positivos/negativos
- [ ] Iterar `extraction_hints` en campos con accuracy < 85%

**Semana 8 — Fine-tuning (si aplica) y catálogo dinámico**

- [ ] Decisión basada en el baseline de la semana 7: ¿fine-tuning custom en Bedrock o los prompts ajustados son suficientes?
  - Si aplica: lanzar `bedrock:CreateModelCustomizationJob` al inicio de la semana (tarda 24–72h)
- [ ] Activar catálogo dinámico en DynamoDB (`enableDynamoCatalog: true`) para agregar tipos sin redeploy
- [ ] Dashboard básico de CloudWatch: documentos procesados, % APROBADO/RECHAZADO/REQUIERE_REVISION, latencia, costo diario

**Por qué seguridad antes que más tipos de documento:** Portal y ERP no pueden conectar contra un endpoint sin autenticación ni rate limiting, sin importar cuántos tipos de documento soporte. Esto es lo que la Integración y Prototipado necesita para pasar de "prototipo" a "prueba real" en el mes 3.

### Integración y Prototipado (continúa)

- [ ] Prototipo funcional en sandbox/staging con **Portal**: probar `POST /v1/documents/upload` con un alta de prueba real usando la API Key recién emitida
- [ ] Diseñar y registrar el tipo de documento `PEDIMENTO` en el catálogo (`catalogs/pedimento.json`): campos, hints, validador inicial (formato de número de pedimento, fecha no futura, RFC válido)
- [ ] Primeras pruebas de extracción con pedimentos reales conseguidos en el mes 1 — accuracy preliminar, no es aún el compromiso formal
- [ ] Ajustar el contrato de API con el feedback real de ambos equipos (campos faltantes, formatos inesperados)

---

## MES 3 — Semanas 9 a 12

### Núcleo IDP

**Entregable del mes:** Catálogo de documentos ampliado y suite de tests/CI-CD lista.

> **Documentación no es una fase, es una tarea de 1–2 días.** `openapi.yaml` se genera en gran parte automáticamente desde el catálogo y los esquemas existentes; los runbooks son notas operativas cortas (media página cada uno), no manuales extensos. Se hace en paralelo durante la semana 12, sin bloquear nada y sin consumir una semana completa.

**Semana 9 — Ampliar catálogo de documentos**

- [ ] Activar y probar con datos reales los tipos ya presentes en el código pero no cerrados en meses 1–2 (CURP, Pasaporte, Constancia de Situación Fiscal, Carta NOM-35)
- [ ] Validadores avanzados pendientes: dígito verificador CURP, checksum de RFC, validación cruzada CURP ↔ datos biográficos
- [ ] Dataset cruzado de prueba (5 docs por tipo), verificar que el clasificador no confunde tipos similares (CSF↔CFDI, CURP↔INE)

**Semana 10 — Tests automatizados y CI/CD**

- [ ] Tests unitarios de validadores y extractores con mocks de Bedrock/Textract
- [ ] Test de regresión automático con el dataset etiquetado (debe mantener accuracy >= baseline)
- [ ] GitHub Actions: `cdk synth` + tests en cada PR, deploy automático a staging en merge

**Semana 11–12 — Soporte a pruebas externas + checklist de pre-producción**

- [ ] Soporte técnico a las sesiones de Integración y Prototipado (ver abajo): ajustes de API, correcciones de campos, fixes de bugs detectados en staging
- [ ] Checklist de pre-producción: secretos en Secrets Manager, backups DynamoDB (PITR), CloudTrail activo, revisión de costos proyectados vs. presupuesto
- [ ] Documentación (1–2 días, dentro de la semana 12): `openapi.yaml` + Postman + una guía de integración de una página por sistema (Portal, ERP) + 3–4 runbooks cortos (agregar tipo de documento, responder alarmas, rotar API Keys, incidentes) + un diagrama de arquitectura

### Integración y Prototipado — Pruebas externas (entregable principal de este mes)

- [ ] Ambiente de staging formal con datos de prueba (10–20 documentos por tipo con resultados conocidos)
- [ ] Sesión de pruebas con **Portal (altas)**: flujo completo de alta de empleado consumiendo la API en staging, verificar que el webhook/polling entrega lo que el portal necesita
- [ ] Sesión de pruebas con **ERP (pedimentos)**: flujo de validación de costos de pedimentos consumiendo la API en staging
- [ ] Registro de bugs e inconsistencias; corrección de los issues críticos (nombres de campo, timeouts, problemas de firewall/webhook)
- [ ] Prueba de carga ligera: documentos simultáneos, verificar que no hay throttling
- [ ] **Visto bueno formal** de Portal y ERP para pasar a integración real en el mes 4

---

## MES 4 — Semanas 13 a 16

## Fase de Integración con Sistemas Externos (Portal — Altas, ERP — Pedimentos)

**Meta:** Portal y ERP consumen el IDP en producción real, dentro de sus propios flujos de alta de empleados y validación de costos de pedimentos.

> Esta fase depende de que cada equipo (Portal, ERP) haga su propio trabajo de integración del lado del consumidor — adaptar su sistema para llamar al IDP dentro de su flujo. El dev IDP da soporte y resuelve dudas del contrato, pero no escribe el código del Portal ni del ERP.

**Semana 13 — Piloto controlado: Portal (altas)**

- [ ] Grupo piloto reducido de Capital Humano usando la integración real en el flujo de alta
- [ ] Monitoreo diario: CloudWatch Logs, tasa de webhooks entregados, issues reportados
- [ ] Correcciones menores sin afectar el flujo activo

**Semana 14 — Piloto controlado: ERP (pedimentos)**

- [ ] Grupo piloto reducido de Costos validando pedimentos reales a través del IDP
- [ ] Verificar que el `Canonical_Result` de `PEDIMENTO` trae exactamente los campos que el ERP necesita para su registro de costos
- [ ] Correcciones menores sin afectar el flujo activo

**Semana 15 — Rollout completo**

- [ ] Activar la integración para todos los usuarios de Portal (altas) y todo el flujo de ERP (pedimentos) dentro del alcance
- [ ] Monitoreo activo — si el error rate de cualquiera de los dos sistemas supera 10%, pausar y analizar antes de continuar
- [ ] Calibrar alarmas de CloudWatch con datos reales de producción

**Semana 16 — Estabilización y entrega formal**

- [ ] Medición de KPIs basales (ver tabla abajo) con datos reales de Portal y ERP
- [ ] Transferencia de documentación final y runbooks a los equipos de Sistemas/Portal/ERP
- [ ] Retrospectiva corta: qué salió bien, qué se repetiría diferente
- [ ] Cierre formal: confirmación escrita de Portal y ERP de que la integración está operacional y aceptada

---

## Resumen por mes

| Mes | Semanas | Núcleo IDP — entregable                                    | Integración y Prototipado — entregable                             |
| --- | ------- | --------------------------------------------------------- | ----------------------------------------------------------------- |
| 1   | 1–4     | API funcional, 1 tipo de documento (CSF)                  | Contrato preliminar con Portal (altas) y ERP (pedimentos)         |
| 2   | 5–8     | Seguridad, rate limiting, fine-tuning/accuracy            | Prototipo en sandbox + primer diseño del tipo `PEDIMENTO`         |
| 3   | 9–12    | Catálogo ampliado + tests/CI-CD (documentación: 1–2 días) | Pruebas externas en staging + visto bueno de ambos sistemas       |
| 4   | 13–16   | Soporte y estabilización                                  | Integración real en producción: Portal (altas) + ERP (pedimentos) |

---

## Stack Técnico

| Capa                               | Tecnología                                         |
| ---------------------------------- | -------------------------------------------------- |
| Infraestructura                    | AWS CDK (TypeScript)                               |
| Clasificación / Extracción         | AWS Bedrock — Claude Haiku 4.5                     |
| OCR fallback                       | AWS Textract (AnalyzeID + DetectDocumentText)      |
| Orquestación del pipeline          | Construct `@cdklabs/cdk-appmod-catalog-blueprints` |
| Validación determinista            | Python Lambda (`validators/`)                      |
| Validación de algoritmos oficiales | Node.js custom validators (`.mjs`)                 |
| Catálogo de tipos de documento     | JSON local + DynamoDB (hot-reload)                 |
| Almacenamiento de documentos       | S3 (versionado + cifrado KMS)                      |
| Resultados y trazabilidad          | DynamoDB                                           |
| Notificaciones / webhooks          | EventBridge + Notification Lambda                  |
| API pública                        | API Gateway REST (`/v1/`) + WAF + Usage Plans      |
| Gestión de secretos                | AWS Secrets Manager                                |
| CI/CD                              | GitHub Actions o AWS CodePipeline                  |

---

## Riesgos y Mitigaciones

| Riesgo                                                                      | Probabilidad | Impacto | Mitigación                                                                                                           |
| --------------------------------------------------------------------------- | ------------ | ------- | -------------------------------------------------------------------------------------------------------------------- |
| Un solo desarrollador llevando las dos líneas de trabajo en paralelo        | Alta         | Alto    | La Integración y Prototipado es deliberadamente ligera en meses 1–2 (reuniones, prototipos) — el esfuerzo pesado se concentra en el mes 4 |
| Portal o ERP no entregan muestras de documentos a tiempo (mes 1)            | Media        | Alto    | Solicitar desde la semana 1 aunque no se usen hasta el mes 2                                                         |
| Campos del pedimento son más variables de lo esperado entre aduanas         | Media        | Alto    | Prototipo en mes 2 detecta esto temprano, antes del compromiso de producción en mes 4                                |
| Accuracy baja en documentos de baja calidad                                 | Alta         | Alto    | Textract fallback + dataset de evaluación formal en mes 2                                                            |
| Firewall corporativo bloquea webhooks hacia Portal/ERP                      | Media        | Alto    | Detectar en pruebas externas (mes 3); polling documentado como alternativa siempre disponible                        |
| Fine-tuning de Bedrock tarda más de 72h                                     | Media        | Medio   | Lanzar el job al inicio de la semana 8, no al final                                                                  |
| Compresión de alcance deja fuera auditoría de seguridad externa formal      | Alta         | Medio   | Aceptado explícitamente en este plan; agendar la auditoría formal como fase posterior si el negocio lo requiere      |
| Error rate alto durante rollout del mes 4 en cualquiera de los dos sistemas | Media        | Alto    | Criterio de pausa explícito: > 10% de error rate detiene el rollout para análisis                                    |

---

## KPIs objetivo (medidos al final del mes 4)

| KPI                                         | Proceso anterior | Objetivo        |
| ------------------------------------------- | ---------------- | --------------- |
| Tiempo promedio de validación por documento | 5–25 min manual  | < 30 segundos   |
| % documentos sin intervención humana        | 0%               | > 60%           |
| Documentos procesados sin error de pipeline | —                | > 95%           |
| Sistemas externos integrados en producción  | 0                | 2 (Portal, ERP) |

> Los objetivos de accuracy y de % sin intervención humana son más conservadores que en el plan de 6 meses (v2) porque hay menos tiempo de iteración de prompts y un catálogo de documentos más acotado. Se pueden revisar al alza en una fase posterior, con más datos reales de producción.

---

## Estimación de Costos AWS (mensual, ~1,240 documentos/mes)

| Servicio                                            | Estimado mensual   |
| --------------------------------------------------- | ------------------ |
| Bedrock — Claude Haiku (clasificación + extracción) | $8–15 USD          |
| Textract — AnalyzeID + DetectDocumentText           | $4–6 USD           |
| S3 — almacenamiento + requests + versionado         | $2–4 USD           |
| Lambda — invocaciones + duración                    | $1 USD             |
| DynamoDB — catálogo + resultados + PITR             | $3 USD             |
| API Gateway + WAF                                   | $2 USD             |
| Secrets Manager + CloudTrail                        | $3 USD             |
| **Total estimado**                                  | **$23–34 USD/mes** |

_Fine-tuning custom Bedrock (si se decide en el mes 2): costo único de $20–80 USD según tamaño del dataset._
_Ambiente de staging (mes 3): costo adicional puntual de $10–20 USD total._

---

_Plan generado: 2026-06-17 — Versión 6.0_

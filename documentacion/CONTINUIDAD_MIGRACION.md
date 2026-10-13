# Relevo técnico: extracción determinística/híbrida y migración del pipeline

**Fecha:** 2026-10-13  
**Rama de trabajo:** `feature/pedimento-erp-deterministic-extraction`  
**Estado:** cambios locales sin commit; despliegue y medición E2E pendientes  
**Propósito:** permitir que otro modelo de IA continúe el trabajo sin reconstruir toda la conversación.

## 1. Objetivo original y evolución

El trabajo comenzó con una pregunta general: cómo mapear documentos con
programación tradicional, configurable por schema, para evitar IA cuando el
formato permite extraer con texto, regex, QR, XML o celdas. El primer piloto fue
`PEDIMENTO_ERP`.

El objetivo evolucionó en cuatro frentes relacionados:

1. extracción determinística y fallback híbrido configurables por catálogo;
2. respuestas de IA compactas para pedir sólo campos faltantes en vez de un
   JSON completo;
3. observabilidad real de IA, tokens, costos y latencia extremo a extremo;
4. eliminación de infraestructura que añadía segundos sin aportar trabajo al
   camino determinístico.

La regla de producto acordada es:

```text
adquirir texto barato
  → intentar código declarativo
    → si está completo, terminar sin IA
    → si faltan campos, pedir únicamente esos campos a IA
      → ensamblar y validar el envelope final con código
```

No existe un endpoint rápido alterno. Se mantiene un solo canal público,
`POST /documents` + PUT prefirmado + `GET /documents/{id}`.

## 2. Extracción configurable que ya existía al iniciar esta última migración

### 2.1 Modos de extracción

El request acepta `extractionMode`, con estos efectos:

| Valor | Efecto |
|---|---|
| `catalog` | Default. El catálogo del tipo decide entre modelo, híbrido o determinístico. |
| `model` | Fuerza extracción por IA para comparación/rollback autorizado. |
| `hybrid` | Código primero; IA compacta sólo para faltantes. |
| `deterministic` | Sólo código; si faltan campos, revisión según el catálogo, sin completar con IA. |

Si ni schema ni API configuran otra cosa, el default es `catalog`. El nombre
antiguo “automático según el tipo” se documentó con su efecto explícito:
consulta el catálogo y ejecuta exactamente la estrategia declarada; no implica
por sí solo que se use o se omita IA.

En producción no se permite que cualquier caller fuerce costos/calidad. Los
overrides de modo/modelo siguen sujetos a los gates ya existentes.

### 2.2 `PEDIMENTO_ERP`

`catalogs/custom/erp/PEDIMENTO_ERP.json` fue convertido en el piloto de mapeo
por código. Sus campos usan patrones configurables del catálogo y roles de
pedimento para evitar condicionar el engine al `TYPE_ID`:

- encabezado y valores por regex sobre texto nativo/Textract;
- número, patente, año, aduana y consecutivo recuperables desde QR;
- facturas y cuadro de liquidación mediante estructuras repetibles;
- `LIQUIDACION_NORMALIZADA` y `Tipo_Pago` derivados por código;
- aliases, normalización y validación declaradas en schema.

La extensibilidad buscada fue “añadir patrones y perfiles al catálogo”, no
crear un `if PEDIMENTO_ERP` por cada formato. La infraestructura genérica vive
principalmente en:

- `pipeline/deterministic_text.py`;
- `pipeline/text_extractor.py`;
- `pipeline/normalize.py`;
- `pipeline/qr_recovery.py`;
- `pipeline/liquidacion.py`;
- `pipeline/derived.py`;
- `validators/engine.py`.

### 2.3 Respuesta compacta de IA

La IA ya no necesita devolver el árbol final completo para cada intento. El
protocolo `compact` pide pares/rutas y valores para los campos requeridos; el
código fusiona esos valores en el schema final. El rollback global es
`AI_RESPONSE_FORMAT=legacy`; en dev también existe override autorizado por
request.

La salida expone `aiResponseFormat` con formato seleccionado, fuente,
efectivo, número de llamadas compactas/legacy y si ocurrió fallback.

### 2.4 Telemetría de IA y ahorro

El envelope final expone:

- `aiExecuted`: booleano inequívoco;
- `aiUsage.executed`, `stages`, `inputTokens`, `outputTokens`, `images` y
  `tokenReportedStages`;
- `deterministicExtraction.aiExecuted`, campos resueltos y faltantes;
- `deterministicExtraction.tokenSavings` por documento;
- `deterministicSavings` agregado.

Los tokens facturados y el ahorro contrafactual están separados. Un ahorro
estimado nunca se suma como costo real. La estimación actual se identifica como
`partial_schema_counterfactual_v1`.

### 2.5 Auditoría de costos

Los costos se escriben en otra tabla DynamoDB, una fila por `DocumentId`, desde
`cost_audit.py`. Distingue Bedrock por etapa, Textract por páginas OCR,
embeddings, Lambda y totales de tokens. Funciona para IA completa, híbrido y
código + Textract. La tabla de costos está separada de la tabla de documentos y
usa `RETAIN`.

## 3. `CERTIFICADO_POLIZA`: código primero, IA como fallback

`catalogs/base/seguros/CERTIFICADO_POLIZA.json` versión `5.2.0` reconoce cuatro
familias iniciales, sin amarrarse a una aseguradora específica:

| Perfil | `CLAVE_RUBRO` | `RUBRO` |
|---|---|---|
| `EMP` | `EMP` | Empresarial |
| `FL` | `FL` | Flotilla |
| `GMM` | `GMM` | Gastos médicos mayores |
| `SV` | `SV` | Seguro de Vida |

Las muestras usadas fueron:

- `test-docs/seguros/emp_cert.pdf`;
- `test-docs/seguros/fl_cert.pdf`;
- `test-docs/seguros/ggm_certificado.pdf`;
- `test-docs/seguros/vida_cert.pdf`.

El catálogo usa perfiles, marcadores, constantes, campos esperados y patrones
por campo. Si el perfil conocido resuelve todo, no ejecuta IA. Si el formato
cambia o quedan faltantes, el fallback puede solicitar únicamente esos campos;
si ni siquiera se reconoce un perfil, el catálogo decide el fallback completo.

## 4. Latencia: hallazgos que motivaron la migración actual

### 4.1 Casos medidos durante la conversación

| Caso | Resultado observado | Diagnóstico |
|---|---:|---|
| `bad-pedimento.pdf`, 2 páginas OCR | 14.70 s | 4.98 s carga/OCR + 9.62 s extracción IA; sí usó IA, 4,074 input + 594 output. |
| Pedimento nativo, 17 páginas | 162.47 s | 160.75 s extracción; 38,804 input + 10,058 output. La IA seguía recibiendo demasiado texto/salida. |
| `emp_cert.pdf`, antes | 2.05 s de pipeline | IA 0; casi todo fue `pageLoad`/Textract. |
| Mismo PDF, variación posterior | 3.33 s de pipeline | IA 0; `pageLoad` subió de 1.97 a 3.25 s por latencia variable de Textract. |
| Prueba sin catálogo sincronizado | 2.51 s | Seguía en el comportamiento anterior; no era evidencia de la optimización nueva. |
| Catálogo `5.2.0` sincronizado | 55.75 ms de pipeline | OCR 0, IA 0; el preflight determinístico resolvió 9/9 campos. |

En la última prueba rápida, aunque el motor tardó sólo 55.75 ms, el tiempo
`uploadToResultSeconds` fue 2.78 s:

- `queue`: 1,320.28 ms;
- `orchestrationBeforeExtraction`: 900.03 ms;
- pipeline útil: 55.75 ms.

Eso demostró que SQS + consumidor + Step Functions ya dominaban la latencia de
un documento determinístico.

### 4.2 OCR diferido y rollback de calidad

`CERTIFICADO_POLIZA` puede declarar:

```json
{
  "text_acquisition": {
    "ocr_mode": "only_if_deterministic_incomplete",
    "ocr_page_scope": "profile_pages_then_remaining"
  }
}
```

Con `DEFERRED_OCR_ENABLED=true`, primero se usa texto nativo y sólo se llama a
Textract si el preflight determinístico queda incompleto. El rollback global es
`DEFERRED_OCR_ENABLED=false`, que restaura el OCR eager conservador para todos
los tipos sin tocar catálogos. No se eliminó Textract: se volvió condicional.

## 5. Migración previa del blueprint

Antes de esta última petición ya se había retirado la dependencia directa de
`@cdklabs/cdk-appmod-catalog-blueprints` y se había creado un construct propio
`MultiModelDocumentProcessing`. Se conservaron los logical IDs importantes
para no reemplazar bucket/tabla por accidente.

En una primera fase, el construct propio todavía reproducía el pipeline del
blueprint:

```text
S3 raw/ → SQS → consumidor → Step Functions
  → InitMetadataEntry
  → ruta estándar o enrichment-only
  → mover original a processed/ o failed/
  → EventBridge
```

La ruta `enrichment-only` evitó las dos llamadas upstream para tipos
field-centric, pero mantuvo aproximadamente dos segundos de tubería vacía.

## 6. Migración actual implementada: S3 directo a Enrichment

### 6.1 Arquitectura nueva

```text
POST /documents
  → reserva AWAITING_UPLOAD y SourceKey en DynamoDB
  → devuelve URL PUT de source/{TIPOS?}/{documentId}/{archivo}

PUT a S3 source/
  → ObjectCreated invoca Enrichment Lambda directamente
  → claim idempotente en DynamoDB
  → extracción/validación
  → S3 results/{documentId}.json
  → DynamoDB EnrichmentResult + FINISHED
  → EventBridge document-processed-successful

fallo no recuperado / timeout
  → reintentos asíncronos de Lambda
  → AsyncFailureFinalizer
  → DynamoDB FAILED
  → EventBridge document-processing-failed
```

Se eliminaron de la plantilla:

- `InitMetadataEntry`;
- Standard Step Functions;
- SQS y DLQ de ingreso;
- consumidor SQS;
- Lambdas upstream de clasificación/procesamiento del flujo legacy;
- `CopyObject` + `DeleteObject` de `raw/` a `processed/failed/`.

El original queda permanentemente en `source/`. Sólo los prefijos `source/` y
`raw/` histórico disparan Enrichment. `results/`, `references/`, `processed/`
y `failed/` no tienen notificación, por lo que escribir el resultado no puede
crear un bucle.

### 6.2 Compatibilidad

- El nuevo Upload API genera claves `source/`.
- Se conserva trigger para `raw/` durante la transición, por URLs prefirmadas
  emitidas antes del deploy o cargas directas históricas.
- `_declared_types_from_key` entiende ambos prefijos.
- Query usa `SourceKey`/`UploadKey` y `HeadObject` para resolver el archivo en
  O(1); sólo recorre prefijos como fallback para filas históricas.
- Reprocess copia el original a un nuevo `source/{TIPOS?}/{nuevoId}/archivo`.
- El handler todavía acepta el payload normalizado antiguo para pruebas o
  ejecuciones que estuvieran drenando durante el deploy.
- Bucket y tabla conservan sus child IDs históricos en CDK.

### 6.3 Idempotencia y recuperación

S3 entrega eventos al menos una vez. El handler usa `ProcessingAttempt` basado
en el `sequencer`, `ProcessingStatus` y `ProcessingLeaseUntil` para:

- evitar dos procesamientos simultáneos de una entrega duplicada;
- no reprocesar una fila `complete`;
- permitir retry cuando la lease vence antes del timeout máximo;
- hacer reintentable inmediatamente una excepción capturable;
- volver a publicar un evento terminal si el resultado se guardó pero
  EventBridge falló (`TerminalEventPublished=false`).

El failure finalizer no puede convertir un documento ya `complete` en
`FAILED`, incluso si llega tarde una falla de una entrega duplicada.

### 6.4 Métricas nuevas

La ruta directa conserva `uploadToResultSeconds` y añade:

- `timings.ingressToHandler`: evento S3 → inicio del handler;
- `timings.preprocessing`: inicio del handler → inicio real del pipeline;
- `tracking.orchestration.route = direct-s3-lambda`.

Ya no debe publicar `queue` ni `orchestrationBeforeExtraction` en ejecuciones
nuevas; esos campos se conservan al leer resultados históricos.

### 6.5 Escrituras finales

Indexación, unique keys y auditoría de costos ahora se ejecutan en paralelo
con `ThreadPoolExecutor`; después se persiste el envelope terminal y se publica
EventBridge. Esto evita sumar serialmente tres round trips independientes.

## 7. Archivos modificados en esta última fase

| Archivo | Cambio |
|---|---|
| `src/document-processing/multi-model-document-processing.ts` | Construct reducido a bucket/tabla, trigger S3 directo, retry async y finalizador de fallas. |
| `src/document-processing/resources/async-failure-lambda/index.py` | Nuevo destino de fallas asíncronas; marca `FAILED` sin pisar éxitos. |
| `src/document-processing/resources/enrichment-lambda/handler.py` | Adaptador de eventos S3, claim/lease, metadata directa, finalización DynamoDB/EventBridge, republish e índices/costos paralelos. |
| `src/document-processing/resources/upload-lambda/body-parser.mjs` | Nuevas keys bajo `source/`; nombre `rawKeyFor` conservado por compatibilidad de imports. |
| `src/document-processing/resources/upload-lambda/index.mjs` | Guarda `SourceKey` y actualiza comentarios del flujo. |
| `src/document-processing/resources/query-lambda/reprocess-request.mjs` | Reprocess escribe en `source/` e hereda tipo desde `SourceKey` o `UploadKey`. |
| `src/document-processing/resources/query-lambda/index.mjs` | Resolución O(1) del original y fallback histórico; reprocess guarda `SourceKey`. |
| `src/document-validation-stack.ts` | Elimina prompts/modelos upstream no usados y pasa sólo las props del flujo directo. |
| `scripts/check-infra-migration.mjs` | Gate nuevo: bucket/tabla/bus estables, sin SQS/SFN, triggers sólo source/raw, retries + destino de falla. |
| `tests/unit/...` | Contratos de keys `source/`, prefijo legacy, adaptador S3 y métricas directas. |

No se hizo commit, por petición expresa del usuario. No se actualizaron los
diagramas ni la documentación arquitectónica extensa en esta fase, también por
petición expresa; quedan deliberadamente pendientes.

## 8. Verificación realizada

Comandos que pasaron durante esta fase:

```bash
python3 -m py_compile \
  src/document-processing/resources/enrichment-lambda/handler.py \
  src/document-processing/resources/async-failure-lambda/index.py

pnpm exec tsc --noEmit
pnpm run build
pnpm run synth:dev
node scripts/check-infra-migration.mjs --env dev
pnpm run test:unit
```

Resultados relevantes:

- TypeScript: OK;
- build de 30 catálogos activos: OK;
- synth dev: OK, usando Docker para el bundle Python;
- contrato de infraestructura directa: OK;
- suite Python completa: 1,217 tests OK;
- tests Node: 16 archivos/suites OK.

## 9. Pendientes antes de dar la migración por cerrada

1. Revisar `cdk diff` antes del deploy: deben eliminarse SQS, DLQ, State
   Machine y Lambdas upstream; bucket/tabla no deben reemplazarse.
2. Desplegar primero en dev.
3. Subir `emp_cert.pdf` con catálogo sincronizado y confirmar:
   - `tracking.orchestration.route = direct-s3-lambda`;
   - ausencia de `queue` y `orchestrationBeforeExtraction` nuevos;
   - `processingMs` cercano a decenas de ms en warm path;
   - `uploadToResultSeconds` dominado sólo por entrega S3/cold start;
   - `aiExecuted=false`, tokens 0, `ocrPages=0`.
4. Probar al menos un documento que sí requiera Textract y otro que sí requiera
   IA compacta; la optimización de infraestructura no debe cambiar calidad.
5. Probar un `AUTO`, una lista guiada de tipos, XML y CSV/XLSX.
6. Probar `GET /documents/{id}/file` y reprocess tanto para una fila nueva
   `source/` como para una fila histórica `processed/failed/`.
7. Forzar una excepción de Enrichment en dev y comprobar dos retries +
   `AsyncFailureFinalizer` + estado `FAILED`.
8. Actualizar después, en un cambio separado, `ARQUITECTURA.md`,
   `TRAZA_CODIGO_COMPLETA.md`, `LATENCIA.md`, `LATENCIA_RESPUESTA.md`, diagramas
   Mermaid/PNG y las referencias `raw/processed/failed` de las API docs.

## 10. Riesgos y decisiones que no deben perderse

- S3 → Lambda sigue siendo asíncrono y at-least-once. Eliminar la cola reduce
  latencia, pero no convierte el API en síncrono ni garantiza 0.5 s E2E.
- Un cold start de Enrichment todavía puede costar cientos de milisegundos. Si
  el siguiente objetivo es p95 estricto, medir antes de considerar provisioned
  concurrency.
- La Lambda tiene límite de 15 minutos. La lease expira diez segundos antes
  del tiempo restante para permitir recuperación de un timeout.
- `results/` debe permanecer fuera de cualquier notificación S3 de ingreso.
- No retirar todavía el trigger `raw/` hasta confirmar que no quedan URLs
  prefirmadas/callers directos con ese contrato.
- No borrar `processed/` o `failed/` históricos; Query los usa como fallback.
- `SourceKey` es ahora la ubicación autoritativa del original.
- `ProcessingStatus=FINISHED` se escribe directamente antes del evento. La
  Status Lambda queda por compatibilidad y proyección de eventos, no como dueño
  exclusivo del estado terminal.
- El resultado de negocio `RECHAZADO` no es una falla técnica: la ejecución
  termina `FINISHED` con `validation.status=RECHAZADO`.
- La ausencia de IA debe decidirse por `aiExecuted/aiUsage`, no por el modelo
  configurado en `tracking.schema.model`.

## 11. Próxima acción recomendada

Continuar desde el working tree actual, correr la suite completa, revisar el
diff de CloudFormation y desplegar dev. No rehacer la migración ni reintroducir
SQS/Step Functions salvo que una prueba E2E demuestre una necesidad concreta.
Después del deploy, comparar varias corridas warm/cold; una sola ejecución de
S3/Lambda no sirve para establecer p50/p95.

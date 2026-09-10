# Document Validator

Motor de validación y extracción inteligente de documentos usando Amazon Bedrock (Claude) + un motor de reglas determinístico configurable por JSON (sin tocar código, hot-reload vía DynamoDB).

El motor es genérico y mantiene dos familias de catálogo: **`base/`**, documentos oficiales o formatos típicos con una salida reutilizable, y **`custom/`**, contratos ligados a una empresa o sistema. Hoy hay 21 tipos activos: nueve tipos aduanales/fiscales/identidad/seguros ya establecidos, ocho catálogos beta para ingreso de personal (`CURP`, `ACTA_NACIMIENTO`, `COMPROBANTE_DOMICILIO`, `CONSTANCIA_NSS`, `PASAPORTE`, `CEDULA_PROFESIONAL`, `COMPROBANTE_ESTUDIOS` e `IDENTIFICACION`), tres CFDI XML beta para gastos aduanales (`CFDI_GASTO_TERCERO_XML`, `CFDI_PAGO_DERECHOS_SANITARIOS_XML` y `FACTURA_LOGISTICA_XML`) y `PEDIMENTO_ERP_XML`, un mapeo beta de la referencia aduanera de una factura CFDI hacia el contrato ERP. `IDENTIFICACION` hace una lectura genérica mínima y requiere `documentType=IDENTIFICACION`; no certifica autenticidad. `PEDIMENTO_ERP` vive en `custom/erp/` porque su salida está acoplada al ERP; `PEDIMENTO_ADUANAL` conserva el contrato neutral. Cualquier subárbol llamado `save/` queda aparcado y no entra a build/sync.

## Documentación

| Documento | Para qué |
|---|---|
| [`documentacion/COMO_FUNCIONA.md`](documentacion/COMO_FUNCIONA.md) | 🧭 Explicación general con diagramas — el mejor punto de partida. |
| [`documentacion/ARQUITECTURA.md`](documentacion/ARQUITECTURA.md) | 🏗️ Diagrama de arquitectura AWS + diagrama de flujo de ejecución, y qué modelo de Bedrock se usa. |
| [`documentacion/RESUMEN_EJECUTIVO.md`](documentacion/RESUMEN_EJECUTIVO.md) | 👔 Versión para directivos: el proceso explicado en una imagen, sin nombres de servicios ni tecnicismos. |
| [`documentacion/GUIA_DESPLIEGUE_Y_USO.md`](documentacion/GUIA_DESPLIEGUE_Y_USO.md) | 📘 Guía operativa completa: instalar, desplegar, subir documentos, actualizar/eliminar catálogos, troubleshooting. |
| [`documentacion/api-docs/`](documentacion/api-docs/README.md) | 📦 **Paquete de documentación del API para compartir** con quien conoce 0 el proyecto: guía de uso, contrato OpenAPI y ejemplos de código listos para copiar. |
| [`documentacion/api-docs/API_USO.md`](documentacion/api-docs/API_USO.md) | Referencia del API REST (todos los endpoints, request/response). |
| [`documentacion/api-docs/ejemplos/curl.md`](documentacion/api-docs/ejemplos/curl.md) | Runbook curl copiar-y-pegar (funciona sin importar el lenguaje del consumidor). |
| [`documentacion/api-docs/ejemplos/csharp.md`](documentacion/api-docs/ejemplos/csharp.md) | Integración completa en C#/.NET: upload, consulta, metadata, búsqueda y descarga. |
| [`documentacion/api-docs/openapi.yaml`](documentacion/api-docs/openapi.yaml) | Contrato OpenAPI 3.1 del mismo API. Ver localmente con `pnpm docs:api` (Scalar en `/`, Swagger UI en `/swagger`). |
| [`documentacion/APIS_INTEGRACION.md`](documentacion/APIS_INTEGRACION.md) | 🔌 Inventario de APIs para integrar con otros sistemas: internas (propias) vs. externas (servicios de AWS consumidos). |
| [`documentacion/AMBIENTES_Y_SEGURIDAD.md`](documentacion/AMBIENTES_Y_SEGURIDAD.md) | dev vs. prod, dominio propio, API keys, WAF. |
| [`documentacion/TESTS.md`](documentacion/TESTS.md) | Qué comando de test correr para cada caso, costos, cómo diagnosticar un bug. |
| [`documentacion/COSTOS.md`](documentacion/COSTOS.md) | Costos reales medidos (no estimados) por página/documento/mes. |
| [`documentacion/FLUJO_EJECUCION.md`](documentacion/FLUJO_EJECUCION.md) | Recorrido general del pipeline y su dataflow. |
| [`documentacion/TRAZA_CODIGO_COMPLETA.md`](documentacion/TRAZA_CODIGO_COMPLETA.md) | Traza técnica independiente, función por función y línea por línea, con payloads, bifurcaciones y llamadas AWS. |
| [`documentacion/ENVIAR_DOCUMENTO_API.md`](documentacion/ENVIAR_DOCUMENTO_API.md) | Cómo mandar un documento y leer su resultado (script o curl). |
| [`catalogs/README.md`](catalogs/README.md) | El DSL del catálogo: shape de cada campo, todas las `rule_type`, cómo crear un tipo nuevo. |
| [`AGENTS.md`](AGENTS.md) | Guía para agentes de IA trabajando en este repo (arquitectura, convenciones, comandos). |

---

## Modelo y arquitectura

El pipeline usa **Amazon Bedrock** (Claude Haiku 4.5, `us.anthropic.claude-haiku-4-5-20251001-v1:0`) para clasificar y extraer, sobre una arquitectura 100% serverless en AWS. Diagrama de arquitectura AWS completo + diagrama de flujo de ejecución (los dos caminos de `handler.py`, con referencias a los módulos reales): **[`documentacion/ARQUITECTURA.md`](documentacion/ARQUITECTURA.md)**.

Características clave del pipeline de expedientes:

- **Costo-eficiente**: el texto nativo del PDF es gratis (pypdf); Textract OCR solo corre en páginas sin capa de texto. Costo real medido: ~$0.0144/página, ~$0.058 un documento de 3 páginas — ver [`documentacion/COSTOS.md`](documentacion/COSTOS.md).
- **Anti-invención**: el modelo copia valores textuales o devuelve `null`; la normalización (montos, fechas, número de pedimento, UUIDs) es determinística y nunca sustituye el valor extraído (`extractedValue` vs. `normalizedValue`).
- **Estados por campo**: `VALIDATED`, `INVALID`, `LOW_CONFIDENCE`, `NOT_FOUND`, `NOT_APPLICABLE`, `REVIEW_REQUIRED`.
- **Estados por documento/expediente**: `APPROVED`, `REJECTED`, `PARTIALLY_APPROVED`, `REVIEW_REQUIRED`, `PROCESSING_ERROR`.
- **Reglas dinámicas en JSON, sin redeploy**: `regex`, `regex_each`, `presence`, `boolean_true`, `date_expiry`, `date_max_age`, `minimum_items`, `custom_formula`, `cfdi_total`, `sum_array`, `equals_field`, `array_length`, `cross_document_match`, `cross_document_amount`, `negative_keyword`, `expected_value_by_app`, `page_ranges_no_overlap` — lista completa y semántica en [`catalogs/README.md`](catalogs/README.md).
- **Checks de expediente completo**: mismo pedimento en todos los documentos, RFC del importador consistente (ignora RFCs genéricos), CFDIs duplicados (copia legítima vs. doble cobro), conciliación de gastos y valores.

### ¿Cómo sabe el modelo dónde buscar cada campo?

Cada campo del catálogo trae un `hint` en español libre, junto a su tipo y sus reglas de validación (DSL field-centric — un solo lugar por campo, ver [`catalogs/README.md`](catalogs/README.md)):

```jsonc
"NUMERO_PEDIMENTO": {
  "type": "string",
  "required": true,
  "hint": "Número de pedimento de 15 dígitos, en el encabezado, formato AA MM PATENTE-CONSECUTIVO.",
  "validation": [{ "rule_type": "regex", "pattern": "^\\d{15}$", "message": "Debe tener 15 dígitos" }]
}
```

---

## Requisitos

- Node.js 20+, pnpm 10+ (`corepack enable`, versión pineada `pnpm@10.20.0`)
- Python 3.12+ (tests locales y bundling de la Lambda)
- AWS CLI configurado, con acceso a Amazon Bedrock (modelo `us.anthropic.claude-haiku-4-5-20251001-v1:0` en us-east-1) y Textract
- AWS CDK v2 (`npm install -g aws-cdk`, o `npx cdk` ya incluido)

## Instalación y primer despliegue

```bash
git clone <repo> && cd documents-validator
pnpm install
pnpm setup:python                     # crea .venv para tests locales
cp .env.example .env.dev              # revisa/ajusta BEDROCK_MODEL_ID, etc.

pnpm build                            # combina catalogs/ + parchea el blueprint + compila TS
pnpm run diff                         # opcional: ver qué se va a crear
pnpm run deploy                       # despliega a DEV (seguro, nunca toca prod)
```

Guía paso a paso completa (primer deploy, verificación, prod con dominio propio): [`documentacion/GUIA_DESPLIEGUE_Y_USO.md`](documentacion/GUIA_DESPLIEGUE_Y_USO.md).

## Comandos principales

```bash
pnpm build                            # catalogs/ → config/catalogs.json + patch + tsc
pnpm run deploy / deploy:dev / deploy:prod
pnpm run diff                         # cambios pendientes vs. lo desplegado
pnpm sync                             # sube TODA la carpeta catalogs/ a DynamoDB DEV (hot-reload)
pnpm sync -- --prune                 # además deshabilita filas remotas que ya no tienen JSON activo
pnpm sync:one -- --type PEDIMENTO_ERP       # sube un solo tipo a dev (--dry-run para validar)
pnpm sync:prod / sync:one:prod        # lo mismo contra la tabla de catálogo de PROD
pnpm procesar -- mi_doc.pdf           # procesa localmente sin pasar por el stack (tus credenciales AWS)
pnpm enviar mi_doc.pdf PEDIMENTO_ERP          # sube al stack desplegado
pnpm enviar facturas.pdf FACTURA_FLETE_EXTRANJERO --occurrence-mode multiple
pnpm resultado -- --id <documentId>   # trae y formatea el resultado
pnpm reporte                          # dev + prod juntos (filtro "Ambiente" en el HTML); -- --env dev|prod para uno solo
pnpm test:unit                        # tests offline del motor de reglas ($0)
pnpm test:quality                     # suite gratuita + JUnit + cobertura + histórico
pnpm test:real -- --env dev           # todo el ground truth por el stack E2E real (con costo)
pnpm comparar -- normal_pend.pdf      # esperado vs. extraído de un documento (AWS, centavos)
pnpm test:pedimentos                  # suite completa de ground truth (AWS, ~$1)
```

Tabla completa de comandos (incluye `test:e2e:pedimento`, `schema:sensitivity`, `backfill:index`, `track`, etc.) en [`AGENTS.md`](AGENTS.md#commands) y el detalle de tests en [`documentacion/TESTS.md`](documentacion/TESTS.md).

## Catálogo de tipos documentales

La carpeta **`catalogs/`** (un archivo JSON por tipo) es la única fuente de verdad. `pnpm build` y `pnpm sync` recorren sus subcarpetas de forma recursiva, ignoran archivos `_*.json` y excluyen cualquier subárbol `save/`. Los tipos activos se agrupan en `base/<dominio>/` y `custom/<integración>/`; DynamoDB gana siempre sobre lo empaquetado en runtime.

Agregar un tipo nuevo:

```bash
cp catalogs/_TEMPLATE.json catalogs/base/mi_dominio/MI_TIPO.json
# edita el archivo siguiendo el DSL field-centric — ver catalogs/README.md
pnpm sync:one -- --type MI_TIPO       # inmediato (DynamoDB), o
pnpm run build && pnpm run deploy     # permanente (empaquetado)
```

Guía completa del DSL (shape de cada campo, todas las `rule_type`, campos computados, reglas cross-documento, cuándo usar `pipeline: "expediente"`): **[`catalogs/README.md`](catalogs/README.md)**.

## Estructura del proyecto

```
documents-validator/
├── bin/app.ts                              # entrypoint CDK (env dev|prod)
├── src/
│   ├── document-validation-stack.ts        # stack CDK (infraestructura)
│   ├── document-catalog.ts                 # lee catalogs/ (DSL legacy) para generar prompts
│   ├── prompt-generator.ts                 # genera prompts Bedrock del flujo estándar
│   └── document-processing/resources/
│       ├── enrichment-lambda/
│       │   ├── handler.py                  # motor de validación (NO modificar a la ligera)
│       │   ├── document_catalog.py         # carga catalogs/ empaquetado + DynamoDB
│       │   ├── config/catalogs.json        # ← GENERADO por pnpm build, no editar
│       │   ├── validators/                 # engine.py (motor de reglas) + validadores legacy
│       │   ├── pipeline/                   # pipeline de expediente: OCR, segmentación,
│       │   │                               #   extracción, normalización, liquidación, QR
│       │   ├── extractors/                 # wrappers de Textract
│       │   └── custom_validators/          # validadores Node.js (solo flujo estándar)
│       ├── upload-lambda/index.mjs         # POST /documents
│       │   └── body-parser.mjs             #   parseo del body + normalización de metadata
│       └── query-lambda/index.mjs          # GET/PATCH + POST /documents/{id}/reprocess
│           ├── reprocess-request.mjs       #   tipos/cardinalidad y key del reproceso
│           ├── search-filters.mjs          #   filtros de búsqueda (tipo, sistema, metadata, campos)
│           └── metadata-rules.mjs          #   merge patch y contrato plano/ASCII/2 KB
├── catalogs/
│   ├── _shared.json                        # field_sets, reglas cross-documento, campos comunes
│   ├── _TEMPLATE.json                      # plantilla DSL field-centric para tipos nuevos
│   ├── base/                               # contratos oficiales o reutilizables
│   │   ├── aduanas/
│   │   ├── fiscal/
│   │   ├── identidad/                     # INE, IDENTIFICACION, CURP, pasaporte, etc.
│   │   ├── recursos_humanos/
│   │   └── seguros/
│   ├── custom/erp/                        # contratos acoplados como PEDIMENTO_ERP
│   ├── save/                               # tipos legacy aparcados (no leídos por build/sync)
│   └── README.md                           # spec completa del DSL
├── scripts/                                # CLI: sync, enviar, procesar, resultado, tests, track
├── documentacion/                          # guías operativas (ver tabla arriba)
├── tests/integration/expected_pedimentos.json  # ground truth de pedimentos
└── test-docs/                              # PDFs de prueba (solo local, sin PII real en commits)
```

## Ambientes

Dos stacks aislados y completos por `-c env=dev|prod`: `DocumentValidationStack-Dev` (default, API abierta, para probar) y `DocumentValidationStack` (dominio propio + API key + WAF). Detalle: [`documentacion/AMBIENTES_Y_SEGURIDAD.md`](documentacion/AMBIENTES_Y_SEGURIDAD.md).

## Resultado de validación

Cada documento procesado produce un resultado con `validation.status` (`APROBADO`/`RECHAZADO`/`REQUIERE_REVISION`) y, para tipos aduanales, el detalle completo del expediente (documentos detectados, campos con evidencia y confianza, checks cruzados). Shape completo, incluyendo dónde vive cada nivel de detalle (DynamoDB compacto vs. S3 completo): [`documentacion/api-docs/API_USO.md`](documentacion/api-docs/API_USO.md).

## Autores

Luis Hernández M.

### Colaboradores

# `catalogs/` — Base única de esquemas

Esta carpeta es la **única fuente de verdad** de los tipos de documento. Un
archivo JSON por tipo, más un archivo compartido. No hay otro lugar donde se
definan tipos — no se toca `handler.py` para agregar/editar un tipo. Las
carpetas expresan si el contrato es reutilizable (`base`) o está acoplado a
una integración (`custom`); el `TYPE_ID`, no la ruta, sigue siendo la identidad
pública del tipo.

```
catalogs/
├── _shared.json      → globals: _field_sets, _common_item_fields, _field_presets, cross_document_validation_rules, _global_output_fields
├── _TEMPLATE.json     → plantilla para crear un tipo nuevo (se ignora en build/sync)
├── base/
│   ├── aduanas/       → contratos neutrales de comercio exterior
│   ├── fiscal/        → documentos fiscales reutilizables
│   ├── identidad/     → identidad y domicilio
│   ├── recursos_humanos/ → alta y acreditación profesional
│   └── seguros/
├── custom/
│   ├── erp/           → variantes acopladas, como PEDIMENTO_ERP
│   └── portal/        → contratos del portal (sufijo _PORTAL): constancia fiscal, identificación,
│                        estado de cuenta, contrato, acta constitutiva, registro público
└── save/              → archivo inactivo; nunca entra a build/sync
```

- El **nombre del archivo** es el `TYPE_ID` (UPPER_SNAKE_CASE): `PEDIMENTO_ADUANAL.json`.
- Los archivos que empiezan con `_` son especiales (no son tipos).
- `pnpm build`/`pnpm sync` recorren recursivamente todas las subcarpetas,
  excepto cualquier directorio llamado exactamente `save`. Mover un tipo a
  `save/` lo quita del bundle; usa `pnpm sync -- --prune` para marcar también
  `enabled=false` una fila antigua que aún exista en DynamoDB.
- Cada archivo debe declarar exactamente un `TYPE_ID`, la llave debe coincidir
  con el nombre del archivo y no puede repetirse en otra carpeta. Build/sync
  fallan temprano si se viola cualquiera de estas reglas.

Los catálogos de ingreso `CURP`, `ACTA_NACIMIENTO`, `CONSTANCIA_NSS` y
`PASAPORTE` están activos como beta y participan en autoclasificación.
`COMPROBANTE_DOMICILIO`, `CEDULA_PROFESIONAL` y `COMPROBANTE_ESTUDIOS` también
están activos, pero declaran `segmentation_candidate: false`: mientras no se
calibren con muestras representativas se usan enviando `documentType`.
`IDENTIFICACION` sigue la misma regla: se invoca explícitamente para decidir si
un archivo tiene la estructura mínima de una identificación y extraer sus datos
fundamentales; no compite con tipos específicos ni afirma autenticidad.

`CONSTANCIA_SITUACION_FISCAL_PORTAL` (`custom/portal/`, beta) entrega los mismos
campos que `CONSTANCIA_SITUACION_FISCAL` más `PAIS` y la identificación fiscal
neutral (`ID_FISCAL`, `TIPO_ID_FISCAL`, `OTROS_IDENTIFICADORES`), y acepta tanto
la constancia del SAT como el documento equivalente de cualquier país (W-9 del
IRS, tarjeta NIF de la AEAT, cuenta GST/HST de la CRA, certificado de la ATO,
extracto del registro mercantil holandés...). **Cada país exige solo su propio
identificador**: a una mexicana se le exige el `RFC` con su formato (y lo propio
del SAT); a una extranjera, `ID_FISCAL`, y su `RFC` queda vacío. `ID_FISCAL` se
puede leer siempre: en una mexicana es una copia del RFC
([`value_from`](#copiar-el-valor-de-otro-campo-value_from-y-no-repetirlo-en-una-lista-exclude_rows)).
Las reglas llevan [`when`](#when-a-qué-documentos-aplica-la-regla) sobre `PAIS`.
`ESTADO_CUENTA_PORTAL`, `ACTA_CONSTITUTIVA_PORTAL` y `CONTRATO_SERVICIO_PORTAL`
(`PROVEEDOR_ID_FISCAL`) siguen la misma convención: en ninguno un `RFC` carga un
identificador extranjero. También declara `segmentation_candidate: false`: se
usa enviando `documentType`. **Se calibra con el modelo desplegado** (MiniMax),
no solo con el default de `pnpm procesar`: se comportan distinto.

Los otros cinco tipos de `custom/portal/` (beta) cubren el resto del expediente
de alta de un proveedor y siguen el mismo patrón: **un tipo por casilla de carga
del portal**, que acepta la variante mexicana y la extranjera del documento, con
`when` para que las reglas mexicanas no reprueben a un documento de otro país.

| Tipo | Qué acepta | Lo que decide las reglas |
|---|---|---|
| `IDENTIFICACION_OFICIAL_PORTAL` | INE, pasaporte de cualquier país, licencia de conducir, cédula, tarjeta de residencia | `TIPO_IDENTIFICACION` (lista cerrada). `INE` fija `PAIS = MEX` (`derived`); la vigencia se exige y se compara contra hoy |
| `ESTADO_CUENTA_PORTAL` | Carátula de estado de cuenta, carta del banco o instrucciones de transferencia | `PAIS` del banco. México exige CLABE de 18 dígitos (que además fija `PAIS = MEX`); fuera de México, número de cuenta o IBAN. Además verifica que el archivo no fue tocado: preset `INTEGRIDAD_PDF`, una revisión visual de la página (`vision.page_review`) y `ORIGEN_DOCUMENTO` — un estado de cuenta generado desde Word es un recorte pegado. Todo en `review` |
| `CONTRATO_SERVICIO_PORTAL` | Contrato de prestación de servicios o de obra, u orden/presupuesto aceptado | Sin variantes: tipo de contrato, las dos partes y el objeto |
| `ACTA_CONSTITUTIVA_PORTAL` | Escritura constitutiva, protocolización de asamblea o su equivalente extranjero | `PAIS`. México exige número de escritura y notario; `TIPO_INSTRUMENTO` informa, no reprueba |
| `REGISTRO_PUBLICO_COMERCIO_PORTAL` | Boleta de inscripción del RPC (SIGER) con o sin forma precodificada, o extracto de un registro extranjero | `PAIS`. México exige folio mercantil y al menos un acto inscrito |

Los nombres de campo se repiten **a propósito** entre ellos y con la constancia
fiscal (`PAIS`, `RFC`, `NOMBRE_O_RAZON_SOCIAL`, `REGIMEN_CAPITAL`, `NOMBRES`,
`PRIMER_APELLIDO`, `SEGUNDO_APELLIDO`; y entre acta y registro, los datos del
instrumento, `SOCIOS` y `REPRESENTANTES_LEGALES`): el portal compara los
documentos de un proveedor campo contra campo. `tests/unit/test_portal_onboarding_types.py`
lo fija; si renombras uno, renómbralo en todos. Los cinco declaran
`segmentation_candidate: false`.

## Los 2 únicos comandos

```bash
pnpm sync                              # sube TODA la carpeta catalogs/ a DynamoDB (hot-reload)
pnpm sync:one -- PEDIMENTO              # sube UN tipo (por nombre difuso)
pnpm sync:one -- --type INE             # por TYPE_ID exacto
pnpm sync:one -- --file catalogs/base/identidad/INE.json # por archivo
pnpm sync -- --prune                     # deshabilita tipos remotos sin JSON activo
```

Opciones: `--dry-run` (no escribe), `--prune`, `--env dev|prod`, `--table`, `--region`.
Sin `--prune`, un sync completo solo **reporta** filas habilitadas que existen
en DynamoDB pero ya no en el árbol activo. La poda nunca borra la fila: cambia
`enabled=false`, y un sync futuro del mismo tipo la reactiva.

**Ambiente:** cada ambiente tiene su propia tabla de catálogo
(`document-validation-catalog-dev` / `document-validation-catalog`). Igual que
`deploy`, el default es **dev**; para producción usa `pnpm sync:prod` /
`pnpm sync:one:prod` (o `--env prod`). El comando imprime `[env=…]` y la tabla
destino antes de escribir.

`sync`/`sync:one` escriben a **DynamoDB** → efecto **inmediato, sin redeploy**
(siempre que el stack tenga `enableDynamoCatalog: true`, que es el caso hoy —
ver `bin/app.ts`). Para hornear los cambios en la Lambda (línea base
permanente, la que se usa si DynamoDB llegara a fallar o vaciarse) usa el
build normal:

```bash
pnpm run build && pnpm run deploy
```

`pnpm run build` combina automáticamente los JSON activos bajo `catalogs/**`
empaquetado (`src/document-processing/resources/enrichment-lambda/config/catalogs.json`,
generado — nunca se edita a mano, está en `.gitignore`). **DynamoDB siempre
gana sobre lo empaquetado en runtime** — si un tipo tiene fila en DynamoDB,
lo que digan los JSON locales no importa hasta que sincronices de
nuevo.

## Cómo crear un tipo nuevo (DSL field-centric)

Desde 2026-07-28 el único DSL vigente es el **field-centric** (`schema_version`
4.x en adelante — el número exacto es solo informativo, ver
[Consideraciones](#consideraciones-y-gotchas)). Cada campo se declara **una
sola vez**: su tipo, si es obligatorio, el hint de dónde vive en el documento
y sus reglas de validación viven juntos bajo `fields`, en vez de repartirse
entre `extraction_fields`/`extraction_hints`/`required_fields`/`validation_rules`
como en el DSL viejo.

0. **Elige la plantilla según la FUENTE del documento, no según qué documento
   de negocio es** — el mismo documento real puede llegar de formas distintas
   (un PDF escaneado, el XML nativo del mismo comprobante, un export Excel
   del mismo reporte) y cada forma es un tipo de catálogo distinto:

   | Fuente | Plantilla | Extracción | Costo Bedrock |
   |---|---|---|---|
   | PDF/escaneo (un modelo tiene que leerlo) | `catalogs/_TEMPLATE.json` | `hint` por campo, vía Bedrock | El normal de extracción |
   | XML ya estructurado (CFDI, VUCEM, ...) | `catalogs/_TEMPLATE_XML.json` | `xpath`/`namespaces` por campo, determinística | **$0** (ver [Tipos con fuente XML](#tipos-con-fuente-xml-xpath-item_xpath-namespaces)) |
   | XLSX/CSV ya estructurado | `catalogs/_TEMPLATE_TABULAR.json` | `cell`/`column` por campo, determinística | **$0** en XLSX; en CSV requiere `documentType` declarado para serlo (ver [Tipos con fuente tabular](#tipos-con-fuente-tabular-cell-column-xlsxcsv)) |

   Ejemplo real: `FACTURA_HONORARIOS.json` (PDF de cortesía del agente
   aduanal, con secciones que el XML no trae) vs. `FACTURA_HONORARIOS_XML.json`
   (el CFDI nativo del SAT del mismo comprobante) son dos tipos DISTINTOS,
   deliberadamente con campos distintos — no el mismo tipo con dos orígenes.

1. **Copia la plantilla que corresponda**:
   ```bash
   cp catalogs/_TEMPLATE.json catalogs/base/identidad/ACTA_NACIMIENTO.json
   cp catalogs/_TEMPLATE_XML.json catalogs/base/fiscal/NUEVO_CFDI_XML.json
   cp catalogs/_TEMPLATE_TABULAR.json catalogs/custom/erp/REPORTE_GASTOS_XLSX.json
   ```
2. **Edita** el archivo: renombra la llave (`NUEVO_TIPO`/`NUEVO_TIPO_XML`/
   `NUEVO_TIPO_XLSX` según la plantilla) al `TYPE_ID` elegido — debe coincidir
   con el nombre del archivo —, ajusta `description` (es lo que ve el
   clasificador — sé discriminante: menciona encabezados, folios, sellos, lo
   que lo distingue de documentos parecidos; para XML/tabular con
   `documentType` siempre declarado esto casi nunca se usa, pero documenta
   igual), y define `fields` con `hint` (PDF) o `xpath` (XML) o `cell`/`column`
   (tabular) según la plantilla. Borra las llaves `__NOTAS__`/`__COMENTARIO_*__`
   (son solo documentación del template, el loader las ignora igual, pero no
   aportan nada en un tipo real).
3. **Aplícalo**:
   ```bash
   pnpm sync:one -- --type ACTA_NACIMIENTO     # inmediato (DynamoDB), o
   pnpm run build && pnpm run deploy           # permanente (empaquetado)
   ```
   Para XML/XLSX/CSV, además recuerda: la subida SIEMPRE debe declarar
   `documentType` (`raw/{TYPE_ID}/{uuid}/archivo.ext`) — no hay clasificación
   automática por visión sobre datos ya estructurados.

### Entry de nivel de tipo

| Campo | Leído por el código | Qué es |
|---|---|---|
| `catalog_status` | no, metadato interno | Madurez operativa: `stable` para tipos calibrados o `beta` mientras falten muestras representativas. No aparece en `GET /document-types` ni cambia el pipeline. |
| `description` | sí | Va al prompt de clasificación (Bedrock) **y** encabeza cada llamada de extracción ("Documento DECLARADO como X. {description}"), donde además es lo que el modelo usa para decidir si el archivo de verdad es ese documento (ver `type_check`). Sé discriminante, pero no la escribas como una lista cerrada de formatos: medido con MiniMax, una descripción que enumera formatos como si fueran los únicos hace que una variante legítima (un extracto de registro holandés) salga vacía o se tome por otro documento. |
| `fields` | sí | El árbol de campos — ver abajo. Su sola presencia es lo que activa el DSL field-centric (`document_catalog.py: if "fields" in data`). |
| `display_name` | sí | Nombre legible (`"Pedimento Aduanal"`). Se guarda en `DocumentTypeName` y el API lo acepta en `?docType=`. Si falta, se deriva del `TYPE_ID`. |
| `aliases` | sí | Otros nombres que el API acepta en `?docType=` (`["pedimento", "pedimento de importación"]`). |
| `min_confidence` | sí | Umbral 0-1: un campo escalar con confianza por debajo se marca inválido (`confidence {x} below minimum {min}`) **pero conserva su valor**, para que una persona lo juzgue. Default 0.70. |
| `discard_below_confidence` | sí | Piso 0-1 por debajo del cual el valor **no se entrega**: sale `value: null`, `valid: false` y un `reason` que lo dice, conservando la confianza baja que lo explica. Más estricto que `min_confidence` (que solo señala): un número en el que nadie confía pero que *parece* dato es peor aguas abajo que un blanco explícito. Default 0.50 (`pipeline/confidence.py`). Solo aplica a campos **con** valor: un campo opcional ausente no está "descartado", nunca estuvo. |
| `schema_version` | solo informativo | Se registra en `tracking.schema.typeVersions` de cada resultado para saber qué versión de schema procesó el documento. **No cambia ningún comportamiento del código** — no hay ningún `if schema_version == ...` en el pipeline. Súbelo cuando quieras dejar rastro de un cambio relevante, no por obligación. |
| `occurrence_mode` | sí, opcional | Default del tipo cuando el POST no manda `occurrenceMode` ni `:N`/`:all`: `single` conserva la primera ocurrencia; `multiple` conserva todas. Si falta, aplica el default global: `single` con un tipo declarado, `multiple` con varios. |
| `pipeline: "expediente"` | sí, pero opcional | Ver [`pipeline: expediente`](#pipeline-expediente-qué-controla-realmente) abajo — **ya no es requisito para que se extraigan los campos.** |
| `deterministic_extraction` | sí, opcional | Activa el mapeo de texto antes de Bedrock para PDF/imagen field-centric. `fallback` admite `missing_fields` (modelo solo para ramas faltantes), `full_model` o `review`. Ver [Extracción determinística desde texto](#extracción-determinística-desde-texto-text_source). |
| `model_id` | sí, opcional | ID de modelo Bedrock (o ARN de inference-profile) que extrae específicamente ESTE tipo — pisa `BEDROCK_EXTRACTOR_MODEL_ID`/`BEDROCK_MODEL_ID` configurados por variable de entorno. Aplica a cualquier fase que corra para este tipo (`extractor`, y `recovery` si el tipo declara `extraction_strategy`) salvo que esa fase tenga su propia entrada en `model_ids`. Ausente o `null` → hereda lo configurado. |
| `model_ids` | sí, opcional | Igual que `model_id` pero por FASE: `{"extractor": "...", "recovery": "..."}`. Pisa a `model_id` cuando ambos nombran la misma fase — así un tipo puede fijar solo `recovery` (p. ej. porque su fallback de Textract necesita un modelo distinto al de extracción normal) sin tocar las demás. `segmenter` no aplica aquí: la segmentación de un expediente corre ANTES de saber a qué tipo pertenece cada rango de páginas, así que no hay un `type_entry` del que leer el override — solo responde a `BEDROCK_SEGMENTER_MODEL_ID` y al override de desarrollo. La fase **`vision`** (etapa de autenticidad) también se fija aquí — `{"vision": "us.amazon.nova-2-lite-v1:0"}` — y es independiente de todo lo demás: resuelve por una cadena propia y más corta, `model_ids.vision` → `BEDROCK_VISION_MODEL_ID` → Haiku 4.5, y **nunca** hereda `model_id`, `BEDROCK_MODEL_ID` ni el override de desarrollo (son modelos de texto; el general de este despliegue no acepta imágenes). Su respaldo por indisponibilidad tampoco es la lista compartida: es `authenticity.vision.fallback_models` del propio tipo (`[]` = no cambia de modelo). El modelo debe aceptar imágenes en Converse — candidatos probados y medidos en `documentacion/MODELOS_VISION.md`; compáralos en tu tipo con `pnpm vision:comparar` antes de fijar uno. |
| ↳ jerarquía completa | — | De mayor a menor prioridad: **(1)** override de desarrollo por request (`ALLOW_MODEL_OVERRIDE`, dev-only, ver `document-validation-stack.ts` — fuerza TODA la invocación sin importar tipo ni fase) → **(2)** `model_ids[fase]` del catálogo → **(3)** `model_id` del catálogo → **(4)** `BEDROCK_<FASE>_MODEL_ID` de entorno → **(5)** `BEDROCK_MODEL_ID` de entorno / fallback fijo. La utilidad que implementa y documenta este orden es `pipeline/bedrock.py::resolve_model` (`_model_candidates` es la única fuente de verdad del orden; `explain_model_resolution` expone los 5 candidatos y cuál ganó, útil para depurar "por qué este documento usó el modelo X"). Esto decide solo **qué modelo se intenta primero**; si ese modelo falla por indisponibilidad (no habilitado, no existe, error/timeout del proveedor), `pipeline/bedrock.py::_invoke_with_fallback` cambia al siguiente modelo de una lista ORDENADA E INDEPENDIENTE (`pipeline/model_fallback.py`, tabla DynamoDB `FALLBACK_MODELS_TABLE` opcional) — ver `documentacion/GUIA_DESPLIEGUE_Y_USO.md` §5.6. Los dos mecanismos no se mezclan: el catálogo nunca es candidato de fallback, y la tabla de fallback nunca decide qué se intenta primero. |
| `validator_module` | sí, **solo en el flujo estándar** | Ver [Custom con scripts](#custom-con-scripts). |
| `custom_validator` | sí, **solo en el flujo estándar** | Ídem. |
| `business_destination` | sí | Metadato de ruteo de negocio; aparece como `businessDestination` en cada documento del expediente. Puramente informativo para el consumidor. |
| `cross_field_validation` | sí | Reglas que cruzan **varios campos del mismo documento** (fórmulas, sumas de arrays) — no pueden vivir dentro de un solo campo. Mismo shape de regla que dentro de `fields[].validation`, pero con `"field"` explícito. |
| `qr_recovery` | sí | Recuperación determinística de campos desde un QR embebido. Declara cómo localizar la página, filtrar la URL, mapear parámetros y publicar el payload/estado; ver la sección siguiente. |
| `unique_key` / `unique_keys` | sí | Campo(s) cuyo valor identifica UN documento de este tipo por sí solo, indexados como `TIPODOC#valor` para que buscar por ellos sea una sola Query. Normalmente se declara sobre el campo (`fields[X].unique_key`); aquí, a nivel de tipo, es la única forma para un tipo del DSL legado. Ver [Unique keys](#unique-keys-unique_key-buscar-por-un-campo-sin-recorrer-el-tipo). |
| `page_selection` | sí | Selección de páginas de interés: manda a Bedrock **solo** las páginas cuyo texto cumple los marcadores del tipo, en vez del documento completo. Opt-in por tipo; ver la sección siguiente. |
| `type_check` | sí, opcional | Ajusta la **verificación de tipo** ("¿este archivo ES el documento que se declaró?"), que todo tipo con `fields` tiene encendida por default: `on_fail` (`reject` default / `review` / `info`), `min_required_filled` (0.5) y `enabled`. Ver [Verificación de tipo](#verificación-de-tipo-type_check-el-archivo-es-lo-que-se-declaró). |
| `segmentation_candidate` | sí, opcional | **Solo `false` tiene efecto**: saca al tipo de la lista de candidatos de la clasificación automática (subidas sin `documentType`). Ausente = se decide por la forma del tipo. Es el escape hatch para un tipo que el clasificador confunde con un hermano — `PEDIMENTO_ERP` lo usa, por ser el mismo documento físico que `PEDIMENTO_ADUANAL`. Nunca afecta a un tipo que el caller declara explícitamente. También lo saca del prompt de clasificación del flujo upstream, que viaja en una variable de entorno de Lambda (4KB): cada tipo listado ahí cuesta ~140 bytes, y un tipo nuevo que solo se usa declarándolo **debe** llevar este flag o el deploy acaba fallando por tamaño (el synth lo avisa antes). Ver [Candidatura de auto-clasificación](#candidatura-de-auto-clasificación-segmentation_candidate). |
| `form_mapping` | sí, pero heredado | Mecanismo viejo para campos computados (ver más abajo) — hoy solo tiene sentido si no usas la convención `concept_aliases`/`source`/`filters` directo en `fields`. Para tipos nuevos, prefiere esa convención. |
| `prompt_profile` | **no, reservado** | Se lee en `_shared.json._model_profiles` pero ningún código lo consume todavía. No lo confundas con algo funcional. |
| `field_sets` | **no, ignorado en este DSL** | Solo aplica al DSL legado (tipos sin `fields`). En un tipo field-centric no hace nada — no lo agregues. |

### Shape de un campo (`fields[NOMBRE]`)

```jsonc
"NOMBRE_DEL_CAMPO": {
  "type": "string",        // string | number | boolean | date | object | array
  "required": true,        // afecta el check de presencia y el status del documento
  "hint": "Dónde vive el dato en el documento, en español, para el modelo.",
  "validation": [ /* reglas — ver tabla de rule_type abajo */ ],

  // Solo si type == "object": recursa con el MISMO shape.
  "fields": { "SUBCAMPO": { "type": "...", ... } },

  // Solo si type == "array" con filas estructuradas: UNA fila, se aplica a todas.
  "item_fields": {
    "$common": ["PAGINA", "ORDEN", "VALOR_CRUDO"],   // ver _common_item_fields
    "SUBCAMPO": { "type": "...", ... }
  },
  // Alternativa si type == "array" es de escalares sueltos (sin item_fields):
  "item_type": "string",

  // Solo si el valor lo calcula el pipeline, no el modelo (ver más abajo):
  "computed": true,

  // Solo si el valor se DECIDE por una condición sobre otros campos ya
  // extraídos, en vez de leerse del documento (ver "Campos derivados"):
  "derived": {
    "from": "CUADRO_LIQUIDACION.TOTAL",
    "cases": [
      { "when": { "gt": 383 }, "value": 1, "label": "Anticipo y Pasivo" },
      { "when": { "lte": 383 }, "value": 3, "label": "Solo Pasivo" }
    ]
  }
}
```

- **Escalar** (`string`/`number`/`boolean`/`date`) → sale como
  `{value, confidence, valid, reason, source}`.
  `confidence` **siempre es un número 0-1, nunca `null`** (ver
  [El contrato de `confidence`](#el-contrato-de-confidence)).
- **`type` no es decorativo: decide cómo se normaliza el valor.** Un campo
  `string` nunca se convierte en número aunque su nombre suene a importe —
  `num_acuse_valor` contiene "VALOR", y por eso el acuse
  `COVE2689JP013 / 35079805` salía como `268901335079805.0`, sin COVE y sin
  diagonal, marcado `NORMALIZED` y válido; lo mismo le pasaba a
  `ENTIDAD_FEDERAT`**IVA**. Y al revés: un campo `number` sí se parsea aunque
  su nombre no dispare ninguna heurística (`FACTOR_MONEDA_FACTURA` salía como
  el string `"1.0"`). `date` → ISO `YYYY-MM-DD`; `boolean` se deja intacto.
  Las convenciones de NOMBRE de `pipeline/normalize.py` (`TOTAL`/`IMPORTE`/
  `VALOR` = importe, `FECHA` = fecha, `RFC`/`UUID`, número de pedimento) solo
  deciden donde no hay tipo declarado: el DSL legacy. Un array de escalares
  usa su `item_type`.
- **Objeto** (`type: "object"`) → sale como
  `{valid, reason, fields: {SUBCAMPO: {...recursivo...}}}`. Un objeto
  `required` cuyos subcampos están TODOS vacíos cuenta como faltante: el objeto
  es lo que traen sus subcampos (antes pasaba como "presente" sin un solo dato). Su propio
  `validation` corre a nivel del objeto completo (ej. una suma que cruza dos
  subcampos — usa `"subfield": "TOTAL"` en la regla para apuntar a uno
  específico, ver `CUADRO_LIQUIDACION` en `PEDIMENTO_ADUANAL.json`).
- **Array** (`type: "array"`) → sale como `{items: [ {...recursivo...} ]}`,
  una entrada por fila detectada.
- **Las reglas dentro de `item_fields` corren FILA POR FILA** (y sus
  `required` también). Se reportan con el índice de la fila —
  `FACTURAS[0].Factura` — así una fila mala no contamina a las buenas: esa
  fila sale `valid: false` con su `reason`, las demás siguen válidas, y el
  documento lo refleja. Cada regla se evalúa con **la fila** como su
  `entity_map`, así que una fórmula que cruza columnas lee las de SU fila; una
  que necesite un campo de nivel de documento no lo encuentra ahí (lo cuenta
  como 0, ver `details.assumptions`) — esa va en `cross_field_validation`.
  Antes del 2026-09-02 estas reglas no se evaluaban en ningún lado: se
  escribían y no hacían nada.
- **`$common`** en `item_fields` inyecta subcampos definidos una sola vez en
  `_shared.json` → `_common_item_fields` (hoy: `PAGINA`, `ORDEN`, `CONFIANZA`,
  `VALOR_CRUDO`) — evita repetir el mismo hint/validation en cada grupo
  repetible del catálogo entero.
- **`$presets`** en `fields` (nivel de tipo) inyecta CONJUNTOS completos de
  campos `computed` definidos una sola vez en `_shared.json` →
  `_field_presets`: las señales de la etapa de autenticidad e integridad.
  Ver «Presets de campos» justo abajo.
- **`computed: true`** marca un campo que llena el pipeline
  **determinísticamente después de la extracción**, no el modelo — se excluye
  del prompt/tool-schema de Bedrock (pedirle al modelo que adivine un valor
  que se va a descartar desperdicia tokens de salida) y su resultado sale
  siempre con `source: "NORMALIZED"` (o el origen real, si quien lo llenó
  declara uno — ver `derived`). Hay dos consumidores de esta convención:
  `LIQUIDACION_NORMALIZADA` (agregación de conceptos) y `derived` (una
  condición), ambos en las secciones de abajo — si marcas un campo como
  `computed` sin declarar ninguno de los dos, simplemente sale vacío/null,
  nadie lo calcula solo.
- **`derived`** declara ESA condición. Ver "Campos derivados de una
  condición" más abajo.
- **`unique_key`** marca el campo por el que se busca este tipo — ver la
  sección siguiente.

### Extracción determinística desde texto (`text_source`)

Un PDF digital ya tiene texto por `pypdf`; una página escaneada ya tiene texto
después de `Textract DetectDocumentText`. Si el formato es suficientemente
estable, el catálogo puede mapear ese texto con regex antes de pedirle a
Bedrock que lo estructure. La guía de implementación, calibración y rollout —con
el caso real de `PEDIMENTO_ERP`— está en
[`documentacion/EXTRACCION_DETERMINISTICA_E_HIBRIDA.md`](../documentacion/EXTRACCION_DETERMINISTICA_E_HIBRIDA.md):

```jsonc
"deterministic_extraction": {
  "enabled": true,
  "fallback": "missing_fields" // missing_fields | full_model | review
},
"fields": {
  "FOLIO": {
    "type": "string",
    "required": true,
    "text_source": {
      "selectors": [{
        "regex": "FOLIO\\s*:\\s*(?P<value>[A-Z0-9-]+)",
        "group": "value",
        "flags": ["ignore_case"],
        "occurrence": "first"
      }],
      "model_fallback": "if_missing" // if_missing | never
    }
  }
}
```

El motor intenta `selectors` en orden y toma el primero que encuentre. Un
selector admite `group` por nombre o índice, `occurrence: first|last`,
`pages: "first"|[1,2,...]`, flags `ignore_case`/`multiline`/`dotall`, los
transforms cerrados `digits`/`upper`, y `accept_regex` para rechazar una captura
que matcheó el contexto pero no tiene una forma confiable. Las regex usan la
sintaxis de Python: un grupo nombrado es `(?P<nombre>...)`.

Para restringir una búsqueda a una sección, agrega al selector —o al bloque de
un array— `region: {start_regex, end_regex, flags}`. Esto evita que una etiqueta
repetida en otra tabla produzca un falso positivo.

Un array de objetos se declara como una extracción atómica:

```jsonc
"text_source": {
  "row_regex": "(?m)^(?P<folio>\\S+)\\s+(?P<importe>[\\d,.]+)$",
  "groups": {"FOLIO": "folio", "IMPORTE": "importe"},
  "deduplicate_by": ["FOLIO"]
}
```

Si una misma tabla tiene dos layouts confiables, usa `row_selectors` en vez de
un solo `row_regex`; cada elemento lleva su propio `row_regex` y `groups`, y
las filas de todos los selectores se combinan antes de aplicar
`deduplicate_by`:

```jsonc
"row_selectors": [
  {"row_regex": "...", "groups": {"FOLIO": "folio"}},
  {"row_regex": "...", "groups": {"FOLIO": "numero_alterno"}}
]
```

- Cada match es una fila. `groups` mapea grupos nombrados a `item_fields`; los
  campos de fila `required` deben estar presentes o ese match se descarta.
- Los arrays son atómicos: si no sale ninguna fila confiable, todo el array va
  al fallback; no se mezclan medias filas del regex con medias filas del modelo.
- Para arrays de escalares usa `regex`/`row_regex` más `group`.
- `model_fallback: never` deja el campo vacío si no hubo match. Sirve para un
  opcional cuya ausencia es legítima; usarlo en un requerido hará que la
  validación normal lo mande a revisión.
- Con `fallback: missing_fields`, `text_extractor` recibe un JSON Schema que
  contiene **solo** las ramas no resueltas. Un valor determinístico gana siempre
  y el modelo nunca lo sobrescribe. Si no queda ninguna rama, no hay llamada.
- Con `fallback: review`, nunca se abre Bedrock para la extracción: los campos
  que el mapper no resuelva quedan vacíos y la validación normal decide
  APROBADO/REQUIERE_REVISION. `PEDIMENTO_ERP` usa este modo por default.
- En dev, el request puede comparar las estrategias sin cambiar el catálogo:
  `extractionMode=model|hybrid|deterministic` (o
  `pnpm enviar ... --extraction-mode ...`). `model` reproduce el schema completo
  anterior; `hybrid` fuerza `missing_fields`; `deterministic` fuerza `review`.
  Omitirlo (`catalog`) conserva esta configuración y es el default global
  recomendado; ver la matriz de todos los tipos y sus garantías en
  [`documentacion/MODOS_EXTRACCION.md`](../documentacion/MODOS_EXTRACCION.md).
  En términos operativos: `catalog` = **usa el mapper configurado; sin mapper,
  IA para todos los campos**, `model` = **IA para todos los campos**, `hybrid` =
  **código primero e IA para faltantes** y `deterministic` = **sólo código;
  faltantes a revisión**. `catalog` no intenta generar un mapper: si el tipo no
  declara este bloque y el request omite el modo, resuelve directamente a
  `model`.
- El resultado marca esos valores con `source: "DETERMINISTIC_TEXT"` y
  `extractionMeta` dice `strategy: deterministic|hybrid|model`, campos
  resueltos/fallback, fuentes de página y duración del mapper.
- El envelope público deja la decisión auditable: `aiExecuted`/`aiUsage`
  confirman si un modelo realmente corrió; `deterministicSavings` estima los
  tokens de entrada/salida evitados contra el schema completo, y
  `documents[].deterministicExtraction.fallbackFields` dice qué ramas sí se
  mandaron al modelo. `documents[].extractionMode` muestra el modo solicitado y
  el efectivo. Los tokens facturados nunca se mezclan con los
  estimados.
- Config inválida o regex sin match falla abierto hacia el fallback. El
  catálogo nunca ejecuta código ni expresiones arbitrarias.

`PEDIMENTO_ERP` es el piloto activo. Para eliminar además la clasificación por
modelo debe subirse declarando `documentType=PEDIMENTO_ERP`; una carga sin tipo
todavía puede usar Bedrock para segmentar/clasificar aunque la extracción sea
determinística.

### Presets de campos (`$presets`): pedir una verificación con una línea

Cada señal de la etapa de autenticidad e integridad es un campo `computed`
completo (tipo, hint, `authenticity_source`, regla, mensaje, `on_fail`).
`CARTA_NOM35` los declaraba todos a mano — 19 campos, ~250 líneas; hoy pide por
preset los de firma, sello y logo contra referencia y solo escribe los suyos.
`_shared.json` → `_field_presets` define esos campos UNA vez y un tipo los pide dentro de `fields`, con el mismo mecanismo que
`$common`:

```jsonc
"fields": {
  "$presets": ["INTEGRIDAD_PDF"],       // se inyectan PRIMERO, en este orden
  "SIN_ANOTACIONES": {                  // redeclarar un campo del preset = override: gana el tipo
    "type": "boolean", "required": false, "computed": true,
    "authenticity_source": "integrity.no_annotations",
    "hint": "…",
    "validation": [{ "rule_type": "boolean_true", "allow_empty": true, "on_fail": "reject", "message": "…" }]
  },
  "FOLIO": { ... }                      // los campos propios del tipo, después
}
```

Los tres presets (campo → `authenticity_source`; todos `computed`, `required:
false`, `allow_empty: true` salvo `AUTENTICIDAD_EVALUADA`):

| Preset | Campos | `on_fail` inicial |
|---|---|---|
| `INTEGRIDAD_PDF` | `AUTENTICIDAD_EVALUADA` → `evaluated` · `ARCHIVO_SIN_REGUARDADO` → `integrity.single_generation` · `FIRMA_DIGITAL_INTACTA` → `integrity.signature_intact` · `FECHAS_METADATOS_CONSISTENTES` → `integrity.dates_consistent` · `SIN_EDITOR_EN_METADATOS` → `integrity.producer_consistent` · `SIN_ANOTACIONES` → `integrity.no_annotations` · `SIN_CAMPOS_FORMULARIO` → `integrity.no_form_fields` · `SIN_TEXTO_SOBRE_ESCANEO` → `integrity.no_text_overlay` · `SIN_IMAGEN_SOBRE_TEXTO` → `integrity.no_image_over_text` · `SIN_CONTENIDO_ACTIVO` → `integrity.no_active_content` · `EDITOR_DETECTADO` → `integrity.editor` (string) · `VEREDICTO_AUTENTICIDAD` · `MOTIVOS_AUTENTICIDAD` | `review` (los strings no llevan regla) |
| `ALTERACIONES_PDF` | `AUTENTICIDAD_EVALUADA` · `SIN_MARCA_DE_AGUA` → `integrity.no_watermark` · `SIN_RECUADRO_SOBRE_TEXTO` → `integrity.no_shape_over_text` · `FUENTES_CONSISTENTES` → `integrity.fonts_consistent` · `SIN_CONTENIDO_OCULTO` → `integrity.no_hidden_content` · `PAGINAS_HOMOGENEAS` → `integrity.pages_consistent` · `VEREDICTO_AUTENTICIDAD` · `MOTIVOS_AUTENTICIDAD` | `review`. Se combina con `INTEGRIDAD_PDF` (`"$presets": ["INTEGRIDAD_PDF", "ALTERACIONES_PDF"]`): lo que el PDF deja a la vista en su contenido — marca de agua, tachadura o parche vectorial, texto agregado con otra fuente, capa oculta, página sustituida. Sigue siendo gratis (capa `integrity`). Medido sobre 178 PDF reales: una sola señal, en una carta ya clasificada como no auténtica |
| `INTEGRIDAD_IMAGEN` | `AUTENTICIDAD_EVALUADA` · `SIN_EDITOR_EN_METADATOS` → `integrity.producer_consistent` · `FECHAS_METADATOS_CONSISTENTES` → `integrity.dates_consistent` · `METADATOS_DE_CAMARA` → `integrity.camera_metadata` · `NIVEL_DE_ERROR_UNIFORME` → `pixels.error_level_uniform` · `EDITOR_DETECTADO` · `VEREDICTO_AUTENTICIDAD` · `MOTIVOS_AUTENTICIDAD` | `review`, salvo `METADATOS_DE_CAMARA` y `NIVEL_DE_ERROR_UNIFORME` en `info` (la mensajería quita el EXIF; el ELA no está calibrado con imágenes reales) |
| `CAPTURA_FISICA` | `AUTENTICIDAD_EVALUADA` · `CAPTURA_FISICA` → `structure.physical_capture` · `ORIGEN_DOCUMENTO` → `structure.origin` · `FONDO_NATURAL` → `pixels.natural_background` · `VEREDICTO_AUTENTICIDAD` · `MOTIVOS_AUTENTICIDAD` | `review` |
| `FIRMA_CONTRA_REFERENCIA` | `AUTENTICIDAD_EVALUADA` · `FIRMA_PRESENTE` → `element.FIRMA.present` · `FIRMA_SIN_INSERCION` → `element.FIRMA.not_pasted` · `FIRMA_COINCIDE_REFERENCIA` → `element.FIRMA.matches_reference` · `VEREDICTO_AUTENTICIDAD` · `MOTIVOS_AUTENTICIDAD` | `review` |
| `SELLO_CONTRA_REFERENCIA` | `AUTENTICIDAD_EVALUADA` · `SELLO_PRESENTE` · `SELLO_SIN_INSERCION` · `TEXTO_SELLO` → `element.SELLO.text` · `SELLO_CORRESPONDE_EMISOR` → `element.SELLO.issuer_consistent` (vacío si el tipo no declara `issuer_field`) · `SELLO_COINCIDE_REFERENCIA` · `VEREDICTO_AUTENTICIDAD` · `MOTIVOS_AUTENTICIDAD` | `review` |
| `LOGO_CONTRA_REFERENCIA` | `AUTENTICIDAD_EVALUADA` · `LOGO_PRESENTE` · `LOGO_COINCIDE_REFERENCIA` · `VEREDICTO_AUTENTICIDAD` · `MOTIVOS_AUTENTICIDAD` | `review` |

Los tres `*_CONTRA_REFERENCIA` son la comparación de un elemento contra las
referencias **registradas** de una entidad. Piden señales de la capa de visión
(de pago) y necesitan que el tipo nombre el elemento en `authenticity.elements`
y declare `authenticity.references` — ver «Elementos compartidos» y
«Referencias» abajo. Un tipo nuevo que solo quiere «¿el logo es el del
proveedor?» son tres líneas:

```jsonc
"authenticity": { "elements": ["LOGO"], "references": { "scope": "PROVEEDOR", "key_from": "metadata.proveedor" } },
"fields": { "$presets": ["LOGO_CONTRA_REFERENCIA"], /* …los campos del tipo… */ }
```

Lo que importa del mecanismo:

- **Un preset no enciende nada por sí mismo: enciende exactamente las capas que
  sus señales necesitan** (ver «Capas bajo demanda» en la sección de
  autenticidad). `INTEGRIDAD_PDF` e `INTEGRIDAD_IMAGEN` solo piden señales
  `integrity.*` (y el ELA sobre los bytes JPEG del archivo): **nunca rasterizan
  una página ni llaman a un modelo**. El tipo paga milisegundos de CPU.
- **El bloque `authenticity` es opcional.** Con el preset basta; el bloque solo
  afina umbrales (`integrity.*`, `expected_capture`, `pixels.ela_*`).
- **Override por campo:** para endurecer una señal, redeclara ese campo con el
  mismo nombre y tu `on_fail`; el resto del preset se conserva. Un preset
  desconocido se ignora con un warning en el log, no truena el tipo.
- **Solo en `fields` de nivel de tipo**, no dentro de `item_fields`.
- **Gotcha de despliegue:** `_field_presets` viaja en el bundle
  (`config/catalogs.json`, como `_common_item_fields`), así que **cambiar un
  preset requiere `pnpm build` + deploy**; en cambio el override que un tipo
  hace de un campo del preset es hot-reloadable con `pnpm sync:one`, como
  cualquier campo. `pnpm build` también expande los presets en el índice que
  ve `GET /document-types/{type}?includeSchema=1` (`scripts/catalog-files.mjs`
  → `expandPresets`, que usa `sync-catalogs.mjs`), así que la API muestra los campos reales, no la
  referencia.
- Los scripts gratuitos (`pnpm autenticidad`, `pnpm test:autenticidad`) cargan
  los presets directo de `_shared.json`, sin `pnpm build`.

Ejemplo mínimo — una factura digital de origen que debe llegar tal como la
generó el sistema emisor, con su XML adjunto (PDF/A-3):

```jsonc
"FACTURA_PROVEEDOR": {
  "description": "…",
  "authenticity": {
    "expected_capture": "DIGITAL",           // un escaneo de la factura es lo raro, no lo normal
    "integrity": { "allow_embedded_files": true }   // el CFDI adjunto no es «contenido activo»
  },
  "fields": {
    "$presets": ["INTEGRIDAD_PDF"],
    "FOLIO": { "type": "string", "required": true, "hint": "…" },
    "TOTAL": { "type": "number", "required": true, "hint": "…" }
  }
}
```

Antes de activarlo en un tipo real: `pnpm autenticidad -- --type <TIPO> --dir
<carpeta>` reporta gratis cuántos archivos reales caerían por cada señal.

### Unique keys (`unique_key`): buscar por un campo sin recorrer el tipo

Un tipo puede declarar el campo cuyo valor identifica **UN** documento de ese
tipo por sí solo: el número de pedimento, el UUID de un CFDI, el folio de una
póliza. Se declara sobre el campo:

```jsonc
"NUMERO_PEDIMENTO_NORMALIZADO": {
  "type": "string",
  "required": true,
  "unique_key": { "normalize": "digits" },   // o simplemente `true`
  "hint": "..."
}
```

**Qué gana.** Sin esto, `GET /documents?docType=PEDIMENTO_ADUANAL&field=
NUMERO_PEDIMENTO&value=...` hace Query al `DocumentTypeIndex` para traer
*todos* los pedimentos, BatchGet de sus envelopes y compara el campo dentro
del Lambda: el costo crece con el número de documentos del tipo. Con la
unique key declarada, es **una sola Query** a la tabla de lookup
(`document-validation-keys[-dev]`) sobre la clave exacta, sin importar cuántos
documentos haya. El resto de la respuesta es idéntica; el campo `lookup` dice
qué camino contestó (`"uniqueKey"` | `"typeIndex"` | `"scan"`).

**La clave es SIEMPRE `TIPODOC#<valor normalizado>`:**

```
PEDIMENTO_ADUANAL#260708236001630
FACTURA_HONORARIOS_XML#A1B2C3D4-...-9F
```

El prefijo de tipo es lo que hace segura la palabra "unique": dos tipos
distintos pueden usar legítimamente el mismo espacio de números, y el prefijo
los mantiene separados. Por eso la búsqueda por unique key **requiere
`docType`** — sin tipo no hay clave.

| `normalize` | Qué hace | Cuándo |
|---|---|---|
| `"upper"` (default, o `true`) | trim + espacios internos colapsados + MAYÚSCULAS | folios, claves alfanuméricas |
| `"alnum"` | `upper` y además tira todo lo que no sea `A-Z0-9` | el mismo identificador impreso con separadores distintos según el documento |
| `"digits"` | solo `0-9` | números que se imprimen con espacios (`26 07 0823 6001630` ↔ `260708236001630`) |
| `"raw"` | trim + espacios colapsados, respeta mayúsculas/minúsculas | valores donde el case significa algo |

La normalización se aplica **igual al escribir y al buscar**, así que quien
consulta puede mandar el valor en cualquiera de las dos formas y cae en la
misma clave. Un `normalize` desconocido no rompe nada: se registra un warning
y se usa el default.

**Se puede marcar más de un campo.** `PEDIMENTO_ADUANAL` marca
`NUMERO_PEDIMENTO` (el impreso, con espacios) y `NUMERO_PEDIMENTO_NORMALIZADO`
(los 15 dígitos): con `digits` los dos producen la MISMA clave, así que se
escribe una sola fila y el usuario puede buscar por cualquiera de los dos
nombres de campo. Cuando dos campos normalizan distinto, se escribe una fila
por cada uno.

**Nivel de tipo.** También se acepta fuera de `fields`, que es la única forma
para un tipo del DSL legado (sin `fields`):

```jsonc
"unique_key": "FOLIO"
"unique_key": ["FOLIO", { "field": "RFC", "normalize": "alnum" }]
```

**Gotchas:**

- Solo campos **escalares de primer nivel**. Un subcampo de un `object` o una
  columna de un `array` no se indexa hoy (`unique_keys.py::_values` lee el mapa
  plano de escalares que el envelope ya publica por documento).
- **En un expediente cada documento se indexa con SU propio tipo**, no con el
  del contenedor: buscar `docType=PEDIMENTO_ADUANAL&field=NUMERO_PEDIMENTO`
  encuentra el expediente que lo contiene, y la fila guarda
  `ContainerType: EXPEDIENTE_ADUANAL_MULTIDOCUMENTO` como procedencia.
- **"Unique" es una declaración de intención, no una restricción.** Nada
  rechaza un segundo documento con el mismo valor: `DocumentId` es sort key,
  así que una Query devuelve todas las subidas que lo traen (reenvío,
  expediente corregido) y el consumidor decide. Colapsarlas en una sola fila
  perdería todas menos la última.
- **Nunca da un falso negativo.** Si la tabla de lookup no tiene la fila
  (documento procesado antes de declarar la key, o antes de que existiera la
  tabla), la búsqueda cae al camino de siempre y lo encuentra igual — más
  lento, no equivocado. Por eso agregar una `unique_key` a un tipo con
  historial pide `pnpm backfill:keys`.
- Se aplica solo con `op=eq`. `op=contains` no puede usar una hash key y
  sigue por el camino normal.

**Después de declararla:**

```bash
pnpm build                                   # empaqueta la declaración
pnpm sync:one -- --type PEDIMENTO_ADUANAL    # hot-reload, sin redeploy
pnpm backfill:keys                           # reconstruye las filas históricas
```

### Tipos con fuente XML (`xpath`, `item_xpath`, `namespaces`)

Cuando el documento de origen ya es XML estructurado (un CFDI timbrado por el
SAT, un acuse de VUCEM, etc.) no hace falta pedirle a un modelo que lo lea:
`pipeline/xml_document.py` extrae los campos determinísticamente con
`xml.etree.ElementTree` (stdlib, sin dependencias nuevas), sin llamar a
Bedrock. Un tipo entra a este camino simplemente declarando `xpath` (en vez de
o junto a `hint`) en sus campos — no hay ningún interruptor especial ni nada
que registrar en código; `handler.py` decide si un documento entra por aquí
según la **extensión del archivo subido** (`.xml`), no según el nombre del
tipo. La convención es nombrar estos tipos con sufijo `_XML`
(`FACTURA_HONORARIOS_XML`) para que sean identificables a simple vista dentro
de `catalogs/`, pero esa convención es solo para humanos — el código nunca
mira el TYPE_ID para decidir el pipeline.

```jsonc
{
  "MI_TIPO_XML": {
    // Prefijo -> URI de cada namespace que usan los xpath de abajo.
    "namespaces": { "cfdi": "http://www.sat.gob.mx/cfd/4" },
    "fields": {
      // "@Attr" = atributo del nodo raíz. "a/b/@Attr" = atributo de un
      // descendiente. "a/b" (sin @) = texto de un descendiente.
      "FOLIO": { "type": "string", "required": true, "xpath": "@Folio", "validation": [] },
      "EMISOR_RFC": { "type": "string", "required": true, "xpath": "cfdi:Emisor/@Rfc", "validation": [] },

      // xpath_regex separa un dato compuesto ya resuelto por xpath. El
      // patrón y el grupo viven en catálogo; el extractor no conoce TYPE_IDs.
      "PATENTE": {
        "type": "string", "required": true,
        "xpath": ".//cfdi:InformacionAduanera/@NumeroPedimento",
        "xpath_regex": {
          "pattern": "^(\\d{2})\\s+(\\d{2})\\s+(\\d{4})\\s+(\\d{7})$",
          "group": 3
        },
        "validation": []
      },

      // type == "array": item_xpath ubica cada fila; item_fields usa xpath
      // RELATIVO a esa fila.
      "CONCEPTOS": {
        "type": "array",
        "item_xpath": ".//cfdi:Conceptos/cfdi:Concepto",
        "item_fields": {
          "IMPORTE": { "type": "number", "required": true, "xpath": "@Importe", "validation": [] }
        }
      }
    }
  }
}
```

- `xpath` (y `item_xpath`) aceptan también una **lista** de rutas: para un
  campo escalar se prueba cada una en orden y gana la primera que resuelva
  algo (útil entre sub-versiones del mismo formato); para un array, las
  rutas se concatenan (unión — ElementTree no soporta el operador `|` de
  XPath real, así que esta es la forma de expresar "esta tabla viene de dos
  elementos hermanos distintos", ej. `Traslados` + `Retenciones`).
- `xpath_regex` es opcional en campos escalares y se aplica **después** de
  resolver `xpath`, antes de convertir el tipo. Acepta
  `{"pattern": "...", "group": 2}` o un patrón string (grupo 1). Sirve para
  separar atributos compuestos — por ejemplo año, aduana, patente y
  consecutivo desde `NumeroPedimento`— sin crear lógica por `TYPE_ID`. Si el
  patrón no coincide, el grupo no existe o la expresión es inválida, el campo
  queda ausente y las reglas `required`/`validation` lo reportan; nunca se
  devuelve silenciosamente el atributo completo como si fuera el subcampo.
- Los predicados simples de ElementTree funcionan tal cual:
  `cfdi:Retenciones/cfdi:Retencion[@Impuesto='001']/@Importe` selecciona la
  fila correcta sin necesitar código nuevo.
- No hay `local-name()`, `contains()` ni funciones XPath reales — es el
  subconjunto que soporta `ElementTree.find`/`findall`. Si un futuro tipo
  necesita XPath 1.0 completo, la opción es sumar `lxml` como dependencia,
  no está incluida hoy (para no forzar bundling con Docker como pypdf).
- La confianza de todo campo resuelto por XML es `1.0` (parseo exacto, sin
  incertidumbre de OCR/LLM) y su `source` de salida es `"XML_EXTRACTION"`
  (no `"MODEL_EXTRACTION"` — nadie adivinó el valor). Ambos vienen de
  `resolve_field_tree(..., default_source="XML_EXTRACTION")`.
- Un tipo XML es siempre un documento único y autocontenido: **exige
  `documentType` declarado en la subida** (`raw/{TIPO}/{uuid}/archivo.xml`) —
  no existe clasificación automática por visión para XML crudo, y las reglas
  cross-documento no aplican (no hay "hermanos" dentro de una sola subida
  XML). Si el mismo tipo también puede llegar por PDF, conviene un archivo
  de catálogo aparte por formato (ver `FACTURA_HONORARIOS.json` vs.
  `FACTURA_HONORARIOS_XML.json`): son fuentes distintas y normalmente el XML
  trae MENOS campos que la representación impresa (solo lo que realmente
  vive en el XML fiscal, sin las secciones que un PDF de cortesía agrega
  encima) — ese es justamente el "mapeo más limpio" que da tener el dato ya
  estructurado.
- El costo de Bedrock para estos tipos ya es cero sin tocar Step Functions:
  `model-invoke-lambda/index.py` bypassa clasificación cuando el tipo viene
  declarado en el key de S3, y bypassa extracción para cualquier tipo
  field-centric (`fields`) porque `document-catalog.ts` no genera prompt de
  extracción para ellos — ver `AGENTS.md` → "Trabajo Bedrock desperdiciado".

### Tipos con fuente tabular (`cell`, `column`, XLSX/CSV)

Mismo principio que XML pero para datos ya tabulares — un reporte XLSX, un
export CSV: `pipeline/tabular_document.py` extrae determinísticamente con
`openpyxl` (XLSX/XLSM, pura Python, sin dependencias compiladas) o el `csv`
de la stdlib (CSV), sin llamar a Bedrock. Un tipo entra a este camino
declarando `cell` (campos fijos) y/o `column` (filas de una tabla) en vez de
`hint`. La convención de nombre es sufijo `_XLSX`/`_CSV`, igual que `_XML` —
solo para humanos, `handler.py` rutea por la extensión del archivo subido.

**XLSX y CSV comparten el mismo mapeo de catálogo.** Ambos se parsean a la
misma forma interna (`{hoja: [[celda, ...], ...]}`) — un CSV es, para efectos
de extracción, un "libro" con una sola hoja implícita. Esto significa que el
MISMO tipo/catálogo puede procesar indistintamente un `.xlsx` o un `.csv` si
el layout de columnas coincide (útil cuando el mismo reporte se exporta en
ambos formatos).

```jsonc
{
  "MI_REPORTE_XLSX": {
    "fields": {
      // Campo fijo: dirección de celda estilo Excel ("B2"). Sin "sheet",
      // usa la primera hoja del libro (o la única, en CSV).
      "FOLIO": { "type": "string", "required": true, "cell": "B1", "validation": [] },
      "TOTAL": { "type": "number", "required": true, "cell": "B3", "validation": [] },

      // type == "array": una tabla. "header_row" (1-indexado, default 1)
      // ubica el renglón de encabezados; "start_row" (default header_row+1)
      // y "end_row" (opcional) acotan el rango de filas. Sin "end_row", la
      // lectura se detiene en la primera fila completamente vacía.
      "GASTOS": {
        "type": "array",
        "header_row": 5,
        "item_fields": {
          // "column" empareja por texto del encabezado (insensible a
          // mayúsculas/espacios). Alternativa para datos sin encabezado:
          // "column_letter": "C".
          "CONCEPTO": { "type": "string", "required": true, "column": "Concepto", "validation": [] },
          "IMPORTE": { "type": "number", "required": true, "column": "Importe", "validation": [] }
        }
      },

      // Un array puede agregar filas de VARIAS hojas del mismo libro (ej. una
      // hoja por mes, mismo layout de columnas en cada una) — ver la sección
      // "Múltiples hojas" más abajo para las 3 formas de item_sheet.
      "GASTOS_ANUAL": {
        "type": "array",
        "item_sheet": ["Enero", "Febrero", "Marzo"],
        "header_row": 1,
        "item_fields": {
          "CONCEPTO": { "type": "string", "required": true, "column": "Concepto", "validation": [] },
          "IMPORTE": { "type": "number", "required": true, "column": "Importe", "validation": [] }
        }
      }
    }
  }
}
```

#### Múltiples hojas (`sheet` / `item_sheet`)

Un libro XLSX puede tener cualquier número de hojas; por default (sin
declarar `sheet`/`item_sheet` en un campo) todo se lee de la **primera hoja
del libro** — igual que hoy. Para leer de otra(s) hoja(s), cada campo declara
su propio `sheet` (escalar) o `item_sheet` (array), en cualquiera de estas 3
formas:

| Valor | Efecto en un campo **escalar** (`cell`) | Efecto en un campo **array** (`item_fields`) |
|---|---|---|
| *(omitido)* | Primera hoja del libro | Primera hoja del libro |
| `"NombreHoja"` | Esa hoja | Esa hoja |
| `["HojaA", "HojaB"]` | Prueba en orden, **gana la primera con la celda no vacía** (fallback — un valor no puede salir de varias hojas a la vez) | **Concatena** las filas de todas, en el orden dado (agregación) |
| `"*"` | Busca la celda en todas las hojas (equivale a listar todas) | Concatena las filas de **todas** las hojas del libro, en su orden dentro del archivo |

- Un nombre de hoja que no existe en el libro se **omite** (queda registrado
  como warning en logs), nunca sustituye silenciosamente otra hoja — así un
  typo en `item_sheet` no corrompe el array con filas de la hoja equivocada,
  simplemente falta esa hoja.
- El mismo `header_row`/`start_row`/`end_row`/`item_fields` se aplica a
  **cada** hoja leída — asume que todas comparten el layout de columnas
  (el caso real más común: una hoja por mes/sucursal con las mismas
  columnas). Si el layout difiere entre hojas, no uses agregación — declara
  un array por hoja en vez de una lista/`"*"` en `item_sheet`.
- Un CSV no tiene concepto de hoja — cualquier `sheet`/`item_sheet` en un
  campo pensado para funcionar también desde CSV se ignora de más (siempre
  resuelve a la única hoja implícita); solo importa cuando el tipo procesa
  XLSX.
- Ver `catalogs/_TEMPLATE_TABULAR.json` para un ejemplo completo y probado
  (campo escalar con fallback entre hojas, array agregando 2 hojas explícitas).

- Celdas con fórmula: `openpyxl.load_workbook(..., data_only=True)` lee el
  último valor calculado (lo que ve un humano al abrir el archivo en Excel),
  no el texto de la fórmula.
- Coerción de tipos: celdas de Excel ya formateadas como número/fecha llegan
  como `float`/`datetime` nativos de `openpyxl` (no strings) — se normalizan
  al mismo contrato que XML/Bedrock (`number` → float, fecha → ISO string).
  Para celdas de texto con formato de moneda ("$1,234.56"), la coerción
  reutiliza `pipeline.normalize.parse_amount` (el mismo parser que ya usan
  las reglas de validación numéricas en todo el catálogo).
- Confianza `1.0` y `source: "TABULAR_EXTRACTION"` para todo campo resuelto
  (mismo mecanismo `default_source` de `resolve_field_tree` que usa XML).
- Igual que XML: documento único y autocontenido, `documentType` obligatorio
  en la subida, sin reglas cross-documento (no hay "hermanos" dentro de una
  sola subida).
- **Gotcha de costo específico de CSV** (no aplica a XLSX/XML): Bedrock
  Converse ya trata `csv` como formato de documento nativo soportado (ver
  `_DOC_FORMATS` en `model-invoke-lambda/index.py`), así que una subida de
  `.csv` SIN `documentType` declarado sí llegaría a intentar una llamada real
  a Bedrock (con costo) antes de fallar — a diferencia de `.xlsx`/`.xml`, que
  fallan gratis por extensión no soportada. `upload-lambda` ya exige
  `documentType` para `xml`/`xlsx`/`csv` en la API (`REQUIRES_DECLARED_TYPE`
  en `index.mjs`) para cerrar esto en el origen.
- No hay soporte para `.xls` (formato binario legado, pre-2007) hoy — el
  mismo mecanismo `cell`/`column` funcionaría con un parser distinto
  (`xlrd`), pero no se agregó sin un caso real que lo necesite.

### Recuperación determinística desde QR (`qr_recovery`)

Para el caso mínimo basta declarar `qr_source` en un campo: eso activa la
lectura del QR y prueba la primera página del segmento. El bloque opcional
`qr_recovery` afina **dónde buscarlo** y qué URL aceptar; cada campo dice
**qué toma del QR**. No se agrega código Python por tipo:

```jsonc
"QR_CONTENIDO": {
  "type": "string",
  "required": false,
  "computed": true,
  "qr_source": "payload",
  "hint": "Contenido completo decodificado del QR.",
  "validation": []
}
```

Cuando además se quieren mapear parámetros y validar el QR:

```jsonc
"qr_recovery": {
  "confidence": 0.99,
  "page_marker_min_matches": 2,
  "short_segment_max_pages": 8,
  "page_markers": ["C[ÉE]DULA DE IDENTIFICACI[ÓO]N FISCAL"],
  "url_contains": "siat.sat.gob.mx/app/qr/faces/pages/mobile/validadorqr.jsf"
},
"fields": {
  "ID_CIF": {
    "type": "string",
    "required": false,
    "hint": "Identificador de la CIF.",
    "qr_source": {
      "param": "d3",
      "split": "_",
      "part": 0,
      "pattern": "^\\d+$"
    },
    "validation": []
  },
  "RFC": {
    "type": "string",
    "required": true,
    "hint": "RFC del contribuyente.",
    "qr_source": {
      "param": "d3",
      "split": "_",
      "part": -1,
      "pattern": "^[A-ZÑ&]{3,4}\\d{6}[A-Z0-9]{3}$"
    },
    "validation": []
  },
  "QR_CONSTANCIA": {
    "type": "string",
    "required": false,
    "computed": true,
    "qr_source": "payload",
    "hint": "URL completa decodificada.",
    "validation": []
  },
  "CONSTANCIA_QR_VALIDO": {
    "type": "boolean",
    "required": true,
    "computed": true,
    "qr_source": "valid",
    "qr_requires": ["ID_CIF", "RFC"],
    "hint": "True cuando el QR contiene todos los campos requeridos.",
    "validation": [
      {"rule_type": "boolean_true", "message": "QR faltante o inválido"}
    ]
  }
}
```

- `page_markers` y `page_marker_min_matches` eligen la página por texto;
  para segmentos cortos también se intenta primero su página inicial.
- `url_contains` evita aceptar otro QR presente en la misma página.
- Si no se necesita localización ni filtrado especial, puede omitirse por
  completo `qr_recovery`; un campo con `qr_source: "payload"` es suficiente.
- Un `qr_source` de parámetros admite `param`, `slice` (`":2"`, `"-2:"`),
  `digits`, `split` + `part` (índices negativos permitidos) y `pattern`.
- `qr_source: "payload"` guarda el texto o URL completo decodificado.
- `qr_source: "valid"` produce un booleano; `qr_requires` enumera los campos
  que deben haberse recuperado. Si se omite, exige todos los mapeos QR.
- Los campos recuperados desde el QR reemplazan la extracción del modelo y
  quedan auditados en `qrRecovery` y `recoveredFields`.
- El formato superior usado originalmente por `PEDIMENTO_ADUANAL`
  (`qr_recovery.fields`, `decoded_text_field`, `validity_field`) sigue
  soportado para compatibilidad; los catálogos nuevos deben preferir
  `qr_source` junto al campo.
- `QR_RECOVERY_ENABLED=false` desactiva globalmente este mecanismo.

### Autenticidad e integridad (`authenticity`)

Dos preguntas distintas, una sola etapa (`pipeline/authenticity.py`):

- **¿El archivo fue tocado después de generarse?** — para CUALQUIER tipo: una
  factura, un pedimento, un estado de cuenta, una foto de identificación.
  Guardado incremental de un editor, anotaciones encima del contenido, texto
  escrito sobre el escaneo, una imagen que tapa el texto original, una firma
  digital que ya no cubre el archivo, Photoshop en el EXIF. Capa 0,
  `integrity`: determinista, gratis, lee los bytes del archivo.
- **¿Es la captura de un papel impreso, firmado con tinta y sellado?** — para
  tipos como `CARTA_NOM35`, donde el riesgo es un Word con la firma *pegada como
  imagen*. Capas 1-4: estructura, pixeles, visión y memoria.

Es el segundo bloque no-textual del DSL (el primero es `qr_recovery`, y está
calcado de él): marcadores por campo (`authenticity_source`) sobre campos
`computed` que el motor de reglas normal califica, más un bloque de tipo
**opcional** que afina umbrales. **Nada en el código conoce un `TYPE_ID`, un
nombre de campo ni un nombre de elemento**: todo sale de aquí. La forma corta de
declararlo es un preset (`"$presets": ["INTEGRIDAD_PDF"]`, ver «Presets de
campos»); la forma larga, campo por campo, es `base/recursos_humanos/CARTA_NOM35.json`.
Un ejemplo sin firmas ni referencias — integridad del archivo más una revisión
visual de la página, para un documento que puede llegar digital o escaneado — es
`custom/portal/ESTADO_CUENTA_PORTAL.json`.

Guía operativa completa (qué detecta cada capa, medición sobre cartas reales,
cómo dar de alta las firmas y sellos de cada proveedor, arranque en observación,
límites):
[`documentacion/AUTENTICIDAD_Y_REFERENCIAS.md`](../documentacion/AUTENTICIDAD_Y_REFERENCIAS.md).

#### Capas bajo demanda: lo que no se pide no corre

La etapa no corre una capa porque esté configurada, sino porque **algún campo
del tipo va a leer una señal suya** (`_layers_needed` en `authenticity.py`).
Es lo que hace que declarar la verificación sea barato: un tipo que solo pide
señales `integrity.*` jamás rasteriza una página ni llama a Bedrock.

| Señales que el tipo pide | Capas que corren |
|---|---|
| `integrity.*` | `integrity` (bytes crudos; nunca un raster) |
| `structure.*` | `structure` |
| `pixels.natural_background` | `structure` + `pixels` (rasteriza) |
| `pixels.error_level_uniform` | `structure` + `pixels` (los bytes JPEG del archivo, sin raster) |
| `element.*`, `capture_kind`, `vision.*` | `vision` + `structure` + `pixels` (las regiones candidatas salen del reporte de página y de la tinta del raster) |
| `memory.*` | `memory` (+ `vision`, `structure` y `pixels` cuando hay `elements` que tomar huella) |
| `evaluated`, `verdict`, `reasons` | ninguna por sí solas |

Una capa no pedida queda `NO_SOLICITADA` en `authenticity.layers` del resultado:
no corrió, no costó y **no cuenta en `evaluated`**. `"<capa>": false` en el bloque
sigue dando `DESACTIVADA`. Un tipo sin ningún campo con `authenticity_source` no
pasa por la etapa (una búsqueda de diccionario).

#### El bloque (todo opcional)

```jsonc
"authenticity": {
  "enabled": true,
  "expected_capture": "ANY",            // FISICO | DIGITAL | ANY. Default: FISICO si algún campo pide
                                        //   structure.physical_capture, ANY si no → structure.capture_as_expected
  "pages": { "max_pages": 2 },          // páginas con contenido a rasterizar (fail-open: si hay más, se avisa)

  "integrity": {                        // CAPA 0 — integridad del archivo. Gratis, determinista, sin raster.
    "ignored_annotations": ["/Link", "/Popup", "/Widget"],   // no son una edición del contenido
    "editor_patterns": ["acrobat pro", "acrobat dc", "acrobat 2", "pdf-xchange", "foxit", "nitro",
                        "pdfelement", "wondershare", "sejda", "ilovepdf", "smallpdf", "pdfescape",
                        "pdffiller", "photoshop", "gimp", "pixlr", "affinity", "inkscape", "pdf24",
                        "soda pdf", "master pdf", "pdfgear", "xodo", "lumin", "pdfsam", "pdf architect",
                        "picsart", "snapseed", "lightroom"],   // subcadenas (minúsculas) que nombran un EDITOR
    "capture_exceptions": ["paper capture", "scan", "pdfmaker"],  // …salvo si la cadena trae esto: es software de captura
    "date_tolerance_s": 120,            //   segundos de diferencia tolerados entre dos sellos de tiempo del mismo evento
    "min_overlay_chars": 8,             //   caracteres visibles tras la imagen de página completa para acusar
    "max_overlay_chars": 200,           //   a partir de aquí no es un escaneo con texto encima sino una composición
                                        //   digital sobre un membrete-imagen (medido en cartas de Word): se abstiene
    "overlay_margin": 0.06,             //   banda superior/inferior de la página: folios, Bates y encabezados no cuentan
    "image_over_text_min_overlap": 0.5, //   parte de un renglón que una imagen debe tapar para contar como "encima"
    "allow_embedded_files": false,      //   true en CFDI PDF/A-3 que llevan el XML adjunto
    "active_actions": ["/JavaScript", "/Launch", "/SubmitForm", "/ImportData"],   // acciones de anotación que son contenido activo
    "severity": {}                      //   override por código: {"ARCHIVO_CON_ACTUALIZACIONES_INCREMENTALES": "hard"}
  },

  "structure": {                        // CAPA 1 — estructura del PDF. Gratis, determinista.
    "min_native_text_chars": 200,       //   texto VISIBLE ≥ esto => documento digital
    "max_scan_text_chars": 40,          //   texto visible ≤ esto => sin capa de texto real
    "full_page_coverage": 0.85,         //   una imagen que cubre ≥ esto ES la página
    "ignore_image_coverage_below": 0.01,//   menos que esto es decoración (marca de agua de CamScanner)
    "origin_patterns": { "ESCANER": ["lexmark", "..."] }   // opcional; solo evidencia
  },

  "pixels": {                           // CAPA 2 — ruido de fondo y ELA. Gratis, señal suave.
    "dpi": 150, "grid": 8, "min_regions": 8,
    "noise_std_min": 0.05, "pure_white_ratio_min": 0.999,
    "ela_quality": 90, "ela_block": 32, "ela_max_ratio": 12.0,   // análisis de nivel de error sobre los bytes
    "ela_min_blocks": 20, "ela_floor": 0.002                   //   JPEG del archivo (pixels.error_level_uniform)
  },

  "vision": {                           // CAPA 3 — un modelo multimodal ve la página. De pago.
    "enabled": true,
    "trigger": "always",                //   "on_suspicion": solo si una capa gratis (0-2) levantó un motivo no informativo;
                                        //   un archivo limpio no paga la llamada (motivo VISION_OMITIDA_SIN_SOSPECHA)
    "page_review": false,               //   true: una llamada más que busca indicios de edición en la página
                                        //   → vision.no_edit_signs (juicio: revisión, nunca rechazo)
    "inspect_crops": true,              //   2ª llamada: examina recortes a resolución nativa
    "inspect_when_digital": "with_references",  // false: si la capa 1 ya probó que es digital, no se paga el examen
                                        //   true: siempre · "with_references": solo si el proveedor tiene referencias
                                        //   registradas ("¿la firma pegada es al menos la registrada?")
    "max_references_per_kind": 3,       //   referencias registradas que se mandan por elemento
    "fallback_models": []               //   [] = nunca cambia de modelo (la lista compartida es de solo texto)
  },

  "elements": {                         // QUÉ se busca. Una lista de nombres compartidos (["FIRMA", "LOGO"]) o un
                                        //   objeto: lo que declares aquí sobreescribe, llave por llave, la definición
                                        //   de `_shared.json` → `_authenticity_elements` (ver «Elementos compartidos»).
                                        //   Un nombre que no está ahí se define completo aquí: los nombres son tuyos.
    "FIRMA": {
      "description": "Firma autógrafa trazada con tinta sobre la línea de firma…",  // va al prompt
      "required": true,
      "ink_when_overlapping": "dark",   // si su caja se traslapa con otra: qué tinta es ESTE elemento
      "similarity_reject": 0.97,        // memoria: a partir de aquí es LA MISMA tinta que otra carta
      "similarity_review": 0.85         //          a partir de aquí se parece demasiado
    },
    "SELLO": {
      "description": "Sello de goma de la empresa emisora…",
      "read_text": true,                // transcribe la leyenda -> `element.SELLO.text`
      "issuer_field": "EMPRESA_EMISORA",// la leyenda debe nombrar a este campo extraído
      "ink_when_overlapping": "colour",
      "similarity_reject": null,        // null = el parecido NUNCA rechaza (una goma rígida siempre se parece)
      "similarity_review": 0.95
    },
    "LOGO": {
      "description": "Logotipo o membrete…",
      "fingerprint": false,             // no se le guarda huella: un logo SÍ se repite legítimamente
      "embedded_image_is_insertion": false,
      "references": { "scope": "MARCA" } // opcional: ESTE elemento se compara en otro scope/clave que el resto
                                        //   (se fusiona sobre el bloque `references` del tipo; `false` = no se compara)
    }
  },

  "references": {                       // referencias registradas de una ENTIDAD (`pnpm refs:*` o la API /references)
    "scope": "PROVEEDOR",               //   a qué clase de entidad pertenecen. Las referencias son del scope + clave,
                                        //   no del tipo: todo tipo que declare este scope usa las mismas
    "key_from": "metadata.proveedor",   //   de dónde sale la clave: `metadata.<clave>` (lo que mandó quien subió) o
                                        //   `fields.<CAMPO>` (lo que se leyó del propio documento, p. ej. el RFC)
    "search": {                         //   SIN clave: buscar el elemento entre las referencias de TODO el scope
      "enabled": false,                 //     por embedding (índice vectorial). Apagado por defecto: mide primero
      "min_similarity": 0.7,            //     con `pnpm refs:eval`. Similitud coseno mínima de un candidato
      "top_k": 3                        //     candidatos que se le muestran al modelo de visión, que es quien decide
    },
    "embedding": { "provider": "titan-multimodal" }   // opcional; default. Ver `pipeline/reference_embeddings.py`
  },

  "memory": {                           // CAPA 4 — memoria entre documentos (tabla de huellas)
    "enabled": true,
    "identity_fields": ["NOMBRE_EMPLEADO", "FECHA_CARTA"],   // qué hace que dos cargas sean "la misma carta"
    "element_similarity_reject": 0.97, "element_similarity_review": 0.85,
    "max_candidates": 2000
  }
}
```

Cada capa se apaga por separado (`"integrity": false`, `"vision": false`,
`"memory": {"enabled": false}`, `"pixels": false`…), aunque casi nunca hace
falta: la capa que ningún campo lee no corre. El interruptor global es
`AUTHENTICITY_ENABLED` — y `false` **no aprueba** lo que no se revisó: manda a
revisión.

`vision` se activa cuando hay `elements` **o** `page_review: true`; con
`page_review` y sin `elements` corre solo esa llamada.

#### Elementos compartidos (`_authenticity_elements`)

Qué **es** una firma, un sello o un logotipo — la descripción que recibe el
modelo de visión, con qué tinta se separa de un vecino, a partir de qué parecido
dos de ellos son «el mismo» — está definido una vez en `catalogs/_shared.json` →
`_authenticity_elements` (`FIRMA`, `SELLO`, `LOGO`). Un tipo los pide por nombre
y solo escribe lo que cambia:

```jsonc
"elements": ["FIRMA", "LOGO"]                                   // tal cual
"elements": { "FIRMA": {}, "SELLO": { "issuer_field": "EMPRESA_EMISORA" } }   // con un ajuste
"elements": { "HOLOGRAMA": { "description": "Holograma de seguridad…", "fingerprint": false } }  // uno propio
```

Mismo mecanismo y mismo gotcha que los presets: la definición compartida viaja
en el **bundle** (`pnpm build` + deploy); el ajuste del tipo es hot-reloadable
con `pnpm sync:one`. Un tipo que usa un elemento o un preset compartido
**nuevo** sincronizado sobre una Lambda desplegada antes de que existiera pierde
ese elemento (nombre desconocido → se ignora con un warning): despliega primero.

#### Referencias: de quién son y cómo se eligen

Una referencia es de una **entidad** — `scope` + clave (`PROVEEDOR` + `10040`) —
y nunca de un tipo de documento. El logotipo de un proveedor se registra una vez
(`pnpm refs:register` o `POST /references/PROVEEDOR/10040`) y lo usa todo tipo
que declare ese `scope`. Los `scope` y elementos válidos para registrar salen de
aquí, por forma: la unión de lo que declaran los tipos activos
(`authenticity.reference_scopes`).

Qué referencias se le muestran al modelo junto a cada recorte (la
**recuperación**; el modelo es quien decide si corresponde):

| Situación | Qué se manda | `references.ranking.<KIND>.mode` |
|---|---|---|
| La entidad tiene ≤ `max_references_per_kind` de ese elemento | todas | `TODAS` — no se calcula ningún embedding |
| Tiene más, y todas llevan embedding del modelo actual | las más parecidas por coseno | `EMBEDDING` |
| Tiene más, sin embeddings (registradas antes, u otro modelo) | las más parecidas por huella de tinta | `HUELLA` |
| Tiene más, sin embedding ni huella (un logo viejo) | las primeras por orden | `ORDEN` — corre `pnpm refs:reindex` |
| La carga no trae la clave y `search.enabled` | las más cercanas de **cualquier** clave del scope con similitud ≥ `min_similarity` | `BUSQUEDA` |

En la búsqueda sin clave, que el modelo **no** confirme ninguna candidata no es
evidencia de nada: `matches_reference` queda vacío. Si confirma, se llena
`references.suggested_key`. La memoria entre documentos (`memory.*`) no cambia de
partición por una clave sugerida.

#### Recetas: un tipo nuevo que necesita que alguien MIRE la hoja

Las dos son solo catálogo. Versión larga, con el alta de referencias, cómo leer
cada resultado y los costos:
[`documentacion/NUEVO_TIPO_CON_FIRMAS_O_VISION.md`](../documentacion/NUEVO_TIPO_CON_FIRMAS_O_VISION.md).

| | **A — comparar contra lo registrado** | **B — solo visión** |
|---|---|---|
| Responde | ¿La firma / sello / logo / imagen es **la de esta entidad**? | ¿Están, y se ven genuinos? |
| Bloque `references` | sí (`scope` + `key_from`) | no |
| Referencias que registrar y mantener | sí, por entidad (`pnpm refs:*` o `/references`) | ninguna |
| Llamadas de visión | 2 | 2 (1 si es digital; +1 con `page_review`) |
| Embeddings | solo con más de 3 referencias de un elemento, o en la búsqueda sin clave | nunca |

**A — comparar firmas, logos u otra imagen contra referencias.** Elementos
compartidos por nombre, uno propio descrito en el tipo, y de quién son las
referencias:

```jsonc
"authenticity": {
  "expected_capture": "ANY",               // FISICO (+ preset CAPTURA_FISICA) si debe ser papel escaneado
  "elements": {
    "FIRMA": {}, "LOGO": {},               // definiciones de _shared.json → _authenticity_elements
    "HOLOGRAMA": {                         // una imagen que no existe ahí: se describe aquí
      "description": "Holograma de seguridad circular, plateado, en la esquina inferior derecha.",
      "fingerprint": false,                // no es un trazo de tinta: sin huella
      "embedded_image_is_insertion": false // en un PDF digital, ser una imagen incrustada no es sospechoso
    }
  },
  "vision": { "inspect_when_digital": "with_references" },   // compara también en PDFs digitales
  "references": {
    "scope": "PROVEEDOR",                  // mismo scope = las mismas referencias que ya usa CARTA_NOM35
    "key_from": "fields.RFC_PROVEEDOR"     // o "metadata.proveedor" (lo afirma quien sube)
  }
},
"fields": {
  "$presets": ["FIRMA_CONTRA_REFERENCIA", "LOGO_CONTRA_REFERENCIA"],
  "RFC_PROVEEDOR": { "type": "string", "required": true, "hint": "…", "validation": [] },
  // El elemento propio no tiene preset: sus campos van a mano.
  "HOLOGRAMA_PRESENTE": {
    "type": "boolean", "required": false, "computed": true,
    "authenticity_source": "element.HOLOGRAMA.present", "hint": "Se ve el holograma.",
    "validation": [{ "rule_type": "boolean_true", "allow_empty": true, "on_fail": "review", "message": "…" }]
  },
  "HOLOGRAMA_COINCIDE_REFERENCIA": {
    "type": "boolean", "required": false, "computed": true,
    "authenticity_source": "element.HOLOGRAMA.matches_reference", "hint": "Corresponde al registrado.",
    "validation": [{ "rule_type": "boolean_true", "allow_empty": true, "on_fail": "review", "message": "…" }]
  }
}
```

Después: `pnpm sync:one -- --type <TIPO>` y registrar las referencias de cada
entidad — `pnpm refs:register -- --scope PROVEEDOR --key <clave> --dir <carpeta>`
(o `--root <carpeta>` con una subcarpeta por entidad, o la API
`POST /references/PROVEEDOR/<clave>`). El elemento propio se puede registrar en
cuanto el tipo está en `catalogs/`: los `kind` válidos de un scope son la unión
de los que declaran sus tipos. Una entidad sin referencias no se rechaza: el
campo `*_COINCIDE_REFERENCIA` queda vacío.

**B — solo visión.** Sin `references`, sin nada que registrar:

```jsonc
"authenticity": {
  "elements": ["FIRMA", "SELLO"],
  "vision": { "page_review": true }        // opcional: una llamada más buscando indicios de edición
},
"fields": {
  "$presets": ["CAPTURA_FISICA"],          // gratis: ¿papel escaneado o un Word con imágenes pegadas?
  "FIRMA_PRESENTE":      { /* computed · authenticity_source: "element.FIRMA.present"    · review */ },
  "FIRMA_SIN_INSERCION": { /* computed · authenticity_source: "element.FIRMA.not_pasted" · review */ },
  "SELLO_PRESENTE":      { /* computed · authenticity_source: "element.SELLO.present"    · review */ },
  "SIN_INDICIOS_DE_EDICION": { /* computed · authenticity_source: "vision.no_edit_signs" · review */ }
}
```

Atajo para B: los presets `FIRMA_CONTRA_REFERENCIA` / `SELLO_CONTRA_REFERENCIA`
sirven tal cual — sin bloque `references` su campo `*_COINCIDE_REFERENCIA`
nunca se llena y, con `allow_empty`, no cuenta. Variantes: `"SELLO":
{"issuer_field": "<CAMPO>"}` + un campo con `element.SELLO.issuer_consistent`
(la leyenda debe nombrar al emisor), y `"vision": {"trigger": "on_suspicion"}`
(un archivo limpio no paga la visión).

Los dos ejemplos resuelven a las capas `structure + pixels + vision`
(comprobado con `authenticity.spec_for`). En ambas: todo lo que sale de la
visión va en `review`; mide antes con `pnpm autenticidad -- --type <TIPO> --dir
<carpeta>`; la visión examina solo la **primera** página con contenido; y la
primera vez en un ambiente hace falta el deploy antes del `sync:one` (los
elementos y presets compartidos viajan en el bundle).

#### Cómo recibe un campo su valor: `authenticity_source`

Los resultados caen en campos `computed` normales. Cada uno dice qué señal quiere
(los presets ya traen estos campos hechos):

```jsonc
"CAPTURA_FISICA": {
  "type": "boolean", "required": false, "computed": true,
  "authenticity_source": "structure.physical_capture",
  "hint": "…",
  "validation": [
    { "rule_type": "boolean_true", "allow_empty": true, "on_fail": "review", "message": "…" }
  ]
}
```

| `authenticity_source` | Valor |
|---|---|
| `evaluated` | `true` si todas las capas **pedidas** corrieron. **Sin `allow_empty`** en su regla: si la etapa no corrió, el documento va a revisión |
| `verdict` · `reasons` · `capture_kind` | `AUTENTICO`/`SOSPECHOSO`/`NO_AUTENTICO`/`NO_EVALUADO` · códigos separados por coma · cómo se capturó la hoja (`capture_kind` la produce **visión**) |
| `integrity.single_generation` | `true` el archivo se escribió una sola vez · `false` guardado sobre sí mismo (actualizaciones incrementales) o después de firmarse · *vacío* si la cadena de referencias cruzadas es ilegible. Descuenta la sección extra de un linearizado y una generación por firma digital |
| `integrity.generations` | cuántas veces se escribió el archivo (entero, evidencia) |
| `integrity.dates_consistent` | `false` = modificación anterior a la creación, o Info y XMP en desacuerdo más de `date_tolerance_s` · *vacío* sin fechas. Una modificación POSTERIOR a la creación es solo información |
| `integrity.producer_consistent` | `false` = Producer/Creator/XMP nombran un editor (`editor_patterns`), o creador de captura con productor ofimático · en imágenes, EXIF `Software`/APP13/PNG `Software`/historial XMP |
| `integrity.editor` | la cadena del editor encontrado (string, evidencia) |
| `integrity.no_annotations` | `false` = alguna página del documento tiene anotaciones fuera de `ignored_annotations` |
| `integrity.no_form_fields` | `false` = hay campos AcroForm (los `/Sig` no cuentan) |
| `integrity.no_text_overlay` | `false` = ≥ `min_overlay_chars` caracteres visibles pintados DESPUÉS de la imagen de página completa · *vacío* sin imagen de página completa o con ≥ `max_overlay_chars` (composición digital, no escaneo editado) |
| `integrity.no_image_over_text` | `false` = una imagen pintada DESPUÉS de texto visible que tapa · *vacío* sin texto visible |
| `integrity.no_shape_over_text` | `false` = un recuadro OPACO pintado DESPUÉS de texto visible que tapa (tachadura, o el parche blanco que un editor pone antes de reescribir un dato). El fondo de una celda se pinta antes del texto y un resaltado es translúcido: ninguno cuenta. Mínimo `integrity.shape_over_text_min_chars` caracteres tapados (default 2) · *vacío* sin texto visible |
| `integrity.no_watermark` | `false` = el archivo lleva una marca de agua: contenido etiquetado como tal (`/Artifact /Watermark`, capa o `/PieceInfo` de Acrobat con ese nombre, anotación `/Watermark`), leyenda en diagonal, texto grande translúcido, imagen translúcida, o la línea de una app de escaneo o editor de prueba (`integrity.watermark_phrases`). Umbrales: `watermark_min_font_size` (14 pt), `watermark_max_alpha` (0.6), `watermark_min_image_coverage` (0.05) · *vacío* en una página que es solo un escaneo: ahí la marca está en los pixeles y la ve `vision.no_watermark` |
| `integrity.fonts_consistent` | `false` = unos pocos caracteres (hasta `foreign_font_max_chars`, default 60, sin contar espacios) en una fuente que el archivo NO incrusta mientras ≥ 90 % del texto usa fuentes incrustadas: texto agregado con un editor · *vacío* con poco texto nativo o si el archivo no incrusta sus fuentes |
| `integrity.no_hidden_content` | `false` = una capa de contenido opcional apagada o una anotación con la bandera de oculta |
| `integrity.pages_consistent` | `false` = páginas escaneadas entre páginas digitales de origen, o páginas digitales de tamaño distinto (`page_size_tolerance`, 5 %): página sustituida o insertada · *vacío* con una sola página clasificable. Los escaneos no se comparan por tamaño |
| `integrity.digitally_signed` | hay firma digital (evidencia) |
| `integrity.signature_intact` | `false` = el `/ByteRange` de la firma más reciente no llega al fin del archivo (modificado tras firmar; motivo **duro**) · *vacío* sin firma. **No verifica criptografía** |
| `integrity.no_active_content` | `false` = JavaScript, `/OpenAction`, `/AA`, adjuntos (salvo `allow_embedded_files`) o acciones de `active_actions` |
| `integrity.camera_metadata` | imágenes: EXIF de cámara presente · *vacío* en PDF. `false` NO acusa (la mensajería quita el EXIF): `info` |
| `structure.physical_capture` | `true` captura de papel · `false` composición digital (determinista) · *vacío* si la estructura sola no alcanza |
| `structure.origin` | `ESCANER` · `APP_CAPTURA` · `OFIMATICA` · `DESCONOCIDO` · `IMAGEN` — solo evidencia |
| `structure.capture_as_expected` | la captura coincide con `expected_capture`: con `FISICO` es `structure.physical_capture`; con `DIGITAL`, `false` si el archivo es un escaneo (`CAPTURA_DISTINTA_DE_LA_ESPERADA`, suave) · *vacío* con `ANY` o indeterminado |
| `pixels.natural_background` | `false` = fondo perfectamente uniforme · *vacío* en bitonales o sin área en blanco. El motivo `FONDO_SIN_RUIDO_DE_CAPTURA` solo se levanta si el tipo lee esta señal o declara `expected_capture: "FISICO"`: en un tipo que acepta PDF digital, el fondo liso no es un hallazgo |
| `pixels.error_level_uniform` | ELA sobre los bytes JPEG del archivo (la carga, o la imagen de página completa de un escaneo): `false` = un bloque con nivel de error muy distinto al resto · *vacío* si no es JPEG, es chico o tiene poco contenido. **Sin calibrar con imágenes reales: evidencia** |
| `element.<KIND>.present` | se ve en la página · *vacío* si la visión no corrió |
| `element.<KIND>.not_pasted` | `false` si hay una imagen incrustada justo ahí (determinista) o el modelo ve indicios |
| `element.<KIND>.text` · `.ink_colour` | leyenda transcrita (con `read_text`) · color de tinta |
| `element.<KIND>.issuer_consistent` | la leyenda nombra al `issuer_field`. Se compara **en Python**, no opina el modelo |
| `element.<KIND>.matches_reference` | corresponde a una referencia registrada · ***vacío* si no hay referencias**, si no se pudieron leer (motivo `REFERENCIAS_NO_DISPONIBLES`, y `evaluated` pasa a `false`) o si las candidatas salieron de la búsqueda sin clave y el modelo no confirmó ninguna |
| `references.suggested_key` | la clave (p. ej. el número de proveedor) a la que corresponden los elementos según las referencias registradas, cuando la carga **no** la declaró y `references.search` está activo. *Vacío* si se declaró la clave, si nada se confirmó, o si los elementos apuntan a claves distintas. Informativo: una sugerencia para quien revisa |
| `vision.no_edit_signs` | con `vision.page_review`: `false` = el modelo ve indicios de edición en la página (`TEXTO_SUPERPUESTO`, `PARCHE_DE_FONDO`, `FUENTE_INCONSISTENTE`, `ALINEACION_ROTA`, `BORDE_DE_RECORTE`, `RESOLUCION_DISTINTA`, `TACHADURA_O_CENSURA`, `CORRECTOR_O_BORRADO`, `ANOTACION_AGREGADA`, `CAPTURA_DE_PANTALLA`) · *vacío* si no corrió |
| `vision.no_watermark` | misma llamada de `vision.page_review` (sin costo extra): `false` = el modelo ve una marca de agua AJENA al emisor (`HERRAMIENTA`: app de escaneo, editor o aviso de versión de prueba · `LEYENDA_SOBREPUESTA`: COPIA, MUESTRA, CANCELADO… · `OTRA`). El logotipo o patrón de fondo del emisor no cuenta · *vacío* si no corrió |
| `vision.watermark_text` | lo que dice esa marca, transcrito por el modelo (string, solo evidencia: nada se decide con él) |
| `memory.elements_unique` | `false` = la misma tinta/imagen en **otra** carta (nivel rechazo) |
| `memory.elements_not_similar` | `false` = demasiado parecida (nivel revisión o peor) |
| `memory.not_duplicate` | `false` = esta misma carta ya se recibió |

Tres reglas para declarar estos campos:

1. **`required: false` siempre.** El `presence` automático de `required: true` no
   acepta `on_fail`, y un campo vacío aquí es una respuesta legítima.
2. **`allow_empty: true` en todos menos en el de `evaluated`.** Vacío = «no se
   pudo determinar», no «no». Es lo que hace que un proveedor sin referencias no
   se rechace.
3. **`on_fail` dice la política**: `reject` solo para lo determinista
   (`integrity.*`, `structure.*`, `memory.elements_unique`) y solo después de
   medirlo; `review` para todo lo que sale de la visión. El modelo puede
   equivocarse y puede ser engañado: nunca rechaza solo. Los presets arrancan
   todo en `review` (observación).

Un `authenticity_source` que no existe deja el campo vacío (y con `allow_empty`
pasa en silencio): si un campo nunca se llena, revisa que el nombre de la señal
esté en la tabla de arriba.

Antes de activar la verificación en un tipo, y antes de pasar un `on_fail` de
`review` a `reject`, **mide con `pnpm autenticidad -- --type <TIPO> --dir
<carpeta>`** (gratis, sin AWS): dice cuántos documentos reales caerían por cada
señal — la capa de integridad corre ahí siempre, aunque el tipo todavía no la
declare. Medido el 2026-09-24 sobre 49 cartas reales: la primera corrida acusó
de guardado incremental al 35 % — eran las dos pasadas con que Word escribe
todo PDF, hoy descontadas — y el ELA a 3 escaneos genuinos; por eso el ELA es
`info` y `CARTA_NOM35` **no** lleva el preset todavía (detalle en
`AUTENTICIDAD_Y_REFERENCIAS.md` §6).

### Selección de páginas de interés (`page_selection`)

Un PDF de 100 páginas cuyos campos viven en 3 sale caro: hoy se manda el
texto de **todas** a Bedrock. `page_selection` declara cómo reconocer las
páginas que sí importan para ESTE tipo, y el pipeline extrae solo ésas
(`pipeline/page_selector.py`).

```jsonc
"page_selection": {
  "enabled": true,                 // opcional; ver "Encendido y apagado"
  "min_score": 3,                  // marcadores DISTINTOS que debe cumplir una página
  "follow_pages": 1,               // páginas de continuación tras cada acierto
  "max_ratio": 0.6,                // arriba de esto se procesa TODO (fail-open)
  "min_pages": 5,                  // <= esto: ni se intenta filtrar
  "expected_pages": 6,             // tamaño de UN documento de este tipo (o [3, 6])
  "duplicate_key_pattern":         // opcional: identidad impresa del documento
    "NUM\\.?\\s*PEDIMENTO[:\\s]*(\\d{2}\\s*\\d{2}\\s*\\d{4}\\s*\\d{7})",
  "markers": [                     // regex, case-insensitive
    "NUM\\.?\\s*PEDIMENTO",
    "CVE\\.?\\s*PEDIM"
  ]
}
```

**Cuándo ni se molesta en filtrar.** Antes de puntuar nada, hay dos salidas
rápidas — en ambas se procesa el documento completo:

- **`min_pages`** (5 por defecto): un documento corto es **una sola llamada a
  Bedrock de todos modos**, así que filtrarlo ahorra tokens de entrada pero
  ni una llamada, mientras que un marcador mal calibrado sí podría tirar una
  página que importaba. Mal negocio.
- **`expected_pages`**: cuánto mide **un** documento de este tipo — un número
  (`6`) o un rango (`[3, 6]`), del que solo se usa el tope. Si la subida no es
  más grande que eso, el archivo **es** ese documento, no un expediente que lo
  contiene: no hay nada que recortar. Un pedimento suelto trae "Página X de 5",
  así que `PEDIMENTO_ADUANAL` declara `6` (los 5 del formato más la forma
  simplificada).

Ninguna de las dos cuenta como fail-open: no es que el selector fallara, es
que decidió no meterse. `pnpm paginas` las muestra como `SIN FILTRAR → ...`.

**Copias del MISMO documento: `duplicate_key_pattern`.** Un expediente real
imprime el mismo pedimento varias veces (copia del agente, copia del
importador, la forma simplificada de una página). Medido en
`test-docs/full-dataset-pedimentos/'26 07 3435 6041353 CG.pdf'`: la selección
se quedaba con 51 páginas que eran **cuatro renderizados del mismo
pedimento** — se extraían los cuatro (7 chunks de Bedrock, 425 s) y el merge
deduplicaba las filas idénticas al final. Se pagaba tres veces para tirar dos.

Declarando un regex cuyo **primer grupo** captura la identidad impresa del
documento, el selector parte las páginas elegidas en los documentos que
forman (abre uno nuevo una "Página 1 de N" o un `start_marker`), lee esa
identidad en cada uno y conserva **el tramo más largo por identidad**:
51 páginas → 17, y de 7 chunks a ~2.

- **Más largo, no el primero**: la forma simplificada de 1 página trae el
  mismo número que el pedimento completo y puede venir antes. Quedarse con la
  primera perdería el documento entero.
- **Dos documentos DISTINTOS se conservan los dos** — tienen identidades
  distintas. Esto es lo que lo separa de un "cortar tras el primero", que sí
  perdería un segundo pedimento en el mismo PDF.
- **Fail-open** como todo el selector: un tramo cuya identidad no se puede
  leer nunca se descarta, un regex inválido se ignora con un warning, y sin
  declarar la llave el comportamiento es idéntico al de antes.
- Cuántas copias se descartaron sale en el `reason` del bloque
  `pageSelection` del resultado.
- **Acepta un regex o una LISTA de regex**, y se prueban en orden hasta que
  uno capture. Una lista y no una alternancia (`(?:A(x)|B(y))`) porque ahí el
  grupo 1 solo existe en la primera rama: un tipo cuyo identificador aparece
  bajo etiquetas distintas según el proveedor necesita un patrón por etiqueta.
  Ver `FACTURA_FLETE_EXTRANJERO`, donde el número de factura se imprime como
  `Invoice #:`, `INVOICE #`, `Invoice Number:` y —lo peor— dentro de una tabla
  cuyo OCR mete texto de otra columna entre la etiqueta y el valor.
- La identidad se normaliza a **alfanumérico en mayúsculas**, no a solo
  dígitos: `SJ191703` no puede volverse `191703`, que otro documento podría
  llevar legítimamente.

**Si el tamaño es demasiado variable: `expected_pages: "auto"`.** Hay tipos
para los que cualquier rango sería mentira. Declarar `"auto"` (o `"variable"`)
dice dos cosas: no hay atajo por tamaño, y **si los marcadores no alcanzan, en
vez de procesar todo se le pregunta al modelo** dónde está el documento
(`pipeline/page_locator.py`).

```jsonc
"page_selection": {
  "expected_pages": "auto",
  "markers": ["..."]
}
```

Esa llamada manda el mismo digest por página que usa el segmenter (~450
caracteres), no el texto completo: sobre un expediente real de 86 páginas
cuesta ~$0.012 contra los ~$0.09 de extraerlo entero. Y **solo corre cuando el
camino gratis ya se rindió** — si los marcadores aciertan, nunca se llama.

El localizador jamás es la razón por la que se pierde una página: ante una
salida impredecible, sin rangos, o un rango que cubre todo el documento,
devuelve "no sé" y se procesan todas. Su costo es real y se registra aparte,
en el servicio **`BedrockPageLocator`** de la tabla de costos — no está
escondido dentro de la extracción.

Úsalo solo si de verdad no puedes dar un rango: el camino de marcadores es
gratis y éste no.

**Límites del documento: dónde empieza y dónde acaba.** Puntuar dice "esta
página *parece* del tipo". Declarar límites dice "este documento va de AQUÍ a
AQUÍ", que es mejor información:

```jsonc
"page_selection": {
  "markers": ["NUM\\.?\\s*PEDIMENTO", "CVE\\.?\\s*PEDIM"],   // confirma el TIPO
  "start_markers": ["PEDIMENTO\\s+P[áa]gina\\s+1\\s+de"],      // opcional
  "end_markers": ["FIN\\s+DE\\s+PEDIMENTO"],                    // opcional
  "page_of_pattern": "P[áa]gina\\s+(\\d+)\\s+de\\s*:?\\s*(\\d+)"  // opcional
}
```

**`page_of_pattern` es el más potente de los tres.** Con sus dos grupos
(página actual, total) **cualquier** página revela el documento entero: si la
página 12 dice "Página 3 de 5", el documento va de la 10 a la 14. Eso importa
porque ya no hace falta que la portada sea legible.

Los tres se aplican **solo sobre páginas ya confirmadas** por `markers`. Es lo
que impide que el "Página 1 de 6" de un CFDI vecino abra un rango propio y se
trague el documento de al lado.

Medido en expedientes reales, contra el scoring con `follow_pages`:

| | Antes | Ahora | Por qué |
| --- | --- | --- | --- |
| `long-docs.pdf` | 3–28 | 3–**27** | `follow_pages` arrastraba el DODA de la 28 |
| expediente 86 págs | **4**–19 | **3**–18 | La portada del pedimento (pág 3) estaba escaneada, sin texto que puntuar: se recupera desde el "Página 2 de 5" de la 4. **Antes se perdía.** |

O sea, no es solo ahorro: se estaba descartando una página buena y quedándose
con una ajena. Cuando el tipo declara límites, éstos **sustituyen** a
`follow_pages` (que sigue siendo el camino para tipos sin límites declarados).

Un rango imposible se ignora en vez de romper nada: "Página 9 de 3" no
describe nada real, y un "Página 1 de 500" en un archivo de 6 páginas se
recorta a los límites del documento.

**Cómo puntúa.** Cada página suma **un punto por marcador DISTINTO** que
aparece en su texto — diez repeticiones de la misma frase valen uno. Es el
mismo scoring que ya usaba `qr_recovery.page_markers` (de hecho comparten
implementación), y por la misma razón: si un escaneo malo destroza una frase,
la página pierde un punto, no la partida entera.

**`follow_pages`** arrastra las N páginas siguientes a cada acierto. Los
campos suelen desbordarse a páginas de continuación que no repiten el
encabezado (las PARTIDAS de un pedimento); una página de más cuesta
centavos, una fila perdida cuesta un dato.

**Nunca recorta: o filtra, o procesa todo.** Si la selección supera
`max_ratio` del documento, se **descarta entera** y se procesan todas las
páginas. Recortar una selección para que quepa en un número tiene el mismo
defecto que truncar el PDF a ciegas: tira contenido real. Medido sobre un
expediente real de 139 páginas con 51 páginas de pedimento (tres copias, cada
una con sus anexos), un tope duro de 8 habría cortado dos tercios de las
partidas; con `max_ratio` se conserva el ahorro del 63 %. `max_pages` existe
como tope absoluto opcional y se comporta igual: dispara fail-open, no tijera.

**Fail-open siempre.** Se procesan todas las páginas cuando el tipo no
declara `page_selection`, cuando no declara `markers`, cuando ninguna página
alcanza `min_score`, o cuando la selección excede el tope. Un selector que no
encontró nada **nunca** significa "nada es relevante".

**Encendido y apagado (por tipo, no global).** Gana lo más específico, salvo
el kill-switch, que solo apaga:

| Situación | Efecto |
| --- | --- |
| El tipo no declara `page_selection` | Apagado. Camino idéntico al de hoy. **Default.** |
| Declara `page_selection` sin `enabled` | Encendido para ese tipo |
| `"enabled": false` | Apagado para ese tipo, **conservando** sus marcadores calibrados |
| `PAGE_SELECTION_ENABLED=false` | Apaga TODOS los tipos. Nunca enciende uno que su catálogo dejó apagado |

Como vive en el catálogo, `pnpm sync:one -- --type <TIPO>` lo prende o apaga
**sin redeploy**.

**Calibra antes de confiar — es gratis.**

```bash
python scripts/analizar-seleccion-paginas.py --type PEDIMENTO_ADUANAL --show-pages
```

Lee solo el texto nativo con pypdf (sin Textract, sin Bedrock, sin
credenciales AWS) y reporta qué páginas elegiría y cuántas ahorrarías.
`--min-score`/`--max-ratio` prueban umbrales sin editar el catálogo. Si
muchos documentos salen en fail-open, los marcadores están mal calibrados.
**Las páginas escaneadas (sin capa de texto) siempre salen en fail-open**:
el selector de texto no puede opinar sobre ellas.

Para esos tipos —los que SIEMPRE llegan escaneados— el modo gratis no sirve
para calibrar: todo saldría en fail-open y el reporte no diría nada. `--ocr`
corre el mismo Textract que corre la Lambda y `--cache` guarda el texto por
página, así que el OCR se paga UNA vez y luego se itera sobre los marcadores
gratis:

```bash
python scripts/analizar-seleccion-paginas.py --type FACTURA_FLETE_EXTRANJERO \
    --dir ~/docs/facturas --ocr --cache .cache-ocr --show-pages
```

Cuando el tipo declara `multi_instance`, el reporte añade en qué documentos se
partiría cada archivo y cuántas copias se descartaron. Así se calibró
`FACTURA_FLETE_EXTRANJERO`: sobre 61 páginas de OCR real las 5 facturas
puntúan entre 5 y 8 y lo más alto que alcanza algo que NO es factura es 3 (el
packing list, que repite "INVOICE NUMBER"), de donde sale `min_score: 4`. El
`__COMENTARIO__` del tipo deja escrita esa medición; **rehazla antes de tocar
`markers` o `min_score`**.

Cada documento filtrado lleva un bloque `pageSelection` en el resultado
(`pagesConsidered`, `selected`, `skipped`, `failOpen`, `reason`, `scores`)
para auditar qué se descartó y por qué. Nada se descarta en silencio.

Dentro va `savings`, que separa lo facturado de lo estimado:
`billedInputTokens`/`billedOutputTokens` son **exactos** (los reportó
Bedrock), y todo lo que empieza con `estimated` valora la llamada cara que
nunca se hizo — input por densidad tokens/carácter de la llamada que sí
corrió, output por chunks evitados. **Ojo:** el ahorro de páginas y el de
tokens no son el mismo número — descartar el 81 % de las páginas de un
expediente real recortó solo el 67 % de los tokens, porque las páginas que se
conservan son las densas en texto. Detalle y validación en
`documentacion/COSTOS.md` §0-bis.

### Cardinalidad del catálogo y de una solicitud

Cada tipo puede declarar su comportamiento normal:

```jsonc
"occurrence_mode": "single"    // conserva la primera ocurrencia
"occurrence_mode": "multiple"  // conserva todas las ocurrencias
```

La precedencia completa, de mayor a menor, es:

1. Sufijo del tipo en el request: `TIPO:N` o `TIPO:all`.
2. `occurrenceMode: "single" | "multiple"` enviado en `POST /documents`, que
   sobrescribe el catálogo para todos los tipos sin sufijo.
3. `occurrence_mode` del catálogo de cada tipo.
4. Default global si el catálogo no lo define: `single` cuando se declaró un
   tipo y `multiple` cuando se declararon varios.

Un valor de catálogo distinto de `single`/`multiple` hace fallar `pnpm build`.
Si un override inválido entra directamente por DynamoDB, el runtime lo trata
como ausente y usa el default global. Consulta `documentacion/api-docs/API_USO.md` o
`documentacion/api-docs/API_RESUMEN.md` para el parámetro del POST.

`FACTURA_FLETE_EXTRANJERO` declara `occurrence_mode: "multiple"`, así que una
subida que pida únicamente ese tipo encuentra todas sus facturas aun sin mandar
`occurrenceMode`. Cambios de esta propiedad se pueden aplicar con
`pnpm sync:one -- --type <TIPO>` igual que el resto del schema.

El catálogo sí debe describir cómo separar físicamente varias instancias:
para eso existen `page_selection.multi_instance` y sus límites
`start_markers`/`page_of_pattern`.

### Varias instancias del MISMO tipo en un archivo (`multi_instance`)

`page_selection` contesta *qué páginas de este documento vale la pena pagar*.
Es la pregunta correcta para un pedimento configurado como `single`: la subida
trae **uno** (más sus reimpresiones). Es la pregunta equivocada para una factura extranjera, donde un
PDF trae rutinariamente **varias facturas distintas** — un archivo de dos
páginas que son dos facturas completas, o un expediente con tres de tres
proveedores.

`occurrence_mode: "multiple"` en el catálogo —o `occurrenceMode: "multiple"`
en el POST— dice que deben conservarse todas, pero
para el camino manual hace falta saber dónde termina cada una. Sin `multi_instance`,
subir ese archivo declarando `documentType` produce **un
solo documento**: `segment_pages` devuelve un segmento que cubre todo el
fichero, así que las N facturas se extraen juntas y solo sobrevive un
`NUMERO_FACTURA`, un `TOTAL` y un `pageStart/pageEnd`.

```jsonc
"page_selection": {
  "multi_instance": { "follow_pages": 0, "max_instances": 25 },
  "markers": ["..."],                      // confirma el TIPO de la página
  "start_markers": ["INVOICE\\s*(?:#|NO\\.?|NUMBER)"],   // abre un documento
  "page_of_pattern": "Page\\s+(\\d+)\\s+of\\s+(\\d+)",
  "duplicate_key_pattern": ["..."]         // colapsa las copias
}
```

`true` equivale a `{}` (todos los valores por defecto).

**Es determinista y gratis**: puro regex sobre el texto que pypdf/Textract ya
produjo. Cero llamadas al modelo — el partidor corre *en vez* de la
clasificación, no además de ella.

**Y sale más barato que no partir**: cada instancia es su propia llamada de
extracción, pero con sus 1-2 páginas en vez del archivo completo. Medido sobre
un expediente real de 30 páginas: 3 llamadas de una página contra una de 30.

**También corrige rangos del clasificador.** En modo guiado/automático el modelo
puede reconocer bien el tipo pero juntar dos documentos consecutivos (por
ejemplo, facturas de las páginas 9–10). Antes de extraer, el segmentador vuelve
a aplicar `multi_instance` dentro de cada rango tipado; si los marcadores
delimitan dos instancias, reemplaza el rango por ambas. Lo que no pueda separar
con confianza permanece intacto (fail-open).

| Knob | Default | Para qué |
|---|---|---|
| `follow_pages` | `0` | Páginas de continuación por instancia. **Nunca cruza al documento siguiente.** Súbelo solo si un proveedor manda facturas de varias páginas SIN numerarlas: arrastrar una de más mete el documento vecino dentro de la factura. |
| `max_instances` | `20` | Por encima de esto se procesa todo como un documento: los marcadores están señalando páginas, no documentos, y cada instancia es una llamada real a Bedrock. |

**Qué ignora a propósito.** `min_pages` y `expected_pages` son atajos de
*costo* ("este archivo es tan chico que filtrarlo solo puede perder páginas"),
que no dicen nada sobre cuántos documentos trae: un archivo de 2 páginas que
son dos facturas completas se tiene que partir igual. `max_ratio` se salta por
lo mismo — que el archivo sea **todo** instancias de este tipo es el caso
normal aquí, no una señal de alarma.

**Fail-open, como todo el módulo.** Vuelve UN segmento que cubre el archivo
entero —o sea, exactamente lo de antes— cuando: el tipo no declara `markers`;
no declara ningún límite (`start_markers` ni `page_of_pattern`), y sin eso todo
caería en un solo tramo de todas formas; ninguna página alcanza `min_score`;
solo se encontró una instancia; o se encontraron más de `max_instances`.

**Precedencia de los límites.** Una página que dice "Page 2 of 2" **no** abre
un documento nuevo por mucho que reimprima el encabezado: la etiqueta de página
gana sobre `start_markers`. Sin eso, un proveedor que repite "Invoice #" en
cada página abriría una instancia falsa por página.

**El orden importa**: las copias se colapsan **antes** de aplicar
`follow_pages`, nunca después. "Gana el tramo más largo" tiene que comparar los
documentos como los encontraron los marcadores; si no, una copia anotada
seguida de una página en blanco le gana al original por una página de
continuación y se conserva la copia en vez del documento que copia.

**Copias físicas y cardinalidad.** El modelo considera una reimpresión como
otra ocurrencia —correcto: desde el texto no puede saber la intención del
caller—. Con `occurrence_mode: "single"`, `occurrenceMode: "single"` o un
override `:1`,
`segmenter._collapse_duplicate_segments` usa `duplicate_key_pattern` para
elegir la impresión más completa antes de conservar sólo la primera; las otras
se marcan internamente `OTRO/COPIA_DUPLICADA`. Con `multiple`, `:all` o `:N`
mayor que uno, las copias **se preservan como ocurrencias físicas del tipo**:
pedir todas y después ocultar una copia contradiría la cardinalidad solicitada.

**Calibra con `--ocr` si tus documentos llegan escaneados.** `pnpm paginas` lee
texto nativo, y un tipo cuyos PDF siempre vienen escaneados saldría entero en
fail-open. Ver más abajo.

### El contrato de `confidence`

Todo escalar resuelto reporta un `confidence` **numérico entre 0 y 1, nunca
`null`** — antes un campo que nadie había medido (los subcampos de
`LIQUIDACION_NORMALIZADA`, un campo `derived` que cayó a su `default`, un
campo que el documento no traía) salía con `confidence: null` y obligaba a
todo consumidor a tratar la ausencia de número antes de poder compararlo
contra nada. Las reglas, todas en `pipeline/confidence.py`:

| Situación | Qué reporta |
|---|---|
| El extractor midió el campo | Su número (self-report del modelo × confianza OCR de la página). |
| Celda de una fila de array | La de **esa fila**, no la del array entero (antes todas las filas compartían el mismo número). Al modelo se le pide **UNA** confianza por fila, no una por celda: medido sobre un expediente real, el `confidence` por celda era el 44 % de todo lo que emitía el modelo (16,662 tokens de salida → ~9,465 sin él), y una fila se lee bien o mal como unidad. Una fila que no reporte número no inventa ninguno: sus celdas caen a la confianza OCR del segmento. |
| Campo calculado de varias celdas | El **promedio** de las celdas que lo alimentaron: un total vale lo que valen los renglones que sumó. |
| Campo `derived` | La del campo que leyó — incluido cuando cayó al `default`: nada matcheó *esa* entrada, así que el fallback es tan confiable como la entrada para la que se eligió. |
| Valor sin medición propia | Qué tan bien se leyó la **fuente** (confianza OCR del segmento; 1.0 en los pipelines deterministas XML/tabular). Así un hueco del pipeline no borra dato bueno. |
| Campo vacío / ausente | `0.0` — "no lo sé". No entra al promedio del documento (no describe nada leído) y **no** se marca como descartado: no estaba, no se perdió. |

Y dos umbrales que **no** son lo mismo:

- `min_confidence` (default 0.70) **señala**: `valid: false` con `reason`,
  el valor se conserva para que alguien lo juzgue.
- `discard_below_confidence` (default 0.50) **borra**: `value: null`,
  `valid: false`, `reason: "value discarded: confidence ... below discard
  floor ..."` y un check `discard_low_confidence` (severidad `warning`, así
  que el documento cae a revisión, no a rechazo). La confianza baja se
  conserva en el nodo: es la que explica por qué no hay valor.

### Verificación de tipo (`type_check`): ¿el archivo ES lo que se declaró?

Cuando quien sube el archivo declara el tipo (`documentType=ESTADO_CUENTA_PORTAL`)
el segmentador no corre: el archivo entero se toma por ese tipo y el extractor
llena el schema con lo que encuentre. Antes nada comprobaba que el contenido
correspondiera. Un archivo ajeno salía mal **por accidente**: si el modelo no
hallaba nada, fallaban los `presence` y se rechazaba con confianza 0; pero si
compartía dos o tres campos (un RFC, una fecha, una razón social) quedaba
`PARTIALLY_APPROVED` con confianza 0.6-0.8 y nadie se enteraba de que era otro
documento. Medido: una constancia fiscal subida en la casilla del estado de
cuenta del portal.

**Cómo funciona (`pipeline/type_check.py`), sin una llamada extra a Bedrock:**

1. En el **mismo tool call** de extracción el schema lleva una llave
   `_DOCUMENTO` (`corresponde`, `confianza`, `descripcion`) y el prompt la regla
   "antes de extraer, decide si el texto ES ese documento; `corresponde=false`
   SOLO si es de otra naturaleza; una variante extranjera, de otro banco, país
   o formato, o un escaneo malo del MISMO documento SÍ corresponde; extrae los
   campos de todos modos". ~30 tokens de salida. Solo la pide la llamada de
   escalares (la partición `arrays` ve el mismo texto). Con varios chunks el
   merge es **fail-open**: basta que uno reconozca el documento.
2. **Guardia gratis** para que el modelo no tumbe solo un documento que
   claramente se leyó: si ≥ `min_required_filled` (default **1.0 = todos**) de
   los campos `required` de primer nivel salieron llenos con confianza ≥
   `min_confidence`, la señal se degrada (`reason: SEÑAL_CONTRADICHA_POR_CAMPOS`)
   y el efecto baja un escalón (`reject` → `review`). Por qué "todos" y no "la
   mayoría" (medido 2026-10-08 con MiniMax): sobre la constancia fiscal subida
   como estado de cuenta el modelo llenó `TITULAR` con la razón social del
   contribuyente y `PAIS` con `MEX` — 2 de 3 obligatorios — y con 0.5 el
   archivo ajeno se escapaba a revisión. Un documento legítimo bien leído tiene
   todos sus obligatorios; si le falta uno iba a revisión de todos modos. Si el
   tipo declara `page_selection.markers`,
   los marcadores deciden en los dos sentidos: alguna página los alcanza →
   degrada aunque no haya campos; ninguna → rechazo pleno aunque haya campos
   (son evidencia más fuerte que tres campos genéricos).
3. **Efecto**: un check `document_type_match` en `documents[i].checks` graduado
   por `on_fail` (default `reject` → `blocking` → `REJECTED` → `RECHAZADO`), y
   `documents[i].confidence` capada a **0.05** — los campos conservan su valor
   y su propia confianza; lo que cae es el número del documento, porque no hay
   documento de ese tipo.

**Salida.** En la raíz del envelope, siempre presentes:
`documentMatchesType` (`true`/`false`/`null` = no evaluado),
`rejectionConfidence` (0-1, la confianza del modelo en que NO es el
documento; `0.0` si corresponde) y `detectedDocument` (qué parece ser). Por
documento, `documents[i].typeCheck` (`matches`, `rejectionConfidence`,
`detected`, `reason`); en el resultado completo de S3 además `guard` con lo
que vio la guardia (`requiredFilled`/`requiredTotal`/`markers`).

**Ajuste por tipo** (opcional, hot-reloadable con `pnpm sync:one`; la
verificación en sí es CÓDIGO y necesita la Lambda desplegada):

```jsonc
"type_check": {
  "on_fail": "review",          // reject (default) | review | info
  "min_required_filled": 1.0,   // 0-1, la guardia (default: todos los obligatorios)
  "enabled": true               // false la apaga para este tipo
}
```

Kill switch global fail-open: `TYPE_CHECK_ENABLED=false` → `evaluated: false`,
sin check ni cap, `documentMatchesType: null`. Un tipo XML/tabular nunca la
evalúa (no hay modelo al que preguntarle).

**Qué NO es.** No es un clasificador: no dice qué tipo del catálogo es el
archivo, solo si es el declarado. No es autenticidad: una carta falsificada
que *parece* carta corresponde. Y no es calidad: un escaneo ilegible del
documento correcto corresponde (y sus campos vacíos lo mandan a revisión por
la vía normal).

**Cuándo tocarlo.** Si una variante legítima sale `documentMatchesType:
false`, el remedio es la `description` del tipo (que diga que acepta esa
variante), no la guardia ni `on_fail`. Mídelo con el modelo DESPLEGADO
(`pnpm procesar -- <pdf> <TIPO> minimax.minimax-m2.5`) sobre los documentos
reales del tipo antes de endurecer nada. Medido el 2026-10-08 sobre los 10
archivos reales de la casilla "Estado de cuenta" del portal (carátulas
mexicanas, cartas de bancos extranjeros, wire instructions y la constancia
fiscal mal subida): 9/9 legítimos `true`, la constancia `false` con
`rejectionConfidence 1.0` y detectada como "Constancia de Situación Fiscal del
SAT" — ver `documentacion/COSTOS.md` §11.

### Reglas de validación soportadas (`rule_type`)

Van dentro de `fields[NOMBRE].validation` (sin `"field"`, es implícito por
estar anidado) o en `cross_field_validation` a nivel de tipo (con `"field"`
explícito). **Solo estos `rule_type` tienen implementación real** —
`validators/engine.py` es el árbitro, no lo que "parezca razonable":

| `rule_type` | Params relevantes | Qué valida |
|---|---|---|
| `regex` | `pattern`, `message` | El valor (normalizado y crudo) matchea el patrón. |
| `regex_each` | `pattern`, `message` | Cada elemento de un array matchea el patrón. |
| `presence` | `message` | El valor no está vacío. `required: true` en el campo ya genera esto automáticamente — no lo dupliques. |
| `boolean_true` | `message`, `allow_empty` | El valor es afirmativo (`si`, `sí`, `yes`, `true`, `1`, `presente`). Vacío = falla, salvo con `"allow_empty": true`, que da el tercer estado **"no evaluado"**: pensado para booleanos `computed` que el pipeline no siempre puede determinar (la etapa no aplica a ese archivo, no hay referencias registradas...). |
| `date_expiry` | `message` | La fecha es futura respecto a hoy (documento no vencido). |
| `date_max_age` | `pattern` o `params.value` (años), `message` | La fecha no es más vieja que N años. |
| `minimum_items` | `params.value` (default 1) | Un array tiene al menos N elementos. |
| `custom_formula` / `cfdi_total` | `params.formula` (ej. `"SUBTOTAL + IVA_TRASLADADO"`, soporta `SUM(ARRAY.CAMPO)`), `params.tolerance` (default 0.01) | El campo declarado coincide con el resultado de la fórmula. Operandos faltantes cuentan como 0 (queda registrado en `details.assumptions`). |
| `sum_array` | `source` (ej. `"CUADRO_LIQUIDACION.CONCEPTOS.IMPORTE"`), `comparison_field` (default: el propio campo), `tolerance` | Un total declarado coincide con la suma de un array. |
| `equals_field` | `source`, `tolerance` | Dos campos/paths numéricos deben coincidir. |
| `array_length` | `source` | Un conteo declarado coincide con `len()` de otro array. |
| `cross_document_match` / `cross_document_amount` | `target_document_type`, `target_field` | El valor coincide con el de otro documento del mismo expediente (requiere `pipeline: expediente` para tener hermanos que comparar). |
| `negative_keyword` | `params.keywords` (lista) | Ninguna de esas palabras aparece en el texto del documento. |
| `expected_value_by_app` | — | El valor coincide con `expected_values` que manda el caller (ej. RFC importador configurado por la app). |
| `page_ranges_no_overlap` | — | Un array de `{PAGE_START, PAGE_END}` no tiene rangos solapados. |

Params opcionales comunes a casi cualquier regla numérica: `"tolerance"`
(default 0.01) y `"allow_empty": true` (no falla si el campo viene vacío).
Y dos que valen para **cualquier** regla: `on_fail` (qué le pasa al documento
si falla) y `when` (a qué documentos aplica), en las dos secciones siguientes.

#### `on_fail`: qué le pasa al documento cuando la regla falla

Param opcional de **cualquier** `rule_type`. Sin él, la severidad la decide el
código de la regla (casi siempre `error`), y aquí hay una sutileza que
sorprende: **un solo `error` entre varios checks buenos NO rechaza el
documento** — da `PARTIALLY_APPROVED`, que el API reporta como
`REQUIERE_REVISION`. Solo se rechaza cuando fallan *todos* los checks. Con
`on_fail` el catálogo lo dice explícitamente, regla por regla:

| `on_fail` | Severidad del check | Efecto en el documento |
|---|---|---|
| `"reject"` | `error` + `blocking: true` | **`RECHAZADO` por ese fallo solo**, sin importar cuántos campos más estén bien. Para señales deterministas e inequívocas. |
| `"review"` | `warning` | `REQUIERE_REVISION`. Para señales de juicio o dudosas. |
| `"info"` | `info` | Se reporta en el campo y en `checks`, pero no cambia el status. |
| *(ausente)* | la que decida la regla | El comportamiento de siempre. |

```jsonc
"validation": [
  {"rule_type": "boolean_true", "allow_empty": true, "on_fail": "reject",
   "message": "Firma o sello idénticos a los de la carta de otro empleado"}
]
```

Cambiar de `"review"` a `"reject"` es un `pnpm sync:one`, sin deploy — la forma
de arrancar una regla nueva **en observación** y endurecerla cuando ya se midió.
Un valor que no sea uno de los tres se ignora (no se adivina), y una regla
`skipped` no bloquea nunca: no hubo veredicto que graduar. No aplica al
`presence` automático de `required: true` (no es una regla declarada); si
necesitas graduar una presencia, declara `required: false` y una regla
`presence` explícita con su `on_fail`.

#### `when`: a qué documentos aplica la regla

Param opcional de **cualquier** `rule_type`. Sin él, la regla aplica a todo
documento del tipo — y un tipo que acepta variantes del mismo documento (la
constancia fiscal de un contribuyente mexicano **o** la de uno extranjero) solo
tendría dos salidas: aflojar la regla para todos, o reprobar siempre a la
variante que no la cumple. Con `when` la regla se queda con su variante:

```jsonc
"RFC": {
  "type": "string", "required": true, "hint": "...",
  "validation": [
    {"rule_type": "regex", "pattern": "^[A-ZÑ&]{3,4}\\d{6}[A-Z0-9]{3}$",
     "message": "El RFC no cumple el formato de 12 o 13 caracteres",
     "when": {"field": "PAIS", "eq": "MEX"}}
  ]
}
```

- Una condición nombra el **`field`** que lee (path con puntos, igual que
  `source` en `equals_field`) y uno o más operadores, combinados con AND. Son
  **los mismos operadores cerrados de `derived`**: `eq`/`ne`, `in` (lista),
  `matches` (regex, sin distinguir mayúsculas), `gt`/`gte`/`lt`/`lte`,
  `empty`/`not_empty`. Una **lista** de condiciones también es AND.
- **Condición falsa = la regla no opina**: no falla y no deja check, igual que
  una regex sobre un campo opcional vacío. Un campo de condición que no se
  leyó cuenta como falso (un `PAIS` vacío no es `MEX`); si de ese campo
  depende qué reglas corren, decláralo `required`.
- **`required` no se puede condicionar** (el `presence` automático no es una
  regla declarada). Para "obligatorio solo en esta variante": `required:
  false` y una regla `presence` explícita con su `when`.
- **Un `when` mal escrito no prende ni apaga la regla en silencio**: sin
  `field`, sin operador o con un operador que no existe, el check sale
  `skipped: true` con `invalid 'when' (...)` — la misma señal que un
  `rule_type` desconocido, y por la misma razón.
- Dentro de `item_fields` el `field` es una columna de **la misma fila** (la
  regla ve la fila como su `entity_map`, ver arriba).
- Para condicionar la regla de un subcampo de un **objeto**, declárala en el
  objeto con `"subfield"` (como `CUADRO_LIQUIDACION`): una regla escrita
  directamente dentro del subcampo anidado no se evalúa hoy, con o sin `when`.
- Se combina con `on_fail`, que solo gradúa la regla cuando sí aplicó.

Ejemplo real: `custom/portal/CONSTANCIA_SITUACION_FISCAL_PORTAL.json` — el
patrón del RFC, el QR del SAT, el estatus en el padrón, el CP de 5 dígitos y
el mínimo de actividades/regímenes aplican solo cuando `PAIS` es México. La
condición usa `matches` con `MEX|MX|MÉXICO` a propósito: si el modelo se sale
de la clave de 3 letras, la `regex` de `PAIS` lo marca y las reglas mexicanas
siguen corriendo en vez de apagarse.

**Gotcha importante:** un `rule_type` que NO está en esta tabla —
`greater_than`, `in`, `less_than`, lo que sea que "suene lógico" — **no
truena ni avisa fuerte**: cae al fallback genérico y se registra como un
`RuleCheck` con `skipped: true, severity: "warning", message: "unknown rule
type '...' skipped"`. El documento se sigue procesando como si esa regla no
existiera. Si escribes una regla y el campo nunca sale inválido pase lo que
pase, **revisa los `checks` devueltos buscando `"skipped": true`** antes de
asumir que tu regla está mal calibrada — probablemente el `rule_type` no
existe todavía. Para agregar uno nuevo: extiende `validators/engine.py`
(`_dispatch_rule` + una función `_check_<nombre>`) — aplica automáticamente a
todos los tipos, sin tocar nada por tipo.

### Reglas cross-documento

Viven en `_shared.json` → `cross_document_validation_rules` (no en el archivo
del tipo) porque comparan **documentos distintos dentro del mismo
expediente** — solo aplican cuando el flujo pasa por el pipeline de
expediente (ver abajo). `rule_type` soportados hoy:
`same_pedimento`, `expected_importer_rfc`, `unique_cfdi_uuid`,
`expense_reconciliation`, `pedimento_value_reconciliation` — cada uno con su
propia lógica en `pipeline/expediente.py` (`_run_cross_document_rules`), no
son config-driven como las reglas de campo. Agregar un `rule_type` nuevo aquí
sí requiere código nuevo en `expediente.py`.

### Campos computados con mapeo de conceptos (patrón `LIQUIDACION_NORMALIZADA`)

Cuando un campo `type: "object"` con `"computed": true` se llama exactamente
`LIQUIDACION_NORMALIZADA`, `pipeline/liquidacion.py` lo llena
determinísticamente sumando/filtrando arrays ya extraídos (`CUADRO_LIQUIDACION.CONCEPTOS`,
`CONTRIBUCIONES`, `PARTIDAS.CONTRIBUCIONES_PARTIDA`), leyendo el mapeo
**directo de cada subcampo** en vez de un bloque `form_mapping` aparte:

```jsonc
"IVA": {
  "type": "number",
  "default": 0,                          // valor cuando no hay match (también sirve de default general del campo)
  "concept_aliases": ["IVA", "IVA PRV", "IVA/PRV"],   // suma CUADRO_LIQUIDACION.CONCEPTOS (o CONTRIBUCIONES si el primero vino vacío) cuyo CLAVE_CONCEPTO/CLAVE_CONTRIBUCION/DESCRIPCION matchee alguno de estos
  "hint": "Suma de IVA, IVA PRV e IVA/PRV.",
  "validation": []
},
"IEPS_8": {
  "type": "number",
  "default": 0,
  "source": "PARTIDAS.CONTRIBUCIONES_PARTIDA",        // en vez de concept_aliases: agrega desde otro array, con filtros
  "filters": {"CLAVE_CONTRIBUCION": "IEPS", "TASA": 8},
  "hint": "IEPS correspondiente a la tasa del 8%.",
  "validation": []
},
"TOTAL_CONTRIBUCIONES": {
  "type": "number",
  "required": true,
  "source": "CUADRO_LIQUIDACION.TOTAL",               // path directo a un escalar (no array): "DOCUMENT source"
  "hint": "TOTAL impreso en el cuadro de liquidación.",
  "validation": []
}
```

Llaves reconocidas por subcampo: `concept_aliases` (implica
`source: "CUADRO_LIQUIDACION.CONCEPTOS"` si no lo pones explícito),
`source` (`"CUADRO_LIQUIDACION.CONCEPTOS"` | `"CONTRIBUCIONES"` |
`"PARTIDAS.CONTRIBUCIONES_PARTIDA"` | `"ARRAY[CLAVE=VALOR].CAMPO"` | un path
escalar plano), `value_field` (default `"IMPORTE"`), `filters` (dict —
soporta `TASA` con tolerancia numérica y `TASA_NOT_IN` con una lista), y
`default`.

**Confianza de una columna calculada**: no es un número inventado ni un
`null`. Una columna que sumó celdas hereda el **promedio** de la confianza de
esas celdas (`pipeline/confidence.py::combine`); un concepto que simplemente
no está en el cuadro hereda la confianza del cuadro en el que se buscó
("se leyó bien y ahí no está" es un 0.00 medido); y si nadie midió la tabla
de la que sale (un `source` que el tipo ni declara), la columna no reporta
nada propio y cae a qué tan bien se leyó el documento. Registrar 0.0 ahí
borraría ceros legítimos — un pedimento sin partidas de IEPS tiene `IEPS_8 =
0` por buena razón, y el piso de descarte lo volvería `null`.

Esto es una **convención de nombre**, no un mecanismo genérico para
cualquier objeto computado: solo el campo literalmente llamado
`LIQUIDACION_NORMALIZADA` dispara esta lógica (`pipeline/liquidacion.py`
busca ese nombre exacto en `field_tree`). Si lo que necesitas es **elegir un
valor por condición** en vez de agregar un array, no escribas código: usa
`derived` (siguiente sección). Solo una lógica de **agregación distinta**
—otra forma de sumar/filtrar filas— sigue necesitando su propio módulo en
`pipeline/` enganchado en `pipeline/expediente.py::_extract_segment`, igual
que se hizo para este.

### Campos derivados de una condición (`derived`)

Algunos campos **no están impresos en el documento**: son una regla de negocio
sobre valores que sí se extrajeron. `Tipo_Pago` del ERP es el caso típico — el
pedimento nunca imprime "tipo de pago", se decide según el total del cuadro de
liquidación. Eso se declara en el catálogo, no en código:

```jsonc
"Tipo_Pago": {
  "type": "number",
  "required": true,
  "computed": true,                       // lo llena el pipeline -> fuera del prompt de Bedrock
  "derived": {
    "from": "CUADRO_LIQUIDACION.TOTAL",    // path punteado a otro campo ya extraído
    "cases": [                             // en orden: gana el PRIMERO que matchee
      { "when": { "gt": 383 },  "value": 1, "label": "Anticipo y Pasivo" },
      { "when": { "lte": 383 }, "value": 3, "label": "Solo Pasivo" }
    ],
    "default": null                        // opcional; si no lo pones, es null
  },
  "hint": "Calculado, no impreso: ...",
  "validation": []
}
```

Sale así, con el código que guarda el sistema destino y, al lado, qué significa:

```jsonc
"Tipo_Pago": { "value": 3, "label": "Solo Pasivo", "confidence": 0.93,
               "valid": true, "reason": null, "source": "DERIVED" }
```

Operadores admitidos dentro de un `when` (varios en el mismo `when` = **AND**):

| Operador | Ejemplo | Matchea cuando |
|---|---|---|
| `gt` `gte` `lt` `lte` | `{"gt": 383}` | Comparación numérica. `"$ 12,450.00"` se parsea igual que `12450`. |
| `eq` `ne` | `{"eq": "A1"}` | Igualdad numérica si ambos lados son números; si no, texto sin espacios ni mayúsculas. |
| `in` | `{"in": ["A1", "A3"]}` | El valor es uno de la lista (misma comparación que `eq`). |
| `matches` | `{"matches": "^A"}` | Regex (case-insensitive) sobre el valor. |
| `empty` / `not_empty` | `{"not_empty": true}` | El campo vino vacío / trae algo. |

Reglas del mecanismo (`pipeline/derived.py`):

- **`label` es opcional y NUNCA sustituye al `value`.** Está para los códigos
  que se guardan como número pero significan algo en palabras (`Tipo_Pago` del
  ERP: 1 = Anticipo y Pasivo, 2 = Solo Anticipo, 3 = Solo Pasivo): el campo
  conserva el código que el destino espera, y quien lea el resultado no
  necesita la tabla de códigos. Se declara por `case` y aparece como `label`
  junto al `value`; un campo sin etiqueta sale exactamente igual que antes.
  Si no matchea ningún `case` no hay etiqueta: ponerle una al `default` sería
  darle significado a un valor que nadie decidió.
- **`from` acepta paths anidados** (`CUADRO_LIQUIDACION.TOTAL`) y se evalúa
  DESPUÉS de `LIQUIDACION_NORMALIZADA`, así que también puedes apuntar a un
  concepto ya normalizado (`LIQUIDACION_NORMALIZADA.CL_Total`).
- **La confianza se hereda** del campo del que se derivó: un campo derivado es
  exactamente tan confiable como su insumo, nunca más.
- **El `source` de salida es `"DERIVED"`**, no `MODEL_EXTRACTION`: el resultado
  dice que el valor lo calculó el sistema.
- **Si el insumo no se pudo leer**, ningún `when` matchea y sale el `default`
  (null si no lo declaras). Con `required: true`, el check de presencia manda
  el documento a revisión — que es lo correcto: mejor vacío que un valor
  inventado a partir de un dato que nadie leyó.
- **No hay evaluación de expresiones**: el conjunto de operadores es cerrado y
  `value` sale literal. Un catálogo no puede ejecutar código. Un operador mal
  escrito **no matchea** (en vez de matchear siempre) y queda logueado.
- Sirve en los tres pipelines (PDF, XML y tabular), y un tipo que no declare
  ningún `derived` no paga nada: el paso no hace trabajo.
- **Un `derived` puede leer otro `derived`** (`TIPO_DOCUMENTO` desde
  `SIGLAS_DOCUMENTO`). Se resuelven **por dependencia, no por orden de
  declaración**: un catálogo sincronizado a DynamoDB vuelve como Map y un Map
  no conserva el orden de sus claves. Una dependencia circular no truena: se
  loguea y se resuelve en el orden declarado.

#### Leer el nombre del archivo (`$FILE_NAME`), `from` por caso y `keep_extracted`

Tres extensiones opcionales del mismo bloque; un `derived` que no use ninguna se
comporta exactamente igual que antes. El caso real es `SIGLAS_DOCUMENTO` de
`PEDIMENTO_GASTOS_ADUANALES_ERP`/`_XML`: quien sube nombra el archivo
`<pedimento>_<SIGLA><n>.<ext>` (`2607-0823-6001803_GT1.pdf`), y eso es un hecho
que ya declaró, no algo que haya que pagarle a un modelo por adivinar.

```jsonc
"SIGLAS_DOCUMENTO": {
  "type": "string",
  "required": true,
  "hint": "Sigla del tipo de documento, decidida por contenido: II = ..., GT = ...",
  "derived": {
    "from": "$FILE_NAME",                  // variable de contexto, no un campo
    "keep_extracted": true,                // sin match -> se queda lo que leyó el modelo
    "cases": [
      { "when": { "matches": "(?:^|[_\\-\\s])(?:II|FAA)\\d*\\.[A-Za-z0-9]+$" }, "value": "II" },
      { "when": { "matches": "(?:^|[_\\-\\s])GT\\d*\\.[A-Za-z0-9]+$" },        "value": "GT" },
      // un caso puede leer OTRO campo: gana sobre el `from` del bloque solo en ese caso
      { "from": "ISR", "when": { "gt": 0 }, "value": "II", "label": "Decidido por contenido: retención de ISR" }
    ]
  }
}
```

- **`$FILE_NAME`** es el nombre base del archivo subido. De la key de S3
  (`raw/{TIPO}/{uuid}/archivo.pdf`) solo se conserva el último segmento: ni el
  TIPO ni el uuid llegan al patrón. El conjunto de variables `$` es cerrado
  (`pipeline/derived.py::build_context`); una desconocida no vale nada y queda
  logueada. Un valor decidido desde una variable de contexto lleva confianza
  **1.0**: el nombre es lo que mandó quien subió, byte por byte. En un PDF con
  varios segmentos todos ven el mismo nombre — describe al ARCHIVO.
- **Ancla el patrón.** `matches` es un `re.search`: una sigla de una letra (`P`)
  buscada sin ancla matchea medio alfabeto. El patrón de arriba exige la sigla
  al final, tras `_`/`-`/espacio y justo antes de la extensión.
- **`from` por caso.** Permite "lo dice el nombre; si no, la retención de ISR;
  si no, el concepto" como UNA lista ordenada, en vez de una cadena de campos
  auxiliares que acabarían todos en la salida. La confianza se hereda del campo
  que leyó **el caso que decidió**.
- **`keep_extracted: true`** — cuando ningún caso matchea, se conserva lo que la
  extracción dejó en el campo en vez de pisarlo con `default`. Es lo que permite
  un campo determinista cuando la señal barata existe y leído por el modelo
  cuando no: el campo lleva `hint` y **no** lleva `computed` (si lo llevara
  saldría del prompt y no habría lectura que conservar). El valor conservado no
  se re-etiqueta — sigue siendo `MODEL_EXTRACTION` con la confianza del modelo —,
  así que en el resultado se distinguen solos: `source: "DERIVED"` = lo dijo el
  nombre, sin `source` = lo leyó el modelo. Si tampoco hubo lectura, sale el
  `default`. En un tipo XML/tabular no hay modelo: ahí el contenido se decide
  con casos `from` sobre campos ya extraídos y el campo sí es `computed`.

#### Copiar el valor de otro campo (`value_from`) y no repetirlo en una lista (`exclude_rows`)

Dos extensiones más, del mismo módulo y con la misma regla de fondo (nada de
expresiones, solo valores que ya existen):

```jsonc
"ID_FISCAL": {
  "type": "string", "hint": "El identificador fiscal en cualquier país...",
  "derived": {
    "from": "RFC",
    "keep_extracted": true,
    "cases": [
      { "when": { "matches": "^[A-ZÑ&]{3,4}\\d{6}[A-Z0-9]{3}$" }, "value_from": "RFC" }
    ]
  }
},
"OTROS_IDENTIFICADORES": {
  "type": "array", "hint": "Los demás identificadores...",
  "exclude_rows": { "column": "NUMERO", "equal_to": ["ID_FISCAL", "RFC", "CURP", "ID_CIF"] },
  "item_fields": { "TIPO": { "type": "string" }, "NUMERO": { "type": "string" } }
}
```

- **`value_from`** en un `case` copia el valor de ese campo en vez de emitir la
  constante `value`. Es UN path leído tal cual, y la confianza se hereda **del
  campo copiado**. Si ese campo está vacío el caso no decide: sigue el
  siguiente, o `keep_extracted`, o el `default`. Como `derived` corre después
  de la recuperación del QR, lo copiado es el valor ya recuperado: `ID_FISCAL`
  nunca puede contradecir al `RFC` del SAT.
- **`exclude_rows`** va en un campo `array` y quita las filas cuya `column`
  repite el valor de alguno de los campos de `equal_to`, comparando sin
  mayúsculas, espacios ni puntuación (`12345 6789 RT0001` = `123456789rt0001`).
  Existe porque una lista se extrae en su propia llamada al modelo, que no sabe
  qué quedó en los escalares: "todos menos el principal" no lo garantiza un
  hint (medido: 3 de 4 constancias extranjeras repetían el identificador
  principal en la lista). Corre al final, con los valores ya derivados; un
  bloque mal escrito o un campo de referencia vacío no quita nada.

Ejemplo real: `custom/portal/CONSTANCIA_SITUACION_FISCAL_PORTAL.json`.

**¿Código o IA?** El criterio para decidir dónde va una regla:

| La regla... | Va en | Por qué |
|---|---|---|
| se escribe como comparación/umbral sobre campos ya extraídos | `derived` | Determinista, $0 de Bedrock, auditable, y `computed: true` la saca del prompt. |
| es una suma/filtro sobre un array de conceptos | `LIQUIDACION_NORMALIZADA` (`concept_aliases`/`filters`) | Ya existe, sección anterior. |
| necesita **leer o interpretar** el documento (texto libre, criterio) | campo normal con `hint` | Lo llena el modelo. Es lo que ya pasa hoy; no hace falta mecanismo nuevo. |

En resumen: si puedes escribir la regla como una condición, no se la preguntes
al modelo — sale gratis, sale igual siempre, y queda documentada en el catálogo
donde la puede cambiar quien conoce el negocio.

## `pipeline: "expediente"`: qué controla realmente

Un tipo con `"pipeline": "expediente"` opta por el pipeline de expediente
(`pipeline/expediente.py`): texto nativo + Textract por página, segmentación,
y las reglas cross-documento de arriba. **Ya no es requisito para que un tipo
con `fields` se extraiga** — `handler.py` rutea al pipeline de expediente si
*cualquiera* de estas es cierta:

1. `pipeline == "expediente"`, o
2. el evento trae `expediente: true` explícito, o
3. **el tipo declara `fields`** (DSL field-centric) — el prompt de extracción
   generado por `src/document-catalog.ts` para el flujo estándar de Bedrock
   solo entiende `extraction_fields`/`extraction_hints` (el DSL viejo); nunca
   aprendió a leer el árbol recursivo nuevo, así que sin esta condición un
   tipo field-centric caería al flujo estándar y no extraería nada.

Y lo que **ya no** depende de él, aunque este documento lo afirmara antes:

- **Candidatura de auto-clasificación** — ahora la decide
  `is_segmentation_candidate` por la FORMA del tipo, no por este flag. Ver la
  sección siguiente. (Con este flag como gate, contra el catálogo real solo
  calificaban 2 de 9 tipos: un PDF sin declarar que traía un pedimento no podía
  clasificarse como `PEDIMENTO_ADUANAL`, porque el tipo nunca estuvo en la lista.)
- **Reglas cross-documento** — se declaran por REGLA, no por tipo:
  `get_cross_document_rules()` devuelve todas las de `_shared.json`, y cada una
  lista en su propio `documents: [...]` a qué tipos aplica (`expediente._docs_of`).

En corto: hoy `pipeline: "expediente"` casi no controla nada por sí solo —
`handler.py` ya rutea por `fields`. Ponerlo no hace daño y sigue siendo la
declaración de intención más clara, pero no es lo que mete a tu tipo en la
clasificación automática ni lo que activa las reglas cruzadas.

### Candidatura de auto-clasificación (`segmentation_candidate`)

Cuando subes un PDF **sin** declarar tipo (`raw/{uuid}/archivo`), el segmentador
tiene que ofrecerle una lista de candidatos al modelo. Un tipo entra en esa lista
por su forma, nunca por su TYPE_ID (`document_catalog.is_segmentation_candidate`):

1. declara `fields` (sin árbol field-centric no hay nada que extraer de un rango
   de páginas);
2. **no** es de fuente estructurada — ningún campo declara `xpath`/`item_xpath`/
   `cell`/`column`. Esto deja fuera a los tipos `_XML`/`_XLSX`, que se rutean por
   la extensión del archivo y cuyos xpaths no significan nada sobre un escaneo;
3. tiene `description` — es el discriminador que el prompt le imprime al modelo,
   así que una descripción que solo explica el ESQUEMA (y no cómo se reconoce el
   documento) deja al clasificador sin nada con qué decidir;
4. no se sacó a mano con `"segmentation_candidate": false`.

Ese último es el escape hatch para un tipo que el clasificador confunde con un
hermano, sin tocar código y hot-reloadable con `pnpm sync:one`. **`PEDIMENTO_ERP`
lo usa**: es el MISMO documento físico que `PEDIMENTO_ADUANAL` (mismos
`page_selection.markers`), solo que con los nombres de campo del ERP en la salida.
Un clasificador que lee texto no tiene con qué elegir entre los dos, y elegir mal
cambia el shape del resultado — esa variante se pide declarándola.

**Declarar el tipo gana siempre.** Si el caller manda `documentType`, ese tipo
entra a la lista de candidatos aunque no califique por forma y aunque se haya
sacado con `segmentation_candidate: false`: nombrarlo explícitamente es una señal
más fuerte que cualquier default del catálogo.

## Custom con scripts

Dos mecanismos de extensión, **ambos solo corren en el flujo estándar de un
solo documento** (`handler.py::_process`, después de que la rama de
expediente devuelve `None` por falta de puntero S3) — **no se ejecutan para
tipos field-centric/expediente** como `PEDIMENTO_ADUANAL` hoy. Para lógica
custom en esos tipos, usa `cross_field_validation`, las reglas cross-documento
de `_shared.json`, o extiende `validators/engine.py` directamente (aplica a
ambos flujos).

### `validator_module` (Python)

Para lógica de validación en Python más allá de lo config-driven. Declara
`"validator_module": "validators.mi_modulo"` en el tipo; el módulo debe
exportar:

```python
def validate_document(
    entity_map: Dict[str, str], rules: List[ValidationRule], required_fields: List[str]
) -> List[CheckResult]:
    ...
```

Ver `validators/identification.py` o `validators/fiscal.py` como ejemplo real
(típicamente delegan en `validators/base.py::validate`/`validate_required_fields`
y le agregan lógica propia encima). Un `validator_module` inválido o que
truena al importarse no rompe el documento — se registra como un check
fallido `_module`/`module_load` y el resto del flujo continúa.

### `custom_validator` (Node.js)

Para lógica que conviene en JS/necesita llamar una API externa. Declara
`"custom_validator": "mi_script"` (sin `.mjs`) en el tipo y crea
`custom_validators/mi_script.mjs` exportando:

```javascript
export async function validate(entityMap, context) {
  // entityMap: { "RFC": "...", "CURP": "...", ... } (flat, valores planos)
  // context: { documentId, classification }
  return [
    { field: "RFC", rule: "custom:sat_lookup", valid: true, message: "..." },
  ];
}
```

Corre como subproceso Node con timeout de 10s; un error o timeout se registra
como check fallido, no tumba el documento. Ver
`custom_validators/README.md` y `custom_validators/example_curp_checkdigit.mjs`
para el detalle completo y otro ejemplo.

### Nueva regla config-driven para todos los tipos

Si lo que necesitas es un `rule_type` nuevo que se pueda declarar desde JSON
en cualquier tipo (no un script aparte), extiende `validators/engine.py`:
agrega una rama en `_dispatch_rule` + una función `_check_<nombre>`. Se vuelve
utilizable desde `fields[].validation` o `cross_field_validation` en
cualquier catálogo sin tocar nada más.

## Consideraciones y gotchas

- **`schema_version` es puramente informativo.** No hay ningún
  `if schema_version == "4.x"` en el código — la detección de qué DSL usa un
  tipo es por la **presencia de la llave `"fields"`**, punto. Puedes subir el
  número cuando quieras dejar rastro de un cambio, pero no esperes que
  cambie ningún comportamiento por sí solo.
- **Solo `save/` significa inactivo.** `pnpm build`/`pnpm sync` recorren todas
  las demás subcarpetas. Un tipo movido a cualquier directorio llamado
  `save` deja de entrar al bundle; el sync completo reporta si quedó una fila
  habilitada en DynamoDB y `--prune` la deshabilita explícitamente.
- **DynamoDB gana siempre sobre lo empaquetado.** Si un tipo tiene fila en
  DynamoDB (por un `sync` anterior), lo que digas en `catalogs/<TIPO>.json`
  localmente no tiene efecto en producción hasta que vuelvas a sincronizar —
  y `pnpm build` solo regenera el bundle local, no toca DynamoDB.
- **`pnpm preview-prompts` no sirve para revisar tipos field-centric.** El
  generador de prompts en TypeScript (`src/document-catalog.ts`) no entiende
  `fields` — para un tipo con ese DSL vas a ver una entrada sin campos de
  extracción listados, aunque el tipo funcione perfecto (la extracción real
  ocurre del lado Python, no en ese prompt).
- **`validator_module`/`custom_validator` no corren para tipos field-centric**
  (ver sección anterior) — no asumas que agregar uno a `PEDIMENTO_ADUANAL.json`
  va a tener efecto sin antes verificar que el tipo no se está enrutando por
  el pipeline de expediente.
- **Una regla con `rule_type` no soportado queda `skipped` en silencio**
  (severity warning) — no falla el documento ni te avisa fuerte. Revisa
  `checks`/`crossDocumentChecks` del resultado si una validación "no hace
  nada".
- **Un `type` mal declarado se ve en el valor, no en un error.** Si una
  columna numérica sale entrecomillada en el resultado, o un texto sale como
  número gigante, revisa el `type` del campo antes que el hint: es lo que
  gobierna la normalización (ver la sección del shape de un campo).
- **`field_sets` es del DSL viejo únicamente** — en un tipo con `fields` no
  tiene ningún efecto, aunque `_TEMPLATE.json` todavía lo liste (es un
  remanente, seguro dejarlo en `[]` o quitarlo). Lo que sí funciona en el DSL
  nuevo es `$presets` (ver «Presets de campos»).
- **Un preset editado no llega con `pnpm sync:one`.** `_field_presets` viaja
  en el bundle, así que cambiar un campo DENTRO de `_shared.json` requiere
  `pnpm build` + deploy; lo hot-reloadable es el override que hace el tipo
  redeclarando ese campo. Y un tipo que pide un preset que no existe sigue
  funcionando sin él (warning en el log, no error): si «no aparecen los campos
  de integridad», revisa el nombre.
- Después de cualquier edición: `pnpm sync:one -- --type TU_TIPO` para
  probar en caliente, y `pnpm run build && pnpm run deploy` cuando quieras
  que quede como línea base permanente (sobrevive a que DynamoDB se vacíe o
  a un tipo nunca sincronizado).

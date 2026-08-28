# `catalogs/` — Base única de esquemas

Esta carpeta es la **única fuente de verdad** de los tipos de documento. Un
archivo JSON por tipo, más un archivo compartido. No hay otro lugar donde se
definan tipos — no se toca `handler.py` para agregar/editar un tipo.

```
catalogs/
├── _shared.json      → globals: _field_sets, _common_item_fields, cross_document_validation_rules, _global_output_fields
├── _TEMPLATE.json     → plantilla para crear un tipo nuevo (se ignora en build/sync)
├── PEDIMENTO_ADUANAL.json
└── <TYPE_ID>.json     → un tipo por archivo, shape { "<TYPE_ID>": { ...entry } }
```

- El **nombre del archivo** es el `TYPE_ID` (UPPER_SNAKE_CASE): `PEDIMENTO_ADUANAL.json`.
- Los archivos que empiezan con `_` son especiales (no son tipos).
- **Solo se leen los archivos directos de esta carpeta.** `pnpm build`/`pnpm sync`
  no bajan a subcarpetas — un tipo movido a `catalogs/algo/` desaparece del
  catálogo empaquetado y de DynamoDB sin ningún error visible. Si necesitas
  "guardar" un tipo sin borrarlo, no lo dejes dentro de `catalogs/`.

## Los 2 únicos comandos

```bash
pnpm sync                              # sube TODA la carpeta catalogs/ a DynamoDB (hot-reload)
pnpm sync:one -- PEDIMENTO              # sube UN tipo (por nombre difuso)
pnpm sync:one -- --type INE             # por TYPE_ID exacto
pnpm sync:one -- --file catalogs/X.json # por archivo
```

Opciones: `--dry-run` (no escribe), `--table`, `--region`.

`sync`/`sync:one` escriben a **DynamoDB** → efecto **inmediato, sin redeploy**
(siempre que el stack tenga `enableDynamoCatalog: true`, que es el caso hoy —
ver `bin/app.ts`). Para hornear los cambios en la Lambda (línea base
permanente, la que se usa si DynamoDB llegara a fallar o vaciarse) usa el
build normal:

```bash
pnpm run build && pnpm run deploy
```

`pnpm run build` combina automáticamente `catalogs/*.json` en el catálogo
empaquetado (`src/document-processing/resources/enrichment-lambda/config/catalogs.json`,
generado — nunca se edita a mano, está en `.gitignore`). **DynamoDB siempre
gana sobre lo empaquetado en runtime** — si un tipo tiene fila en DynamoDB,
lo que digan `catalogs/*.json` localmente no importa hasta que sincronices de
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
   cp catalogs/_TEMPLATE.json catalogs/ACTA_NACIMIENTO.json               # PDF/escaneo
   cp catalogs/_TEMPLATE_XML.json catalogs/ACTA_NACIMIENTO_XML.json       # XML ya estructurado
   cp catalogs/_TEMPLATE_TABULAR.json catalogs/REPORTE_GASTOS_XLSX.json   # XLSX/CSV ya estructurado
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
| `description` | sí | Va al prompt de clasificación (Bedrock). Sé discriminante. |
| `fields` | sí | El árbol de campos — ver abajo. Su sola presencia es lo que activa el DSL field-centric (`document_catalog.py: if "fields" in data`). |
| `display_name` | sí | Nombre legible (`"Pedimento Aduanal"`). Se guarda en `DocumentTypeName` y el API lo acepta en `?docType=`. Si falta, se deriva del `TYPE_ID`. |
| `aliases` | sí | Otros nombres que el API acepta en `?docType=` (`["pedimento", "pedimento de importación"]`). |
| `min_confidence` | sí | Umbral 0-1: un campo escalar con confianza por debajo se marca inválido (`confidence {x} below minimum {min}`). Default 0.70. |
| `schema_version` | solo informativo | Se registra en `tracking.schema.typeVersions` de cada resultado para saber qué versión de schema procesó el documento. **No cambia ningún comportamiento del código** — no hay ningún `if schema_version == ...` en el pipeline. Súbelo cuando quieras dejar rastro de un cambio relevante, no por obligación. |
| `pipeline: "expediente"` | sí, pero opcional | Ver [`pipeline: expediente`](#pipeline-expediente-qué-controla-realmente) abajo — **ya no es requisito para que se extraigan los campos.** |
| `model_id` | sí, opcional | ID de modelo Bedrock (o ARN de inference-profile) que extrae específicamente ESTE tipo — pisa `BEDROCK_EXTRACTOR_MODEL_ID`/`BEDROCK_MODEL_ID` configurados por variable de entorno. Aplica a cualquier fase que corra para este tipo (`extractor`, y `recovery` si el tipo declara `extraction_strategy`) salvo que esa fase tenga su propia entrada en `model_ids`. Ausente o `null` → hereda lo configurado. |
| `model_ids` | sí, opcional | Igual que `model_id` pero por FASE: `{"extractor": "...", "recovery": "..."}`. Pisa a `model_id` cuando ambos nombran la misma fase — así un tipo puede fijar solo `recovery` (p. ej. porque su fallback de Textract necesita un modelo distinto al de extracción normal) sin tocar las demás. `segmenter` no aplica aquí: la segmentación de un expediente corre ANTES de saber a qué tipo pertenece cada rango de páginas, así que no hay un `type_entry` del que leer el override — solo responde a `BEDROCK_SEGMENTER_MODEL_ID` y al override de desarrollo. |
| ↳ jerarquía completa | — | De mayor a menor prioridad: **(1)** override de desarrollo por request (`ALLOW_MODEL_OVERRIDE`, dev-only, ver `document-validation-stack.ts` — fuerza TODA la invocación sin importar tipo ni fase) → **(2)** `model_ids[fase]` del catálogo → **(3)** `model_id` del catálogo → **(4)** `BEDROCK_<FASE>_MODEL_ID` de entorno → **(5)** `BEDROCK_MODEL_ID` de entorno / fallback fijo. La utilidad que implementa y documenta este orden es `pipeline/bedrock.py::resolve_model` (`_model_candidates` es la única fuente de verdad del orden; `explain_model_resolution` expone los 5 candidatos y cuál ganó, útil para depurar "por qué este documento usó el modelo X"). Esto decide solo **qué modelo se intenta primero**; si ese modelo falla por indisponibilidad (no habilitado, no existe, error/timeout del proveedor), `pipeline/bedrock.py::_invoke_with_fallback` cambia al siguiente modelo de una lista ORDENADA E INDEPENDIENTE (`pipeline/model_fallback.py`, tabla DynamoDB `FALLBACK_MODELS_TABLE` opcional) — ver `documentacion/GUIA_DESPLIEGUE_Y_USO.md` §5.6. Los dos mecanismos no se mezclan: el catálogo nunca es candidato de fallback, y la tabla de fallback nunca decide qué se intenta primero. |
| `validator_module` | sí, **solo en el flujo estándar** | Ver [Custom con scripts](#custom-con-scripts). |
| `custom_validator` | sí, **solo en el flujo estándar** | Ídem. |
| `business_destination` | sí | Metadato de ruteo de negocio; aparece como `businessDestination` en cada documento del expediente. Puramente informativo para el consumidor. |
| `cross_field_validation` | sí | Reglas que cruzan **varios campos del mismo documento** (fórmulas, sumas de arrays) — no pueden vivir dentro de un solo campo. Mismo shape de regla que dentro de `fields[].validation`, pero con `"field"` explícito. |
| `qr_recovery` | sí | Recuperación determinística de campos desde un QR embebido. Declara cómo localizar la página, filtrar la URL, mapear parámetros y publicar el payload/estado; ver la sección siguiente. |
| `page_selection` | sí | Selección de páginas de interés: manda a Bedrock **solo** las páginas cuyo texto cumple los marcadores del tipo, en vez del documento completo. Opt-in por tipo; ver la sección siguiente. |
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
  "computed": true
}
```

- **Escalar** (`string`/`number`/`boolean`/`date`) → sale como
  `{value, confidence, valid, reason, source}`.
- **Objeto** (`type: "object"`) → sale como
  `{valid, reason, fields: {SUBCAMPO: {...recursivo...}}}`. Su propio
  `validation` corre a nivel del objeto completo (ej. una suma que cruza dos
  subcampos — usa `"subfield": "TOTAL"` en la regla para apuntar a uno
  específico, ver `CUADRO_LIQUIDACION` en `PEDIMENTO_ADUANAL.json`).
- **Array** (`type: "array"`) → sale como `{items: [ {...recursivo...} ]}`,
  una entrada por fila detectada.
- **`$common`** en `item_fields` inyecta subcampos definidos una sola vez en
  `_shared.json` → `_common_item_fields` (hoy: `PAGINA`, `ORDEN`, `CONFIANZA`,
  `VALOR_CRUDO`) — evita repetir el mismo hint/validation en cada grupo
  repetible del catálogo entero.
- **`computed: true`** marca un campo que llena el pipeline
  **determinísticamente después de la extracción**, no el modelo — se excluye
  del prompt/tool-schema de Bedrock (pedirle al modelo que adivine un valor
  que se va a descartar desperdicia tokens de salida) y su resultado sale
  siempre con `source: "NORMALIZED"`. El único consumidor de esta convención
  hoy es `LIQUIDACION_NORMALIZADA` (ver próxima sección) — si marcas otro
  campo como `computed` sin que algo en `pipeline/` lo llene explícitamente,
  simplemente sale vacío/null, nadie lo calcula solo.

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
| `boolean_true` | `message` | El valor es afirmativo (`si`, `sí`, `yes`, `true`, `1`, `presente`). |
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

Esto es una **convención de nombre**, no un mecanismo genérico para
cualquier objeto computado: solo el campo literalmente llamado
`LIQUIDACION_NORMALIZADA` dispara esta lógica (`pipeline/liquidacion.py`
busca ese nombre exacto en `field_tree`). Si necesitas otro objeto calculado
con una lógica de agregación distinta, hay que escribir su propio módulo en
`pipeline/` y engancharlo en `pipeline/expediente.py::_extract_segment`,
igual que se hizo para este.

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

Lo que **sí** sigue dependiendo exclusivamente de `pipeline: "expediente"`:

- **Candidatura de auto-segmentación**: cuando subes un PDF *sin* declarar
  tipo (`raw/{uuid}/archivo`, no `raw/{TIPO}/{uuid}/archivo`), el segmentador
  solo considera como candidatos los tipos con `pipeline: "expediente"`
  (`segmenter._candidate_types`). Un tipo field-centric sin este flag nunca
  será reconocido dentro de un expediente multi-documento sin declarar —
  solo funciona si el uploader declara el tipo explícitamente en la key de S3.
- **Reglas cross-documento**: solo corren para documentos procesados por el
  pipeline de expediente (necesitan "hermanos" — otros documentos del mismo
  expediente — para comparar).

En corto: pon `pipeline: "expediente"` si tu tipo puede aparecer sin
declararse dentro de un PDF multi-documento, o si necesita reglas
cross-documento. Si siempre se sube con tipo declarado y no cruza datos con
otros documentos, puedes omitirlo — la extracción funciona igual.

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
- **`catalogs/save/` (o cualquier subcarpeta) no se incluye en el build.**
  `pnpm build`/`pnpm sync` solo leen archivos `.json` directos de `catalogs/`.
  Un tipo movido a una subcarpeta desaparece silenciosamente del catálogo —
  sin error, sin warning.
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
- **`field_sets` es del DSL viejo únicamente** — en un tipo con `fields` no
  tiene ningún efecto, aunque `_TEMPLATE.json` todavía lo liste (es un
  remanente, seguro dejarlo en `[]` o quitarlo).
- Después de cualquier edición: `pnpm sync:one -- --type TU_TIPO` para
  probar en caliente, y `pnpm run build && pnpm run deploy` cuando quieras
  que quede como línea base permanente (sobrevive a que DynamoDB se vacíe o
  a un tipo nunca sincronizado).

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

1. **Copia la plantilla**:
   ```bash
   cp catalogs/_TEMPLATE.json catalogs/ACTA_NACIMIENTO.json
   ```
2. **Edita** el archivo: renombra la llave `NUEVO_TIPO` → `ACTA_NACIMIENTO`
   (debe coincidir con el nombre del archivo), ajusta `description` (es lo
   que ve el clasificador — sé discriminante: menciona encabezados, folios,
   sellos, lo que lo distingue de documentos parecidos), y define `fields`.
   Borra las llaves `__NOTAS__`/`__COMENTARIO_*__` (son solo documentación
   del template, el loader las ignora igual, pero no aportan nada en un tipo
   real).
3. **Aplícalo**:
   ```bash
   pnpm sync:one -- --type ACTA_NACIMIENTO     # inmediato (DynamoDB), o
   pnpm run build && pnpm run deploy           # permanente (empaquetado)
   ```

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
| `validator_module` | sí, **solo en el flujo estándar** | Ver [Custom con scripts](#custom-con-scripts). |
| `custom_validator` | sí, **solo en el flujo estándar** | Ídem. |
| `business_destination` | sí | Metadato de ruteo de negocio; aparece como `businessDestination` en cada documento del expediente. Puramente informativo para el consumidor. |
| `cross_field_validation` | sí | Reglas que cruzan **varios campos del mismo documento** (fórmulas, sumas de arrays) — no pueden vivir dentro de un solo campo. Mismo shape de regla que dentro de `fields[].validation`, pero con `"field"` explícito. |
| `qr_recovery` | sí | Recuperación determinística de campos desde un QR embebido. Declara cómo localizar la página, filtrar la URL, mapear parámetros y publicar el payload/estado; ver la sección siguiente. |
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

### Recuperación determinística desde QR (`qr_recovery`)

Un tipo puede declarar un bloque `qr_recovery` para localizar y decodificar
el QR del PDF sin pedirle al modelo que lo interprete. El mapeo es por
parámetros de URL y no está acoplado a un `TYPE_ID`:

```jsonc
"qr_recovery": {
  "confidence": 0.99,
  "decoded_text_field": "QR_CONSTANCIA",
  "validity_field": "CONSTANCIA_QR_VALIDO",
  "required_fields": ["ID_CIF", "RFC"],
  "page_marker_min_matches": 2,
  "short_segment_max_pages": 8,
  "page_markers": ["C[ÉE]DULA DE IDENTIFICACI[ÓO]N FISCAL"],
  "url_contains": "siat.sat.gob.mx/app/qr/faces/pages/mobile/validadorqr.jsf",
  "fields": {
    "ID_CIF": {
      "param": "d3",
      "split": "_",
      "part": 0,
      "pattern": "^\\d+$"
    },
    "RFC": {
      "param": "d3",
      "split": "_",
      "part": -1,
      "pattern": "^[A-ZÑ&]{3,4}\\d{6}[A-Z0-9]{3}$"
    }
  }
}
```

- `page_markers` y `page_marker_min_matches` eligen la página por texto;
  para segmentos cortos también se intenta primero su página inicial.
- `url_contains` evita aceptar otro QR presente en la misma página.
- Cada entrada de `fields` admite `param`, `slice` (`":2"`, `"-2:"`),
  `digits`, `split` + `part` (índices negativos permitidos) y `pattern`.
- `decoded_text_field` recibe el payload/URL completo. Debe existir en
  `fields` con `computed: true`.
- `validity_field` recibe `true` solo cuando están todos los
  `required_fields`; también debe ser un campo computado y normalmente lleva
  una regla `boolean_true` para que un QR faltante o incompleto invalide el
  documento.
- Los campos recuperados desde el QR reemplazan la extracción del modelo y
  quedan auditados en `qrRecovery` y `recoveredFields`.
- `QR_RECOVERY_ENABLED=false` desactiva globalmente este mecanismo.

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

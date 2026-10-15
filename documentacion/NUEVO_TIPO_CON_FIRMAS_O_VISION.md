# Nuevo tipo de documento con firmas, sellos o imágenes

Dos recetas para dar de alta un tipo que necesita que **alguien mire la hoja**,
no solo que lea su texto. Ninguna lleva código: son catálogo.

| | Receta A — **comparar contra lo registrado** | Receta B — **solo visión** |
|---|---|---|
| Pregunta que responde | ¿La firma / sello / logo / imagen es **la de esta entidad**? | ¿La firma y el sello **están**, y se ven genuinos? |
| Necesita referencias registradas | Sí, por entidad (proveedor, emisor, firmante…) | No |
| Necesita saber de quién es el documento | Sí: metadata de la carga o un campo del propio documento | No |
| Llamadas de visión por documento | 2 (`locate` + `inspect`) | 2; 1 si es digital; 3 con `page_review` |
| Embeddings | Solo si una entidad tiene más de 3 referencias de un elemento, o en la búsqueda sin clave | Nunca |
| Trabajo operativo continuo | Mantener las referencias de cada entidad | Ninguno |
| Ejemplo real | `CARTA_NOM35` | — |

Las dos comparten lo que no cambia: el modelo de visión **nunca rechaza solo**
(todo lo que sale de él va con `on_fail: "review"`), vacío no es falso, y lo
que le pasa al documento lo decide el catálogo. Contexto completo:
[`AUTENTICIDAD_Y_REFERENCIAS.md`](AUTENTICIDAD_Y_REFERENCIAS.md); spec del
bloque: [`catalogs/README.md`](../catalogs/README.md) → «Autenticidad e
integridad».

> **Antes de la primera vez en un ambiente:** los elementos compartidos
> (`FIRMA`, `SELLO`, `LOGO`), los presets `*_CONTRA_REFERENCIA`, la API
> `/references` y el índice vectorial viajan con el **deploy**. En una Lambda
> desplegada antes de ellos, un tipo que los use pierde sus elementos sin
> avisar. `pnpm run deploy:dev` una vez; de ahí en adelante un tipo nuevo es
> solo `pnpm sync:one`.

---

## Receta A — Comparar firmas, logos u otra imagen contra referencias

Ejemplo: un contrato de proveedor donde la firma y el logotipo deben ser los
registrados de ese proveedor, y además lleva un holograma propio.

### A1. El catálogo

`catalogs/base/<dominio>/CONTRATO_PROVEEDOR.json` (copia `_TEMPLATE.json` y
agrega esto):

```jsonc
"authenticity": {
  "expected_capture": "ANY",               // FISICO si debe ser papel escaneado (entonces agrega el preset CAPTURA_FISICA)
  "elements": {
    "FIRMA": {},                           // definición compartida, tal cual
    "LOGO": {},
    "HOLOGRAMA": {                         // una imagen que no existe en _shared.json: se describe aquí
      "description": "Holograma de seguridad circular, plateado, en la esquina inferior derecha.",
      "fingerprint": false,                // no es un trazo de tinta: sin huella
      "embedded_image_is_insertion": false // en un PDF digital, que sea una imagen incrustada NO es sospechoso
    }
  },
  "vision": { "inspect_when_digital": "with_references" },   // compara también en PDFs digitales
  "references": {
    "scope": "PROVEEDOR",                  // de QUIÉN son las referencias; mismo scope = mismas referencias que CARTA_NOM35
    "key_from": "fields.RFC_PROVEEDOR"     // la clave sale del propio documento…
    // "key_from": "metadata.proveedor"    // …o de lo que manda quien sube
  }
},
"fields": {
  "$presets": ["FIRMA_CONTRA_REFERENCIA", "LOGO_CONTRA_REFERENCIA"],
  "RFC_PROVEEDOR": { "type": "string", "required": true, "hint": "RFC del proveedor…", "validation": [] },

  // El elemento propio no tiene preset: sus campos se declaran a mano.
  "HOLOGRAMA_PRESENTE": {
    "type": "boolean", "required": false, "computed": true,
    "authenticity_source": "element.HOLOGRAMA.present", "hint": "Se ve el holograma.",
    "validation": [{ "rule_type": "boolean_true", "allow_empty": true, "on_fail": "review",
                     "message": "No se detectó el holograma de seguridad" }]
  },
  "HOLOGRAMA_COINCIDE_REFERENCIA": {
    "type": "boolean", "required": false, "computed": true,
    "authenticity_source": "element.HOLOGRAMA.matches_reference", "hint": "Corresponde al registrado.",
    "validation": [{ "rule_type": "boolean_true", "allow_empty": true, "on_fail": "review",
                     "message": "El holograma no corresponde al registrado del proveedor" }]
  }
}
```

Lo que decide cada línea:

- **`scope`** — reusar `PROVEEDOR` hace que este tipo compare contra las mismas
  referencias que ya registraste para la NOM-035 (el logo se registra una sola
  vez). Un `scope` nuevo (`FIRMANTE`, `MARCA`) es un universo aparte.
- **`key_from`** — `fields.<CAMPO>` lee la clave del documento (no depende de
  quien sube, pero depende de que la extracción la lea bien); `metadata.<clave>`
  la afirma quien sube. La clave solo admite letras, dígitos, `.`, `-`, `_`.
- **Un elemento con otra entidad** — si la firma es de una persona y el logo de
  una empresa: `"FIRMA": { "references": { "scope": "FIRMANTE", "key_from": "fields.CURP_FIRMANTE" } }`.
- **Subir a rechazo** — redeclara el campo del preset en el tipo con
  `"on_fail": "reject"`. Solo para señales deterministas; lo de visión se queda
  en `review`.

Este catálogo, tal cual, resuelve a las capas `structure + pixels + vision`,
tres elementos y el scope `PROVEEDOR` con `FIRMA`, `LOGO` y `HOLOGRAMA`
(verificado con `authenticity.spec_for`). No pide memoria entre documentos: se
agrega declarando campos `memory.*` y el bloque `memory`, como `CARTA_NOM35`.

### A2. Medir gratis antes de publicar

```bash
pnpm autenticidad -- --type CONTRATO_PROVEEDOR --dir <carpeta con contratos reales>
```

Sin AWS ni costo: dice cuántos son captura de papel y cuántos digitales. Es lo
que define `expected_capture` y si conviene `inspect_when_digital`.

### A3. Publicar

```bash
pnpm sync:one -- --type CONTRATO_PROVEEDOR --dry-run
pnpm sync:one -- --type CONTRATO_PROVEEDOR
```

Sin deploy (salvo la primera vez en el ambiente, ver la nota de arriba).

### A4. Registrar las referencias de cada entidad

De un original **en papel verificado**, un elemento por archivo
(`AUTENTICIDAD_Y_REFERENCIAS.md` §3.1–3.3 dice cómo recortar y qué evitar).

```bash
# CLI — un proveedor, o una carpeta con una subcarpeta por proveedor
pnpm refs:register -- --scope PROVEEDOR --key ABC010203XY1 --dir referencias/ABC010203XY1 --dry-run
pnpm refs:register -- --scope PROVEEDOR --key ABC010203XY1 --dir referencias/ABC010203XY1
pnpm refs:register -- --scope PROVEEDOR --root referencias/ --dry-run

# API — desde otro sistema (la app necesita: pnpm apps:grant -- --name <app> --env dev --reference-write)
curl -X POST "$API/references/PROVEEDOR/ABC010203XY1" -H "x-api-key: $KEY" -H "Content-Type: application/json" \
  -d '{"kind": "HOLOGRAMA", "label": "Holograma 2026"}'
curl -X PUT --data-binary @holograma.png "$UPLOAD_URL"
curl -X POST "$API/references/PROVEEDOR/ABC010203XY1/HOLOGRAMA/holograma-2026/confirm" -H "x-api-key: $KEY"
```

Los archivos se clasifican por subcarpeta (`firma/`, `logo/`, `holograma/`) o
por prefijo del nombre. `HOLOGRAMA` es válido para registrar **en cuanto el
tipo está en el catálogo**: los elementos aceptados salen de ahí, no del script.

### A5. Probar

```bash
# Local, con las referencias de dev en solo lectura (~$0.015)
pnpm procesar contrato.pdf CONTRATO_PROVEEDOR --refs-env dev

# Contra el stack
pnpm enviar contrato.pdf CONTRATO_PROVEEDOR
pnpm resultado -- --id <documentId> --wait
```

Con `key_from: "metadata.proveedor"` agrega `--meta proveedor=<clave>` a ambos.

Qué mirar en el resultado:

| Sale | Significa |
|---|---|
| `FIRMA_COINCIDE_REFERENCIA: true` | El modelo la reconoció como una de las registradas |
| `…: false` + `FIRMA_NO_COINCIDE_CON_REFERENCIAS` → revisión | Se comparó y no corresponde a ninguna |
| Campo **ausente** + `SIN_REFERENCIAS_REGISTRADAS` | Esa entidad no tiene nada registrado. No se rechaza por eso |
| Campo ausente + `SIN_CLAVE_DE_REFERENCIAS` | No llegó la clave (el campo no se extrajo, o faltó la metadata) |
| Campo ausente + `REFERENCIAS_NO_DISPONIBLES` → revisión | Existen pero no se pudieron leer. Reprocesar |
| `authenticity.references.ranking.<ELEMENTO>.mode` (resultado completo en S3) | Cómo se eligieron las referencias mostradas: `TODAS`, `EMBEDDING`, `HUELLA`, `ORDEN` |
| `bboxSource: "MODELO"` en un elemento | El modelo lo vio pero no hubo recorte confiable: no se examinó ni comparó |

### A6. Opcional: sin clave, que el sistema sugiera de quién es

`"search": { "enabled": true, "min_similarity": 0.7, "top_k": 3 }` dentro de
`references` (o dentro de un elemento). **Mide primero** con tus recortes:

```bash
pnpm refs:eval -- --dir <carpeta con una subcarpeta por entidad> --kinds FIRMA,LOGO,HOLOGRAMA
```

Con los de la NOM-035 rindió en sellos y logos (recall@3 0.93 / 0.96) y no en
firmas (0.80): actívala por elemento, no para todo.

---

## Receta B — Solo visión: que estén y se vean genuinos

Ejemplo: un acta de entrega que debe venir firmada y sellada, sin importar de
quién, y de la que quieres saber si la hoja muestra señales de edición.

### B1. El catálogo

```jsonc
"authenticity": {
  "elements": ["FIRMA", "SELLO"],          // sin bloque `references`: nada que comparar, nada que registrar
  "vision": { "page_review": true }        // opcional: una llamada más buscando indicios de edición en la página
},
"fields": {
  "$presets": ["CAPTURA_FISICA"],          // gratis: ¿es papel escaneado o un Word con imágenes pegadas?

  "FIRMA_PRESENTE": {
    "type": "boolean", "required": false, "computed": true,
    "authenticity_source": "element.FIRMA.present", "hint": "Se ve una firma autógrafa.",
    "validation": [{ "rule_type": "boolean_true", "allow_empty": true, "on_fail": "review",
                     "message": "No se detectó firma en el acta" }]
  },
  "FIRMA_SIN_INSERCION": {
    "type": "boolean", "required": false, "computed": true,
    "authenticity_source": "element.FIRMA.not_pasted", "hint": "Falso si la firma es una imagen pegada.",
    "validation": [{ "rule_type": "boolean_true", "allow_empty": true, "on_fail": "review",
                     "message": "La firma es una imagen insertada o muestra indicios de serlo" }]
  },
  "SELLO_PRESENTE": {
    "type": "boolean", "required": false, "computed": true,
    "authenticity_source": "element.SELLO.present", "hint": "Se ve un sello.",
    "validation": [{ "rule_type": "boolean_true", "allow_empty": true, "on_fail": "review",
                     "message": "No se detectó sello en el acta" }]
  },
  "SIN_INDICIOS_DE_EDICION": {
    "type": "boolean", "required": false, "computed": true,
    "authenticity_source": "vision.no_edit_signs", "hint": "Falso si la página muestra señales de edición.",
    "validation": [{ "rule_type": "boolean_true", "allow_empty": true, "on_fail": "review",
                     "message": "La página muestra indicios de edición" }]
  }
}
```

Atajo: los presets `FIRMA_CONTRA_REFERENCIA` / `SELLO_CONTRA_REFERENCIA`
también sirven aquí. Sin bloque `references`, su campo `*_COINCIDE_REFERENCIA`
simplemente nunca se llena (y con `allow_empty` no cuenta); te ahorras escribir
los de presencia e inserción.

Variantes:

- **Que el sello nombre al emisor** — `"SELLO": { "issuer_field": "EMPRESA_EMISORA" }`
  y un campo con `element.SELLO.issuer_consistent`. La comparación se hace en
  código entre dos textos; el modelo solo transcribe la leyenda.
- **Pagar la visión solo si algo huele mal** — `"vision": { "trigger": "on_suspicion" }`:
  un archivo que las capas gratis ven limpio no llama al modelo.
- **Solo integridad del archivo, sin visión** — `"$presets": ["INTEGRIDAD_PDF"]`
  y ningún elemento: ni raster ni modelo, costo $0.

Este catálogo resuelve a `structure + pixels + vision`, dos elementos, captura
esperada `FISICO` (por pedir `CAPTURA_FISICA`) y **ningún** scope de referencias.

### B2. Publicar y probar

```bash
pnpm autenticidad -- --type ACTA_ENTREGA --dir <carpeta>      # gratis
pnpm sync:one -- --type ACTA_ENTREGA
pnpm procesar acta.pdf ACTA_ENTREGA                           # local, ~$0.01–0.015
pnpm enviar acta.pdf ACTA_ENTREGA
```

No hay referencias que registrar, permiso que conceder ni índice que mantener.

---

## Lo que aplica a las dos

- **Arranca en observación.** Todo en `review`; mide con documentos reales y
  solo entonces sube a `reject` lo determinista (`CAPTURA_FISICA`,
  `memory.elements_unique`), con `pnpm sync:one`.
- **`AUTENTICIDAD_EVALUADA` va siempre** (los presets ya lo traen): si la etapa
  no pudo correr, el documento va a una persona; nunca se aprueba callado.
- **Costo.** Visión ~$0.009–0.0115 por documento (medido en la NOM-035 con Haiku 4.5, antes
  del cambio a Haiku 5.5, que cobra 10x menos por token), en la línea `BedrockAuthenticity`; embeddings $0.00006 por imagen en
  `BedrockEmbeddings`, y $0 cuando la entidad tiene 3 referencias o menos por
  elemento. `pnpm costos -- --id <id>` muestra lo real.
- **Límites conocidos.** La visión examina **solo la primera página** con
  contenido: una firma en la página 2 sale como ausente. Imprimir un documento
  compuesto y re-escanearlo pasa las capas gratis; ahí solo quedan el juicio de
  visión y, si la declaras, la memoria entre documentos.
- **El tipo se declara al subir** (`documentType`). Si además debe poder
  auto-clasificarse, necesita una `description` que lo distinga
  (`catalogs/README.md` → «Candidatura de auto-clasificación»).
- **Un elemento que nunca se llena** — revisa que el nombre del
  `authenticity_source` exista en la tabla de `catalogs/README.md`, y que el
  ambiente esté desplegado con los elementos compartidos.

## Cuándo ya no alcanza el catálogo

Comparar **dos documentos entre sí** (la firma de la identificación contra la
del contrato del mismo expediente) no existe todavía. Sería una regla en
`cross_document_validation_rules` que tome los recortes que la etapa ya guarda
de cada documento y llame al mismo examen de visión — sin tabla, sin API y sin
alta de referencias, pero con código.

---

## Qué falta para que TODO sea parámetro y esquema

> **Propuesta, no implementado.** Lo de arriba funciona hoy. Esta sección
> lista lo que todavía vive en código, en orden de cuánto estorba, con la
> forma de catálogo que tendría y dónde se tocaría. Sirve de plan para el día
> que un tipo nuevo tropiece con alguno.

Hoy ya son parámetro: qué elementos se buscan y cómo se describen, de quién son
las referencias y de dónde sale la clave, cuántas se muestran, cuándo se paga la
visión, todos los umbrales de reutilización, la severidad de cada señal, el
modelo de visión y el proveedor de embeddings. Lo que no:

### 1. El texto de los prompts de visión asume «papel firmado y sellado»

**Hoy.** `vision_inspector.inspect_prompt` dice literalmente que el recorte
viene de *«un documento que debió imprimirse, firmarse con tinta, sellarse con
sello de goma y escanearse»*, y los indicios que se le piden son una lista fija
(`PASTED_SIGNS`: halo rectangular, borde recortado, fondo uniforme). Para un
holograma, una fotografía o un código de barras esa pregunta no aplica.

**Como esquema.** Que cada elemento diga qué se le examina:

```jsonc
"HOLOGRAMA": {
  "description": "…",
  "examine": {
    "context": "Elemento de seguridad impreso de fábrica en el documento.",   // sustituye el párrafo fijo
    "signs": ["BORDE_RECORTADO", "RESOLUCION_DISTINTA"],                      // subconjunto de un vocabulario cerrado
    "ask_ink_colour": false
  }
}
```

**Dónde.** `vision_inspector.inspect_prompt` / `inspect_schema` (armar el
párrafo y el enum por elemento) y `_authenticity_elements` (mover ahí el
contexto actual de `FIRMA`/`SELLO`, para que su comportamiento no cambie). El
vocabulario de indicios sigue siendo cerrado y en código: es lo que impide que
una imagen le dicte la respuesta al modelo. **Esfuerzo:** chico. **Es el
primero que haría**: es el único que hoy puede dar una respuesta equivocada en
un elemento que no sea tinta.

### 2. Un elemento propio obliga a escribir sus campos a mano

**Hoy.** Los presets `FIRMA_/SELLO_/LOGO_CONTRA_REFERENCIA` existen porque
alguien los escribió. `HOLOGRAMA` necesita sus 2–3 campos copiados y adaptados.

**Como esquema.** Un preset **plantilla** con el elemento como parámetro:

```jsonc
"$presets": [
  "FIRMA_CONTRA_REFERENCIA",
  { "preset": "ELEMENTO_CONTRA_REFERENCIA", "element": "HOLOGRAMA", "label": "holograma" }
]
// → HOLOGRAMA_PRESENTE, HOLOGRAMA_SIN_INSERCION, HOLOGRAMA_COINCIDE_REFERENCIA
```

**Dónde.** `document_catalog._parse_field_tree` y su espejo
`scripts/catalog-files.mjs::expandPresets` (sustituir `{ELEMENT}`/`{label}` en
nombres, `authenticity_source` y mensajes); la plantilla en `_field_presets`;
`tests/unit/test_field_presets.py` y `tests/unit/node/catalog-files.test.mjs`.
Los tres presets actuales pasarían a ser tres usos de la plantilla.
**Esfuerzo:** chico.

### 3. Los límites del alta de una referencia son constantes

**Hoy.** `reference_registry.py`: 3.5 MB, lado menor ≥ 120 px (80 sin huella) y
≤ 2200 px, 12 MP, «misma tinta» ≥ 0.97, 20 activas por entidad y elemento.
Sirven para una firma o un sello; una fotografía de identificación o un
membrete a página completa pueden necesitar otros.

**Como esquema.** Por elemento, con los valores de hoy como default:

```jsonc
"LOGO": { "reference": { "min_side_px": 80, "max_side_px": 3000, "max_active": 5, "duplicate_similarity": null } }
```

**Dónde.** `reference_registry.review` / `find_duplicate` / `reserve` ya
reciben el `element`: leer de ahí con `DEFAULT_REFERENCE`. El CLI y la API no
cambian. **Esfuerzo:** chico.

### 4. La visión solo examina la primera página

**Hoy.** `pages.max_pages` rasteriza N páginas, pero `locate` e `inspect` ven
solo la primera (`authenticity._evaluate`: `page_index = next(iter(rasters))`).
Una firma en la última hoja de un contrato sale como ausente.

**Como esquema.**

```jsonc
"vision": { "pages": "first" }           // hoy
"vision": { "pages": "last" }            // contratos: la hoja de firmas
"vision": { "pages": "all", "max_pages": 3 }
"FIRMA": { "page": "last" }              // o por elemento
```

**Dónde.** `authenticity._evaluate` → repetir `locate` por página pedida y
quedarse, por elemento, con la mejor; costo = una llamada `locate` por página.
**Esfuerzo:** mediano (es el único que toca el flujo, y hay que cuidar la
facturación y la memoria entre documentos).

### 5. El permiso de la API es todo o nada

**Hoy.** `AllowReferenceWrite` deja a una app registrar referencias de
cualquier `scope` y cualquier entidad.

**Como esquema.** Por app, en `AppsTable`: `AllowedReferenceScopes`
(`["PROVEEDOR"]`) y, si hace falta, lectura y escritura separadas
(`AllowReferenceRead`). Mismo patrón que `AllowedDocTypes`.

**Dónde.** `references_api.resolve_caller` (ya resuelve la app) y
`scripts/manage-apps.mjs` (`--reference-scopes A,B` en grant/deny).
**Esfuerzo:** chico.

### 6. El bloque `authenticity` no tiene esquema que lo valide

**Hoy.** Una llave mal escrita (`inspect_when_digtal`, `key_form`) se ignora en
silencio y el tipo corre con el default. Un `scope` o un nombre de elemento con
`#` rompería las llaves de DynamoDB y del índice vectorial, y nada lo impide.

**Como esquema.** Un JSON Schema del bloque (`catalogs/_schema/authenticity.schema.json`)
validado en `pnpm build` / `pnpm sync*` (`scripts/sync-catalogs.mjs`,
`scripts/sync-schema.mjs`): llaves desconocidas = error, `scope` y nombres de
elemento `^[A-Z][A-Z0-9_]*$`, cada `authenticity_source` debe existir, cada
preset y elemento por nombre debe estar definido. Convierte «falló callado en
producción» en «no compila». **Esfuerzo:** mediano. **Es el segundo que haría.**

### 7. Constantes menores que solo importan en casos raros

| Constante (código) | Qué controla | Forma de catálogo |
|---|---|---|
| `_MAX_EMBEDDED_CANDIDATES` 8 · `_MAX_INK_CANDIDATES` 12 (`authenticity.py`) | Cuántas regiones candidatas se le enseñan a `locate` | `vision.max_candidates` |
| `_MAX_PASTED_COVERAGE` 0.35 | Hasta qué tamaño una imagen incrustada es «un elemento pegado» | `structure.max_pasted_coverage` |
| `_PREPARE_MAX_EDGE` 512 · `_PREPARE_MARGIN` 0.06 (`reference_embeddings.py`) | Cómo se prepara el recorte antes del embedding | **No lo parametrizaría**: cambiarlo invalida todos los vectores guardados; es parte del «modelo» y obliga a `refs:reindex` |
| `_LEGAL_NOISE` (`vision_inspector.py`) | Palabras que se ignoran al comparar la leyenda del sello con el emisor (S.A. de C.V., Grupo…) | `elements.SELLO.issuer_noise` (para emisores extranjeros: LLC, GmbH, Ltd) |
| `REFERENCE_EMBEDDING` (`document-validation-stack.ts`) | Proveedor y dimensión del índice vectorial, duplicados de la clase Python | Un solo `catalogs/_shared.json → _reference_embedding` leído por el stack y por la Lambda |
| Mensajes de los motivos (`authenticity.py`, en español) | El texto de `reasons[].message` | Una tabla `_reason_messages` en `_shared.json`; los **códigos** se quedan en código |

### 8. Lo que NO es parámetro, a propósito

- **Dónde está cada elemento** lo calcula el código, nunca el modelo ni el
  catálogo: medido, el modelo ubica mal y clasifica bien.
- **Que la visión no rechace sola** no es una opción: `on_fail: "reject"` sobre
  una señal de visión se puede escribir, y es un error de diseño.
- **Comparar dos documentos entre sí** sigue siendo código nuevo (una regla
  cross-documento), aunque reutilice los recortes y el examen de visión.

### Orden sugerido

1 (prompt por elemento) → 6 (esquema que valide) → 2 (preset plantilla) → 3
(límites del alta) → 5 (permiso por scope) → 4 (páginas). Los cuatro primeros
son chicos e independientes entre sí; con ellos, un elemento que no sea tinta
(holograma, fotografía, código) queda al mismo nivel que firma, sello y logo
sin tocar código.

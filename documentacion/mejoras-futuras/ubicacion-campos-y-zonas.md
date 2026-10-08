# Ubicación de cada campo en el documento y zonas de extracción

| | |
|---|---|
| **Fase** | 2 (dev) |
| **Estado** | Pendiente, sin implementar |
| **Dificultad** | Media |
| **Fecha del plan** | 2026-10-08 |

Resumen de todas las mejoras: [`MEJORAS_FUTURAS.md`](MEJORAS_FUTURAS.md).

## Qué resuelve

Hoy el pipeline devuelve por campo `{value, confidence, valid, reason, source}` y nada sobre **dónde** estaba el dato. La mejora tiene dos partes:

- **Mostrar el origen**: devolver página + caja de cada campo para que un frontend externo (ERP/portal) subraye de dónde salió el valor.
- **Zonas de extracción**: declarar por tipo de documento la zona donde vive un campo. La zona es flexible (pista) salvo que se declare fija; aun siendo fija, si el dato no está ahí se busca en el resto del documento.

Decisiones ya tomadas:

- Este proyecto entrega **solo API + SDK**; no construye visor ni editor.
- La primera versión cubre **campos escalares en documentos cortos** (tipos `_PORTAL`, cartas, CFDI). Tablas y partidas quedan sin caja.

## Estado actual

Rutas relativas a `src/document-processing/resources/enrichment-lambda/` salvo que se indique.

- `pipeline/pdf_pages.py` (`_blocks_to_page`) usa la caja de Textract solo para ordenar líneas y la descarta; los bloques `WORD` nunca se leen. La respuesta cruda de Textract no se guarda.
- El texto nativo sale de pypdf en modo `layout`, que devuelve solo texto.
- El modelo recibe texto plano y devuelve `{value, confidence}` (`pipeline/text_extractor.py`), sin página ni cita.
- El merge de chunks (`_merge_field_tree_entities`) no registra de qué página salió cada valor o fila.
- La etapa `authenticity` ya define la convención de caja `[x0, y0, x1, y1]` normalizada 0..1, origen arriba-izquierda, con `bboxSource` (`pipeline/pdf_forensics.py` `_visual_rect`, `pipeline/page_raster.py` `crop`/`pixel_box`).
- `pypdfium2` ya es dependencia; hoy solo rasteriza.
- `GET /documents/{id}/file` ya entrega el archivo original para que un visor lo pinte.

## Principio de diseño

**No pedirle coordenadas al modelo.** El código ya midió ~10% de error del alto de página en sus cajas (`pipeline/vision_inspector.py`). La ubicación se calcula de forma determinista después de extraer, buscando el valor crudo en las palabras de la página. El prompt ya exige copiar los valores tal como están impresos, que es lo que hace viable esa búsqueda.

## Etapa A — Mostrar el origen

### A1. Geometría de palabras por página — nuevo `pipeline/page_geometry.py`

- Estructura: lista de palabras `{text, bbox}` por página, con la misma convención de caja que `authenticity`.
- **Páginas OCR**: en `pdf_pages._blocks_to_page` recoger también los bloques `WORD` (texto + `Geometry.BoundingBox` → `[Left, Top, Left+Width, Top+Height]`). Agregar a `PageText` un campo opcional `words` (default `None`) para no romper a los consumidores actuales.
- **Páginas nativas**: función perezosa que abre la página con pypdfium2 (`get_textpage`, cajas por carácter), agrupa caracteres en palabras, normaliza por tamaño de página, invierte el eje Y y aplica la rotación reutilizando `pdf_forensics._visual_rect`. Se calcula solo para las páginas del segmento y solo si el tipo lo pide, así que cuesta cero si está apagado.
- Las imágenes sueltas (`load_image_page`) quedan cubiertas por el mismo `_blocks_to_page`.

### A2. Localizador — nuevo `pipeline/field_locator.py`

- Entrada: `doc["_entities"]` (valores **crudos** del modelo, antes de `normalize_field`, que es justo lo impreso), el `field_tree` del tipo y las palabras de las páginas enviadas a extracción.
- Búsqueda por ventana deslizante de palabras, en cascada:
  1. `EXACT`: igualdad tras plegar mayúsculas, acentos y espacios.
  2. `RELAXED`: ignorando puntuación y separadores (montos `1,234.50`, fechas, RFC con guiones).
- Varias ocurrencias: se elige la primera en orden de lectura (o la más cercana a la zona, ver etapa B) y se reporta `occurrences`.
- Se omiten: campos `computed`, los recuperados (`_recovered_sources`: QR, `derived`), la liquidación calculada y los arreglos.
- Salida: mapa `ruta → {page, bbox, bboxSource: "OCR"|"PDF", match, occurrences}`. `page` es el número absoluto en el archivo (1-based), igual que `pageStart`/`pageEnd`.

### A3. Enganche en el pipeline

- `pipeline/expediente.py`, tras `extract_document` y las recuperaciones: llamar al localizador y guardar `doc["_locations"]`.
- `validators/engine.py` `resolve_field_tree`: recibir `locations` y adjuntar `node["location"]` en el escalar, con el mismo patrón que ya usa `value_labels`.
- Activación por tipo en el catálogo (código genérico, JSON por tipo): bandera `locate_fields: true` en la entrada del tipo, leída en `document_catalog.py`. Apagada por defecto; se enciende primero en `catalogs/custom/portal/*.json`.

### A4. Salida y contrato

- `pipeline/envelope.py` `compact_node`: emitir `location` cuando exista. `fit_to_budget`: si se excede el tope de 180 KB, quitar primero las `location` (siguen disponibles en `results/{id}.json` vía `?full=1`).
- `documentacion/api-docs/openapi.yaml`: esquema `FieldLocation` en el campo escalar. Actualizar `documentacion/api-docs/API_USO.md`, `documentacion/COMO_FUNCIONA.md` y `catalogs/README.md` según las reglas de `AGENTS.md`.
- SDK (`../documents-validator-sdk`): `location?: FieldLocation` en `ScalarField` (`src/types.ts`), helper en `src/fields.ts`, luego `npm run sync:spec && npm test`.

Forma propuesta por campo:

```json
{
  "value": "OFU910626UQ0",
  "confidence": 0.97,
  "valid": true,
  "location": { "page": 1, "bbox": [0.62, 0.08, 0.81, 0.10], "bboxSource": "PDF", "match": "EXACT", "occurrences": 1 }
}
```

## Etapa B — Zonas de extracción

### B1. DSL en el catálogo

Por campo, bloque opcional:

```json
"zone": { "page": 1, "bbox": [0.62, 0.08, 0.95, 0.12], "mode": "hint", "tolerance": 0.02 }
```

- `page` es relativa al segmento (1 = primera, -1 = última).
- `mode: "hint"` (default): la zona solo desempata entre ocurrencias; el dato puede estar en cualquier parte.
- `mode: "fixed"`: se lee el texto de las palabras dentro de la zona, ampliada por `tolerance`.

### B2. Comportamiento en `field_locator.py`

- `hint`: gana la ocurrencia que cae en la zona o la más cercana; `match` conserva `EXACT`/`RELAXED`.
- `fixed`:
  - Si el valor del modelo está en la zona → `match: "ZONE"`.
  - Si la zona trae otro texto que sí pasa las reglas del campo (tipo/regex del catálogo) → se usa el de la zona, `source: "ZONE_EXTRACTION"`.
  - Si la zona está vacía o no pasa las reglas → búsqueda en todo el documento, `match: "RELOCATED"`, para que el consumidor sepa que el formato cambió.
- La llamada al modelo se mantiene siempre. Saltarla para tipos 100% de zona fija es una optimización posterior.

### B3. Cómo se obtienen las coordenadas de una zona

Sin editor: se procesa un documento de muestra con la etapa A y el `location.bbox` devuelto se copia al `zone` del catálogo.

## Dificultad por parte

| Parte | Dificultad | Por qué |
|---|---|---|
| Conservar cajas de Textract (páginas escaneadas) | Baja | El dato ya llega; solo hay que no tirarlo |
| Cajas en PDFs nativos | Media | Hay que sacar la geometría con pypdfium2, en paralelo al texto de pypdf |
| Localizar campos escalares (RFC, folio, fecha, total) | Media | Búsqueda del valor con tolerancia a espacios y formato |
| Valores normalizados (fechas, montos) | Media | El valor final ya no coincide con lo impreso; hay que buscar el crudo |
| Zonas `hint` / `fixed` con búsqueda de respaldo | Media | DSL nuevo en el catálogo y tres caminos de resolución |
| Campos repetidos y filas de tabla | Alta | Fuera de alcance de esta versión |
| Campos derivados, calculados o de QR | No aplica | No están impresos; van sin caja |

## Fuera de alcance

- Tablas, partidas de pedimento y cuadro de liquidación: requiere que el modelo devuelva página por fila y cambiar `_merge_field_tree_entities`.
- Visor o editor visual.
- Persistir la respuesta cruda de Textract o caché de OCR.
- Flujo legado sin `field_tree`.

## Verificación

- **Unitarias** (`tests/unit/`): `test_page_geometry.py` (respuesta de Textract de fixture → palabras; PDF nativo generado → cajas, incluida una página rotada), `test_field_locator.py` (exacto, relajado, repetidos, omitidos, modos `hint`/`fixed`/`RELOCATED`) y casos nuevos de envelope para el recorte por presupuesto.
- **Visual**: script `scripts/ver-ubicaciones.py` que toma un `resultados/<archivo>.json` y el PDF, rasteriza con `page_raster.render_page_rgb` y dibuja las cajas en PNG. Revisar a ojo un PDF nativo y uno escaneado de cada tipo `_PORTAL`.
- **Local e2e**: `pnpm procesar` sobre las muestras `_PORTAL` con `locate_fields` encendido; medir el % de escalares con `location` y el tiempo extra por documento.
- **Punto a comprobar pronto**: que la caja de Textract y la de pypdfium2 caigan en el mismo espacio visual en páginas con `/Rotate` (Textract recibe la página re-envuelta como PDF de una página).
- Suite existente completa en verde con la bandera apagada, sin cambios en resultados.
- SDK: `npm run sync:spec && npm test`.
- Deploy a dev antes de sincronizar catálogos.

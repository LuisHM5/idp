# Extracción determinística e híbrida para formatos fijos

Esta guía explica cómo `PEDIMENTO_ERP` pasó de enviar el schema completo a un
modelo a mapear sus campos por código, cómo `CERTIFICADO_POLIZA` maneja cuatro
familias de formato con perfiles, y cómo repetir ambos patrones sin convertir
el motor genérico en código específico por `TYPE_ID`.

La idea central es sencilla:

```text
PDF digital ──pypdf──────────┐
                             ├─ texto por página ─ mapper del catálogo
PDF escaneado ──Textract OCR─┘                         │
                                                      ├─ resueltos ───────────────┐
                                                      ├─ faltantes + review ─────┤
                                                      └─ faltantes + missing_fields
                                                                  └─ IA parcial ─┤
                                                                                 └─ merge → QR/derivados → validación
```

“Por código” se refiere a la **estructuración de campos**: regex, regiones,
normalización, QR y reglas declarativas. En un PDF nativo todo ese camino puede
ser código puro. En un escaneo todavía se paga Textract para convertir la imagen
en texto, pero no se usa IA generativa para estructurarlo.

## 1. Cuándo conviene este enfoque

| Fuente | Estrategia recomendada | Motivo |
|---|---|---|
| XML con estructura conocida | `xpath`, pipeline XML | Ya existe estructura; no convertirla a texto ni usar IA |
| XLSX/XLSM/CSV conocido | `cell`/`column`, pipeline tabular | La celda o columna es la fuente de verdad |
| PDF con etiquetas y tablas estables | `text_source` determinístico | Los anclajes permiten capturas verificables |
| PDF con núcleo fijo y campos narrativos/variables | Híbrida, `fallback: missing_fields` | Código para lo estable; IA sólo para lo ambiguo |
| Muchos layouts sin anclajes confiables | Modelo | Mantener regex por variante sería frágil |
| Firma, sello, logo o manipulación visual | Etapa `authenticity` | `text_source` no interpreta píxeles ni autenticidad |

Un “formato fijo” no significa coordenadas idénticas. El mapper actual trabaja
sobre texto y necesita etiquetas, separadores o límites de sección estables. No
extrae por coordenadas ni intenta reconstruir visualmente una tabla.

## 2. Qué se hizo en `PEDIMENTO_ERP`

### 2.1 Se conservó un contrato de salida explícito

`PEDIMENTO_ERP` es el mismo documento físico que `PEDIMENTO_ADUANAL`, pero sus
campos usan directamente los nombres del ERP. Se declaró
`segmentation_candidate: false` porque un clasificador no puede distinguir dos
contratos de salida observando el mismo papel; el integrador debe enviar
`documentType=PEDIMENTO_ERP`.

Esto también elimina la clasificación generativa cuando se carga exactamente
ese tipo.

### 2.2 Se activó el mapper en el catálogo

El catálogo contiene:

```jsonc
"deterministic_extraction": {
  "enabled": true,
  "fallback": "review"
}
```

`review` significa: ejecutar los selectores por código y **no abrir Bedrock para
completar faltantes**. Si falta un requerido, la validación manda el documento a
revisión. No se inventa un valor ni se degrada silenciosamente a IA.

Antes de llegar a esta política se probó `missing_fields`. En el pedimento real
de 17 páginas algunos huecos abrieron una generación de 38,804 tokens de entrada
y 10,058 de salida, llevando la extracción a 160.75 s. Después de cubrir el
layout y usar `review`, la ejecución desplegada terminó en 2.47 s con 0/0 tokens.

### 2.3 Se reutilizó el texto que el pipeline ya tenía

No se agregó otra lectura del PDF:

1. `pypdf` entrega el texto de páginas nativas.
2. Sólo las páginas sin texto utilizable pasan por Textract OCR.
3. `page_selection` determina qué páginas contienen el pedimento.
4. `deterministic_text.extract()` aplica los `text_source` del catálogo.

La selección ocurre después de cargar las páginas. Reduce texto/tokens de
extracción, pero actualmente no evita el OCR de una página escaneada.

### 2.4 Se dividió el documento por clase de dato

El catálogo declara 22 `text_source` de nivel superior y 4 dentro del cuadro de
liquidación. No existe un parser monolítico; cada campo tiene una fuente pequeña
y auditable.

| Clase | Ejemplos | Mecanismo |
|---|---|---|
| Identidad del pedimento | `No_Pedimento`, año, aduana, patente, consecutivo | Regex del encabezado + corrección QR |
| Encabezado | `Referencia`, `Fecha_Entrada`, `Tipo`, `Cve_Pedimento`, `Tipo_Cambio` | Selectores alternativos por etiqueta/layout |
| Importes | valores comercial/aduana e incrementables | Captura numérica junto a encabezados estables |
| Filas | `FACTURAS`, `ACUSES_VALOR`, conceptos de liquidación | `row_regex`/`row_selectors` + grupos nombrados |
| Calculados | `Tipo_Pago`, `LIQUIDACION_NORMALIZADA` | DSL `derived` y normalizador de liquidación |
| Verificación | URL QR y validez del depósito referenciado | `qr_recovery`, separado de las regex |

### 2.5 Qué se hizo en `CERTIFICADO_POLIZA`

El certificado de seguro conserva un solo contrato de salida, pero sus campos
cambian por ramo y aseguradora. El catálogo `5.1.0` reconoce cuatro perfiles
por **combinaciones semánticas**, no por el logo o nombre del emisor:

| Perfil | `CLAVE_RUBRO` | `RUBRO` | Señales típicas |
|---|---|---|---|
| Empresarial | `EMP` | `Empresarial` | seguro empresarial + ramos/edificio + certificado |
| Flotilla | `FL` | `Flotilla` | carátula de automóvil + vehículo + serie/placas + inciso |
| Gastos médicos | `GMM` | `Gastos medicos mayores` | póliza GMM + plan + tabla de asegurados/categoría |
| Vida | `SV` | `Seguro de Vida` | vida grupo + certificado individual + fallecimiento/beneficiarios |

Los cuatro PDF de calibración en `test-docs/seguros/` tienen texto nativo. El
camino probado es pypdf → perfiles/regex → validación: cero Textract y cero
Bedrock de extracción. Los selectores buscan etiquetas genéricas (`Póliza`,
`Certificado`, `Vigencia`, `Datos del contratante`, `Datos del vehículo`, etc.)
y no codifican Banorte, Afirme o Zurich como condición para extraer.

Cada perfil declara `expected_fields`: es el contrato mínimo que ese layout
debe entregar. Si se reconoce el perfil pero falta uno de esos campos,
`fallback: missing_fields` manda **sólo esa rama** a IA. Si el layout es nuevo,
ningún perfil alcanza el umbral o existe empate, `profile_fallback: full_model`
descarta las coincidencias tentativas y manda el schema completo al modelo. Así
un cambio de aseguradora con etiquetas equivalentes sigue por código, mientras
que un formato realmente distinto falla abierto sin publicar regex parciales
como si fueran confiables.

## 3. Técnicas usadas en el catálogo

### 3.1 Campo escalar con transformación cerrada

El número completo se captura con sus espacios y luego `digits` conserva sólo
los 15 dígitos:

```jsonc
"No_Pedimento": {
  "type": "string",
  "required": true,
  "text_source": {
    "selectors": [{
      "regex": "NUM\\.?\\s*PEDIMENTO\\s*:\\s*(?P<value>\\d{2}\\s+\\d{2}\\s+\\d{4}\\s+\\d{7})",
      "group": "value",
      "transform": "digits",
      "flags": ["ignore_case"]
    }]
  }
}
```

Los transforms permitidos son cerrados: `strip`, `collapse_whitespace`,
`digits`, `alnum`, `upper`, `lower`, `number`, `integer`, `date_iso`, `replace`,
`regex_sub` y `map`. `transforms` permite encadenarlos. El catálogo no ejecuta
expresiones ni código arbitrario.

### 3.2 Selectores alternativos para variantes conocidas

Los selectores se prueban en orden. `Cve_Pedimento` contempla el texto pegado
`REGIMEN:IMPA1` y la etiqueta explícita `CVE. PEDIMENTO`; `accept_regex` evita
aceptar una captura que no tenga la forma letra+dígito:

```jsonc
"selectors": [
  {
    "regex": "REGIMEN\\s*:\\s*(?:IMP|EXP)(?P<value>[A-Z][0-9])\\b",
    "group": "value",
    "transform": "upper",
    "flags": ["ignore_case"]
  },
  {
    "regex": "CVE\\.?\\s*PEDIM(?:ENTO)?\\s*:\\s*(?P<value>[A-Z0-9]+)",
    "group": "value",
    "accept_regex": "^[A-Z][0-9]$",
    "transform": "upper",
    "flags": ["ignore_case"]
  }
]
```

Un selector también admite:

- `occurrence: first|last`;
- `pages: "first"|[1,2,...]`;
- flags `ignore_case`, `multiline` y `dotall`;
- `region` para restringir dónde puede hacer match.

### 3.3 Regiones para no tomar una etiqueta homónima

`TOTAL`, `EFECTIVO`, `OTROS` y los conceptos sólo se buscan entre “CUADRO DE
LIQUIDACIÓN” y “DEPÓSITO REFERENCIADO”:

```jsonc
"region": {
  "start_regex": "CUADRO\\s+DE\\s+LIQUIDACI[OÓ]N",
  "end_regex": "DEP[OÓ]SITO\\s+REFERENCIADO",
  "flags": ["ignore_case"]
}
```

Sin esa ventana, un `TOTAL` de otra tabla puede producir un valor válido en
forma pero incorrecto en significado.

### 3.4 Arrays de filas completos

`FACTURAS` reconoce dos layouts: factura directa y COVE/factura. Cada match
produce una fila y los grupos se asignan a `item_fields`:

```jsonc
"text_source": {
  "row_selectors": [
    {
      "row_regex": "...(?P<factura>...) (?P<fecha>...) ... (?P<total>...)...",
      "groups": {
        "Factura": "factura",
        "Fecha_Factura": "fecha",
        "Total_factura": "total"
      }
    },
    {
      "row_regex": "...(?P<acuse>COVE... / (?P<factura>...)) ...",
      "groups": {
        "Factura": "factura",
        "num_acuse_valor": "acuse",
        "Fecha_Factura": "fecha",
        "Total_factura": "total"
      }
    }
  ],
  "deduplicate_by": ["Factura", "Fecha_Factura"],
  "region": {"start_regex": "...", "end_regex": "..."}
}
```

Reglas importantes:

- una fila incompleta en un subcampo `required` se descarta;
- las filas de todos los `row_selectors` se combinan y después se deduplican;
- si no sale ninguna fila, el array completo queda sin resolver y puede ir al
  fallback;
- si sí salió una fila, el array se considera resuelto: el mapper no sabe si el
  PDF contenía otra fila que la regex no reconoció. Por eso los arrays requieren
  corpus y conteos de ground truth, no sólo “al menos una fila”.

`ACUSES_VALOR` es un array escalar independiente. No se une por posición con
`FACTURAS`, porque el papel no declara esa correspondencia; hacerlo sería
inventar datos.

### 3.5 Ausencia legítima sin gastar IA

Campos opcionales como algunos incrementables y acuses usan:

```jsonc
"model_fallback": "never"
```

Si no hay match, no se envían al modelo en una estrategia híbrida. Esto sirve
cuando la ausencia es normal o existe un default seguro. En un campo requerido,
`never` no lo vuelve válido: la validación lo enviará a revisión.

### 3.6 Datos calculados, no “extraídos”

El mapper ignora campos `computed`. Se llenan después con componentes genéricos:

- `qr_recovery` decodifica el QR y corrige patente, consecutivo, año y aduana;
- `LIQUIDACION_NORMALIZADA` suma conceptos según `concept_aliases`, fuentes y
  filtros declarados;
- `Tipo_Pago` usa `derived` sobre el total de liquidación;
- normalización/coerción convierte fechas y montos al tipo del schema;
- el motor de reglas valida formato, presencia y consistencia.

Separar captura y cálculo evita mantener la misma fórmula dentro de varias
regex o pedirle al modelo que calcule algo que el sistema puede demostrar.

### 3.7 Perfiles para layouts conocidos

Un perfil necesita varias señales independientes y un contrato explícito:

```jsonc
"deterministic_extraction": {
  "enabled": true,
  "fallback": "missing_fields",
  "profile_fallback": "full_model",
  "profiles": [{
    "id": "FL",
    "pages": "first",
    "markers": [
      "CARÁTULA\\s+DE\\s+SEGURO\\s+DE\\s+AUTOMÓVILES",
      "DATOS\\s+DEL\\s+VEHÍCULO",
      "Número\\s+de\\s+Serie|Placas\\s*:"
    ],
    "min_score": 3,
    "constants": {"CLAVE_RUBRO": "FL", "RUBRO": "Flotilla"},
    "expected_fields": [
      "CLAVE_RUBRO", "RUBRO", "NUMERO_POLIZA", "ASEGURADOS",
      "VIGENCIA_INICIO", "VIGENCIA_FIN", "BIEN_ASEGURADO.NUMERO_SERIE"
    ]
  }]
}
```

No pongas todos los campos opcionales del tipo en `expected_fields`: uno que el
layout legítimamente no imprime abriría IA en cada documento. Tampoco omitas un
campo que el negocio sí espera de ese perfil: entonces un miss quedaría vacío
sin fallback. `pnpm catalogs:lint` exige una lista no vacía, valida sus rutas y
obliga a incluir ahí cualquier constante.

## 4. Cómo funciona la estrategia híbrida

Con `fallback: missing_fields` el flujo es:

```text
schema completo
   │
   ├─ mapper resuelve FOLIO, FECHA y TOTAL
   │
   └─ unresolved_tree construye un schema sólo con los campos restantes
                               │
                               └─ Bedrock extrae únicamente ese subárbol
                                           │
                                           └─ merge: el valor por código siempre gana
```

Propiedades de seguridad:

- Bedrock nunca sobrescribe un path que el mapper marcó como resuelto.
- Los campos `computed` nunca entran al prompt.
- `model_fallback: never` excluye ese campo del sub-schema pendiente.
- Una regex inválida o sin match produce warning y deja el path pendiente; con
  `missing_fields` falla abierto hacia el modelo.
- Si no queda ningún path pendiente, no existe llamada de extracción.
- `fallback: full_model` conserva el modelo completo aunque haya matches; sirve
  como compatibilidad temporal, pero no ahorra tokens. El override de request
  `model` va más lejos: omite el mapper y reproduce el camino anterior.

La estrategia híbrida no debe verse como destino obligatorio. Es una etapa de
migración útil mientras se mide cobertura. Si un solo campo faltante abre un
modelo sobre muchas páginas, su p95 puede seguir siendo alto; eso fue exactamente
lo observado en el pedimento de 17 páginas.

## 5. Receta para un nuevo formato fijo

### Paso 1: declarar el tipo y escoger el contrato

Define primero los campos que realmente necesita el consumidor, sus tipos,
cuáles son requeridos y sus reglas. No diseñes regex antes de fijar el contrato.

Si el documento físico comparte apariencia con otro `TYPE_ID` pero cambia el
shape de salida, usa `segmentation_candidate: false` y exige `documentType`.

### Paso 2: reunir un corpus representativo

Incluye como mínimo:

- PDF nativo y escaneado;
- distintas cantidades de páginas y filas;
- valores ausentes legítimos;
- variantes conocidas de encabezados;
- ruido real de OCR;
- al menos un documento que **no** corresponda al tipo.

Define ground truth de valores y conteos. Un regex que funciona en un ejemplo no
demuestra cobertura.

Si el mismo `TYPE_ID` tiene varias familias, define un perfil por familia.
Prefiere palabras del ramo y estructura del documento sobre nombres de
aseguradora: cambiar de emisor no debería forzar IA si conserva el patrón.

### Paso 3: seleccionar páginas con anclajes positivos

Declara `page_selection.markers` y calibra antes de extraer:

```bash
pnpm paginas -- --type MI_TIPO --dir test-docs/mi-tipo --show-pages

# Si el corpus es escaneado, paga OCR una vez y reutiliza el texto:
pnpm paginas -- --type MI_TIPO --dir test-docs/mi-tipo \
  --ocr --cache .cache-ocr --show-pages
```

La selección debe fallar abierta: si los marcadores no son concluyentes, procesa
todo en vez de recortar contenido real.

### Paso 4: empezar por escalares de alta certeza

Agrega `text_source` sólo donde haya una etiqueta y una forma verificable. Usa
`accept_regex`, regiones y selectores alternativos; evita patrones globales como
`TOTAL.*` sin límite de sección.

### Paso 5: mapear tablas por fila

Cada `row_regex` debe capturar una fila completa. Declara como `required` sólo
las celdas cuya ausencia invalida realmente esa fila y deduplica por una llave de
negocio estable.

### Paso 6: separar derivados y verificaciones

Usa `derived`, normalizadores, QR y reglas para cálculos/consistencia. No hagas
que una regex calcule ni que el modelo repita una fórmula determinística.

### Paso 7: iniciar en híbrido

```jsonc
"deterministic_extraction": {
  "enabled": true,
  "fallback": "missing_fields"
}
```

Mide en cada documento:

- `deterministicExtraction.resolvedFieldCount`;
- `fallbackFields`;
- `strategy`;
- diferencias contra ground truth;
- `aiExecuted`, tokens, costo y latencia.

### Paso 8: pasar a sólo código únicamente con evidencia

Cambia a `fallback: review` cuando:

- todos los requeridos tienen cobertura en el corpus;
- los arrays tienen conteos exactos, no sólo alguna fila;
- no existen falsos positivos entre secciones;
- un miss produce revisión aceptable para el negocio;
- el resultado determinístico coincide con ground truth y no únicamente con la
  salida previa del modelo.

Incrementa `schema_version`, ejecuta tests, despliega el motor si cambió código y
sincroniza el catálogo.

## 6. Pruebas y comparación

Pruebas locales del mapper y del contrato ERP:

```bash
.venv/bin/pytest \
  tests/unit/test_deterministic_text.py \
  tests/unit/test_pedimento_mapping.py -q

pnpm catalogs:lint
```

`test_certificado_poliza_cuatro_perfiles_sin_fallback_de_ia` carga los cuatro
PDF locales de seguros, exige perfil/rubro/póliza correctos y comprueba que no
quede ninguna rama esperada para fallback. Como `test-docs/**` no se versiona,
la prueba se omite explícitamente en un clon que no tenga ese corpus; las
pruebas sintéticas de perfiles/fallback siguen corriendo siempre.

Pipeline local con la política del catálogo:

```bash
pnpm procesar -- archivo.pdf PEDIMENTO_ERP
```

Contrafactuales desplegados, sólo en dev:

```bash
pnpm enviar archivo.pdf PEDIMENTO_ERP --extraction-mode model
pnpm enviar archivo.pdf PEDIMENTO_ERP --extraction-mode hybrid
pnpm enviar archivo.pdf PEDIMENTO_ERP --extraction-mode deterministic
```

La comparación mínima debe revisar campos, status, conteos de arrays,
`fallbackFields`, `tracking.timings.extraction`, `aiUsage` y
`deterministicSavings`. Menos tokens con valores incorrectos no es una
optimización.

## 7. Qué publica el resultado

- Cada valor del mapper lleva `source: "DETERMINISTIC_TEXT"`.
- `extractionMode.requested` muestra lo pedido por el cliente.
- `extractionMode.effective` muestra la política aplicable.
- `deterministicExtraction.strategy` dice si finalmente fue `deterministic`,
  `hybrid` o `model`.
- `deterministicExtraction.profile` publica `id`, `status` y el score por
  perfil. El resultado profundo conserva además
  `extractionMeta.deterministicExtraction.candidateFields` y `resolvedFields`
  para separar coincidencias tentativas de valores realmente conservados.
- `fallbackFields` enumera el subárbol enviado a Bedrock.
- `aiExecuted`/`aiUsage.stages` confirman cualquier etapa generativa real.
- `deterministicSavings` estima tokens evitados y permanece separado del usage
  facturado.

El costo facturado se guarda aparte en `document-validation-costs[-dev]`:
Bedrock cobra sólo los tokens reales del fallback, Textract sólo las páginas
OCR y el tiempo del mapper queda dentro de Lambda. El ahorro estimado no se suma
a `TotalCostUsd`.

## 8. Límites que no deben ocultarse

- Un cambio de layout puede convertir un match en miss; con `review` eso produce
  revisión, no IA automática.
- Un match con forma válida todavía puede pertenecer a otra sección: por eso se
  usan regiones y reglas semánticas.
- Un array parcialmente reconocido parece “resuelto”; su cobertura debe probarse
  con el número esperado de filas.
- El mapper no enlaza relaciones que el documento no imprime. Por ejemplo, no
  empareja un COVE suelto con una factura por posición.
- Los campos narrativos o visuales no deben forzarse a regex para presumir cero
  IA; deben quedar híbridos, en modelo o en autenticidad según su naturaleza.
- La confianza determinística hereda la menor confianza de las páginas donde
  cayó el match; no significa probabilidad aprendida del valor.

## 9. Implementación de referencia

- Catálogo piloto: `catalogs/custom/erp/PEDIMENTO_ERP.json`.
- Catálogo con perfiles: `catalogs/base/seguros/CERTIFICADO_POLIZA.json`.
- Motor cerrado de regex: `pipeline/deterministic_text.py`.
- Orquestación, fallback y merge: `pipeline/expediente.py::_extract_segment`.
- OCR/texto: `pipeline/pdf_pages.py`.
- QR: `pipeline/qr_recovery.py`.
- Campos derivados: `pipeline/derived.py`.
- Liquidación: `pipeline/liquidacion.py`.
- Tests genéricos: `tests/unit/test_deterministic_text.py`.
- Tests del contrato ERP: `tests/unit/test_pedimento_mapping.py`.
- Linter previo a build/sync: `scripts/validar-mapeos-deterministas.py`.


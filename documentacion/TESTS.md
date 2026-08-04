# Pruebas — Document Validator

Cómo correr los tests del proyecto, qué prueba cada uno, y cómo comparar
valores esperados vs. extraídos de un documento para decidir si hay un bug.

| Comando                                   | Qué prueba                                                                     | Requiere AWS            | Costo aprox. |
| ----------------------------------------- | ------------------------------------------------------------------------------ | ----------------------- | ------------ |
| `pnpm test:unit`                          | Motor de reglas, normalizadores, catálogo, y el ground truth de pedimentos (esquema/nombres de campo válidos) — ver [1](#1-antes-de-gastar-dinero-valida-el-ground-truth-gratis) | No | $0 |
| `pnpm comparar -- normal_pend.pdf`        | **Un solo documento**: valores esperados vs extraídos, campo por campo — ver [2](#2-comparar-expected-vs-real-de-un-documento-con-costo-script-simple) | Sí (Textract + Bedrock) | ~$0.02–0.35  |
| `pnpm test:pedimentos`                    | Pipeline completo contra los 9 PDFs reales de `test-docs/pedimentos docs/docs` | Sí (Textract + Bedrock) | ~$1 USD      |
| `pnpm test:pedimentos -- --file B2401416` | Igual a `pnpm comparar` pero sin el prompt de confirmación (útil en CI/scripts) | Sí                      | ~$0.02–0.35  |
| `pnpm test:pedimentos -- --save /tmp/res` | Corre todo + guarda los JSON completos de resultado                            | Sí                      | —            |
| `pnpm test-local --type INE`              | Motor de validación con entidades simuladas                                    | No                      | $0           |
| `pnpm test-e2e --file doc.pdf`            | Pipeline desplegado end-to-end genérico (API → S3 → SFN → EventBridge), sin comparar contra ground truth | Sí (stack desplegado) | centavos     |
| `pnpm test:e2e:pedimento -- --file normal_pend.pdf` | **E2E real de verdad**: sube por el stack desplegado (S3→SQS→SFN→Lambdas→DynamoDB/S3) Y compara contra el ground truth — ver [5](#5-e2e-real-contra-el-stack-desplegado) | Sí (stack desplegado) | ~$0.05–0.10 + minutos |
| `pnpm schema:sensitivity -- --file normal_pend.pdf` | Experimento: qué tan preciso/impreciso se vuelve el resultado al cambiar el catálogo (hints, campos requeridos, tolerancias) — ver [6](#6-experimento-de-sensibilidad-del-schema) | Sí (parcial, ver abajo) | ~$0.05–0.15 |

La verdad de terreno de los tests de pedimentos vive en
`tests/integration/expected_pedimentos.json` — al agregar documentos de
prueba nuevos, añade ahí sus valores esperados (nombre exacto del campo tal
como está en `catalogs/*.json`, no un alias inventado).

## 1. Antes de gastar dinero: valida el ground truth (gratis)

`pnpm test:unit` incluye `tests/unit/test_expected_pedimentos_schema.py`,
que valida `expected_pedimentos.json` **sin tocar AWS**:

- que el JSON sea válido,
- que cada nombre de campo (`fields`, `required_present`) exista de verdad
  en el catálogo — atrapa typos como escribir `DESTINO` cuando el catálogo
  dice `DESTINO_PAIS`, que si no se atrapan aquí se ven como "el pipeline no
  extrajo el campo" cuando en realidad el error está en el fixture,
- que `pedimento_consecutivo` tenga exactamente 7 dígitos.

Corre esto primero siempre que edites `expected_pedimentos.json` — es
gratis y evita gastar la corrida real por un error de tipeo en el propio
archivo de expectativas.

```bash
pnpm test:unit
```

## 2. Comparar expected vs real de un documento (con costo, script simple)

```bash
pnpm comparar -- normal_pend.pdf
# o directo:
bash scripts/comparar-documento.sh normal_pend.pdf
```

Es una envoltura de una sola confirmación (`¿Continuar? [y/N]`, porque llama
a Bedrock/Textract de verdad) sobre `scripts/test-pedimentos.py --file`. Sin
argumentos, lista los PDFs disponibles en `test-docs/pedimentos docs/docs`.

Qué imprime, de arriba a abajo:

1. **Clasificación y páginas**: tipo de documento detectado, páginas
   totales / nativas / OCR, tiempo de proceso.
2. **Campo por campo**, comparando el valor esperado (del JSON) contra el
   `normalizedValue`/`extractedValue` real:
   ```
   ✓ NUMERO_PEDIMENTO_NORMALIZADO=260708236001630
   ✗ PRECIO_PAGADO_VALOR_COMERCIAL=435730.0 != 421841.0
   ```
   `✓` = coincide (números con tolerancia de 0.01); `✗` muestra el valor
   real y el esperado lado a lado — esa línea sola ya dice si el bug es "no
   extrajo nada", "extrajo el valor de otro campo" o "extrajo bien pero mal
   formateado".
3. **`engine: N failing validation rule(s)`**: las reglas del catálogo
   (`validators/engine.py`) que fallan en ese documento, aunque los campos
   individuales hayan coincidido — por ejemplo una fórmula que cruza varios
   campos (`VALOR_ADUANA` vs `VALOR_COMERCIAL_PESOS + TOTAL_INCREMENTABLES -
   TOTAL_DECREMENTABLES`). Esto es lo que responde "por qué está fallando"
   cuando el campo en sí se ve bien.
4. **`PASS`/`FAIL` por expectativa** y `FILE PASS`/`FILE FAIL` al final.
5. **Costo real** de la corrida: tokens de entrada/salida de Bedrock y su
   estimado en USD.

Con esa salida ya se puede decidir sin adivinar: si el `✗` es un valor de
otro campo (confusión del modelo), si es un formato (ceros, mayúsculas,
concatenación), o si el campo en sí pasó pero una regla cruzada sigue
fallando.

## 3. Agregar un documento de prueba nuevo

1. Copia el PDF a `test-docs/pedimentos docs/docs/` (esa carpeta está en
   `.gitignore` — el PDF se queda local, no se sube a git; solo el JSON de
   abajo sí se versiona).
2. Lee los valores reales directamente del PDF (no de una corrida previa
   del pipeline) y agrégalos a `tests/integration/expected_pedimentos.json`
   bajo `"files"`, con el nombre exacto del archivo como llave.
3. Corre `pnpm test:unit` para validar que los nombres de campo existan en
   el catálogo antes de gastar dinero.
4. Corre `pnpm comparar -- tu_archivo.pdf` para ver el resultado real.

## 5. E2E real contra el stack desplegado

```bash
pnpm test:e2e:pedimento -- --file normal_pend.pdf
pnpm test:e2e:pedimento -- --file normal_pend.pdf --env dev --wait 900
```

`pnpm comparar`/`pnpm test:pedimentos` llaman `process_pdf()` directo en
Python — **nunca tocan S3, SQS, Step Functions ni DynamoDB**. Son rápidos y
baratos, pero no pueden detectar un bug de infraestructura (por ejemplo, el
bug de `documentId` mal resuelto en clasificación manual que se arregló en
esta misma etapa del proyecto, o una ejecución que muere en
`ExecutionTimedOut` en vez de fallar limpio).

`scripts/test-e2e-pedimento.py` sí pasa por el flujo real: sube el PDF por
`POST /upload` al stack desplegado, espera la ejecución real de Step
Functions, y valida:

1. **Infra**: que la ejecución termine en `SUCCEEDED` (no timeout/error), y
   que el archivo se haya movido de `raw/` a `processed/` (o `failed/`).
2. **Ground truth**: la misma comparación campo por campo que `pnpm
   comparar`, pero sobre el resultado que salió de verdad del pipeline
   desplegado — reutiliza `_check_doc_expectation`/`_print_engine_checks` de
   `test-pedimentos.py` en vez de duplicar la lógica.

Corrida real verificada (2026-07-28, `normal_pend.pdf`, stack
`DocumentValidationStack-Dev`): Step Functions `SUCCEEDED` en 64s, archivo
movido correctamente a `processed/{documentId}-.../normal_pend.pdf`
(el `documentId` de esa corrida incluía el nombre de archivo concatenado —
bug arreglado después en `patches/default-sqs-consumer-index.py`; hoy la
forma es `processed/{documentId}/normal_pend.pdf`, sin el nombre duplicado
dentro del `documentId` ni el anidado extra que causaba), y el
mismo resultado campo por campo que la corrida en proceso — confirma que el
Lambda desplegado y el código local están en sync (sin drift de deploy).

## 6. Experimento de sensibilidad del schema

```bash
pnpm schema:sensitivity -- --list                       # ver mutaciones disponibles
pnpm schema:sensitivity -- --file normal_pend.pdf        # corre todas (~$0.15)
pnpm schema:sensitivity -- --file normal_pend.pdf --skip-paid   # solo las gratis
pnpm schema:sensitivity -- --file normal_pend.pdf --only tighten_tolerance_incrementables
```

Responde "¿qué pasa si cambio un poco el catálogo?" con datos reales en vez
de intuición. Corre UNA extracción baseline real y luego aplica mutaciones
al catálogo de `PEDIMENTO_ADUANAL`, midiendo el efecto en el ground truth y
en las reglas de validación. Dos categorías, con costo muy distinto **por
diseño**:

- **`validation` (gratis)**: tocan `validation_rules`, `required_fields` o
  `min_confidence`. No cambian el prompt que ve el modelo, así que se
  reevalúan sobre la MISMA extracción baseline — cero llamadas a Bedrock.
- **`extraction` (paga, ~$0.05 c/u)**: tocan `extraction_hints` o
  `extraction_fields` — cambian el prompt/tool-schema real, así que cada una
  dispara una llamada nueva a Bedrock.

Termina con una sección `Conclusiones` generada de los números reales de esa
corrida (delta de ground-truth, checks que se rompieron o arreglaron, si
cambió el status del documento) — no es texto fijo, cambia según el
documento y el catálogo vigente al momento de correrlo.

**Hallazgos de la corrida verificada (2026-07-28, `normal_pend.pdf`)** —
válidos para ese documento y ese catálogo en esa fecha, no una garantía
permanente:

| Mutación | Resultado | Lectura |
| --- | --- | --- |
| `tighten_tolerance_incrementables` | sin cambio | la tolerancia de `TOTAL_INCREMENTABLES` no es el cuello de botella en este documento |
| `loosen_tolerance_valor_aduana` (5→20000) | el check `VALOR_ADUANA` **sigue fallando** | la diferencia real entre el valor calculado y el impreso es mayor a 20,000 — no es un problema de redondeo, es un campo mal leído (confirma que `PRECIO_PAGADO_VALOR_COMERCIAL` está usando el valor de otro campo, no un valor "cercano pero impreciso") |
| `drop_anio_pedimento_required` | sin cambio en `status` | el status del documento lo está determinando el check de fórmula fallido, no la ausencia de `ANIO_PEDIMENTO` como requerido |
| `raise_min_confidence` (0.80→0.97) | sin cambio | el modelo reporta confianza ≥0.97 en todos los campos evaluados — **incluido el campo que está mal** (`PRECIO_PAGADO_VALOR_COMERCIAL`). La confianza reportada por el modelo no es una señal confiable de corrección aquí |
| `disambiguate_valor_comercial_hint` (hint explícito distinguiendo de `VALOR_ADUANA`) | **no arregló el bug** | agregar texto aclaratorio al hint no fue suficiente para que el modelo distinga los dos valores en este documento — sugiere que el problema no es de nomenclatura sino de qué tan cerca están impresos en la página, o que amerita revisar si el ground truth (leído a mano) está describiendo el campo correcto |
| `remove_anio_pedimento_hint` (quita el hint "solo 2 dígitos, no 2026") | **el bug NO volvió** | el año se sigue extrayendo bien sin el hint — la recuperación determinística en código (`_recover_references()`, agregada esta sesión) ya cubre este caso independientemente del prompt: hay defensa en profundidad, no solo dependencia del hint |

## 7. Solución de problemas de tests

| Síntoma | Causa probable | Solución |
| --- | --- | --- |
| `test_field_names_exist_in_catalog` falla | Nombre de campo con typo en `expected_pedimentos.json`, o campo que no existe en el catálogo | Revisa `catalogs/PEDIMENTO_ADUANAL.json` (o el tipo correspondiente) y corrige el nombre exacto |
| `no expectations defined for this file` | El PDF no tiene entrada en `expected_pedimentos.json` | Normal si es un documento sin ground truth aún — agrégalo (ver sección 3) |
| `PROCESSING ERROR` al correr `pnpm comparar` | Error real del pipeline (credenciales AWS, modelo no habilitado, etc.) | Revisa que `aws sts get-caller-identity` funcione y que el modelo Bedrock esté habilitado (ver `GUIA_DESPLIEGUE_Y_USO.md` sección 1) |
| `✗` en un campo pero el valor se ve razonable | Puede ser tolerancia de formato (mayúsculas/espacios en strings, 0.01 en números) | Revisa la nota impresa junto al `✗`: muestra el valor exacto extraído vs esperado |
| `test:e2e:pedimento` se queda esperando y nunca aparece la ejecución | El SQS consumer no disparó Step Functions, o la Lambda de consumo falló | Revisa la SQS DLQ y los logs del consumer Lambda en CloudWatch |
| `test:e2e:pedimento` dice `status != SUCCEEDED` | Falla real de infraestructura (timeout, error no atrapado) — justo lo que este test existe para detectar y que `pnpm comparar` no puede ver | `pnpm track --execution-arn <arn>` (el comando se imprime en la salida) |

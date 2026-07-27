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
| `pnpm test-e2e --file doc.pdf`            | Pipeline desplegado end-to-end (API → S3 → SFN → EventBridge)                  | Sí (stack desplegado)   | centavos     |

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

## 4. Solución de problemas de tests

| Síntoma | Causa probable | Solución |
| --- | --- | --- |
| `test_field_names_exist_in_catalog` falla | Nombre de campo con typo en `expected_pedimentos.json`, o campo que no existe en el catálogo | Revisa `catalogs/PEDIMENTO_ADUANAL.json` (o el tipo correspondiente) y corrige el nombre exacto |
| `no expectations defined for this file` | El PDF no tiene entrada en `expected_pedimentos.json` | Normal si es un documento sin ground truth aún — agrégalo (ver sección 3) |
| `PROCESSING ERROR` al correr `pnpm comparar` | Error real del pipeline (credenciales AWS, modelo no habilitado, etc.) | Revisa que `aws sts get-caller-identity` funcione y que el modelo Bedrock esté habilitado (ver `GUIA_DESPLIEGUE_Y_USO.md` sección 1) |
| `✗` en un campo pero el valor se ve razonable | Puede ser tolerancia de formato (mayúsculas/espacios en strings, 0.01 en números) | Revisa la nota impresa junto al `✗`: muestra el valor exacto extraído vs esperado |

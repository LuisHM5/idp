# Pruebas — Document Validator

Cómo correr los tests del proyecto, qué prueba cada uno, y cómo comparar
valores esperados vs. extraídos de un documento para decidir si hay un bug.

Si es tu primera vez, empieza por la [guía rápida](#0-guía-rápida-cómo-correr-los-tests).
Las secciones 1–8 son la referencia detallada de cada comando.

## 0. Guía rápida: cómo correr los tests

### 0.1 Preparación (una sola vez)

```bash
pnpm install
pnpm setup:python              # crea .venv con pytest, pypdf, boto3 (requirements-dev.txt)
aws sts get-caller-identity    # los tests con costo usan TUS credenciales AWS
```

Los tests gratuitos no necesitan nada más. Los que suben documentos por el API
necesitan además una API key según el ambiente:

- **dev**: nada, salvo que `.env.dev` tenga `DEV_API_KEY_REQUIRED=true`; en ese
  caso pon `API_KEY_APP=<valor>` en `.env.dev` y los scripts la toman solos.
- **prod**: siempre hay que pasarla explícita — a propósito, nunca se lee sola.
  El script lo verifica antes de subir nada:

  ```bash
  API_KEY=<valor> pnpm test:e2e:suite -- --env prod    # inline: bash, zsh y fish
  set -gx API_KEY <valor>                               # fish, para toda la sesión
  export API_KEY=<valor>                                # bash/zsh, para toda la sesión
  ```

  Si no tienes una: `pnpm apps:register -- --name local-scripts --env prod`
  (ver [AMBIENTES_Y_SEGURIDAD.md](AMBIENTES_Y_SEGURIDAD.md)).

Los argumentos se pasan después del nombre del comando; el `--` intermedio es
opcional (`pnpm test:e2e:pedimento -- --limit 2` y `pnpm test:e2e:pedimento -n 2`
hacen lo mismo). Cada comando E2E tiene `--help` estilo página man (USO,
OPCIONES, EJEMPLOS, ENTORNO, ESTADO DE SALIDA); la sinopsis de los tres está en
[0.9](#09-sinopsis-de-los-comandos-e2e).

### 0.2 Ruta recomendada: de gratis a caro

Cada paso atrapa algo que el anterior no ve; no gastes en el siguiente si el
anterior falla.

| # | Comando | Costo | Qué te dice | Pasa si |
|---|---|---|---|---|
| 1 | `pnpm test:unit` | $0, segundos | motor de reglas, catálogo, ground truth bien escrito, análisis del E2E | `N passed`, cero `failed` |
| 2 | `pnpm test:quality` | $0 | lo mismo + cobertura contra la baseline | termina en exit 0 |
| 3 | `pnpm test:e2e:pedimento -- --file normal_pend.pdf --env dev` | centavos, ~1–2 min | que el stack desplegado funciona de punta a punta con un archivo | `E2E PASS` |
| 4 | `pnpm test:e2e:pedimento -- --docs <carpeta> --doc-type <TIPO> --limit 2` | centavos | que el modo dataset y el análisis corren con tus documentos | veredicto impreso, sin errores de infraestructura |
| 5 | `pnpm test:e2e:pedimento -- --docs <carpeta> --doc-type <TIPO> --env prod` | el plan lo dice antes | **si puedes confiar en ese tipo** con un dataset real | veredicto `CONFIABLE` o `CON RESERVAS` revisadas |
| 6 | `pnpm test:e2e:suite -- --env prod` | el plan lo dice antes | lo mismo para todos los tipos que tengan documentos | ningún tipo `NO CONFIABLE` |

Bedrock y Textract cuestan **lo mismo en dev que en prod**
([COSTOS.md](COSTOS.md)): experimenta en dev y usa prod cuando lo que quieres
validar es justo lo que está desplegado ahí.

### 0.3 ¿Qué corro? según lo que cambiaste o quieres saber

| Situación | Corre |
|---|---|
| Cambié código Python del pipeline o del motor | `pnpm test:unit`; si afecta extracción de pedimentos, después `pnpm comparar -- normal_pend.pdf` (con costo) |
| Edité `expected_pedimentos.json` | `pnpm test:unit` (valida nombres de campo gratis, [sección 1](#1-antes-de-gastar-dinero-valida-el-ground-truth-gratis)) |
| Cambié el catálogo de un tipo y ya lo sincronicé | dataset E2E de ese tipo contra dev (paso 5 con `--env dev`) y revisa la sección 10 del análisis: qué cambió contra la corrida anterior |
| Acabo de desplegar | paso 3; para todo el corpus etiquetado, `pnpm test:real -- --env dev` ([sección 8](#8-suite-seria-cobertura-e-histórico-local)) |
| Quiero saber si un tipo procesa bien documentos reales | paso 5 ([sección 5.1](#51-modo-dataset-análisis-de-confianza)) |
| Quiero ir probando muchos tipos poco a poco | `pnpm test:e2e:init`, llenar carpetas, paso 6 ([sección 5.2](#52-suite-multi-tipo-por-carpetas)) |
| Toqué la etapa de autenticidad e integridad (`pdf_integrity`, `pdf_forensics`, `pixel_forensics`, `authenticity`…), los presets de `_shared.json` o los umbrales del bloque `authenticity` de un catálogo | `pnpm test:unit` y luego `pnpm test:autenticidad` (gratis los dos); si tocaste `vision_inspector` o los prompts, `pnpm test:autenticidad -- --vision` (~$0.08) |
| Toqué el registro de referencias o su API (`reference_registry`, `references_api`, `reference_embeddings`, `reference_index`) o el permiso `AllowReferenceWrite` de `manage-apps.mjs` | `pnpm test:unit` (gratis: `test_reference_registry.py`, `test_reference_embeddings.py`, `node/app-permissions.test.mjs`, `node/app-limits.test.mjs`) |
| Quiero saber si el embedding recupera la referencia correcta antes de activar `references.search` en un tipo | `pnpm refs:eval` (~$0.01; `-- --cache <archivo>` para no volver a pagar) |
| Quiero saber cuántos documentos reales caerían por captura física o por cada señal de integridad (guardado incremental, anotaciones, texto sobre el escaneo…) | `pnpm autenticidad -- --dir <carpeta> [--type TIPO]` (gratis, sin AWS; la integridad corre aunque el tipo no la declare, `--sin-integridad` la quita) |
| Un documento concreto salió mal y quiero ver por qué | `pnpm comparar -- archivo.pdf` ([sección 2](#2-comparar-expected-vs-real-de-un-documento-con-costo-script-simple)) |

### 0.4 Paso a paso: dataset de un tipo

1. Junta los documentos de **un solo tipo** en una carpeta (dentro de
   `test-docs/`, que está en `.gitignore`). Si el nombre del archivo trae el
   número de pedimento (`26 07 3435 6041353 CG.pdf`), el análisis lo usa para
   verificar el número extraído; si no, ese chequeo simplemente no aplica.
2. Corre el comando con el tipo declarado:

   ```bash
   API_KEY=<valor> pnpm test:e2e:pedimento -- --docs ./test-docs/full-dataset-pedimentos --doc-type PEDIMENTO_ERP --env prod
   ```

3. **Lee el plan antes de confirmar.** Se calcula localmente, sin costo:

   ```
   Tipo                          Modelo      Arch  Págs  OCR Llam  Textract   Bedrock   Lambda  Estimado     Tope
   PEDIMENTO_ERP                 minimax       10   421  129   11     $0.19   $0.0576  $0.0082     $0.26    $0.56
        26 07 3435 6041353 CG.pdf              17/139 pág a IA     2 llam  in  24,497 out  4,468   $0.0288 (tope $0.13) ~
        OPERADORA P2607 0823 6001841.pdf       3/30 pág a IA       1 llam  in   6,901 out  2,234   $0.0510 (tope $0.0714) ~
        …
   ¿Procesar 10 archivo(s) en PROD por ≈ $0.26 (tope $0.56)? [y/N]
   ```

   El estimado se calcula **como lo cobra el pipeline**, no con un $/página
   promedio (ver [COSTOS.md §9](COSTOS.md#9-costo-estimado-antes-de-procesar)):
   `Modelo` es el que correría el Lambda desplegado para ese tipo; `OCR`, las
   páginas sin capa de texto (Textract, exacto); `N/M pág a IA`, las páginas que
   deja la selección de páginas; `Llam`, las llamadas a Bedrock (chunks ×
   particiones del schema), con los tokens del prompt real. `~` marca archivos
   con páginas escaneadas, donde el texto OCR y la selección se estiman con la
   calibración. `Tope` es el peor caso: selección en fail-open y salida alta. Al
   pie se muestra el error típico medido contra documentos reales. Responde `N`
   si solo querías ver el plan: no se sube nada.
4. **Mientras corre** verás una línea por archivo al terminar cada uno
   (`✓ [3/10] archivo.pdf — APPROVED · negocio APROBADO · 212s · $0.0310`). Un
   `✗` imprime debajo el detalle de qué falló. Los expedientes de 80+ páginas
   son los que más tardan; `--concurrency` controla cuántos van a la vez
   (default 3).
5. **Al final** se imprime el análisis completo y se guarda en
   `resultados-pruebas/<env>/<fecha>-<commit>-e2e-dataset/analisis.md` (ver
   [0.7](#07-dónde-quedan-los-resultados)).

Primera vez con un dataset nuevo: agrega `--limit 2` para comprobar por
centavos que todo corre antes de lanzar la carpeta completa.

Lo mismo con la suite, que además deja `suite.md` y admite `-p` para ver solo
el plan: `pnpm test:e2e:suite PEDIMENTO_ERP -d ./test-docs/full-dataset-pedimentos -e prod`.
Ambos comandos comparan contra la corrida anterior del mismo tipo y la misma
carpeta, sin importar con cuál de los dos se hizo.

### 0.5 Paso a paso: varios tipos a la vez

```bash
pnpm test:e2e:init                          # 1. crea test-docs/e2e-suite/<TIPO>/ (gratis)
#                                             2. copia documentos a la carpeta de su tipo
pnpm test:e2e:suite -p -e prod              # 3. archivos y costo por tipo, sin ejecutar
API_KEY=<valor> pnpm test:e2e:suite -e prod # 4. confirma y ejecuta
```

Para **un solo tipo** (o un solo dataset), pásalo como argumento:

```bash
pnpm test:e2e:suite PEDIMENTO_ERP -e prod                                  # desde test-docs/e2e-suite/PEDIMENTO_ERP/
pnpm test:e2e:suite PEDIMENTO_ERP -d ./test-docs/full-dataset-pedimentos -e prod   # desde cualquier carpeta
pnpm test:e2e:suite curp ine -n 2                                          # varios tipos, 2 archivos c/u
```

- `pnpm test:e2e:init` muestra qué formato espera cada carpeta (`xml`,
  `xlsx/csv` o `pdf/png/jpg`) y cuántos archivos tiene ya. Puedes correrlo
  cuantas veces quieras: nunca borra ni sobrescribe. `pnpm test:e2e:init CURP INE`
  crea solo esas.
- Los tipos se escriben en mayúsculas o minúsculas; si uno está mal escrito
  el comando sale antes de gastar y sugiere el más parecido
  (`'pedimento_er' no es un tipo activo — ¿quisiste decir PEDIMENTO_ERP?`).
- `-d DIR` solo vale con exactamente un tipo.
- Sin tipos, las carpetas vacías salen como *pendientes* y no cuestan nada;
  llénalas cuando tengas documentos. Si pides un tipo cuya carpeta está vacía,
  sale con error (exit 2).
- `-c` (`--confirm-each`) pregunta antes de cada tipo (responder `N` salta ese
  tipo); `-n N` limita archivos por tipo.
- El resumen queda en `resultados-pruebas/<env>/<fecha>-<commit>-e2e-suite/suite.md`,
  con un `analisis.md` por tipo en su subcarpeta.

### 0.6 Cómo leer el resultado

Empieza por el **veredicto** (arriba del análisis); cada motivo viene listado:

| Veredicto | Significa | Qué hacer |
|---|---|---|
| `CONFIABLE` | infraestructura, reglas y coherencia bien en todo el dataset | nada; guarda la corrida como referencia para la siguiente |
| `CON RESERVAS` | procesa bien, pero hay algo que revisar: campos obligatorios con índice bajo, reglas del catálogo que no corren, cuadres que no dan, degradaciones, o valores distintos a la corrida anterior | lee cada `!` y decide si es aceptable; no bloquea |
| `NO CONFIABLE` | falla de infraestructura, ground truth incumplido, un gate `--min-*` violado, o **un documento aprobado con número de pedimento incoherente** | no confíes en ese tipo hasta corregirlo; el `✗` dice archivo y valor |

Después, en este orden:

1. **Sección 4, parámetros con índice más bajo**: los campos más débiles,
   por qué fallan y en qué archivos. El índice va de 0 a 100: un obligatorio
   que falta cuenta 0 y un opcional vacío no cuenta.
2. **Sección 7, coherencia interna**: detecta valores equivocados aunque el
   modelo diga 0.99 de confianza.
3. **Sección 6, reglas del motor**: las que fallan y las *inertes* (el catálogo
   las declara pero nunca se ejecutan, así que no están validando nada).
4. **Sección 10, contra la corrida anterior**: un valor que cambió sin que
   cambiaras nada es no-determinismo; si sí cambiaste algo, es su efecto.

La confianza por campo la reporta el propio modelo: sirve para ubicar los
campos débiles, no como prueba de que el valor es correcto. Esa prueba la dan
el ground truth y la coherencia interna.

**Códigos de salida** (para CI o scripts):

| Exit | Significa |
|---|---|
| `0` | terminó y el veredicto no es `NO CONFIABLE` |
| `1` | veredicto `NO CONFIABLE` (en la suite: algún tipo) o, con `--file`, `E2E FAIL` |
| `2` | no se pudo empezar: sin archivos (o un TIPO pedido con la carpeta vacía), tipo inexistente, `-d` sin exactamente un TIPO, sin `API_KEY` en prod, tope mayor que `-m`, o una opción inválida |
| `130` | cancelado en la confirmación o con Ctrl+C |

Sin terminal interactiva (CI, pipes) la confirmación no puede preguntar:
agrega `--yes`, y conviene acompañarlo de `--max-cost-usd`.

### 0.7 Dónde quedan los resultados

Todo bajo `resultados-pruebas/` (ignorado por git, archivos con permisos
privados: contienen datos aduanales, no los compartas fuera del equipo).

```
resultados-pruebas/<env>/<fecha>-<commit>-e2e-dataset/
  analisis.md               ← el reporte para leer
  analisis.json             ← lo mismo estructurado; la siguiente corrida se compara contra él
  run.json                  ← resumen de la corrida (estado, costo, duración)
  cases/001.json            ← resumen técnico de cada archivo (documentId, infraestructura, costo)
  cases/001.result.json     ← resultado completo del pipeline, con todos los valores extraídos
  logs/001-<archivo>.log    ← salida detallada de ese archivo

resultados-pruebas/<env>/<fecha>-<commit>-e2e-suite/
  suite.md                  ← resumen por tipo
  run.json
  <TIPO>/analisis.md …      ← misma estructura que un dataset, un directorio por tipo
```

Para comparar contra una corrida específica en vez de la anterior:
`--compare-with resultados-pruebas/prod/<corrida>/analisis.json`; para no
comparar, `--no-compare`.

### 0.8 Controlar el costo

- Mira siempre el plan; `N` en la confirmación no gasta nada.
- `-n N` (`--limit`) para probar barato: archivos del dataset, o por tipo en la suite.
- `-m X` (`--max-cost-usd`) cancela antes de empezar si el **tope** supera `X`.
- `-c` (`--confirm-each`) en la suite para aprobar tipo por tipo.
- XML, XLSX y CSV no usan IA ni OCR: cuestan $0.
- El costo **real** (tabla de auditoría de costos) sale en el análisis junto al
  estimado del plan: total, por servicio y tokens, y un aviso si se desvió más
  de 50%. `suite.md` lo pone por tipo.
- `pnpm costos:validar` mide qué tan bien estima contra todos los documentos
  reales cuyo archivo tienes en `test-docs/`, y con `--write` recalibra
  (`scripts/cost_calibration.json`). Gratis: solo lee DynamoDB/S3.
- Ctrl+C detiene la espera local, pero los documentos ya subidos terminan de
  procesarse (y de cobrarse) en AWS.

### 0.9 Sinopsis de los comandos E2E

Lo mismo que imprime `--help` (ahí vienen además ejemplos, variables de
entorno y estados de salida). `[]` es opcional, `...` se puede repetir, las
MAYÚSCULAS son valores.

```
pnpm test:e2e:pedimento -f ARCHIVO [-d DIR] [-t TIPO] [-e ENV] [-w SEG]
pnpm test:e2e:pedimento -d DIR -t TIPO [-e ENV] [-n N] [-j N] [-y] [-m USD]
                        [--min-approval-rate R] [--min-field-index I]

pnpm test:e2e:suite [-e ENV] [-p] [-y] [-c] [-n N] [-j N] [-m USD] [-r DIR] [TIPO ...]
pnpm test:e2e:suite TIPO -d DIR [-e ENV] [-p] [-y] [-n N] [-j N] [-m USD]

pnpm test:e2e:init [-r DIR] [TIPO ...]
```

| Corta | Larga | Valor | En | Qué hace |
|---|---|---|---|---|
| `-h` | `--help` | | todos | ayuda estilo man |
| `-e` | `--env` | `dev` \| `prod` | pedimento, suite | ambiente (default `dev`) |
| `-f` | `--file` | ARCHIVO | pedimento | un solo archivo de DIR |
| `-d` | `--docs` | DIR | pedimento, suite | carpeta del dataset (en la suite, solo con un TIPO) |
| `-t` | `--doc-type` | TIPO | pedimento, suite | tipo declarado (en la suite equivale a TIPO posicional) |
| `-n` | `--limit` | N | pedimento, suite | máximo de archivos (en la suite, por tipo; también `--limit-per-type`) |
| `-j` | `--concurrency` | N | pedimento, suite | archivos en paralelo (default 3) |
| `-y` | `--yes` | | pedimento, suite | no pedir confirmación |
| `-m` | `--max-cost-usd` | USD | pedimento, suite | cancela si el tope estimado supera USD |
| `-w` | `--wait` | SEG | pedimento, suite | espera máxima por archivo |
| `-p` | `--plan` | | suite | solo plan y costo |
| `-c` | `--confirm-each` | | suite | confirmar tipo por tipo |
| `-r` | `--root` | DIR | suite, init | raíz con una carpeta por tipo (default `test-docs/e2e-suite`) |
| | `--min-approval-rate` | R (0–1) | pedimento, suite | `NO CONFIABLE` si la tasa de `APPROVED` es menor |
| | `--min-field-index` | I (0–100) | pedimento, suite | `NO CONFIABLE` si un obligatorio tiene índice menor |
| | `--compare-with` | JSON | pedimento | compara contra ese `analisis.json` |
| | `--no-compare` | | pedimento, suite | no compara contra la corrida anterior |

Las opciones cortas se pueden combinar con las largas y en cualquier orden
respecto a los TIPO (`pnpm test:e2e:suite -e prod curp -n 2` es válido). Las
opciones largas de antes (`--plan`, `--only A,B`, `--limit-per-type`, …) siguen
funcionando.

### Todos los comandos

| Comando                                   | Qué prueba                                                                     | Requiere AWS            | Costo aprox. |
| ----------------------------------------- | ------------------------------------------------------------------------------ | ----------------------- | ------------ |
| `pnpm test:unit`                          | Motor de reglas, normalizadores, catálogo, el ground truth de pedimentos (esquema/nombres de campo válidos — ver [1](#1-antes-de-gastar-dinero-valida-el-ground-truth-gratis)) la forma del envelope (`documents[]`, qué campo se omite, el presupuesto de bytes) y el contrato de la API: parseo del body del upload, los tipos declarados (uno o varios separados por coma → `manual`/`guided`/`auto`, forma de la llave de S3, lista cerrada de candidatos del segmentador), normalización de `metadata`, filtros de `GET /documents` y la lectura de envelopes de cualquier versión (`tests/unit/node/`, corren con `node --test`) | No | $0 |
| `pnpm test:quality`                       | Toda la suite gratuita con JUnit, cobertura Python/Node, baseline anti-regresión e histórico local | No | $0 |
| `pnpm test:real -- --env dev`             | Primero la suite gratuita y después cada fixture etiquetado por API/S3/SQS/SFN/Lambdas/DynamoDB, contra ground truth | Sí | Muestra rango antes de confirmar |
| `pnpm test:encryption`                    | Sintetiza ambos stacks y exige 0 CMK en dev / 1 CMK compartida en prod | No (sí requiere Docker para bundling) | $0 |
| `pnpm comparar -- normal_pend.pdf`        | **Un solo documento**: valores esperados vs extraídos, campo por campo — ver [2](#2-comparar-expected-vs-real-de-un-documento-con-costo-script-simple) | Sí (Textract + Bedrock) | ~$0.02–0.35  |
| `pnpm test:pedimentos`                    | Pipeline directo contra todos los PDFs locales; compara los que están declarados en el ground truth | Sí (Textract + Bedrock) | Depende del corpus |
| `pnpm test:pedimentos -- --file B2401416` | Igual a `pnpm comparar` pero sin el prompt de confirmación (útil en CI/scripts) | Sí                      | ~$0.02–0.35  |
| `pnpm test:pedimentos -- --save /tmp/res` | Corre todo + guarda los JSON completos de resultado                            | Sí                      | —            |
| `pnpm test:autenticidad`                  | Etapa de autenticidad e integridad contra `tests/integration/expected_autenticidad.json` sobre `test-docs/nom35`: veredicto de estructura, `physical_capture`, motivos duros, que ninguna captura genuina se acuse de fondo artificial, los motivos de integridad que el ground truth fije (`integrity_reasons_include` / `integrity_reasons_exclude`, opcionales) y el invariante de que una captura genuina revisada a ojo nunca levanta un motivo DURO de integridad. Solo capas deterministas; memoria y referencias apagadas | No | $0 |
| `pnpm test:autenticidad -- --vision`      | Lo mismo + la capa de visión REAL sobre las carpetas ancla revisadas a ojo: exige que el modelo encuentre los elementos `required` en las capturas genuinas y lista lo que opinó de cada recorte | Sí (Bedrock) | ~$0.08, confirma antes |
| `pnpm autenticidad -- --dir <carpeta>`    | No es un test: corre las capas gratuitas — integridad incluida, aunque el tipo no la declare (`--sin-integridad` la quita) — sobre cualquier carpeta y dice cuántos documentos caerían por captura física y **por cada código de integridad**; `--detalle` imprime la cadena de guardados, fechas, software, firmas, anotaciones y texto sobre imagen. Para calibrar antes de activar un preset o endurecer un `on_fail` | No | $0 |
| `pnpm refs:eval [-- --dir <carpeta> --cache <archivo.json>]` | No es un test con aprobado/reprobado: mide si el embedding **recupera la referencia correcta**. Embebe cada recorte de `test-docs/nom35/samples` (una subcarpeta por proveedor, `firma*`/`sello*`/`logo*`), lo usa como consulta contra todos los demás y reporta, por elemento, recall@1/recall@3, el coseno contra la misma entidad vs. contra otra, y un `min_similarity` sugerido. Mismo código que la Lambda (`pipeline/reference_embeddings.py`). `--cache` guarda los vectores para repetir el análisis sin volver a pagar. Solo lectura: no escribe en AWS | Sí (Bedrock, Titan Multimodal Embeddings) | ~$0.01 (136 recortes = ~$0.008, 2026-10-06); $0 con `--cache` ya lleno |
| `pnpm test-local --type INE`              | Motor de validación con entidades simuladas                                    | No                      | $0           |
| `pnpm test-e2e -- doc.pdf TIPO_DOCUMENTO` | Contrato público desplegado completo: `POST /documents` → `PUT` S3 → polling de `GET /documents/{id}` hasta `FINISHED`; imprime el JSON final | Sí (stack desplegado) | centavos     |
| `pnpm test:e2e:pedimento -- --file normal_pend.pdf` | **E2E real de verdad**: sube por el stack desplegado (S3→SQS→SFN→Lambdas→DynamoDB/S3) Y compara contra el ground truth — ver [5](#5-e2e-real-contra-el-stack-desplegado) | Sí (stack desplegado) | ~$0.05–0.10 + minutos |
| `pnpm test:e2e:pedimento -- --docs <carpeta> --doc-type PEDIMENTO_ERP --env prod` | **Dataset completo** por el stack real + análisis de confianza: aprobados, índice por parámetro, reglas que fallan/inertes, coherencia del número de pedimento y del valor aduana, costo, cambios contra la corrida anterior — ver [5.1](#51-modo-dataset-análisis-de-confianza) | Sí (stack desplegado) | Muestra estimado y tope antes de confirmar |
| `pnpm costos:validar`                     | Qué tan bien estima el plan de costo: compara el estimado contra lo cobrado a cada documento real cuyo archivo está en `test-docs/`; `--write` recalibra — ver [COSTOS.md §9](COSTOS.md#9-costo-estimado-antes-de-procesar) | Solo lectura (DynamoDB/S3) | $0 |
| `pnpm test:e2e:init`                      | Crea `test-docs/e2e-suite/<TIPO>/` para cada tipo activo y muestra el formato esperado y cuántos archivos tiene cada carpeta — ver [5.2](#52-suite-multi-tipo-por-carpetas) | No | $0 |
| `pnpm test:e2e:suite [TIPO ...] [-d DIR] -e prod` | **Varios tipos de golpe** (o uno solo): una carpeta por tipo en `test-docs/e2e-suite/<TIPO>/` —o `-d DIR` para un solo tipo—, plan con costo por tipo, confirmación, y el mismo análisis por tipo — ver [5.2](#52-suite-multi-tipo-por-carpetas) | Sí (stack desplegado) | Muestra estimado y tope antes de confirmar |
| `pnpm schema:sensitivity -- --file normal_pend.pdf` | Experimento: qué tan preciso/impreciso se vuelve el resultado al cambiar el catálogo (hints, campos requeridos, tolerancias) — ver [6](#6-experimento-de-sensibilidad-del-schema) | Sí (parcial, ver abajo) | ~$0.05–0.15 |

La etapa de autenticidad tiene su propia verdad de terreno,
`tests/integration/expected_autenticidad.json` (validada gratis por
`tests/unit/test_expected_autenticidad_schema.py`): `verified_by_eye: true` marca
las carpetas ancla revisadas a mano; el resto es lo medido y sirve para detectar
regresiones. Sus pruebas unitarias simulan el modelo de visión y DynamoDB, así que
toda la etapa — incluido el contrato con el catálogo real de `CARTA_NOM35` y el
motor de reglas — se prueba sin AWS
(`tests/unit/test_authenticity_stage.py`). La capa de integridad y sus piezas
tienen las suyas, también gratis: `tests/unit/test_pdf_integrity.py` (cada
manipulación reproducida con pypdf/Pillow: guardado incremental, linearizado,
fechas, editor, anotaciones, formularios, firma rota, JavaScript, texto sobre
escaneo, imagen sobre texto, EXIF/APP13/PNG/XMP), `test_pdf_forensics_overlay.py`
(orden de pintado y posición de cada string en el walker),
`test_pixel_forensics_ela.py` (el ELA nunca acusa una página JPEG genuina y se
abstiene en lo que no es JPEG), `test_field_presets.py` (`$presets`: inyección,
override por campo, presets del repo con señales que la etapa conoce) y
`test_authenticity_integrity.py` (capas bajo demanda: un tipo que solo pide
`integrity.*` jamás rasteriza ni llama a un modelo, `expected_capture`,
`trigger: on_suspicion`, `page_review`, bytes originales tras el truncado).
Guía: [AUTENTICIDAD_Y_REFERENCIAS.md](AUTENTICIDAD_Y_REFERENCIAS.md).

El **registro de referencias y su API REST** (`/references`) también se prueban
enteros sin AWS, dentro de `pnpm test:unit`:

- `tests/unit/test_reference_registry.py` — la revisión del recorte (cada
  código de rechazo, la cabecera que declara una imagen enorme, el formato real
  leído de los bytes), la clave que se rechaza con su forma sugerida, los
  `scope`/`kind` que salen del catálogo, y la API completa contra una tabla, un
  S3 y un índice simulados: permiso `AllowReferenceWrite` en todas las rutas y
  falla cerrado, alta en dos pasos, reserva sin confirmar, `409
  UPLOAD_NOT_FOUND`, `422` que no deja nada, idempotencia de `confirm`,
  duplicado / misma tinta con `force`, validaciones de entrada, tope por
  elemento, detalle con `imageUrl`, baja, `404` de lo vencido, reintento del
  índice, y que el alta directa del CLI escribe lo mismo que la API. Es además
  el mejor catálogo de ejemplos de cada respuesta.
- `tests/unit/test_reference_embeddings.py` — preprocesado del recorte, el
  embedding normalizado y lo que factura, que un fallo del modelo es «sin
  vector» y nunca una excepción, el request/response de Titan, que todo
  proveedor registrado tiene tarifa, y el índice vectorial (metadatos
  filtrables, búsqueda por `scope`/elemento/modelo, similitud en vez de
  distancia, errores que no truenan).
- `tests/unit/node/app-permissions.test.mjs` — `AllowReferenceWrite` aparece en
  `manage-apps.mjs` en los mismos sitios que `AllowMetadataWrite`, los flags
  existen en `register`/`reconfigure` y en `grant`/`deny`, y rotar una key
  conserva el permiso. `tests/unit/node/app-limits.test.mjs` sigue exigiendo
  que toda ruta no-GET del stack esté en `WRITE_METHODS` y toda ruta GET en
  `DEV_READ_METHOD_PATHS` — las de `/references` incluidas.

Lo único de pago es `pnpm refs:eval` (tabla de arriba), que no es parte de
ninguna suite.

La verdad de terreno de los tests de pedimentos vive en
`tests/integration/expected_pedimentos.json` — al agregar documentos de
prueba nuevos, añade ahí sus valores esperados (nombre exacto del campo tal
como está en el árbol activo `catalogs/**`, no un alias inventado).

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

## 4. E2E genérico a través del API público

Este es el smoke test para cualquier tipo y cualquier documento real, sin
ground truth específico:

```bash
pnpm test-e2e -- ./ruta/factura.pdf FACTURA_FLETE_EXTRANJERO
pnpm test-e2e -- ./ruta/pedimento.pdf PEDIMENTO_ADUANAL --env prod
```

El archivo y el tipo son los dos parámetros requeridos. También se aceptan
como opciones (`--file` y `--type`/`--doc-type`). El ambiente por default es
`dev`; `--stack NOMBRE` y `--endpoint URL` permiten apuntar a un destino
explícito. `--timeout 1200` controla solamente el límite total de espera, no
un tiempo fijo que el cliente deba adivinar.

El test recorre exactamente el contrato que usaría una integración externa:

1. registra el trabajo con `POST /documents`, incluyendo `documentType`;
2. sube los bytes por el `PUT` prefirmado, sin `x-api-key`;
3. consulta inmediatamente `GET /documents/{id}`;
4. ante `202`, respeta `Retry-After` y aplica backoff exponencial con jitter;
5. termina al recibir `200` y exige `processingStatus: "FINISHED"` más un
   `result` no nulo.

Los mensajes de avance se escriben en `stderr`. `stdout` contiene únicamente
el cuerpo JSON del `GET` final, por lo que se puede guardar o procesar sin
limpiarlo:

```bash
pnpm --silent test-e2e -- ./ruta/factura.pdf FACTURA_FLETE_EXTRANJERO > resultado.json
pnpm --silent test-e2e -- ./ruta/factura.pdf FACTURA_FLETE_EXTRANJERO | jq '.result.documents'
```

La salida final conserva la respuesta del API completa; este ejemplo abrevia
`fields` para mostrar solamente la estructura relevante:

```jsonc
{
  "documentId": "e18d014a-...",
  "processingStatus": "FINISHED",
  "workflowStatus": "complete",
  "documentType": "FACTURA_FLETE_EXTRANJERO",
  "result": {
    "envelopeVersion": 2,
    "documentClassification": "FACTURA_FLETE_EXTRANJERO",
    "validation": { "status": "APROBADO", "reviewRequired": false },
    "documents": [{
      "index": 1,
      "documentType": "FACTURA_FLETE_EXTRANJERO",
      "status": "APPROVED",
      "fields": {
        "NUMERO_FACTURA": { "value": "SJ191703", "confidence": 0.98, "valid": true },
        "TOTAL": { "value": 14612.4, "confidence": 0.93, "valid": true }
      }
    }]
  }
}
```

`--full` agrega `?full=1` al polling y conserva también `fullResult` en la
salida. Un resultado de negocio `RECHAZADO` sigue siendo una ejecución E2E
correcta si el estado técnico es `FINISHED`; `FAILED`, timeout, `404` o cualquier
respuesta distinta de `200`/`202` hacen fallar el comando.

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
`POST /documents` al stack desplegado, espera la ejecución real de Step
Functions, y valida:

1. **Infra**: que la ejecución termine en `SUCCEEDED` (no timeout/error), y
   que el archivo se haya movido de `raw/` a `processed/` (o `failed/`).
2. **Ground truth**: la misma comparación campo por campo que `pnpm
   comparar`, pero sobre el resultado que salió de verdad del pipeline
   desplegado — reutiliza `_check_doc_expectation`/`_print_engine_checks` de
   `test-pedimentos.py` en vez de duplicar la lógica. Solo se aplica cuando
   el archivo está declarado **para el mismo `--doc-type`**: `normal_pend.pdf`
   tiene ground truth de `PEDIMENTO_ADUANAL`, así que corrido como
   `PEDIMENTO_ERP` se reporta "ground truth n/a" en vez de fallar por tipo.
   Un archivo sin ground truth ya no aborta: se valida la infraestructura.

`prod` exige `API_KEY` exportado y el script lo verifica antes de subir nada.

Corrida real verificada (2026-07-28, `normal_pend.pdf`, stack
`DocumentValidationStack-Dev`): Step Functions `SUCCEEDED` en 64s, archivo
movido correctamente a `processed/{documentId}-.../normal_pend.pdf`
(el `documentId` de esa corrida incluía el nombre de archivo concatenado —
bug arreglado después en `patches/default-sqs-consumer-index.py`; hoy la
forma es `processed/{documentId}/normal_pend.pdf`, sin el nombre duplicado
dentro del `documentId` ni el anidado extra que causaba), y el
mismo resultado campo por campo que la corrida en proceso — confirma que el
Lambda desplegado y el código local están en sync (sin drift de deploy).

### 5.1 Modo dataset: análisis de confianza

```bash
pnpm test:e2e:pedimento -- --docs ./test-docs/full-dataset-pedimentos --doc-type PEDIMENTO_ERP --env prod
pnpm test:e2e:pedimento -- --docs <carpeta> --doc-type PEDIMENTO_ERP --limit 2        # prueba barata
pnpm test:e2e:pedimento -- --docs <carpeta> --doc-type PEDIMENTO_ERP --min-approval-rate 0.8 --min-field-index 90
```

Sin `--file`, sube **todos** los archivos de la carpeta (pdf/png/jpg/jpeg/xml/xlsx/csv),
`--concurrency` a la vez (default 3), cada uno con exactamente las mismas
validaciones de infraestructura del caso único. Antes de gastar muestra el
**plan de costo** por archivo, calculado con el mismo código del Lambda: modelo
desplegado, páginas OCR, selección de páginas, llamadas y tokens del prompt
real, más un **tope** para el peor caso ([COSTOS.md §9](COSTOS.md#9-costo-estimado-antes-de-procesar)).
Nada corre sin `[y/N]`; `--yes` lo omite y `--max-cost-usd` cancela si el tope se
pasa. Cada caso guarda su estimado, y el análisis final lo compara contra lo que
registró la auditoría de costos (total, Textract, Bedrock, Lambda y tokens).

Al terminar imprime (y deja en `resultados-pruebas/<env>/<runId>/analisis.{md,json}`,
junto con cada caso, su resultado completo y su log) un análisis en secciones:

| Sección | Qué responde |
|---|---|
| **Veredicto** | `CONFIABLE`, `CON RESERVAS` o `NO CONFIABLE`, con cada motivo |
| 1. Ejecución | infra OK, Step Functions, ground truth, tiempo p50/p95, costo total / por archivo / por página, páginas OCR, versión de catálogo desplegada vs local |
| 2. Resultado de negocio | documentos por estado (`APPROVED`/`REVIEW_REQUIRED`/`REJECTED`…), tasa de aprobación, validación pública (`APROBADO`…), confianza por documento |
| 3. Confianza por parámetro | por cada campo del catálogo — también por fila de arreglo, `FACTURAS[].Factura` —: % llenado, % válido, confianza media/mínima/p10/σ, lecturas bajo `min_confidence`, origen (modelo/QR/derivado/normalizado) e **índice** |
| 4. Índice más bajo | los 10 parámetros más débiles con sus motivos de invalidez y en qué archivos |
| 5. Arreglos | filas por documento (mín/media/máx) y documentos con el arreglo vacío |
| 6. Reglas del motor | reglas que fallan (por regla+campo) y **reglas inertes**: declaradas en el catálogo pero `skipped` en cada documento (hoy `in` y `greater_than`) |
| 7. Coherencia interna | sin ground truth: número = año+aduana+patente+consecutivo; referencia del **nombre del archivo** (`26 07 3435 6041353 CG.pdf`) vs la extraída; VALOR ADUANA = valor comercial + incrementables (±5, la fórmula de `PEDIMENTO_ADUANAL` que en `PEDIMENTO_ERP` no puede correr) |
| 8. Distribución de valores | códigos que se repiten (`Cve_Pedimento`, `Tipo_Pago`, `INCOTERM`…) para ver valores fuera de catálogo |
| 9. Por archivo | estado, negocio, ground truth, coherencia, páginas, tiempo, costo y señales de degradación (fallback de modelo, selección de páginas en fail-open, truncado) |
| 10. Contra la corrida anterior | la última del mismo tipo sobre una carpeta del mismo nombre (o `--compare-with`): cambios de estado, **valores que cambiaron** y índices que se movieron ≥5 puntos |

**Índice por parámetro** = confianza media en las ocurrencias donde el campo
aplica: presente, o inválido. Un obligatorio faltante cuenta 0, un inválido se
topa en `min_confidence` (igual que hace el motor) y un opcional vacío no
cuenta. La confianza es auto-reportada por el modelo: el índice sirve para
encontrar los parámetros débiles, no para probar exactitud — por eso el
veredicto se apoya también en reglas, coherencia y ground truth.

**Veredicto.** `NO CONFIABLE` (exit 1): falla de infraestructura, ground truth
incumplido, ningún documento del tipo, un gate `--min-*` violado, o un documento
**APROBADO con número de pedimento incoherente** — el peor caso, porque llega
al ERP sin que nadie lo revise. La misma incoherencia en un documento que ya
quedó en revisión, un cuadre aritmético que falla, un obligatorio con índice
bajo `min_confidence`, reglas inertes, degradaciones, drift de catálogo o
valores distintos a la corrida anterior son `CON RESERVAS` (exit 0).

La referencia del nombre del archivo solo se evalúa en tipos que declaran los
componentes del número de pedimento; los nombres se resuelven con
`pedimento_role`, así que funciona igual con `PEDIMENTO_ADUANAL` y `PEDIMENTO_ERP`.

### 5.2 Suite multi-tipo por carpetas

```bash
pnpm test:e2e:init                                  # una carpeta vacía por cada tipo activo
pnpm test:e2e:init PEDIMENTO_ERP CURP               # solo esas
pnpm test:e2e:suite -p -e prod                      # archivos y costo por tipo, sin ejecutar
pnpm test:e2e:suite -e prod                         # plan → confirmación → ejecuta
pnpm test:e2e:suite -e prod -c                      # confirma tipo por tipo
pnpm test:e2e:suite PEDIMENTO_ERP -e prod           # un solo tipo
pnpm test:e2e:suite PEDIMENTO_ERP -d <carpeta>      # un solo tipo con un dataset en otra carpeta
pnpm test:e2e:suite PEDIMENTO_ERP CURP -n 2 -m 3    # dos tipos, 2 archivos c/u, tope $3
```

Estructura (`test-docs/` está en `.gitignore`):

```
test-docs/e2e-suite/
  PEDIMENTO_ERP/     ← nombre EXACTO del TYPE_ID en catalogs/base o catalogs/custom
  CURP/
  INE/
  CFDI_GASTO_TERCERO_XML/
```

`pnpm test:e2e:init` crea las carpetas que falten (una por tipo activo de
`catalogs/base` y `catalogs/custom`) y lista cada una con su nombre legible, el
formato que espera — deducido de la forma del catálogo: `xpath` → XML,
`cell`/`column` → XLSX/CSV, el resto PDF o imagen — y cuántos archivos tiene ya.
Nunca sobrescribe ni borra: al agregar un tipo al catálogo, correrlo de nuevo
solo crea su carpeta. `--root` cambia la raíz.

Cada archivo se sube **declarado** como el tipo de su carpeta. Las carpetas
vacías salen como *pendientes* (sin costo) — se llenan poco a poco y la
siguiente corrida las incluye solas —, y una carpeta que no es un tipo activo
se reporta como ignorada. El plan muestra por tipo archivos, páginas, OCR,
archivos con ground truth, estimado, tope y de dónde salió la tarifa; XML/XLSX/CSV
van por pipelines deterministas y cuestan $0.

Cada tipo corre con el mismo motor del modo dataset (mismo análisis, misma
comparación contra su corrida anterior) y deja `resultados-pruebas/<env>/<runId>/<TIPO>/analisis.md`;
al final `suite.md` resume veredicto, aprobación, campo obligatorio más débil,
reglas que fallan/inertes y costo estimado vs real por tipo. Exit 1 si algún
tipo queda `NO CONFIABLE`.

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
| `test_field_names_exist_in_catalog` falla | Nombre de campo con typo en `expected_pedimentos.json`, o campo que no existe en el catálogo | Revisa `catalogs/base/aduanas/PEDIMENTO_ADUANAL.json` (o el tipo correspondiente) y corrige el nombre exacto |
| `no expectations defined for this file` | El PDF no tiene entrada en `expected_pedimentos.json` | Normal si es un documento sin ground truth aún — agrégalo (ver sección 3) |
| `PROCESSING ERROR` al correr `pnpm comparar` | Error real del pipeline (credenciales AWS, modelo no habilitado, etc.) | Revisa que `aws sts get-caller-identity` funcione y que el modelo Bedrock esté habilitado (ver `GUIA_DESPLIEGUE_Y_USO.md` sección 1) |
| `✗` en un campo pero el valor se ve razonable | Puede ser tolerancia de formato (mayúsculas/espacios en strings, 0.01 en números) | Revisa la nota impresa junto al `✗`: muestra el valor exacto extraído vs esperado |
| `test:e2e:pedimento` se queda esperando y nunca aparece la ejecución | El SQS consumer no disparó Step Functions, o la Lambda de consumo falló | Revisa la SQS DLQ y los logs del consumer Lambda en CloudWatch |
| `test:e2e:pedimento` dice `status != SUCCEEDED` | Falla real de infraestructura (timeout, error no atrapado) — justo lo que este test existe para detectar y que `pnpm comparar` no puede ver | `pnpm track --execution-arn <arn>` (el comando se imprime en la salida) |
| `prod exige x-api-key: exporta API_KEY` | Corriste contra prod sin `API_KEY` en el ambiente; en prod nunca se toma sola | `API_KEY=<valor> pnpm ...` o `set -gx API_KEY <valor>` en fish ([0.1](#01-preparación-una-sola-vez)) |
| `POST /documents → HTTP 403` en todos los archivos | Llave equivocada, revocada o de otro ambiente | `pnpm apps:list -- --env prod` para ver las apps activas; revisa que `--env` coincida con la llave |
| `Sin terminal interactiva para confirmar el gasto: agrega --yes` | Se corrió desde CI, un pipe o con stdin redirigido | `--yes`, idealmente con `--max-cost-usd` |
| `No hay archivos soportados … en <carpeta>` | Ruta equivocada o extensiones no soportadas | Ruta relativa a la raíz del repo; formatos: pdf, png, jpg, jpeg, xml, xlsx, csv |
| `No pude leer la tabla de costos … uso tarifas de referencia` | Tus credenciales no pueden leer `document-validation-costs[-dev]` | No bloquea: el estimado usa COSTOS.md; el costo real sigue apareciendo al final si la Lambda lo registró |
| `Ignoradas (no son un TYPE_ID activo): X` en la suite | La carpeta no se llama exactamente como un tipo del catálogo | Renómbrala; `pnpm test:e2e:init` lista los nombres válidos |
| `ground truth declarado para PEDIMENTO_ADUANAL, no para PEDIMENTO_ERP` | El archivo tiene ground truth, pero de otro tipo | Informativo, no es falla; agrega una expectativa para el tipo que corres si quieres compararlo |
| `NO CONFIABLE` por referencia del nombre de archivo, pero el nombre no es el pedimento | El nombre trae 2-2-4-7 dígitos que no son el número de pedimento | Renombra el archivo; el chequeo lee el número del nombre |
| La corrida termina en `NO CONFIABLE` sin fallas de infraestructura | Un documento aprobado con número incoherente, ground truth incumplido o un gate `--min-*` | El veredicto lista cada motivo con archivo y valor; abre `logs/NNN-<archivo>.log` y `cases/NNN.result.json` de ese caso |

## 8. Suite seria, cobertura e histórico local

La entrada habitual es gratuita:

```bash
pnpm test:quality
```

Genera una carpeta privada e inmutable bajo
`resultados-pruebas/local/<fecha>-<commit>-quality-free/` con `run.json`,
JUnit de Python y Node, LCOV de Node, cobertura JSON/HTML de Python y logs. La
carpeta está ignorada por Git porque los artefactos de una prueba real pueden
contener información aduanal. `tests/quality-baseline.json` conserva la
cobertura aceptada; una caída mayor a 0.5 puntos en líneas o ramas falla el
comando. Para aceptar deliberadamente una nueva base:

```bash
pnpm test:quality -- --update-baseline
```

La corrida pagada nunca es automática:

```bash
pnpm test:real -- --env dev
pnpm test:real -- --env prod --allow-prod
# no interactivo en prod requiere además ALLOW_PROD_TESTS=1 y --yes
```

Antes de gastar valida que todos los archivos declarados existan y que su
SHA-256 coincida con el revisado en `expected_pedimentos.json`. Cada upload se
marca con `test-run-id`/`test-suite`; el dashboard puede separarlo del tráfico
operativo. Para cada documento comprueba infraestructura, respuesta pública,
movimiento a `processed/`, descarga byte a byte, auditoría de costo, búsqueda
por unique key y ground truth. El primer caso también prueba PATCH de metadata.

`pnpm reporte` incorpora las corridas de cada ambiente leído (dev y prod
sin `--env`, o solo el de `-- --env dev|prod`) y las gratuitas locales. La sección **Calidad comprobada** no confunde tres señales:
aprobación de reglas, confianza declarada por el modelo y exactitud contra
valores leídos por una persona. Cada paquete incluye `snapshot.json`; se puede
regenerar sin AWS con `pnpm reporte -- --snapshot ruta/snapshot.json`. En el
HTML, pasar el cursor (o enfocar con teclado) sobre cada barra, segmento o
punto de las gráficas abre el desglose del elemento seleccionado, con el valor
sin el redondeo visual y su contexto.

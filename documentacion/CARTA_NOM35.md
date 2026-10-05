# Carta NOM-035: guía práctica

Cómo **probar, operar y entender** la validación automática de la carta NOM-035
que entrega cada promotor (`CARTA_NOM35`). Es la guía de «cómo lo uso»; el «cómo
funciona por dentro» y el detalle de referencias está en
[`AUTENTICIDAD_Y_REFERENCIAS.md`](AUTENTICIDAD_Y_REFERENCIAS.md).

**Qué valida, en una línea:** que la carta sea una hoja **impresa, firmada con
tinta, sellada y escaneada** — no un Word con la firma y el sello pegados — y que
diga lo que debe decir (dirigida a Operadora Futurama, menciona la NOM-035,
vigente).

---

## 1. Pruébalo en 3 pasos (de gratis a caro)

No hace falta desplegar nada para los pasos 1 y 2.

### Paso 1 — Gratis, sin AWS, 5 segundos

```bash
pnpm autenticidad -- --file test-docs/nom35/samples/10037/doc1.pdf --detalle   # una carta fabricada
pnpm autenticidad -- --file test-docs/nom35/samples/10021/doc3.pdf --detalle   # una genuina
pnpm autenticidad -- --dir test-docs/nom35                              # toda la carpeta
```

| Archivo | Debe salir | Por qué |
|---|---|---|
| `10037/doc1.pdf` | `DIGITAL` + `TEXTO_NATIVO_CON_IMAGEN_PEGADA` | PDF de Word: 821 caracteres de texto real y una imagen de 340×332 que cubre 8 % de la página (el bloque firma+sello) |
| `10021/doc3.pdf` | `FISICO`, origen `ESCANER`, fondo `natural` | Escaneo de multifuncional: cero texto, una imagen a página completa |

Esto corre solo las capas deterministas (integridad del archivo, estructura del
PDF y pixeles). **Pásale cualquier carta tuya** con `--file`: en 5 segundos sabes
si el sistema la ve como papel real o como documento digital, sin gastar nada. Al
final te dice cuántas de la carpeta caerían si la regla ya rechazara, y cuántas
caerían por cada señal de integridad.

> **Sobre la columna `integridad`.** El catálogo de `CARTA_NOM35` declara ahora
> `"expected_capture": "FISICO"` de forma explícita (es lo que siempre exigió) y
> **no** lleva el preset `INTEGRIDAD_PDF` a propósito: medido el 2026-09-24
> sobre las 49 cartas reales, lo único que encontraría (`IMAGEN_SOBRE_TEXTO_NATIVO`
> en 7 cartas de Word con la firma pegada) ya lo manda a revisión
> `CAPTURA_FISICA`, y ningún archivo editado a mano ha pasado por la capa.
> Activarlo hoy solo duplicaría el motivo.
> Para volver a medir antes de decidir:
> `pnpm autenticidad -- --dir test-docs/nom35/samples` (la integridad corre ahí
> aunque el tipo no la declare; detalle en
> [`AUTENTICIDAD_Y_REFERENCIAS.md`](AUTENTICIDAD_Y_REFERENCIAS.md) §6).

### Paso 2 — Una carta completa, en local (~$0.015 cada una)

Corre el pipeline **real** desde tu máquina con tus credenciales de AWS: Textract,
extracción de campos y la capa de visión. Sin stack desplegado.

```bash
pnpm procesar test-docs/nom35/samples/10021/doc3.pdf CARTA_NOM35 --meta proveedor=10021
pnpm procesar test-docs/nom35/samples/10037/doc1.pdf CARTA_NOM35 --meta proveedor=10037

# Con las referencias YA registradas en dev (paso 3), en solo lectura:
pnpm procesar test-docs/nom35/fake-nom35/11734/doc1.pdf CARTA_NOM35 --meta proveedor=11734 --refs-env dev
```

**`--refs-env dev|prod`** hace que la corrida local use las referencias
registradas (y el historial de huellas) del stack desplegado, en **solo lectura**:
compara, pero no deja huellas de tus pruebas en la tabla real. **Sin ese flag, en
local no hay contra qué comparar** y los campos `*_COINCIDE_REFERENCIA` salen
vacíos — no es un error, es que `--meta proveedor=N` solo dice *de quién* buscar
referencias, no de dónde sacarlas.

> `pnpm procesar` usa posicionales (`ARCHIVO TIPO`) y **no** lleva `--`. Los demás
> comandos de esta guía sí: `pnpm <comando> -- --flag`.

El resultado queda en `resultados/CARTA-NOM35/<archivo>/001/` (002, 003… en cada
corrida; nunca se sobreescribe):

| Archivo | Qué mirar |
|---|---|
| `_resumen.md` | Estatus, costo por servicio (la visión sale en su propia línea, «Bedrock autenticidad (visión)»), validez campo por campo |
| `<archivo>.json` | El resultado completo. Busca el bloque `"authenticity"`: `verdict`, `layers`, `reasons` |
| `_prompt_input.json` / `_prompt_output.json` | Qué se le preguntó al modelo de visión y qué contestó (las imágenes salen como su huella, no sus bytes) |

Qué esperar:

| Carta | Estatus | Campos clave |
|---|---|---|
| `10021/doc3.pdf` (genuina) | `APROBADO`, o `REQUIERE_REVISION` si el modelo duda de algo | `CAPTURA_FISICA: true`, `FIRMA_PRESENTE` / `SELLO_PRESENTE` / `LOGO_PRESENTE: true`, `VEREDICTO_AUTENTICIDAD: AUTENTICO` |
| `10037/doc1.pdf` (fabricada) | `REQUIERE_REVISION` (modo observación, §5) | `CAPTURA_FISICA: false`, `FIRMA_SIN_INSERCION: false`, `VEREDICTO_AUTENTICIDAD: NO_AUTENTICO` |

**Sin `--refs-env`**, en local los campos `*_COINCIDE_REFERENCIA`,
`FIRMA_SELLO_NO_REUTILIZADOS` y `CARTA_NO_DUPLICADA` salen **vacíos** (no hay tabla
de huellas). Vacío = «no se pudo determinar», y no afecta el estatus.

Resultado **medido** con una carta fabricada real (`fake-nom35/11734/doc1.pdf`,
hecha en Word, con sus propios recortes registrados como referencia, $0.0161):

| Campo | Valor | Lectura |
|---|---|---|
| `CAPTURA_FISICA` | `false` | PDF de Word: 874 caracteres de texto + 4 imágenes insertadas |
| `FIRMA_SIN_INSERCION` · `SELLO_SIN_INSERCION` | `false` · `false` | Firma y sello son **objetos de imagen del PDF** — prueba directa, no opinión |
| `FIRMA_` · `SELLO_` · `LOGO_COINCIDE_REFERENCIA` | `true` · `true` · `true` | Corresponden a lo registrado… porque las referencias se recortaron de esta misma carta |
| `SELLO_CORRESPONDE_EMISOR` | `true` | El sello dice «RDE PROMOCIONES, S.A. DE C.V.», igual que la carta |
| Estatus | `REQUIERE_REVISION` · `NO_AUTENTICO` | Modo observación (§5) |

La lección de ese caso: **coincidir con la referencia no vuelve genuina a una
carta**. Son preguntas distintas — «¿es la firma de quien dice?» y «¿la firmaron
en papel?» — y por eso las referencias deben salir de una carta en papel (§4).

### Paso 3 — Todo junto, contra el stack de dev

```bash
# 1. Desplegar (una vez). Crea la tabla de huellas; te la muestra y pide confirmar.
pnpm run deploy:dev
pnpm sync:one -- --type CARTA_NOM35

# 2. Dar de alta las referencias de un proveedor GENUINO
pnpm refs:register -- --key 10021 --dir test-docs/nom35/samples/10021 --dry-run   # ensayo
pnpm refs:register -- --key 10021 --dir test-docs/nom35/samples/10021             # de verdad
pnpm refs:list -- --key 10021

# 3. Subir cartas con su proveedor
pnpm enviar test-docs/nom35/samples/10021/doc3.pdf CARTA_NOM35 --meta proveedor=10021
pnpm enviar test-docs/nom35/samples/10037/doc1.pdf CARTA_NOM35 --meta proveedor=10037
pnpm enviar test-docs/nom35/samples/10037/doc2.pdf CARTA_NOM35 --meta proveedor=10037
pnpm enviar test-docs/nom35/samples/10037/doc3.pdf CARTA_NOM35 --meta proveedor=10037

# 4. Ver resultados y costo real
pnpm resultado -- --id <documentId> --wait
pnpm costos -- --id <documentId>
```

La prueba estrella son las tres cartas de `10037`: traen **la misma imagen** de
firma+sello en cartas de tres empleados distintos.

| Carta | Debe salir | Motivo |
|---|---|---|
| `10037/doc1.pdf` (la primera) | `REQUIERE_REVISION` | Documento digital; aún no hay otra carta con qué compararla |
| `10037/doc2.pdf` y `doc3.pdf` | **`RECHAZADO`** | `FIRMA_REUTILIZADO_EN_OTRA_CARTA` / `SELLO_REUTILIZADO_EN_OTRA_CARTA`: la misma imagen bajo la firma de otro empleado |
| `10021/doc3.pdf` | `APROBADO` con `authenticity.referencesFound: true` | Captura genuina y corresponde a sus referencias registradas |
| Volver a subir `10021/doc3.pdf` | mismo estatus + aviso `CARTA_YA_RECIBIDA` | Misma carta (empleado + fecha): duplicado, no fraude |

---

## 2. Si algo sale raro

| Síntoma | Causa y qué hacer |
|---|---|
| `error: unrecognized arguments` al usar `pnpm refs:*` / `autenticidad` | Falta el `--` separador: `pnpm refs:register -- --key …`. (`pnpm procesar` es la excepción: sin `--`) |
| `refs:register`: «el stack no expone `DocumentBucketName`» | Aún no despliegas. Usa `--dry-run` (funciona sin stack) o corre `pnpm run deploy:dev` |
| **Todas** las cartas en revisión con `AUTENTICIDAD_EVALUADA: false` | La capa de visión no corre. Casi siempre: el modelo de visión (Claude Haiku 4.5) no tiene «Model access» en Bedrock en esa cuenta/región. El error exacto está en `authenticity.visionError` del `.json` |
| `FIRMA_PRESENTE: true` pero `FIRMA_SIN_INSERCION` y `FIRMA_COINCIDE_REFERENCIA` vacíos (`bboxSource: "MODELO"` en el `.json`) | El modelo vio la firma en la hoja pero no la reconoció en ninguna de las regiones que el código propuso, así que no hay recorte confiable y no se examina ni se compara (recortar «a ojo» del modelo fabricaría un falso «no coincide»). Suele ser una firma muy tenue o pegada a un bloque de texto. Guarda el caso: se ajusta `pixel_forensics.propose_regions` |
| El modelo no encuentra el sello, o dice que una firma genuina parece pegada | Ajuste de prompt (`pipeline/vision_inspector.py`). Guarda el `.json` y los `_prompt_*.json` de ese caso: son la evidencia para afinarlo. Mientras tanto no rechaza nada — lo que sale de la visión solo manda a revisión |
| `referencesFound: false` aunque registraste referencias | Subiste sin `--meta proveedor=<n>`, o registraste en el otro ambiente (dev y prod no comparten) |
| Una captura genuina sale `INDETERMINADO` | Es un escaneo pegado dentro de Word. No se acusa; deciden visión y memoria. Pide el PDF directo del escáner |
| Aviso «etiqueta '1' no dice nada» al registrar | El archivo se llama `firma1.png`. No bloquea, pero renómbralo a quién firma o qué sucursal (§4) |

Más casos: [`AUTENTICIDAD_Y_REFERENCIAS.md`](AUTENTICIDAD_Y_REFERENCIAS.md) §7.

---

## 3. Qué extrae y qué valida

Campos **leídos de la carta** (Textract + modelo de texto):

| Campo | Obligatorio | Regla |
|---|---|---|
| `DESTINATARIO` | sí | Debe ser Operadora Futurama |
| `NOMBRE_EMPLEADO` | sí | — |
| `EMPRESA_EMISORA` | sí | El sello debe nombrarla (`SELLO_CORRESPONDE_EMISOR`) |
| `FECHA_CARTA` | sí | Más de 1 año → revisión |
| `NOMBRE_FIRMANTE` | sí | — |
| `REFERENCIA_NOM035` | sí | Debe mencionar «NOM-035» / «NORMA 035» |
| `FECHA_INGRESO`, `PUESTO`, `CARGO_FIRMANTE` | no | — |

Campos **calculados por la etapa de autenticidad** y qué le hace cada uno al
documento cuando sale en falso:

| Campo | En falso significa | Efecto |
|---|---|---|
| `CAPTURA_FISICA` | PDF digital con imágenes insertadas | revisión hoy → rechazo (§5) |
| `FIRMA_SELLO_NO_REUTILIZADOS` | La misma firma/sello en la carta de **otro** empleado | **rechazo** |
| `FIRMA_PRESENTE` · `SELLO_PRESENTE` · `LOGO_PRESENTE` | No se ve el elemento | revisión |
| `FIRMA_SIN_INSERCION` · `SELLO_SIN_INSERCION` | Es una imagen incrustada, o se ve pegada/impresa | revisión |
| `SELLO_CORRESPONDE_EMISOR` | El sello nombra a otra empresa | revisión |
| `FIRMA_` · `SELLO_` · `LOGO_COINCIDE_REFERENCIA` | No corresponde a lo registrado (**vacío** si no hay nada registrado) | revisión |
| `FIRMA_SELLO_NO_PARECIDOS` | Se parecen demasiado a los de otra carta | revisión |
| `FONDO_NATURAL` | Fondo perfectamente uniforme | revisión |
| `AUTENTICIDAD_EVALUADA` | La verificación no pudo correr | revisión (nunca aprueba callado) |
| `CARTA_NO_DUPLICADA` | Esta misma carta ya se recibió | solo aviso |

Informativos: `ORIGEN_DOCUMENTO`, `TEXTO_SELLO`, `VEREDICTO_AUTENTICIDAD`,
`MOTIVOS_AUTENTICIDAD`.

Catálogo: `catalogs/base/recursos_humanos/CARTA_NOM35.json`. Cambiar un mensaje,
un umbral o un `on_fail` es editar ese JSON y `pnpm sync:one -- --type CARTA_NOM35`
— sin deploy.

---

## 4. Dar de alta los sellos y firmas de un proveedor

Resumen operativo; la versión larga (cómo recortar, qué evitar) está en
[`AUTENTICIDAD_Y_REFERENCIAS.md`](AUTENTICIDAD_Y_REFERENCIAS.md) §3.

1. **Consigue una carta en papel verificada** de ese proveedor. Compruébalo:
   `pnpm autenticidad -- --file carta.pdf` debe decir `FISICO`. **Si dice `DIGITAL`,
   no saques referencias de ahí** — registrarías la imagen pegada como válida.
2. **Recorta** cada elemento por separado (firma sola, sello solo, logo solo),
   escaneado a 200–300 dpi, y nómbralo con quién firma o qué sucursal:

   ```
   referencias/10040/firma/julio-cesar-lemus.png
   referencias/10040/firma/omar-hernandez-reynosa.png
   referencias/10040/sello/planta-envasadora.png
   referencias/10040/sello/ppl-sur.png
   referencias/10040/logo/membrete-superior.png
   ```

   Un proveedor puede tener varias de cada una: todas las activas son válidas.
3. **Registra**: `pnpm refs:register -- --key 10040 --dir referencias/10040`
   (primero con `--dry-run`). Te dice cuáles entran y cuáles rechaza y por qué.
4. **Quien sube la carta manda el proveedor**: `"metadata": {"proveedor": "10040"}`
   en `POST /documents` (o `--meta proveedor=10040` en `pnpm enviar` / `pnpm procesar`).

Un proveedor **sin** referencias no se rechaza: se le exige que firma, sello y
logo estén presentes y no sean imágenes insertadas.

La carpeta `referencias/` está en `.gitignore`: son firmas de personas reales.

---

## 5. De observación a rechazo

Hoy ~45 % de las cartas reales son documentos digitales. Para no cortar la
operación de golpe, `CAPTURA_FISICA` arranca mandando a **revisión**. Lo único que
rechaza desde el primer día es la firma/sello reutilizados entre empleados.

Cuando ya avisaste a los proveedores:

```bash
pnpm autenticidad -- --dir <cartas-recientes>     # cuántas caerían hoy (gratis)
# En catalogs/base/recursos_humanos/CARTA_NOM35.json → CAPTURA_FISICA:
#     "on_fail": "review"   →   "on_fail": "reject"
pnpm sync:one -- --type CARTA_NOM35               # dev
pnpm sync:one:prod -- --type CARTA_NOM35          # prod, tras validar
```

El proveedor recibe el mensaje de la regla, que ya es accionable: *«la carta debe
imprimirse, firmarse con tinta, sellarse y escanearse»*.

---

## 6. Lo que no detecta

Quien imprime el Word con la firma pegada y lo vuelve a escanear pasa las dos
capas gratuitas: el archivo *es* entonces una captura real. Lo atrapan solo la
visión (halo, nitidez distinta, trama de impresora → revisión) y la memoria (la
misma tinta en la carta de otro empleado → rechazo). Por eso conviene revisar a
mano lo que sale `SOSPECHOSO`, y por eso la tabla de huellas vale más mientras
más cartas ha visto.

Costo por carta (estimado, aún sin medir): ~$0.014 una genuina, ~$0.008 una
digital. Ver [`COSTOS.md`](COSTOS.md) §10.

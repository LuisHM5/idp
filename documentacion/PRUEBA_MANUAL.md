# Prueba manual rápida: `enviar` vs `procesar`

Dos formas de probar un documento a mano. Elige una:

- **`pnpm run enviar`** → sube el archivo al **stack desplegado** (API real, S3, Step
  Functions). Úsalo para probar el sistema end-to-end tal como lo usaría un
  cliente.
- **`pnpm run procesar`** → corre el **pipeline localmente** (misma lógica que la
  Lambda, sin pasar por el stack). Úsalo para iterar rápido mientras arreglas
  un bug, sin esperar a Step Functions.

|                           | `enviar`                          | `procesar`                                   |
| ------------------------- | --------------------------------- | -------------------------------------------- |
| Necesita stack desplegado | Sí                                | No                                           |
| Necesita credenciales AWS | Sí (para leer el endpoint)        | Sí (Textract+Bedrock, salvo .xml/.xlsx/.csv) |
| Dónde ves el resultado    | `pnpm run resultado -- --id <id>` | Consola + carpeta `resultados/`              |
| Cuándo usarlo             | Probar el flujo real / un cliente | Iterar rápido en un bug de extracción        |

---

## Opción A: `pnpm run enviar` (contra el stack desplegado)

1. Autentícate en AWS (`aws sso login` o equivalente).
2. Envía el documento:

   ```bash
   pnpm run enviar "ruta/al/archivo.pdf" PEDIMENTO_ADUANAL
   # sin tipo → Bedrock clasifica automáticamente
   pnpm run enviar "ruta/al/archivo.pdf"
   ```

   El script imprime un `documentId`.

3. Consulta el resultado (espera si aún está corriendo):

   ```bash
   pnpm run resultado -- --id <documentId> --wait
   ```

Opciones útiles de `enviar`:

```bash
pnpm run enviar doc.pdf PEDIMENTO_ADUANAL --env prod          # contra prod
pnpm run enviar doc.pdf PEDIMENTO_ADUANAL --model amazon.nova-pro-v1:0  # solo dev
```

Detalle completo: `documentacion/ENVIAR_DOCUMENTO_API.md`.

---

## Opción B: `pnpm run procesar` (local, sin stack)

1. Autentícate en AWS solo si el documento necesita Textract/Bedrock (PDF,
   imagen). Para `.xml`/`.xlsx`/`.csv` no hace falta.
2. Procesa uno o varios documentos:

   ```bash
   pnpm run procesar -- --file "mi expediente.pdf"
   pnpm run procesar -- --dir /ruta/a/mis/docs
   pnpm run procesar -- --file factura.xml --type FACTURA_HONORARIOS_XML   # --type obligatorio en .xml/.xlsx/.csv
   ```

3. Revisa el resultado:
   - Resumen legible impreso en consola (tipo, estado, campos clave, warnings).
   - `resultados/<archivo>.json` → resultado completo.
   - `resultados/_resumen.md` → tabla de toda la corrida.

---

# Quick Manual Test: `enviar` vs `procesar`

Two ways to test a document by hand. Pick one:

- **`pnpm run enviar`** → uploads the file to the **deployed stack** (real API,
  S3, Step Functions). Use it to test the system end-to-end the way a real
  client would.
- **`pnpm run procesar`** → runs the **pipeline locally** (same logic as the
  Lambda, without going through the stack). Use it to iterate quickly while
  fixing a bug, without waiting on Step Functions.

|                          | `enviar`                          | `procesar`                                     |
| ------------------------ | --------------------------------- | ---------------------------------------------- |
| Needs a deployed stack   | Yes                               | No                                             |
| Needs AWS credentials    | Yes (to read the endpoint)        | Yes (Textract+Bedrock, except .xml/.xlsx/.csv) |
| Where you see the result | `pnpm run resultado -- --id <id>` | Console + `resultados/` folder                 |
| When to use it           | Testing the real flow / a client  | Fast iteration on an extraction bug            |

---

## Option A: `pnpm run enviar` (against the deployed stack)

1. Authenticate with AWS (`aws sso login` or equivalent).
2. Send the document:

   ```bash
   pnpm run enviar "path/to/file.pdf" PEDIMENTO_ADUANAL
   # no type → Bedrock classifies it automatically
   pnpm run enviar "path/to/file.pdf"
   ```

   The script prints a `documentId`.

3. Check the result (waits if still running):

   ```bash
   pnpm run resultado -- --id <documentId> --wait
   ```

Useful `enviar` options:

```bash
pnpm run enviar doc.pdf PEDIMENTO_ADUANAL --env prod                     # against prod
pnpm run enviar doc.pdf PEDIMENTO_ADUANAL --model amazon.nova-pro-v1:0   # dev only
```

Full details: `documentacion/ENVIAR_DOCUMENTO_API.md`.

---

## Option B: `pnpm run procesar` (local, no stack needed)

1. Authenticate with AWS only if the document needs Textract/Bedrock (PDF,
   image). Not needed for `.xml`/`.xlsx`/`.csv`.
2. Process one or several documents:

   ```bash
   pnpm run procesar -- --file "my file.pdf"
   pnpm run procesar -- --dir /path/to/my/docs
   pnpm run procesar -- --file invoice.xml --type FACTURA_HONORARIOS_XML   # --type required for .xml/.xlsx/.csv
   ```

3. Check the result:
   - Human-readable summary printed to the console (type, status, key fields, warnings).
   - `resultados/<file>.json` → full result.
   - `resultados/_resumen.md` → table summarizing the whole run.

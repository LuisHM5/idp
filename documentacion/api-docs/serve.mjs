#!/usr/bin/env node
/**
 * Sirve la documentación del API REST (./openapi.yaml, mismo directorio)
 * localmente. Prioriza Scalar (UI moderna, "/") pero deja Swagger UI
 * disponible como alternativa compatible ("/swagger") sobre el mismo spec —
 * ninguna build, solo un servidor HTTP mínimo con Node core (sin
 * dependencias que instalar) + los bundles de Scalar/Swagger UI vía CDN
 * (jsdelivr/unpkg), apuntando siempre al YAML local.
 *
 * Standalone a propósito: este archivo no importa nada de `scripts/` ni
 * depende de estar dentro del repo completo — es parte del paquete
 * autocontenido `documentacion/api-docs/` (ver su README.md), así que corre
 * igual si alguien solo tiene esta carpeta (sin pnpm, sin el resto del repo,
 * sin `npm install`).
 *
 * Uso:
 *   pnpm docs:api                       # desde la raíz del repo (Scalar)
 *   node serve.mjs                      # parado en documentacion/api-docs/
 *   node documentacion/api-docs/serve.mjs   # desde cualquier otro lado
 *   ... -- --swagger                    # abre Swagger UI en vez de Scalar
 *   ... -- --port 4500
 *   ... -- --no-open                    # no abrir navegador, solo imprime las URLs
 */

import { createServer } from "http";
import { readFile } from "fs/promises";
import { existsSync } from "fs";
import { execFile } from "child_process";
import path from "path";
import { fileURLToPath } from "url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SPEC_PATH = path.join(__dirname, "openapi.yaml");

function parseArgs(argv) {
  const o = { port: 4001, open: true, swagger: false };
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === "--port") o.port = Number(argv[++i]);
    else if (argv[i] === "--swagger") o.swagger = true;
    else if (argv[i] === "--no-open") o.open = false;
  }
  return o;
}

function scalarHtml() {
  return `<!doctype html>
<html>
<head>
  <title>Documents Validator API — Scalar</title>
  <meta charset="utf-8" />
</head>
<body>
  <script id="api-reference" data-url="/openapi.yaml"></script>
  <script src="https://cdn.jsdelivr.net/npm/@scalar/api-reference"></script>
</body>
</html>`;
}

function swaggerHtml() {
  return `<!doctype html>
<html>
<head>
  <title>Documents Validator API — Swagger UI</title>
  <meta charset="utf-8" />
  <link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist/swagger-ui.css" />
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="https://unpkg.com/swagger-ui-dist/swagger-ui-bundle.js"></script>
  <script>
    window.onload = () => {
      window.ui = SwaggerUIBundle({ url: "/openapi.yaml", dom_id: "#swagger-ui" });
    };
  </script>
</body>
</html>`;
}

function openBrowser(url) {
  const cmd = process.platform === "darwin" ? "open" : process.platform === "win32" ? "start" : "xdg-open";
  execFile(cmd, process.platform === "win32" ? ["", url] : [url], () => {});
}

async function main() {
  const a = parseArgs(process.argv.slice(2));

  if (!existsSync(SPEC_PATH)) {
    console.error(`No existe ${SPEC_PATH}.`);
    process.exit(1);
  }

  const server = createServer(async (req, res) => {
    const url = req.url.split("?")[0];
    try {
      if (url === "/openapi.yaml") {
        const body = await readFile(SPEC_PATH, "utf8");
        res.writeHead(200, { "content-type": "text/yaml; charset=utf-8" });
        res.end(body);
      } else if (url === "/" || url === "/index.html") {
        res.writeHead(200, { "content-type": "text/html; charset=utf-8" });
        res.end(scalarHtml());
      } else if (url === "/swagger") {
        res.writeHead(200, { "content-type": "text/html; charset=utf-8" });
        res.end(swaggerHtml());
      } else {
        res.writeHead(404, { "content-type": "text/plain; charset=utf-8" });
        res.end("Not found");
      }
    } catch (e) {
      res.writeHead(500, { "content-type": "text/plain; charset=utf-8" });
      res.end(String(e));
    }
  });

  server.listen(a.port, () => {
    const base = `http://localhost:${a.port}`;
    console.log(`\nDocumentación del API servida en:`);
    console.log(`  Scalar:  ${base}/`);
    console.log(`  Swagger: ${base}/swagger`);
    console.log(`  Spec:    ${base}/openapi.yaml\n`);
    console.log("Ctrl+C para detener.\n");
    if (a.open) openBrowser(`${base}${a.swagger ? "/swagger" : "/"}`);
  });
}

main();

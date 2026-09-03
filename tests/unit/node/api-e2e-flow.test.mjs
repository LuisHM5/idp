import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { parseArgs, runE2E } from "../../../scripts/test-e2e.mjs";

test("recorre POST -> PUT -> GET 202 -> GET 200 y entrega el JSON final", async () => {
  const directory = await mkdtemp(join(tmpdir(), "documents-validator-e2e-"));
  const documentPath = join(directory, "factura-real.pdf");
  const documentBytes = Buffer.from("%PDF-1.7\ncontenido binario de prueba\n%%EOF\n");
  await writeFile(documentPath, documentBytes);

  const requests = [];
  const uploads = [];
  let getCount = 0;
  const endpoint = "https://api.example.test/prod";
  const finalJson = {
    documentId: "doc-e2e-123",
    processingStatus: "FINISHED",
    workflowStatus: "complete",
    documentType: "FACTURA_FLETE_EXTRANJERO",
    result: {
      envelopeVersion: 2,
      documentClassification: "FACTURA_FLETE_EXTRANJERO",
      validation: { status: "APROBADO" },
      documents: [{ index: 1, documentType: "FACTURA_FLETE_EXTRANJERO", fields: {} }],
    },
  };

  const fetchImpl = async (url, options = {}) => {
    requests.push({ url, options });
    if (options.method === "POST" && url === `${endpoint}/documents`) {
      return new Response(JSON.stringify({
        documentId: "doc-e2e-123",
        processingStatus: "AWAITING_UPLOAD",
        uploadUrl: "https://s3.example.test/signed-upload",
      }), { status: 200, headers: { "Content-Type": "application/json" } });
    }
    if (!options.method && url === `${endpoint}/documents/doc-e2e-123?full=1`) {
      getCount++;
      if (getCount === 1) {
        return new Response(JSON.stringify({
          documentId: "doc-e2e-123",
          processingStatus: "QUEUED",
          retryAfterSeconds: 0,
          result: null,
        }), { status: 202, headers: { "Content-Type": "application/json", "Retry-After": "0" } });
      }
      if (getCount === 2) {
        return new Response(JSON.stringify({
          documentId: "doc-e2e-123",
          processingStatus: "PROCESSING",
          retryAfterSeconds: 0,
          result: null,
        }), { status: 202, headers: { "Content-Type": "application/json", "Retry-After": "0" } });
      }
      return new Response(JSON.stringify(finalJson), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }
    return new Response(JSON.stringify({ error: "NOT_FOUND" }), {
      status: 404,
      headers: { "Content-Type": "application/json" },
    });
  };

  const putFileImpl = async (uploadUrl, filePath) => {
    uploads.push({ uploadUrl, filePath, body: await readFile(filePath) });
  };

  try {
    const logs = [];
    const result = await runE2E({
      endpoint,
      filePath: documentPath,
      documentType: "FACTURA_FLETE_EXTRANJERO",
      apiKey: "test-api-key",
      timeoutMs: 60_000,
      full: true,
      sleep: async () => {},
      jitter: () => 0,
      fetchImpl,
      putFileImpl,
      log: (message) => logs.push(message),
    });

    assert.deepEqual(result, finalJson);
    assert.deepEqual(requests.map(({ options }) => options.method || "GET"), ["POST", "GET", "GET", "GET"]);

    const posted = JSON.parse(requests[0].options.body);
    assert.deepEqual(posted, {
      fileName: "factura-real.pdf",
      documentType: "FACTURA_FLETE_EXTRANJERO",
      metadata: { source: "test-e2e" },
    });
    assert.equal(requests[0].options.headers["x-api-key"], "test-api-key");
    assert.deepEqual(uploads, [{
      uploadUrl: "https://s3.example.test/signed-upload",
      filePath: documentPath,
      body: documentBytes,
    }]);
    assert.equal(requests[1].options.headers["x-api-key"], "test-api-key");
    assert.ok(logs.some((line) => line.startsWith("QUEUED")));
    assert.ok(logs.some((line) => line.startsWith("PROCESSING")));
    assert.ok(logs.some((line) => line === "FINISHED"));
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test("acepta documento y tipo como posicionales o como opciones", () => {
  assert.deepEqual(
    parseArgs(["factura.pdf", "FACTURA_FLETE_EXTRANJERO", "--env", "prod", "--full"]),
    {
      positionals: [],
      full: true,
      file: "factura.pdf",
      type: "FACTURA_FLETE_EXTRANJERO",
      env: "prod",
      timeout: undefined,
    }
  );
  assert.equal(parseArgs(["--file", "doc.pdf", "--doc-type", "PEDIMENTO_ADUANAL"]).type, "PEDIMENTO_ADUANAL");
});

// El permiso `AllowReferenceWrite` (API de referencias de autenticidad) tiene
// que vivir en CADA sitio de manage-apps.mjs donde vive `AllowMetadataWrite`:
// alta, rotación, reconfiguración, grant, deny, adopt y los listados. El que
// se olvida no falla — pierde el permiso en silencio (p. ej. al rotar la key).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const SOURCE = readFileSync(new URL("../../../scripts/manage-apps.mjs", import.meta.url), "utf8");
const count = (needle) => SOURCE.split(needle).length - 1;

test("AllowReferenceWrite aparece en los mismos sitios que AllowMetadataWrite", () => {
  assert.ok(count("AllowMetadataWrite") > 5);
  assert.equal(count("AllowReferenceWrite"), count("AllowMetadataWrite"));
});

test("los flags de referencias existen en register/reconfigure y en grant/deny", () => {
  for (const flag of ['"allow-reference-write"', '"disallow-reference-write"']) assert.equal(count(flag), 1, flag);
  assert.equal(count('args["reference-write"]'), 2);
  assert.equal(count('args["metadata-write"]'), 2);
});

test("rotar una key conserva el permiso", () => {
  assert.match(SOURCE, /AllowReferenceWrite: \{ BOOL: old\.AllowReferenceWrite \?\? false \}/);
});

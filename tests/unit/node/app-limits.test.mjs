/**
 * Límites por app (scripts/_app-limits.mjs): recomendación lectura/escritura,
 * payloads de usage plan y patch paths, y la sincronía de WRITE_METHODS /
 * DEV_READ_METHOD_PATHS con las rutas reales del stack.
 *
 *   node --test tests/unit/node/
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  WRITE_METHODS,
  effectiveLimits,
  methodThrottlePath,
  parseLimit,
  recommendLimits,
  usagePlanInput,
  usagePlanPatchOps,
} from "../../../scripts/_app-limits.mjs";

const STACK_SOURCE = readFileSync(new URL("../../../src/document-validation-stack.ts", import.meta.url), "utf8");

/**
 * Rutas `${path}/${METHOD}` declaradas en el stack. Entiende las dos formas
 * que usa hoy: `const x = padre.addResource("seg")` y cadenas
 * `padre.addResource("a").addResource("b").addMethod("M"`.
 */
function stackRoutes(source) {
  const paths = { "api.root": "" };
  for (const m of source.matchAll(/const (\w+) = (api\.root|\w+)\.addResource\("([^"]+)"\)/g)) {
    paths[m[1]] = `${paths[m[2]]}/${m[3]}`;
  }
  const routes = [];
  for (const m of source.matchAll(/(api\.root|\w+)((?:\.addResource\("[^"]+"\))*)\.addMethod\("(\w+)"/g)) {
    assert.ok(m[1] in paths, `recurso desconocido en el stack: ${m[1]}`);
    const chained = [...m[2].matchAll(/addResource\("([^"]+)"\)/g)].map((c) => `/${c[1]}`).join("");
    routes.push(`${paths[m[1]]}${chained}/${m[3]}`);
  }
  return routes;
}

test("las rutas no-GET del stack son exactamente WRITE_METHODS", () => {
  const writes = stackRoutes(STACK_SOURCE).filter((r) => !r.endsWith("/GET"));
  assert.deepEqual([...writes].sort(), [...WRITE_METHODS].sort());
});

test("las rutas GET del stack son exactamente DEV_READ_METHOD_PATHS", () => {
  const gets = stackRoutes(STACK_SOURCE).filter((r) => r.endsWith("/GET"));
  const block = STACK_SOURCE.match(/DEV_READ_METHOD_PATHS = \[([\s\S]*?)\];/);
  assert.ok(block, "no encontré DEV_READ_METHOD_PATHS en el stack");
  const declared = [...block[1].matchAll(/"([^"]+)"/g)].map((m) => m[1]);
  assert.equal(gets.length, 5);
  assert.deepEqual([...gets].sort(), [...declared].sort());
});

test("recommendLimits: lectura 4× escritura, docs con margen y techo por requests", () => {
  assert.deepEqual(recommendLimits(0), {
    rateLimit: 5, burstLimit: 10, readRateLimit: 20, readBurstLimit: 40,
    docsPerDay: 20, quotaPerDay: 1000, tierLabel: "bajo (≤50/día)",
  });
  const medio = recommendLimits(100);
  assert.equal(medio.rateLimit, 10);
  assert.equal(medio.readRateLimit, 40);
  assert.equal(medio.readBurstLimit, 80);
  assert.equal(medio.docsPerDay, 150);
  // El techo siempre deja pasar el tope de documentos con su polling.
  assert.equal(medio.quotaPerDay, 150 * 50);
  assert.equal(recommendLimits("basura").docsPerDay, 20);
});

test("parseLimit rechaza lo que API Gateway/DynamoDB no aceptarían", () => {
  assert.deepEqual(parseLimit("2.5", "--rate-limit"), { value: 2.5 });
  assert.deepEqual(parseLimit("40", "--burst-limit", { integer: true }), { value: 40 });
  for (const bad of ["0", "-3", "abc", "", true, undefined]) {
    assert.ok(parseLimit(bad, "--x").error, `debió rechazar ${bad}`);
  }
  assert.match(parseLimit("2.5", "--burst-limit", { integer: true }).error, /entero/);
});

test("usagePlanInput: throttle del plan = lectura, override por método = escritura", () => {
  const input = usagePlanInput({
    name: "idp-X-dev", apiId: "abc", stage: "prod",
    write: { rateLimit: 5, burstLimit: 10 }, read: { rateLimit: 20, burstLimit: 40 }, quotaPerDay: 1000,
  });
  assert.deepEqual(input.throttle, { rateLimit: 20, burstLimit: 40 });
  assert.deepEqual(input.quota, { limit: 1000, period: "DAY" });
  assert.equal(input.apiStages.length, 1);
  assert.deepEqual(Object.keys(input.apiStages[0].throttle).sort(), [...WRITE_METHODS].sort());
  assert.deepEqual(input.apiStages[0].throttle["/documents/POST"], { rateLimit: 5, burstLimit: 10 });
  // Ningún GET lleva override: heredan el throttle del plan.
  assert.ok(!Object.keys(input.apiStages[0].throttle).some((k) => k.endsWith("/GET")));
});

test("methodThrottlePath deja el resourcePath literal (UpdateUsagePlan no desescapa ~1)", () => {
  assert.equal(
    methodThrottlePath("abc", "prod", "/documents/{id}/reprocess/POST", "rateLimit"),
    "/apiStages/abc:prod/throttle/documents/{id}/reprocess/POST/rateLimit"
  );
  assert.equal(
    methodThrottlePath("abc", "prod", "/upload/POST", "burstLimit"),
    "/apiStages/abc:prod/throttle/upload/POST/burstLimit"
  );
});

const legacyPlan = {
  throttle: { rateLimit: 1, burstLimit: 1 },
  quota: { limit: 100 },
  apiStages: [{ apiId: "abc", stage: "prod" }],
};
const splitPlan = {
  throttle: { rateLimit: 20, burstLimit: 40 },
  quota: { limit: 1000 },
  apiStages: [{ apiId: "abc", stage: "prod", throttle: {
    "/documents/POST": { rateLimit: 5, burstLimit: 10 },
    "/upload/POST": { rateLimit: 5, burstLimit: 10 },
    "/documents/{id}/reprocess/POST": { rateLimit: 5, burstLimit: 10 },
    "/documents/{id}/metadata/PATCH": { rateLimit: 5, burstLimit: 10 },
  } }],
};

test("effectiveLimits distingue un plan antiguo (throttle único) de uno separado", () => {
  assert.deepEqual(effectiveLimits(legacyPlan), {
    legacy: true,
    read: { rateLimit: 1, burstLimit: 1 },
    write: { rateLimit: 1, burstLimit: 1 },
    quotaPerDay: 100,
  });
  assert.deepEqual(effectiveLimits(splitPlan), {
    legacy: false,
    read: { rateLimit: 20, burstLimit: 40 },
    write: { rateLimit: 5, burstLimit: 10 },
    quotaPerDay: 1000,
  });
});

test("usagePlanPatchOps: 'add' crea los overrides de un plan antiguo, 'replace' los existentes", () => {
  const write = { rateLimit: 1, burstLimit: 1 };
  const read = { rateLimit: 4, burstLimit: 4 };
  const legacyOps = usagePlanPatchOps(legacyPlan, { read, write, quotaPerDay: 1000 });
  assert.deepEqual(legacyOps.slice(0, 3), [
    { op: "replace", path: "/throttle/rateLimit", value: "4" },
    { op: "replace", path: "/throttle/burstLimit", value: "4" },
    { op: "replace", path: "/quota/limit", value: "1000" },
  ]);
  const methodOps = legacyOps.slice(3);
  assert.equal(methodOps.length, WRITE_METHODS.length * 2);
  assert.ok(methodOps.every((o) => o.op === "add" && o.value === "1"));

  const splitOps = usagePlanPatchOps(splitPlan, { write: { rateLimit: 2, burstLimit: 3 } });
  assert.equal(splitOps.length, WRITE_METHODS.length * 2);
  assert.ok(splitOps.every((o) => o.op === "replace"));
  assert.ok(splitOps.some((o) => o.path.endsWith("/throttle/documents/POST/burstLimit") && o.value === "3"));

  assert.deepEqual(usagePlanPatchOps(splitPlan, { quotaPerDay: 50 }), [
    { op: "replace", path: "/quota/limit", value: "50" },
  ]);
});

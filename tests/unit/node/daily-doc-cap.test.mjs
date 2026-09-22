/**
 * Tope de documentos por día (daily-doc-cap.mjs, copia idéntica en
 * upload-lambda y query-lambda): contador atómico por (key, día UTC), 429 con
 * Retry-After hasta la medianoche UTC y fail-open ante errores de DynamoDB.
 *
 *   node --test tests/unit/node/
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  DAILY_LIMIT_ERROR,
  consumeDailyDocument,
  dailyCapUpdateParams,
  dailyLimitResponse,
  nextUtcMidnight,
  utcDay,
} from "../../../src/document-processing/resources/upload-lambda/daily-doc-cap.mjs";

const RESOURCES = new URL("../../../src/document-processing/resources/", import.meta.url);

test("upload-lambda y query-lambda llevan la misma copia del módulo", () => {
  const upload = readFileSync(new URL("upload-lambda/daily-doc-cap.mjs", RESOURCES), "utf8");
  const query = readFileSync(new URL("query-lambda/daily-doc-cap.mjs", RESOURCES), "utf8");
  assert.equal(query, upload, "edita las dos copias de daily-doc-cap.mjs a la vez");
});

test("día y reinicio en UTC, no en hora local", () => {
  const now = new Date("2026-09-21T23:59:30.000Z");
  assert.equal(utcDay(now), "2026-09-21");
  assert.equal(nextUtcMidnight(now).toISOString(), "2026-09-22T00:00:00.000Z");
  assert.equal(nextUtcMidnight(new Date("2026-09-22T00:00:00.000Z")).toISOString(), "2026-09-23T00:00:00.000Z");
});

test("UpdateItem condicional: ADD atómico, condición < tope y TTL", () => {
  const now = new Date("2026-09-21T10:00:00.000Z");
  const params = dailyCapUpdateParams({ table: "usage", apiKeyId: "k1", appName: "ERP", limit: 30, now });
  assert.deepEqual(params.Key, { ApiKeyId: { S: "k1" }, Day: { S: "2026-09-21" } });
  assert.match(params.UpdateExpression, /ADD #docs :one/);
  assert.equal(params.ConditionExpression, "attribute_not_exists(#docs) OR #docs < :max");
  assert.deepEqual(params.ExpressionAttributeValues[":max"], { N: "30" });
  assert.deepEqual(params.ExpressionAttributeValues[":app"], { S: "ERP" });
  const ttl = Number(params.ExpressionAttributeValues[":exp"].N);
  assert.equal(ttl, Math.floor(now.getTime() / 1000) + 90 * 86400);
});

test("sin tope, sin key o sin tabla: permitido sin tocar DynamoDB", async () => {
  const update = () => assert.fail("no debió llamar a DynamoDB");
  for (const opts of [
    { table: "usage", apiKeyId: "k1", maxDocsPerDay: null },
    { table: "usage", apiKeyId: "k1", maxDocsPerDay: "0" },
    { table: "usage", apiKeyId: null, maxDocsPerDay: 5 },
    { table: undefined, apiKeyId: "k1", maxDocsPerDay: 5 },
  ]) {
    assert.deepEqual(await consumeDailyDocument({ update, ...opts }), { allowed: true, counted: false });
  }
});

test("bajo el tope cuenta y deja pasar", async () => {
  const calls = [];
  const result = await consumeDailyDocument({
    update: async (params) => {
      calls.push(params);
      return { Attributes: { Docs: { N: "3" } } };
    },
    table: "usage", apiKeyId: "k1", appName: "ERP", maxDocsPerDay: "10",
    now: new Date("2026-09-21T10:00:00.000Z"),
  });
  assert.deepEqual(result, { allowed: true, counted: true, used: 3, limit: 10 });
  assert.equal(calls.length, 1);
});

test("tope alcanzado: 429 DAILY_DOCUMENT_LIMIT con Retry-After hasta medianoche UTC", async () => {
  const err = Object.assign(new Error("The conditional request failed"), { name: "ConditionalCheckFailedException" });
  const result = await consumeDailyDocument({
    update: async () => { throw err; },
    table: "usage", apiKeyId: "k1", appName: "ERP", maxDocsPerDay: 10,
    now: new Date("2026-09-21T23:59:30.000Z"),
  });
  assert.deepEqual(result, {
    allowed: false, limit: 10, resetsAt: "2026-09-22T00:00:00.000Z", retryAfterSeconds: 30,
  });

  const response = dailyLimitResponse(result);
  assert.equal(response.statusCode, 429);
  assert.equal(response.headers["Retry-After"], "30");
  const body = JSON.parse(response.body);
  assert.equal(body.error, DAILY_LIMIT_ERROR);
  assert.equal(body.limit, 10);
  assert.equal(body.resetsAt, "2026-09-22T00:00:00.000Z");
});

test("cualquier otro error de DynamoDB falla abierto (deja pasar y lo registra)", async () => {
  const logged = [];
  const result = await consumeDailyDocument({
    update: async () => { throw Object.assign(new Error("boom"), { name: "ProvisionedThroughputExceededException" }); },
    table: "usage", apiKeyId: "k1", maxDocsPerDay: 10,
    log: { error: (...args) => logged.push(args) },
  });
  assert.deepEqual(result, { allowed: true, counted: false, error: true });
  assert.equal(logged.length, 1);
});

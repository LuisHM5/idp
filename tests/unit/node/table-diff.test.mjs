import assert from "node:assert/strict";
import { test } from "node:test";
import { diffTables, hasTableChanges } from "../../../scripts/_table-diff.mjs";

function table(props = {}, extra = {}) {
  return {
    Type: "AWS::DynamoDB::Table",
    Properties: {
      TableName: "t",
      KeySchema: [{ AttributeName: "PK", KeyType: "HASH" }],
      ...props,
    },
    ...extra,
  };
}

function template(resources) {
  return { Resources: { Fn: { Type: "AWS::Lambda::Function", Properties: {} }, ...resources } };
}

test("sin cambios", () => {
  const diff = diffTables(template({ A: table() }), template({ A: table() }));
  assert.equal(hasTableChanges(diff), false);
});

test("tabla nueva", () => {
  const diff = diffTables(template({ A: table() }), template({ A: table(), B: table() }));
  assert.deepEqual(diff.created, [{ logicalId: "B" }]);
  assert.equal(hasTableChanges(diff), true);
});

test("stack inexistente: todas las tablas son nuevas", () => {
  const diff = diffTables(null, template({ A: table(), B: table() }));
  assert.deepEqual(diff.created.map((t) => t.logicalId), ["A", "B"]);
  assert.deepEqual(diff.removed, []);
});

test("tabla eliminada reporta su DeletionPolicy", () => {
  const diff = diffTables(
    template({ A: table(), B: table({}, { DeletionPolicy: "Retain" }), C: table() }),
    template({ A: table() })
  );
  assert.deepEqual(diff.removed, [
    { logicalId: "B", deletionPolicy: "Retain" },
    { logicalId: "C", deletionPolicy: "Delete" },
  ]);
});

test("cambio de TTL es in-place", () => {
  const diff = diffTables(
    template({ A: table() }),
    template({ A: table({ TimeToLiveSpecification: { AttributeName: "ExpiresAt", Enabled: true } }) })
  );
  assert.deepEqual(diff.updated, [{ logicalId: "A", changed: ["TimeToLiveSpecification"] }]);
  assert.deepEqual(diff.replaced, []);
});

test("cambio de KeySchema o TableName es reemplazo", () => {
  const diff = diffTables(
    template({ A: table(), B: table() }),
    template({
      A: table({ KeySchema: [{ AttributeName: "Id", KeyType: "HASH" }] }),
      B: table({ TableName: "otra", BillingMode: "PAY_PER_REQUEST" }),
    })
  );
  assert.deepEqual(diff.replaced, [
    { logicalId: "A", changed: ["KeySchema"] },
    { logicalId: "B", changed: ["BillingMode", "TableName"] },
  ]);
  assert.deepEqual(diff.updated, []);
});

test("ignora recursos que no son tablas", () => {
  const diff = diffTables(
    template({}),
    { Resources: { Fn: { Type: "AWS::Lambda::Function", Properties: { Timeout: 30 } } } }
  );
  assert.equal(hasTableChanges(diff), false);
});

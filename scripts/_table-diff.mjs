/**
 * Pure diff of the DynamoDB tables between two CloudFormation templates (the
 * deployed one and the freshly synthesized one). Kept free of I/O so
 * `scripts/check-pending-tables.mjs` stays a thin CLI and this is unit-tested
 * (tests/unit/node/table-diff.test.mjs).
 */
import { isDeepStrictEqual } from "node:util";

const TABLE_TYPE = "AWS::DynamoDB::Table";

// Changing any of these makes CloudFormation REPLACE the table (new physical
// resource, old one deleted or orphaned depending on DeletionPolicy).
const REPLACEMENT_PROPERTIES = ["TableName", "KeySchema", "LocalSecondaryIndexes"];

function tablesOf(template) {
  const out = {};
  for (const [logicalId, resource] of Object.entries(template?.Resources || {})) {
    if (resource?.Type === TABLE_TYPE) out[logicalId] = resource;
  }
  return out;
}

/**
 * @param {object|null} deployedTemplate  null when the stack does not exist yet
 * @param {object} newTemplate
 * @returns {{
 *   created: {logicalId: string}[],
 *   removed: {logicalId: string, deletionPolicy: string}[],
 *   replaced: {logicalId: string, changed: string[]}[],
 *   updated: {logicalId: string, changed: string[]}[],
 * }}
 */
export function diffTables(deployedTemplate, newTemplate) {
  const before = tablesOf(deployedTemplate);
  const after = tablesOf(newTemplate);
  const result = { created: [], removed: [], replaced: [], updated: [] };

  for (const logicalId of Object.keys(after)) {
    if (!(logicalId in before)) {
      result.created.push({ logicalId });
      continue;
    }
    const oldProps = before[logicalId].Properties || {};
    const newProps = after[logicalId].Properties || {};
    const changed = [...new Set([...Object.keys(oldProps), ...Object.keys(newProps)])]
      .filter((key) => !isDeepStrictEqual(oldProps[key], newProps[key]))
      .sort();
    if (changed.length === 0) continue;
    const bucket = changed.some((key) => REPLACEMENT_PROPERTIES.includes(key))
      ? result.replaced
      : result.updated;
    bucket.push({ logicalId, changed });
  }

  for (const logicalId of Object.keys(before)) {
    if (!(logicalId in after)) {
      result.removed.push({
        logicalId,
        deletionPolicy: before[logicalId].DeletionPolicy || "Delete",
      });
    }
  }

  return result;
}

export function hasTableChanges(diff) {
  return Object.values(diff).some((entries) => entries.length > 0);
}

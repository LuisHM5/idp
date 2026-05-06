/**
 * Example custom validator: CURP check digit verification.
 *
 * This validates that the 18th character of a CURP is the correct
 * check digit based on the first 17 characters.
 *
 * To use: set "custom_validator": "example_curp_checkdigit" in the
 * document type config JSON or DynamoDB entry.
 */

const CURP_CHARS = "0123456789ABCDEFGHIJKLMNÑOPQRSTUVWXYZ";

function charValue(c) {
  const idx = CURP_CHARS.indexOf(c.toUpperCase());
  return idx >= 0 ? idx : 0;
}

function calculateCheckDigit(curp17) {
  let sum = 0;
  for (let i = 0; i < 17; i++) {
    sum += charValue(curp17[i]) * (18 - i);
  }
  const remainder = sum % 10;
  return remainder === 0 ? 0 : 10 - remainder;
}

/**
 * @param {Record<string, string>} entityMap - Extracted entities
 * @param {{ documentId: string, classification: string, typeConfig: object }} context
 * @returns {Promise<Array<{field: string, rule: string, valid: boolean, message: string}>>}
 */
export async function validate(entityMap, context) {
  const curp = entityMap["CURP"];
  if (!curp || curp.length !== 18) {
    return []; // Skip if CURP not present or wrong length
  }

  const expected = calculateCheckDigit(curp.substring(0, 17));
  const actual = parseInt(curp[17], 10);
  const valid = expected === actual;

  return [
    {
      field: "CURP",
      rule: "custom:checkdigit",
      valid,
      message: valid
        ? "Digito verificador de CURP correcto"
        : `Digito verificador incorrecto: esperado ${expected}, encontrado ${actual}`,
    },
  ];
}

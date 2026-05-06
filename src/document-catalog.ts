/**
 * Document Catalog - TypeScript representation for CDK prompt generation.
 *
 * Reads from the same JSON config file used by the Python Lambda at runtime.
 * This ensures prompts and validation rules are always in sync.
 *
 * To add a new document type:
 *   node scripts/add-document-type.mjs --type ACTA_NACIMIENTO --fields "NOMBRE,CURP" --required "NOMBRE"
 *
 * Or manually edit: src/document-processing/resources/enrichment-lambda/config/document_types.json
 */

import * as fs from "fs";
import * as path from "path";

export interface ValidationRuleConfig {
  field: string;
  rule_type: string;
  pattern?: string;
  message?: string;
}

export interface DocumentTypeEntry {
  typeId: string;
  description: string;
  extractionFields: string[];
  requiredFields: string[];
  validationRules: ValidationRuleConfig[];
  validatorModule: string | null;
  customValidator?: string | null;
  /** Per-field hints telling Bedrock where/how to find each field in the document */
  extractionHints?: Record<string, string>;
}

interface RawTypeConfig {
  description?: string;
  extraction_fields?: string[];
  required_fields?: string[];
  extraction_hints?: Record<string, string>;
  validation_rules?: Array<{
    field: string;
    rule_type: string;
    pattern?: string;
    message?: string;
  }>;
  validator_module?: string | null;
  custom_validator?: string | null;
}

interface RawCatalogConfig {
  types: Record<string, RawTypeConfig>;
}

/**
 * Load the document catalog from the shared JSON config file.
 * This is the single source of truth for both CDK (prompts) and Lambda (validation).
 */
export function loadCatalog(): DocumentTypeEntry[] {
  // Resolve config path relative to project root (works from dist/src/ and src/)
  const candidates = [
    path.join(__dirname, "../../src/document-processing/resources/enrichment-lambda/config/document_types.json"),
    path.join(__dirname, "../src/document-processing/resources/enrichment-lambda/config/document_types.json"),
    path.join(__dirname, "document-processing/resources/enrichment-lambda/config/document_types.json"),
  ];

  let configPath = "";
  for (const p of candidates) {
    if (fs.existsSync(p)) {
      configPath = p;
      break;
    }
  }

  if (!configPath) {
    throw new Error(`document_types.json not found. Tried: ${candidates.join(", ")}`);
  }

  const raw: RawCatalogConfig = JSON.parse(
    fs.readFileSync(configPath, "utf-8")
  );

  return Object.entries(raw.types).map(([typeId, config]) => ({
    typeId,
    description: config.description || "",
    extractionFields: config.extraction_fields || [],
    requiredFields: config.required_fields || [],
    extractionHints: config.extraction_hints || {},
    validationRules: (config.validation_rules || []).map((r) => ({
      field: r.field,
      rule_type: r.rule_type,
      pattern: r.pattern,
      message: r.message,
    })),
    validatorModule: config.validator_module || null,
    customValidator: config.custom_validator || null,
  }));
}

/**
 * Convenience: get the catalog as a static array.
 * Falls back to inline defaults if the JSON file is not found (e.g., during tests).
 */
export const DOCUMENT_CATALOG: DocumentTypeEntry[] = (() => {
  try {
    return loadCatalog();
  } catch {
    // Fallback for environments where the JSON isn't available
    return [
      { typeId: "INE", description: "Credencial INE", extractionFields: ["NOMBRE_COMPLETO", "CURP", "VIGENCIA"], requiredFields: ["NOMBRE_COMPLETO", "CURP"], validationRules: [], validatorModule: "validators.identification" },
      { typeId: "CONSTANCIA_SITUACION_FISCAL", description: "CSF", extractionFields: ["RFC", "NOMBRE_O_RAZON_SOCIAL"], requiredFields: ["RFC"], validationRules: [], validatorModule: "validators.fiscal" },
      { typeId: "OTRO", description: "No clasificado", extractionFields: [], requiredFields: [], validationRules: [], validatorModule: null },
    ];
  }
})();

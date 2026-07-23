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
  /**
   * Customs (pedimentos) types are classified upstream but extracted by the
   * enrichment Lambda's expediente pipeline, so they carry no extraction
   * fields here (keeps the processing prompt inside the 4KB env var limit).
   */
  isCustoms?: boolean;
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

  const entries: DocumentTypeEntry[] = Object.entries(raw.types).map(([typeId, config]) => ({
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

  entries.push(...loadCustomsTypes(configPath));
  return entries;
}

/**
 * Short, discriminant classification labels for the customs catalog.
 * The full descriptions live in customs_catalog.json; these terse versions
 * exist because the classification prompt must fit a 4KB Lambda env var.
 */
const CUSTOMS_SHORT_DESCRIPTIONS: Record<string, string> = {
  EXPEDIENTE_ADUANAL_MULTIDOCUMENTO:
    "PDF con VARIOS documentos de una operacion de comercio exterior (pedimento + facturas + anexos)",
  PEDIMENTO_ADUANAL:
    "Pedimento SAT de importacion/exportacion: NUM. PEDIMENTO, T.OPER, CVE.PEDIM, cuadro de liquidacion; sin UUID",
  FACTURA_AGENCIA_ADUANAL:
    "CFDI de agencia/agente aduanal: honorarios de despacho, CAAAREM/COVE/precinto, gastos por cuenta de terceros",
  CUENTA_GASTOS_AGENTE_ADUANAL:
    "Cuenta de gastos del agente aduanal: honorarios netos + resumen de pagos por cuenta del importador",
  CFDI_GASTO_TERCERO:
    "CFDI de un tercero ligado a un pedimento: pago de derechos, muestreo, inspeccion (tiene UUID y sellos)",
  FACTURA_LOGISTICA:
    "CFDI de maniobras, almacenaje, acceso de vehiculos, muestreo o flete nacional referenciando un pedimento",
  FACTURA_PROVEEDOR_EXTRANJERO:
    "Commercial invoice del proveedor extranjero de la mercancia; sin UUID ni estructura CFDI",
  FACTURA_FLETE_EXTRANJERO:
    "Freight/trucking invoice extranjera: BOL, carrier, origin/destination; sin CFDI",
  PERMISO_CERTIFICADO_SANITARIO:
    "Permiso o certificado oficial SENASICA/COFEPRIS (fitosanitario, zoosanitario); sin UUID ni sellos CFDI",
  COMPROBANTE_PAGO_ADUANAL:
    "Comprobante de pago de contribuciones de comercio exterior con linea de captura o desglose de contribuciones",
  DOCUMENTO_TRANSPORTE:
    "Bill of Lading, guia aerea, carta porte o lista de empaque",
  ACUSE_VUCEM:
    "Acuse de la Ventanilla Digital (VUCEM): folio de solicitud, numero e-document, cadena original",
};

/**
 * Load customs (pedimentos) types from customs_catalog.json for the
 * CLASSIFICATION prompt only. extractionFields stays empty on purpose: the
 * expediente pipeline in the enrichment Lambda performs the real extraction,
 * and listing ~50 fields per type would blow the processing prompt's limit.
 */
function loadCustomsTypes(documentTypesPath: string): DocumentTypeEntry[] {
  const customsPath = path.join(path.dirname(documentTypesPath), "customs_catalog.json");
  if (!fs.existsSync(customsPath)) return [];

  const raw = JSON.parse(fs.readFileSync(customsPath, "utf-8"));
  const types: Record<string, { description?: string }> = raw.document_types || {};
  return Object.keys(types).map((typeId) => ({
    typeId,
    description:
      CUSTOMS_SHORT_DESCRIPTIONS[typeId] ||
      (types[typeId].description || "").slice(0, 110),
    extractionFields: [],
    requiredFields: [],
    validationRules: [],
    validatorModule: null,
    isCustoms: true,
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

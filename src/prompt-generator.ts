/**
 * Prompt Generator - Dynamically builds Bedrock prompts from the Document Catalog.
 *
 * IMPORTANT: The processing prompt is stored as a Lambda env var (4KB limit).
 * Hints are kept short and only for required fields to stay under the limit.
 */

import { DOCUMENT_CATALOG, DocumentTypeEntry } from "./document-catalog";

/**
 * Generate the classification prompt listing all known document types.
 *
 * SIZE CONSTRAINT: This prompt ends up in a Lambda env var; total env vars
 * per Lambda must stay under ~4KB. Keep descriptions terse and focused on
 * unique discriminators, not exhaustive detail.
 */
export function generateClassificationPrompt(
  catalog: DocumentTypeEntry[] = DOCUMENT_CATALOG
): string {
  // Cap per-type descriptions: the whole prompt lives in a 4KB Lambda env
  // var alongside other variables, so verbose catalog descriptions (INE) get
  // truncated at a sentence boundary when possible.
  const MAX_DESC = 300;
  const typesList = catalog
    .filter((t) => t.typeId !== "OTRO")
    .map((t) => {
      let desc = t.description || "";
      if (desc.length > MAX_DESC) {
        const cut = desc.lastIndexOf(". ", MAX_DESC);
        desc = desc.slice(0, cut > 120 ? cut + 1 : MAX_DESC);
      }
      return `- ${t.typeId}${desc ? `: ${desc}` : ""}`;
    })
    .join("\n");

  return `Clasifica el documento adjunto en UNA categoria mexicana oficial.

Categorias (lee la descripcion completa y elige la que mejor coincida con la evidencia visual y textual):
${typesList}
- OTRO: no encaja claramente en ninguna

Guia:
- Usa marcas concretas: encabezados, logos, emisor, identificadores.
- Prefiere la categoria mas especifica; no caigas a INE solo por ver foto+ID.
- OTRO solo si ninguna aplica claramente.

Responde SOLO JSON crudo, sin markdown:
{"documentClassification":"CATEGORIA"}

Documento:

`;
}

/**
 * Generate the processing/extraction prompt.
 * MUST stay under 3KB to fit in Lambda 4KB env var limit with other vars.
 * No hints in prompt - they're in the catalog for documentation only.
 */
export function generateProcessingPrompt(
  catalog: DocumentTypeEntry[] = DOCUMENT_CATALOG
): string {
  const typeSections = catalog
    .filter((t) => t.extractionFields.length > 0)
    .map((t) => `${t.typeId}: ${t.extractionFields.join(", ")}`)
    .join("\n");

  return `Document classified as [ACTUAL_CLASSIFICATION]. Extract fields by type:

${typeSections}
OTRO: any relevant entities

Reglas ESTRICTAS:
- Transcribe EXACTO carácter por carácter como aparece; NO corrijas, NO completes abreviaturas (si ves "Mtz" devuelve "Mtz", NO "Martinez").
- NO inventes datos faltantes; si no es legible usa "".
- INE anverso: debajo del rótulo "NOMBRE" hay TRES líneas apiladas en orden FIJO → línea 1 = APELLIDO_PATERNO, línea 2 = APELLIDO_MATERNO, línea 3 = NOMBRE(S). Ej "HERNANDEZ / LARA / LUIS" → paterno=HERNANDEZ, materno=LARA, nombre=LUIS. NUNCA pongas un apellido en el campo NOMBRE ni un nombre de pila en APELLIDO_PATERNO. Si el OCR las trae pegadas ("HERNANDEZLARALUIS"), sepáralas usando saltos de línea, mayúsculas y la CURP.
- NOMBRE_COMPLETO = APELLIDO_PATERNO + " " + APELLIDO_MATERNO + " " + NOMBRE (ese orden).
- Verifica que las 4 primeras letras del CURP coincidan con: 1ª letra apellido paterno, 1ª vocal interna apellido paterno, 1ª letra apellido materno (o "X" si no hay), 1ª letra del nombre. Si hay discrepancia, RELEE antes de responder.
- FIRMA_PRESENTE: "SI" o "NO".
- Valores en MAYÚSCULAS cuando el documento los muestra así.
- SOLO JSON crudo, sin markdown, sin explicación.

{"documentClassification":"TYPE","result":{"entities":[{"type":"FIELD","value":"VALUE"}]}}

Document:

`;
}

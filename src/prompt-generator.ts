/**
 * Prompt Generator - Dynamically builds Bedrock prompts from the Document Catalog.
 *
 * IMPORTANT: The processing prompt is stored as a Lambda env var (4KB limit).
 * Hints are kept short and only for required fields to stay under the limit.
 */

import { DOCUMENT_CATALOG, DocumentTypeEntry, loadCatalog } from "./document-catalog";

/**
 * Generate the classification prompt listing all known document types.
 */
export function generateClassificationPrompt(
  catalog: DocumentTypeEntry[] = DOCUMENT_CATALOG
): string {
  const typesList = catalog
    .filter((t) => t.typeId !== "OTRO")
    .map((t) => {
      const desc = t.description ? `: ${t.description}` : "";
      return `- ${t.typeId}${desc}`;
    })
    .join("\n");

  return `You are a document classifier for Mexican official documents. Analyze the attached document and classify it into exactly one category.

Allowed categories (read each description carefully and pick the one that best matches the visual and textual evidence in the document):
${typesList}
- OTRO: cualquier documento que no encaje claramente en los anteriores

Decision guidance:
- Read the full description of every category before choosing.
- Prefer the most specific category that matches. Do not default to INE just because you see a photo ID.
- Use OTRO only when no category clearly applies, not as a tiebreaker.
- Base the decision on concrete evidence: headings, logos, issuing institution names, document layout, and unique identifiers visible in the document.

Rules:
- Return ONLY a raw JSON object, nothing else.
- Do NOT wrap in markdown code blocks.
- Do NOT add any explanation or text before or after the JSON.

Return exactly this format:
{"documentClassification":"CATEGORY_HERE"}

Attached document:

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

Rules:
- ONLY raw JSON, no markdown, no explanation.
- Types UPPERCASE with underscores. Values exact from document.
- Do NOT invent missing data.
- FIRMA_PRESENTE: "SI" or "NO".

{"documentClassification":"TYPE","result":{"entities":[{"type":"FIELD","value":"VALUE"}]}}

Document:

`;
}

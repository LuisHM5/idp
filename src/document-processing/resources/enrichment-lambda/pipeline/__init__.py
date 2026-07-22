"""Expediente/pedimento processing pipeline.

Modules:
- pdf_pages: per-page text acquisition (native PDF text layer, Textract OCR fallback)
- segmenter: split a multi-document PDF into typed page ranges
- text_extractor: catalog-driven structured extraction over page text (Bedrock)
- expediente: orchestrator (pages -> segments -> extraction -> validation)
- normalize: deterministic normalizers (amounts, dates, pedimento numbers)
- bedrock: shared Bedrock Converse helper with JSON parsing and retries
"""

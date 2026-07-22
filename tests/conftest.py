"""Make the enrichment-lambda package importable from the repo root."""

import sys
from pathlib import Path

LAMBDA_DIR = (
    Path(__file__).parent.parent
    / "src" / "document-processing" / "resources" / "enrichment-lambda"
)
sys.path.insert(0, str(LAMBDA_DIR))

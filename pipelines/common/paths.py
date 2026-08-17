"""Where the pipelines keep their data directories.

Derived from this file's location, not the working directory. The defaults used to
be relative — `Path("data/raw")` — so they resolved against wherever the command
was started: from the repository root that meant `<root>/data/raw` while every
real input lives in `<root>/pipelines/data/raw`. In the ingestion container it
pointed outside the bind mount, so a download would not have survived the run.
"""

from __future__ import annotations

from pathlib import Path

# .../pipelines — this file is pipelines/common/paths.py.
PACKAGE_ROOT = Path(__file__).resolve().parents[1]

DATA_DIR = PACKAGE_ROOT / "data"

# Downloads and manually placed sources. Gitignored.
RAW_DIR = DATA_DIR / "raw"

# Artefacts the prototype exporter writes. Not the platform's storage path.
PROCESSED_DIR = DATA_DIR / "processed"

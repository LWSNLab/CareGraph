"""Where the pipelines keep their data directories.

Derived from this file's location rather than the working directory. The defaults
used to be relative — `Path("data/raw")`, `Path("data/processed")` — which meant
they resolved against wherever the command happened to be started. Everything is
run from the repository root (`uv run --project pipelines python -m
pipelines.run_load …`), so `data/raw` resolved to `<root>/data/raw` while every
actual input lives in `<root>/pipelines/data/raw`. Two directories, one of them
always empty, and a URL download would have written to the wrong one.

It mattered in the container too: the ingestion image bind-mounts the host's
`pipelines/data` at `/app/pipelines/data`, so a cwd-relative `data/raw` pointed at
`/app/data/raw` — not mounted, and gone with the container.

Absolute and package-relative removes the question. `RAW_DIR` is the same
directory whether the caller starts in the repository root, in `pipelines/`, or in
the image.
"""

from __future__ import annotations

from pathlib import Path

# .../pipelines — this file is pipelines/common/paths.py.
PACKAGE_ROOT = Path(__file__).resolve().parents[1]

DATA_DIR = PACKAGE_ROOT / "data"

# Downloads and manually placed sources: the GKV list PDF, the Bundes-Klinik-Atlas
# export, scraper output. Gitignored — see .gitignore, which ignores everything
# fetched from a source whatever its extension.
RAW_DIR = DATA_DIR / "raw"

# Artefacts the prototype exporter writes (CSV/JSON/SQL for a standalone
# `krankenkassen` schema). Not the platform's storage path; that is Postgres.
PROCESSED_DIR = DATA_DIR / "processed"

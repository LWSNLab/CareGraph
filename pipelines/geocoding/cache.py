from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

from pipelines.common.paths import PROCESSED_DIR
from pipelines.geocoding.nominatim import Address

log = logging.getLogger(__name__)

# Under `processed/` because that is what it is — derived data, already gitignored.
# Not because of size: a few thousand OSM-derived addresses do not belong in the
# repository, and putting the file anywhere else in `pipelines/data/` would have
# committed them on the next `git add -A`.
DEFAULT_PATH = PROCESSED_DIR / "geocode_cache.json"

# Five decimals ≈ 1.1 m at this latitude. Finer than any building, coarse enough
# that the same row keys identically across runs.
PRECISION = 5


def key_for(lat: float, lon: float) -> str:
    return f"{round(lat, PRECISION)},{round(lon, PRECISION)}"


class GeocodeCache:
    """Coordinate → address, persisted as one JSON file.

    JSON rather than SQLite: it is a few thousand short records, it diffs, and a
    human can read it when an address looks wrong. Written atomically, so an
    interrupted run leaves the previous cache rather than half of this one.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or DEFAULT_PATH
        self._entries: dict[str, dict | None] = {}
        self._dirty = False
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            self._entries = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as err:
            # A corrupt cache is a performance problem, not a correctness one:
            # start empty rather than failing a run that can still do its work.
            log.warning("geocode cache unreadable (%s) — starting empty", err)
            self._entries = {}

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, coordinate: tuple[float, float]) -> bool:
        return key_for(*coordinate) in self._entries

    def get(self, lat: float, lon: float) -> Address | None:
        """The stored answer. Raises KeyError when the pair was never asked.

        Distinguish from a stored None, which means Nominatim answered and had
        nothing — a fact worth keeping.
        """
        entry = self._entries[key_for(lat, lon)]
        return None if entry is None else Address(**entry)

    def put(self, lat: float, lon: float, address: Address | None) -> None:
        self._entries[key_for(lat, lon)] = (
            None if address is None else address.__dict__.copy()
        )
        self._dirty = True

    def save(self) -> None:
        """Write the cache, atomically, if anything changed."""
        if not self._dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)

        temp_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent,
                prefix=f".{self.path.name}.", suffix=".tmp", delete=False,
            ) as temp:
                temp_path = temp.name
                json.dump(self._entries, temp, ensure_ascii=False, indent=1, sort_keys=True)
                temp.write("\n")
            os.replace(temp_path, self.path)
            temp_path = None
            self._dirty = False
        finally:
            if temp_path is not None:
                try:
                    os.unlink(temp_path)
                except FileNotFoundError:
                    pass

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import requests

log = logging.getLogger(__name__)

PUBLIC_ENDPOINT = "https://nominatim.openstreetmap.org"

# The policy's ceiling, not a suggestion. Overridable downwards only in the sense
# that a self-hosted instance may accept more — that is the operator's call, and
# it is passed in explicitly rather than defaulted to.
MIN_INTERVAL_S = 1.0

# ODbL requires attribution wherever the derived value travels. Recorded per
# record, not just in a README, because the record is what gets exported.
ATTRIBUTION = "© OpenStreetMap contributors (ODbL)"


class NominatimError(RuntimeError):
    """Nominatim could not be reached, or refused to answer."""


@dataclass(frozen=True)
class Address:
    """The part of a Nominatim answer this project stores."""

    strasse: str | None
    plz: str | None
    ort: str | None
    bundesland: str | None
    place_id: str | None

    @property
    def is_empty(self) -> bool:
        return not any((self.strasse, self.plz, self.ort))


def _first(mapping: dict, *keys: str) -> str | None:
    """Nominatim names the same thing differently by place type.

    A village is `village`, a town is `town`, a city is `city`. Asking for one of
    them and calling the answer missing is how a third of German care providers
    would end up without a city.
    """
    for key in keys:
        value = mapping.get(key)
        if value:
            return str(value).strip()
    return None


def parse_address(payload: dict) -> Address:
    """Map one Nominatim response to the fields this project stores."""
    address = payload.get("address") or {}

    road = _first(address, "road", "pedestrian", "footway")
    number = _first(address, "house_number")
    strasse = f"{road} {number}" if road and number else road

    return Address(
        strasse=strasse,
        plz=_first(address, "postcode"),
        ort=_first(address, "city", "town", "village", "municipality", "suburb"),
        bundesland=_first(address, "state"),
        place_id=str(payload["place_id"]) if payload.get("place_id") else None,
    )


class NominatimClient:
    """A rate-limited reverse geocoder.

    One instance per run. The interval is held on the instance because that is
    what makes the limit real: sharing the client shares the clock.
    """

    def __init__(
        self,
        user_agent: str,
        base_url: str = PUBLIC_ENDPOINT,
        min_interval_s: float = MIN_INTERVAL_S,
        timeout: int = 30,
        max_attempts: int = 3,
    ) -> None:
        if not user_agent or "python-requests" in user_agent:
            raise ValueError(
                "Nominatim requires an identifying User-Agent naming the application"
            )
        self.base_url = base_url.rstrip("/")
        self.min_interval_s = min_interval_s
        self.timeout = timeout
        self.max_attempts = max_attempts
        # A session, so the TLS handshake is paid once rather than per lookup.
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent})
        self._last_request_at = 0.0

    def _wait_turn(self) -> None:
        """Hold the caller until the next request is allowed."""
        elapsed = time.monotonic() - self._last_request_at
        remaining = self.min_interval_s - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def reverse(self, lat: float, lon: float) -> Address | None:
        """Look up one coordinate. None when Nominatim knows no address for it."""
        params = {
            "lat": f"{lat:.7f}",
            "lon": f"{lon:.7f}",
            "format": "jsonv2",
            "addressdetails": "1",
            # 18 is "building"; asking for more precision than the data holds
            # returns the same answer with a longer name.
            "zoom": "18",
        }

        for attempt in range(1, self.max_attempts + 1):
            self._wait_turn()
            try:
                response = self.session.get(
                    f"{self.base_url}/reverse", params=params, timeout=self.timeout
                )
            except requests.RequestException as err:
                if attempt == self.max_attempts:
                    raise NominatimError(f"reverse({lat}, {lon}) failed: {err}") from err
                self._backoff(attempt)
                continue
            finally:
                self._last_request_at = time.monotonic()

            # Throttled or overloaded: it says when to come back, so come back then.
            if response.status_code in (429, 503):
                if attempt == self.max_attempts:
                    raise NominatimError(
                        f"reverse({lat}, {lon}): {response.status_code} after "
                        f"{attempt} attempts — stop and try later, not harder"
                    )
                self._backoff(attempt, response.headers.get("Retry-After"))
                continue

            if not response.ok:
                raise NominatimError(
                    f"reverse({lat}, {lon}): HTTP {response.status_code}"
                )

            try:
                payload = response.json()
            except ValueError as err:
                raise NominatimError(f"reverse({lat}, {lon}): not JSON") from err

            # A coordinate in the North Sea is a valid question with no answer.
            if payload.get("error"):
                return None

            address = parse_address(payload)
            return None if address.is_empty else address

        return None  # unreachable; every path above returns or raises

    def _backoff(self, attempt: int, retry_after: str | None = None) -> None:
        if retry_after:
            try:
                delay = float(retry_after)
            except ValueError:
                delay = self.min_interval_s * 2**attempt
        else:
            delay = self.min_interval_s * 2**attempt
        log.warning("nominatim: backing off %.1fs (attempt %d)", delay, attempt)
        time.sleep(delay)

"""Tests for reverse geocoding (story E1-S3).

Nothing here touches the network. The client is driven through a stub session,
which is the only way to assert the things the usage policy actually requires:
that requests are spaced, that a 429 is waited out rather than retried harder,
and that an anonymous User-Agent never leaves the process.
"""

from __future__ import annotations

import json
import time

import pytest

from pipelines.geocoding.backfill import _lookup, _missing
from pipelines.geocoding.cache import GeocodeCache, key_for
from pipelines.geocoding.nominatim import (
    Address,
    NominatimClient,
    NominatimError,
    parse_address,
)

# --------------------------------------------------------------------- parsing


def test_house_number_is_joined_to_the_road():
    address = parse_address({
        "place_id": 42,
        "address": {"road": "Gertraudenstraße", "house_number": "19", "postcode": "10178",
                    "city": "Berlin", "state": "Berlin"},
    })
    assert address.strasse == "Gertraudenstraße 19"
    assert (address.plz, address.ort, address.bundesland) == ("10178", "Berlin", "Berlin")
    assert address.place_id == "42"


def test_a_road_without_a_number_is_still_a_street():
    assert parse_address({"address": {"road": "Landstraße"}}).strasse == "Landstraße"


@pytest.mark.parametrize("key", ["city", "town", "village", "municipality", "suburb"])
def test_the_place_is_found_whatever_osm_calls_it(key):
    """Asking only for `city` would lose every provider in a village."""
    assert parse_address({"address": {key: "Donauwörth"}}).ort == "Donauwörth"


def test_an_answer_with_no_address_is_empty():
    assert parse_address({"address": {"country": "Deutschland"}}).is_empty


def test_bundesland_alone_does_not_count_as_an_address():
    """A state is not an address; filling only that would look like success."""
    assert parse_address({"address": {"state": "Bayern"}}).is_empty


# ---------------------------------------------------------------------- client


class StubResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        if self._payload is _NOT_JSON:
            raise ValueError("not json")
        return self._payload


_NOT_JSON = object()


class StubSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.headers = {}

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        return self.responses.pop(0)


def client_with(*responses, **kwargs):
    client = NominatimClient(user_agent="CareGraph/test (+https://example.invalid)", **kwargs)
    client.session = StubSession(*responses)
    return client


def test_an_anonymous_user_agent_is_refused():
    """The usage policy requires an identifiable application. A default
    requests agent is grounds for being blocked, so it never gets sent."""
    with pytest.raises(ValueError, match="User-Agent"):
        NominatimClient(user_agent="python-requests/2.32")
    with pytest.raises(ValueError, match="User-Agent"):
        NominatimClient(user_agent="")


def test_requests_are_spaced_by_the_rate_limit():
    client = client_with(
        StubResponse(payload={"place_id": 1, "address": {"city": "Berlin"}}),
        StubResponse(payload={"place_id": 2, "address": {"city": "Hamburg"}}),
        min_interval_s=0.25,
    )
    started = time.monotonic()
    client.reverse(52.5, 13.4)
    client.reverse(53.5, 10.0)
    elapsed = time.monotonic() - started

    assert elapsed >= 0.25, "the second request did not wait its turn"


def test_a_throttled_request_waits_and_then_succeeds(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr("pipelines.geocoding.nominatim.time.sleep", slept.append)

    client = client_with(
        StubResponse(429, headers={"Retry-After": "7"}),
        StubResponse(payload={"place_id": 9, "address": {"city": "Bremen"}}),
        min_interval_s=0,
    )
    address = client.reverse(53.0, 8.8)

    assert address.ort == "Bremen"
    assert 7 in slept, f"Retry-After was ignored: {slept}"


def test_persistent_throttling_raises_rather_than_hammering(monkeypatch):
    monkeypatch.setattr("pipelines.geocoding.nominatim.time.sleep", lambda _: None)
    client = client_with(
        StubResponse(503), StubResponse(503), StubResponse(503), min_interval_s=0,
    )
    with pytest.raises(NominatimError, match="try later"):
        client.reverse(1.0, 2.0)


def test_a_coordinate_with_no_address_answers_none():
    """A point in the North Sea is a valid question with no answer."""
    client = client_with(StubResponse(payload={"error": "Unable to geocode"}), min_interval_s=0)
    assert client.reverse(56.0, 3.0) is None


def test_an_answer_without_street_postcode_or_city_counts_as_none():
    client = client_with(
        StubResponse(payload={"place_id": 5, "address": {"country": "Deutschland"}}),
        min_interval_s=0,
    )
    assert client.reverse(51.0, 9.0) is None


def test_a_non_json_body_is_an_error_not_an_empty_address():
    client = client_with(StubResponse(payload=_NOT_JSON), min_interval_s=0)
    with pytest.raises(NominatimError, match="not JSON"):
        client.reverse(51.0, 9.0)


# ----------------------------------------------------------------------- cache


def test_the_key_rounds_so_the_same_row_hits_the_same_entry():
    assert key_for(52.520006612, 13.404954) == key_for(52.5200064, 13.4049543)


def test_a_known_nothing_is_stored_and_distinguished_from_a_miss(tmp_path):
    """Without this, the coordinates Nominatim cannot answer are re-asked on
    every run — and those are exactly the ones a backfill keeps meeting."""
    cache = GeocodeCache(tmp_path / "c.json")
    cache.put(1.0, 2.0, None)

    assert (1.0, 2.0) in cache
    assert cache.get(1.0, 2.0) is None
    with pytest.raises(KeyError):
        cache.get(9.0, 9.0)


def test_the_cache_survives_a_round_trip(tmp_path):
    path = tmp_path / "c.json"
    first = GeocodeCache(path)
    first.put(52.5, 13.4, Address("Straße 1", "10178", "Berlin", "Berlin", "42"))
    first.save()

    second = GeocodeCache(path)
    assert second.get(52.5, 13.4).strasse == "Straße 1"
    assert len(second) == 1


def test_a_corrupt_cache_starts_empty_instead_of_failing_the_run(tmp_path):
    path = tmp_path / "c.json"
    path.write_text("{not json", encoding="utf-8")
    assert len(GeocodeCache(path)) == 0


def test_saving_leaves_no_temporary_file_behind(tmp_path):
    cache = GeocodeCache(tmp_path / "c.json")
    cache.put(1.0, 2.0, None)
    cache.save()
    assert [p.name for p in tmp_path.iterdir()] == ["c.json"]


def test_nothing_is_written_when_nothing_changed(tmp_path):
    path = tmp_path / "c.json"
    GeocodeCache(path).save()
    assert not path.exists()


def test_the_file_is_valid_json_a_human_can_read(tmp_path):
    path = tmp_path / "c.json"
    cache = GeocodeCache(path)
    cache.put(52.5, 13.4, Address("A 1", "10178", "Berlin", "Berlin", "1"))
    cache.save()
    assert json.loads(path.read_text(encoding="utf-8"))["52.5,13.4"]["ort"] == "Berlin"


# ------------------------------------------------------------------- selection


class CountingClient:
    """Stands in for the network, and counts how often it was used."""

    def __init__(self, answer=None):
        self.answer = answer
        self.calls = 0

    def reverse(self, lat, lon):
        self.calls += 1
        return self.answer


def test_a_cached_coordinate_is_never_asked_again(tmp_path):
    cache = GeocodeCache(tmp_path / "c.json")
    cache.put(52.5, 13.4, Address("A 1", "10178", "Berlin", "Berlin", "1"))
    client = CountingClient()

    address, cached = _lookup(client, cache, 52.5, 13.4)

    assert cached and client.calls == 0
    assert address.ort == "Berlin"


def test_an_unanswerable_coordinate_is_remembered_as_such(tmp_path):
    """The point of storing a None: a backfill meets these rows every run, and
    without it each run asks Nominatim the same unanswerable question again."""
    cache = GeocodeCache(tmp_path / "c.json")
    client = CountingClient(answer=None)

    _lookup(client, cache, 56.0, 3.0)
    address, cached = _lookup(client, cache, 56.0, 3.0)

    assert client.calls == 1, "the second lookup went to the network"
    assert cached and address is None


@pytest.mark.parametrize(
    "row,expected",
    [
        ({"strasse": None, "plz": None, "ort": None, "bundesland": None},
         ["strasse", "plz", "ort", "bundesland"]),
        ({"strasse": "A 1", "plz": "10178", "ort": "Berlin", "bundesland": "Berlin"}, []),
        # An empty string is missing, not present — a row filled with "" would
        # otherwise never be completed and never be reported as incomplete.
        ({"strasse": "", "plz": "10178", "ort": "Berlin", "bundesland": "Berlin"},
         ["strasse"]),
    ],
)
def test_which_fields_count_as_missing(row, expected):
    assert _missing(row) == expected

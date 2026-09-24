"""Data port, synthetic fixture replay and normalization (S03).

Each test writes a small inline synthetic dataset in the committed manifest format, so these
tests do not depend on the generated dataset under data/fixtures/.
"""

import csv
import hashlib
import io
import json
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from aura.data import (
    AvailabilityBasis,
    BarPayload,
    Cursor,
    CursorMismatch,
    FixtureIntegrityError,
    FixtureReplayProvider,
    FixtureSchemaError,
    HealthStatus,
    IngestReport,
    InMemoryObservationStore,
    NormalizedBatch,
    ObservationKind,
    ProvenanceClass,
    ProviderBatch,
    QuarantineReason,
    ReadExclusionReason,
    UnavailableReason,
    content_fingerprint,
    normalize_bars,
)
from pydantic import ValidationError

NOW = datetime(2026, 9, 23, 12, tzinfo=UTC)
BAR_COLUMNS = [
    "instrument_id",
    "provider_record_id",
    "revision",
    "interval_start",
    "interval_end",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "final",
    "adjustment_basis",
    "currency",
    "available_at",
]
INSTRUMENTS = [
    {"instrument_id": "INS-AAA", "security_type": "EQUITY", "currency": "USD", "name": "Alpha"},
    {"instrument_id": "INS-ETF", "security_type": "ETF", "currency": "USD", "name": "Index"},
]
SYMBOLS = [
    # AAA is renamed AAB effective 2024-01-04 (effective_to is exclusive).
    {"instrument_id": "INS-AAA", "symbol": "AAA", "effective_from": "2023-01-02",
     "effective_to": "2024-01-04"},
    {"instrument_id": "INS-AAA", "symbol": "AAB", "effective_from": "2024-01-04",
     "effective_to": ""},
    {"instrument_id": "INS-ETF", "symbol": "SYNX", "effective_from": "2023-01-02",
     "effective_to": ""},
]


def clock() -> datetime:
    return NOW


def ticking() -> Callable[[], datetime]:
    """A clock that advances on every call, like a restarted process's wall clock."""
    moments = iter(NOW + timedelta(minutes=n) for n in range(1, 10_000))
    return lambda: next(moments)


def at(text: str) -> datetime:
    return datetime.fromisoformat(text)


def bar(day: str = "2024-01-02", instrument: str = "INS-AAA", **overrides: str) -> dict[str, str]:
    """A valid final daily bar. January sessions are 14:30-21:00 UTC (09:30-16:00 EST)."""
    row = {
        "instrument_id": instrument,
        "provider_record_id": f"{instrument}/1d/{day}",
        "revision": "1",
        "interval_start": f"{day}T14:30:00Z",
        "interval_end": f"{day}T21:00:00Z",
        "open": "10",
        "high": "11",
        "low": "9",
        "close": "10.5",
        "volume": "1000",
        "final": "true",
        "adjustment_basis": "RAW",
        "currency": "USD",
        "available_at": f"{day}T21:15:00Z",
    }
    row.update(overrides)
    return row


def csv_bytes(columns: list[str], rows: list[dict[str, str]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(columns)
    writer.writerows([[row.get(c, "") for c in columns] for row in rows])
    return buffer.getvalue().encode()


def write_dataset(
    root: Path,
    bars: list[dict[str, str]],
    *,
    bar_columns: list[str] | None = None,
    manifest: dict[str, Any] | None = None,
    rights: dict[str, Any] | None = None,
    raw_bar_lines: tuple[str, ...] = (),
    symbols: list[dict[str, str]] | None = None,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    tables = {
        "instruments.csv": (["instrument_id", "security_type", "currency", "name"], INSTRUMENTS),
        "symbol_history.csv": (
            ["instrument_id", "symbol", "effective_from", "effective_to"],
            SYMBOLS if symbols is None else symbols,
        ),
        "universe_membership.csv": (
            ["universe_id", "instrument_id", "effective_from", "effective_to"],
            [{"universe_id": "U1", "instrument_id": "INS-AAA", "effective_from": "2023-01-02"}],
        ),
        "bars_1d.csv": (bar_columns or BAR_COLUMNS, bars),
        "corporate_actions.csv": (["action_id", "instrument_id", "kind", "ex_date"], []),
    }
    files = {}
    for name, (columns, rows) in tables.items():
        extra = raw_bar_lines if name == "bars_1d.csv" else ()
        data = csv_bytes(columns, rows) + "".join(line + "\n" for line in extra).encode()
        (root / name).write_bytes(data)
        files[name] = {"sha256": hashlib.sha256(data).hexdigest(), "rows": len(rows) + len(extra)}
    document: dict[str, Any] = {
        "dataset_id": "synthetic-us-equities-test",
        "version": "1",
        "generator": "tests/test_data_provider.py",
        "seed": 7,
        "calendar": "FIXTURE_WEEKDAY_V1",
        "files": files,
        "event_range": {"start": "2024-01-02T14:30:00Z", "end": "2024-01-08T21:00:00Z"},
        "availability_range": {"start": "2024-01-02T21:15:00Z", "end": "2024-01-09T00:00:00Z"},
        "scenarios": ["inline"],
        "rights": {
            "license": "MIT; synthetic test data",
            "third_party_content": False,
            "provenance_class": "SYNTHETIC_FIXTURE",
            "not_market_data": True,
        },
    }
    document.update(manifest or {})
    document["rights"].update(rights or {})
    (root / "manifest.json").write_text(json.dumps(document, indent=2), encoding="utf-8")
    return root


def normalize(provider: FixtureReplayProvider, batch: ProviderBatch) -> NormalizedBatch:
    return normalize_bars(
        batch, instruments=provider.instruments(), calendar=provider.calendar, recorded_at=NOW
    )


def drain(
    provider: FixtureReplayProvider,
    store: InMemoryObservationStore,
    *,
    batch_size: int,
    cursor: Cursor | None = None,
) -> list[IngestReport]:
    """Consumer loop: ingest, then checkpoint the cursor; stop when the replay is exhausted."""
    reports = []
    while True:
        batch = provider.read_batch(cursor, batch_size)
        reports.append(store.ingest(normalize(provider, batch)))
        cursor = batch.next_cursor
        if batch.exhausted:
            return reports


def loaded(tmp_path: Path, bars: list[dict[str, str]], **kwargs: Any) -> tuple[
    FixtureReplayProvider, InMemoryObservationStore
]:
    provider = FixtureReplayProvider(write_dataset(tmp_path, bars, **kwargs), clock=clock)
    store = InMemoryObservationStore()
    drain(provider, store, batch_size=2)
    return provider, store


def test_identical_duplicate_is_deduplicated_and_conflicting_duplicate_quarantined(tmp_path):
    # Same fact spelled differently (decimal scale, UTC offset) is an identical duplicate.
    same_fact = bar(close="10.50", available_at="2024-01-02T21:15:00+00:00")
    conflicting = bar(close="10.75")  # same provider record and revision, different fact
    provider = FixtureReplayProvider(
        write_dataset(tmp_path, [bar(), same_fact, conflicting]), clock=clock
    )
    store = InMemoryObservationStore()
    [report] = drain(provider, store, batch_size=10)

    [kept] = store.observations
    assert (len(report.accepted), len(report.duplicates), len(report.quarantined)) == (1, 1, 1)
    assert report.duplicates == (kept.id,) and kept.payload.close == Decimal("10.5")
    [conflict] = store.quarantine
    assert conflict.reasons == (QuarantineReason.CONFLICTING_DUPLICATE,)
    assert conflict.conflicts_with == kept.id
    assert conflict.rejected_observation is not None
    assert conflict.rejected_observation.payload.close == Decimal("10.75")

    # Once both variants are knowable the record fails closed instead of picking a winner.
    read = store.read_as_of(["INS-AAA"], at("2024-01-03T00:00Z"), at("2024-01-03T00:00Z"), None)
    assert read.observations == ()
    assert [e.reason for e in read.excluded] == [ReadExclusionReason.CONFLICTING_DUPLICATE]
    assert [u.reason for u in read.unavailable] == [UnavailableReason.NO_ELIGIBLE_OBSERVATIONS]


def test_out_of_order_delivery_gives_the_same_point_in_time_view(tmp_path):
    rows = [
        bar("2024-01-04"),
        # The correction arrives before the record it corrects.
        bar("2024-01-02", revision="2", close="10.8", available_at="2024-01-03T12:00:00Z"),
        bar("2024-01-03"),
        bar("2024-01-02"),
    ]
    provider = FixtureReplayProvider(write_dataset(tmp_path, rows), clock=clock)
    batches, cursor = [], None
    while not batches or not batches[-1].exhausted:
        batches.append(provider.read_batch(cursor, 1))
        cursor = batches[-1].next_cursor
    forward, backward = InMemoryObservationStore(), InMemoryObservationStore()
    for batch in batches:
        forward.ingest(normalize(provider, batch))
    for batch in reversed(batches):
        backward.ingest(normalize(provider, batch))
    assert forward.quarantine == backward.quarantine == ()

    cutoff = at("2024-01-05T00:00Z")
    views = [
        store.read_as_of(["INS-AAA"], cutoff, cutoff, provider.manifest.ref)
        for store in (forward, backward)
    ]
    assert views[0].observations == views[1].observations
    assert views[0].selection_hash == views[1].selection_hash
    assert [o.event_time.date().isoformat() for o in views[0].observations] == [
        "2024-01-02",
        "2024-01-03",
        "2024-01-04",
    ]
    assert views[0].observations[0].revision == 2
    assert views[0].observations[0].payload.close == Decimal("10.8")


def test_cursor_resume_after_crash_returns_same_set_without_duplicates(tmp_path):
    rows = [bar("2024-01-02"), bar("2024-01-03"), bar("2024-01-04"), bar("2024-01-08", high="8")]
    rows += [bar("2024-01-02", instrument="INS-ETF"), bar("2024-01-05")]
    dataset = write_dataset(tmp_path, rows)
    reference = InMemoryObservationStore()
    drain(FixtureReplayProvider(dataset, clock=clock), reference, batch_size=2)
    fresh = FixtureReplayProvider(dataset, clock=clock)
    uninterrupted_tail = fresh.read_batch(fresh.read_batch(None, 2).next_cursor, 100)

    provider = FixtureReplayProvider(dataset, clock=clock)
    store = InMemoryObservationStore()
    first = provider.read_batch(None, 2)
    store.ingest(normalize(provider, first))
    checkpoint = first.next_cursor.model_dump_json()  # durably committed with batch 1
    lost = provider.read_batch(first.next_cursor, 2)
    store.ingest(normalize(provider, lost))  # applied, but the process dies before checkpointing
    del provider

    restarted = FixtureReplayProvider(dataset, clock=ticking())  # new process, later receipts
    resume = Cursor.model_validate_json(checkpoint)
    assert restarted.read_batch(resume, 100).records == uninterrupted_tail.records
    reports = drain(restarted, store, batch_size=2, cursor=resume)

    # The lost batch (one valid row, one rejected row) is re-delivered and fully accounted for.
    assert reports[0].accepted == reports[0].quarantined == ()
    assert (len(reports[0].duplicates), len(reports[0].quarantine_duplicates)) == (1, 1)
    facts = [(o.id, content_fingerprint(o)) for o in store.observations]
    assert facts == [(o.id, content_fingerprint(o)) for o in reference.observations]
    assert [q.id for q in store.quarantine] == [q.id for q in reference.quarantine]
    assert len({o.id for o in store.observations}) == len(store.observations) == 5


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"instrument_id": "INS-NOPE", "provider_record_id": "X"}, "UNKNOWN_INSTRUMENT"),
        ({"interval_start": "2024-01-02T14:30:00"}, "NAIVE_TIMESTAMP"),
        ({"available_at": "2024-01-02T21:15:00"}, "NAIVE_TIMESTAMP"),
        ({"high": "8.5"}, "HIGH_BELOW_LOW"),
    ],
)
def test_invalid_records_are_quarantined_with_reason_codes(tmp_path, overrides, reason):
    provider, store = loaded(tmp_path, [bar("2024-01-02", **overrides), bar("2024-01-03")])
    [record] = store.quarantine
    assert QuarantineReason(reason) in record.reasons
    assert set(overrides.items()) <= set(record.raw_fields)  # the raw input is preserved
    assert record.provenance_class is ProvenanceClass.SYNTHETIC_FIXTURE
    assert [o.provider_record_id for o in store.observations] == ["INS-AAA/1d/2024-01-03"]


def test_every_record_is_accounted_for_never_silently_dropped(tmp_path):
    rows = [
        bar("2024-01-02"),
        bar("2024-07-01", interval_start="2024-07-01T13:30:00Z",
            interval_end="2024-07-01T20:00:00Z", available_at="2024-07-01T20:15:00Z"),  # EDT
        bar("2024-07-02"),  # EST offsets during daylight time
        bar("2024-01-06"),  # Saturday
        bar("2024-01-03", open="NaN"),
        bar("2024-01-03", volume="-1"),
        bar("2024-01-03", close="12"),
        bar("2024-01-03", available_at="2024-01-03T20:00:00Z"),  # final before the close
        bar("2024-01-03", final="maybe"),
        bar("2024-01-03", revision="0"),
        bar("2024-01-03", currency="EUR"),
        bar("2024-01-03", adjustment_basis="SPLIT_ADJUSTED"),
        bar("2024-01-03", interval_end="2024-01-03T14:00:00Z"),
        bar("2024-01-03", available_at="after the close"),
        bar("2024-01-03", volume=""),
        bar("2024-01-03", low="0"),
    ]
    dataset = write_dataset(tmp_path, rows, raw_bar_lines=("INS-AAA,short-row",))
    provider = FixtureReplayProvider(dataset, clock=clock)
    batch = provider.read_batch(None, 100)
    normalized = normalize(provider, batch)

    assert len(normalized.observations) + len(normalized.quarantined) == len(batch.records)
    assert [o.event_time for o in normalized.observations] == [
        at("2024-01-02T21:00Z"),
        at("2024-07-01T20:00Z"),
    ]
    reasons = [q.reasons for q in normalized.quarantined]
    expected = [
        "OFF_CALENDAR",
        "OFF_CALENDAR",
        "INVALID_NUMBER",
        "NEGATIVE_VOLUME",
        "PRICE_OUTSIDE_RANGE",
        "AVAILABILITY_BEFORE_EVENT",
        "INVALID_ENUM",
        "INVALID_REVISION",
        "CURRENCY_MISMATCH",
        "INVALID_ENUM",
        "INTERVAL_ORDER",
        "MALFORMED_TIMESTAMP",
        "MISSING_FIELD",
        "NON_POSITIVE_PRICE",
        "MALFORMED_RECORD",
    ]
    assert reasons == [(QuarantineReason(code),) for code in expected]
    assert normalized.quarantined[-1].raw_fields == (
        ("instrument_id", "INS-AAA"),
        ("provider_record_id", "short-row"),
    )


def test_symbols_map_identity_through_effective_dated_history(tmp_path):
    columns = [c for c in BAR_COLUMNS if c not in {"instrument_id", "provider_record_id"}]
    rows = [
        bar("2024-01-03", symbol="AAA"),
        bar("2024-01-04", symbol="AAB"),  # renamed; same stable instrument
        bar("2024-01-05", symbol="AAA"),  # old ticker no longer maps to anything
    ]
    provider, store = loaded(tmp_path, rows, bar_columns=columns + ["symbol"])
    assert [o.instrument_id for o in store.observations] == ["INS-AAA", "INS-AAA"]
    [record] = store.quarantine
    assert record.reasons == (QuarantineReason.UNKNOWN_INSTRUMENT,)

    mismatch = [bar("2024-01-04", symbol="AAA")]  # instrument_id given, ticker not effective
    _, store = loaded(tmp_path / "mismatch", mismatch, bar_columns=BAR_COLUMNS + ["symbol"])
    assert store.quarantine[0].reasons == (QuarantineReason.IDENTITY_MISMATCH,)


def test_provisional_bars_are_excluded_from_close_based_reads(tmp_path):
    rows = [
        bar("2024-01-02"),
        bar("2024-01-03", final="false", close="10.4", available_at="2024-01-03T21:01:00Z"),
        bar("2024-01-03", revision="2", close="10.6", available_at="2024-01-04T01:00:00Z"),
    ]
    provider, store = loaded(tmp_path, rows)
    assert store.quarantine == ()
    dataset = provider.manifest.ref
    evening = at("2024-01-03T22:00Z")

    close_based = store.read_as_of(["INS-AAA"], evening, evening, dataset)
    assert [o.event_time for o in close_based.observations] == [at("2024-01-02T21:00Z")]
    [excluded] = close_based.excluded
    assert excluded.reason is ReadExclusionReason.PROVISIONAL

    monitoring = store.read_as_of(["INS-AAA"], evening, evening, dataset, include_provisional=True)
    assert [o.payload.final for o in monitoring.observations] == [True, False]

    next_day = at("2024-01-04T02:00Z")
    final = store.read_as_of(["INS-AAA"], next_day, next_day, dataset)
    assert [(o.revision, o.payload.close) for o in final.observations][-1] == (2, Decimal("10.6"))
    assert final.excluded == ()


def test_point_in_time_read_excludes_later_revisions_and_future_bars(tmp_path):
    """AC-09: nothing from after the decision's cutoff enters it, or is even mentioned."""
    rows = [
        bar("2024-01-02"),
        bar("2024-01-02", revision="2", high="12", close="12", available_at="2024-01-05T12:00Z"),
        bar("2024-01-03"),
    ]
    provider, store = loaded(tmp_path, rows)
    assert store.quarantine == ()
    decision = at("2024-01-02T22:00Z")

    read = store.read_as_of(["INS-AAA"], decision, decision, provider.manifest.ref)
    [seen] = read.observations
    assert (seen.revision, seen.payload.close) == (1, Decimal("10.5"))
    assert read.excluded == () and read.unavailable == ()

    # The same as-of question with a later knowledge cutoff sees the correction, while a bar
    # completing after as_of still cannot enter.
    revisited = store.read_as_of(["INS-AAA"], decision, at("2024-01-06T00:00Z"), None)
    assert [(o.revision, o.payload.close) for o in revisited.observations] == [(2, Decimal("12"))]

    later = at("2024-01-06T00:00Z")
    mid_session = store.read_as_of(["INS-AAA"], at("2024-01-03T18:00Z"), later, None)
    assert [o.event_time.date().isoformat() for o in mid_session.observations] == ["2024-01-02"]


def test_unknown_availability_cannot_support_an_as_of_read(tmp_path):
    provider = FixtureReplayProvider(write_dataset(tmp_path, [bar()]), clock=clock)
    normalized = normalize(provider, provider.read_batch(None, 1))
    unplaceable = normalized.observations[0].model_copy(
        update={"availability_basis": AvailabilityBasis.UNKNOWN}
    )
    store = InMemoryObservationStore()
    store.ingest(normalized.model_copy(update={"observations": (unplaceable,)}))
    read = store.read_as_of(["INS-AAA"], at("2024-01-09T00:00Z"), at("2024-01-09T00:00Z"), None)
    assert read.observations == ()
    assert read.excluded[0].reason is ReadExclusionReason.AVAILABILITY_UNKNOWN


def test_unpinned_or_unknown_manifest_reads_report_unavailability(tmp_path):
    provider, store = loaded(tmp_path, [bar()])
    other = provider.manifest.ref.model_copy(update={"content_hash": "0" * 64})
    moment = at("2024-01-09T00:00Z")
    read = store.read_as_of(["INS-AAA", "INS-ETF"], moment, moment, other)
    assert read.observations == ()
    assert {u.reason for u in read.unavailable} == {UnavailableReason.MANIFEST_UNAVAILABLE}
    with pytest.raises(TypeError):
        store.read_as_of("INS-AAA", at("2024-01-09T00:00Z"), at("2024-01-09T00:00Z"), None)
    with pytest.raises(ValueError):
        store.read_as_of(["INS-AAA"], datetime(2024, 1, 9), at("2024-01-09T00:00Z"), None)


@pytest.mark.parametrize("target", ["bars_1d.csv", "universe_membership.csv"])
def test_hash_mismatch_refuses_to_load(tmp_path, target):
    dataset = write_dataset(tmp_path, [bar()])
    path = dataset / target
    path.write_bytes(path.read_bytes().replace(b"INS-AAA", b"INS-AAB"))
    with pytest.raises(FixtureIntegrityError, match="SHA-256"):
        FixtureReplayProvider(dataset)


def test_row_count_or_missing_file_refuses_to_load(tmp_path):
    dataset = write_dataset(tmp_path / "rows", [bar()])
    document = json.loads((dataset / "manifest.json").read_text())
    document["files"]["bars_1d.csv"]["rows"] = 2
    (dataset / "manifest.json").write_text(json.dumps(document))
    with pytest.raises(FixtureIntegrityError, match="data rows"):
        FixtureReplayProvider(dataset)

    missing = write_dataset(tmp_path / "missing", [bar()])
    (missing / "corporate_actions.csv").unlink()
    with pytest.raises(FixtureIntegrityError, match="missing"):
        FixtureReplayProvider(missing)


@pytest.mark.parametrize(
    ("manifest", "rights", "bar_columns", "message"),
    [
        ({}, {"provenance_class": "LICENSED_VENDOR"}, None, "only replays SYNTHETIC_FIXTURE"),
        ({}, {"not_market_data": False}, None, "not_market_data"),
        ({}, {"third_party_content": True}, None, "third-party content"),
        ({"calendar": "XNYS"}, {}, None, "unsupported calendar"),
        ({"calendar_definition": {"session_open_local": "10:00"}}, {}, None, "contradicts"),
        ({"files": {"../x.csv": {"sha256": "0" * 64, "rows": 0}}}, {}, None, "unsafe"),
        ({}, {}, BAR_COLUMNS + ["adj_close"], "unknown columns: adj_close"),
        ({}, {}, [c for c in BAR_COLUMNS if c != "final"], "missing required columns: final"),
    ],
)
def test_unsupported_manifest_or_schema_refuses_to_load(
    tmp_path, manifest, rights, bar_columns, message
):
    dataset = write_dataset(
        tmp_path, [bar()], manifest=manifest, rights=rights, bar_columns=bar_columns
    )
    with pytest.raises(FixtureSchemaError, match=message):
        FixtureReplayProvider(dataset)


def test_ambiguous_symbol_history_refuses_to_load(tmp_path):
    shared = {"symbol": "AAA", "effective_from": "2023-01-02", "effective_to": ""}
    symbols = [{"instrument_id": "INS-AAA"} | shared, {"instrument_id": "INS-ETF"} | shared]
    with pytest.raises(FixtureSchemaError, match="ambiguous"):
        FixtureReplayProvider(write_dataset(tmp_path, [bar()], symbols=symbols))


def test_declared_fixture_holidays_are_not_sessions(tmp_path):
    definition = {
        "id": "FIXTURE_WEEKDAY_V1",
        "timezone": "America/New_York",
        "session_open_local": "09:30",
        "session_close_local": "16:00",
        "holidays": ["2024-01-03"],
    }
    provider, store = loaded(
        tmp_path,
        [bar("2024-01-02"), bar("2024-01-03")],
        manifest={"calendar_definition": definition},
        rights={"third_party_content": "none"},  # the generator's spelling of "no content"
    )
    assert provider.manifest.holidays == (date(2024, 1, 3),)
    assert [o.event_time.date() for o in store.observations] == [date(2024, 1, 2)]
    assert [q.reasons for q in store.quarantine] == [(QuarantineReason.OFF_CALENDAR,)]


def test_foreign_cursor_is_rejected(tmp_path):
    first = FixtureReplayProvider(write_dataset(tmp_path / "a", [bar("2024-01-02")]))
    # Same dataset ID and version with different content must not share cursors.
    second = FixtureReplayProvider(write_dataset(tmp_path / "b", [bar("2024-01-03")]))
    cursor = first.read_batch(None, 1).next_cursor
    with pytest.raises(CursorMismatch):
        second.read_batch(cursor, 1)
    with pytest.raises(CursorMismatch):
        first.read_batch(cursor.model_copy(update={"token": cursor.token + "9"}), 1)


def test_capabilities_health_and_immutable_provenance(tmp_path):
    provider, store = loaded(tmp_path, [bar()])
    capabilities = provider.capabilities()
    assert capabilities.provenance_class is ProvenanceClass.SYNTHETIC_FIXTURE
    assert capabilities.kinds == (ObservationKind.BAR,)
    assert capabilities.availability_basis is AvailabilityBasis.VERIFIED_HISTORICAL
    assert capabilities.rights.not_market_data and not capabilities.live
    assert capabilities.dataset == provider.manifest.ref
    assert "VERIFIED_NOT_NORMALIZED: universe_membership.csv" in capabilities.limitations
    assert [i.symbol_on(date(2024, 1, 4)) for i in provider.instruments()] == ["AAB", "SYNX"]
    assert provider.health().status is HealthStatus.HEALTHY

    [observation] = store.observations
    assert observation.provenance_class is ProvenanceClass.SYNTHETIC_FIXTURE
    assert observation.raw_artifact.manifest_id == "synthetic-us-equities-test@1"
    with pytest.raises(ValidationError):
        observation.revision = 2
    fields = observation.payload.model_dump()
    with pytest.raises(ValidationError, match="high is below low"):
        BarPayload(**(fields | {"low": Decimal("12")}))
    with pytest.raises(ValidationError, match="instance of Decimal"):
        BarPayload(**(fields | {"close": 10.5}))  # strict contracts never accept floats

    (tmp_path / "bars_1d.csv").write_bytes(b"tampered")
    health = provider.health()
    assert health.status is HealthStatus.DEGRADED
    assert health.reasons == ("SOURCE_CHANGED: bars_1d.csv",)
    assert provider.read_batch(None, 1).records  # the verified snapshot is still what is served

"""Synthetic fixture replay provider. Its data is SYNTHETIC_FIXTURE, never market data.

It loads a committed dataset directory described by ``manifest.json``. Before interpreting any
content it checks every declared file's SHA-256 and data-row count, and it refuses to load on a
mismatch, a missing file, an unsafe file name, an unsupported calendar or rights that are not
synthetic. Bars replay through the MarketDataProvider port with a cursor bound to the manifest.

Manifest (S02 format): ``dataset_id``, ``version``, ``calendar`` (FIXTURE_WEEKDAY_V1),
``files`` {name: {sha256, rows}}, ``rights`` {license, third_party_content: false or "none",
provenance_class: SYNTHETIC_FIXTURE, not_market_data: true}. Optional
``calendar_definition.holidays`` declares fixture holidays; a declared timezone or session time
that contradicts FIXTURE_WEEKDAY_V1 refuses the load. ``generator``, ``seed``, ``event_range``,
``availability_range`` and ``scenarios`` are carried as descriptive metadata; other top-level
keys are not interpreted but stay pinned by the manifest content hash.

Parsed files: instruments.csv (instrument_id, security_type, currency; optional name),
symbol_history.csv (instrument_id, symbol, effective_from; optional effective_to [exclusive],
exchange) and bars_1d.csv (see ``aura.data.normalize``). Extra reference columns are kept
verbatim as uninterpreted attributes; an unknown bar column refuses the load. Other declared
files, such as universe_membership.csv and corporate_actions.csv, are verified, not normalized.
"""

import csv
import hashlib
import io
import json
import re
from collections.abc import Callable, Mapping
from datetime import date, datetime
from itertools import combinations
from pathlib import Path
from typing import Any, NamedTuple

from pydantic import NonNegativeInt, ValidationError

from aura.common import now
from aura.data.contracts import (
    FIXTURE_WEEKDAY_V1,
    ArtifactRef,
    AvailabilityBasis,
    BarInterval,
    DataContract,
    FixtureWeekdayCalendarV1,
    Identifier,
    Instrument,
    ObservationKind,
    ProvenanceClass,
    RawRecordRef,
    SecurityType,
    Sha256Hex,
    SymbolPeriod,
)
from aura.data.normalize import bar_header_problems
from aura.data.ports import (
    Cursor,
    CursorMismatch,
    HealthStatus,
    IdentityMapping,
    MarketDataProvider,
    ProviderBatch,
    ProviderCapabilities,
    ProviderHealth,
    ProviderRights,
    RawRecord,
    RevisionPolicy,
)

MANIFEST_FILE = "manifest.json"
INSTRUMENTS_FILE = "instruments.csv"
SYMBOL_HISTORY_FILE = "symbol_history.csv"
BARS_FILE = "bars_1d.csv"
PARSED_FILES = (INSTRUMENTS_FILE, SYMBOL_HISTORY_FILE, BARS_FILE)
FIXTURE_PROVIDER_PREFIX = "fixture-replay"

_CURSOR_VERSION = "v1"
_CURSOR = re.compile(rf"{_CURSOR_VERSION}:([0-9a-f]{{64}}):([0-9]{{1,12}})")
_SAFE_FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.csv")
_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"} | {
    f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(10)
}
_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_CALENDARS = {FIXTURE_WEEKDAY_V1: FixtureWeekdayCalendarV1}
_MANIFEST_REQUIRED = frozenset({"dataset_id", "version", "calendar", "files", "rights"})
_RIGHTS_KEYS = frozenset({"license", "third_party_content", "provenance_class", "not_market_data"})
_INSTRUMENT_COLUMNS = (("instrument_id", "security_type", "currency"), ("name",))
_SYMBOL_COLUMNS = (("instrument_id", "symbol", "effective_from"), ("effective_to", "exchange"))


class FixtureLoadError(Exception):
    """The dataset was refused; the provider serves nothing from it."""


class FixtureIntegrityError(FixtureLoadError):
    """Bytes on disk do not match what the manifest declares."""


class FixtureSchemaError(FixtureLoadError):
    """The manifest or a file does not follow the supported fixture schema."""


class ManifestFile(DataContract):
    name: Identifier
    sha256: Sha256Hex
    rows: NonNegativeInt


class DatasetManifest(DataContract):
    """Parsed manifest. ``ref.content_hash`` hashes the canonical JSON (sorted keys, compact),
    so the pin survives line-ending or whitespace changes while any value change alters it."""

    ref: ArtifactRef
    dataset_id: Identifier
    version: Identifier
    calendar_id: Identifier
    holidays: tuple[date, ...]
    files: tuple[ManifestFile, ...]
    rights: ProviderRights
    generator: str | None
    seed: str | None
    event_range: str | None
    availability_range: str | None
    scenarios: tuple[str, ...]


class _Table(NamedTuple):
    header: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _describe(value: object) -> str | None:
    if value is None or isinstance(value, str):
        return value
    return _canonical_json(value).decode()


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise FixtureSchemaError(f"{MANIFEST_FILE}: duplicate key {key[:64]!r}")
        result[key] = value
    return result


def _read(dataset_dir: Path, name: str) -> bytes:
    try:
        return (dataset_dir / name).read_bytes()
    except FileNotFoundError as exc:
        raise FixtureIntegrityError(f"{name}: declared file is missing") from exc
    except OSError as exc:
        raise FixtureIntegrityError(f"{name}: file is unreadable") from exc


def _text(document: Mapping[str, Any], key: str) -> str:
    value = document[key]
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if not isinstance(value, str) or not value.strip():
        raise FixtureSchemaError(f"{MANIFEST_FILE}: {key} must be a non-empty string")
    return value


def _rights(value: object) -> ProviderRights:
    if not isinstance(value, dict) or set(value) != _RIGHTS_KEYS:
        raise FixtureSchemaError("rights must declare exactly: " + ", ".join(sorted(_RIGHTS_KEYS)))
    if value["provenance_class"] != ProvenanceClass.SYNTHETIC_FIXTURE:
        raise FixtureSchemaError("the fixture provider only replays SYNTHETIC_FIXTURE datasets")
    if value["not_market_data"] is not True:
        raise FixtureSchemaError("rights must declare not_market_data: true")
    third_party = value["third_party_content"]
    if third_party is not False and not (
        isinstance(third_party, str) and third_party.lower() == "none"
    ):
        raise FixtureSchemaError("third-party content needs a rights review and licensed adapter")
    if not isinstance(value["license"], str) or not value["license"].strip():
        raise FixtureSchemaError("rights.license must be a non-empty string")
    return ProviderRights(
        license=value["license"],
        third_party_content=False,
        provenance_class=ProvenanceClass.SYNTHETIC_FIXTURE,
        not_market_data=True,
    )


def _files(value: object) -> tuple[ManifestFile, ...]:
    if not isinstance(value, dict) or not value:
        raise FixtureSchemaError("files must be a non-empty object")
    entries = []
    for name, spec in sorted(value.items()):
        if not _SAFE_FILE_NAME.fullmatch(name) or name[:-4].upper() in _RESERVED_NAMES:
            raise FixtureSchemaError(f"unsafe or unsupported file name {name[:64]!r}")
        if not isinstance(spec, dict) or set(spec) != {"sha256", "rows"}:
            raise FixtureSchemaError(f"{name}: expected exactly sha256 and rows")
        digest, rows = spec["sha256"], spec["rows"]
        if not isinstance(digest, str) or not isinstance(rows, int) or isinstance(rows, bool):
            raise FixtureSchemaError(f"{name}: sha256 must be a string and rows an integer")
        try:
            entries.append(ManifestFile(name=name, sha256=digest.lower(), rows=rows))
        except ValidationError as exc:
            raise FixtureSchemaError(f"{name}: invalid sha256 or row count") from exc
    if missing := [name for name in PARSED_FILES if name not in value]:
        raise FixtureSchemaError("files must include " + ", ".join(missing))
    return tuple(entries)


def _holidays(definition: object, calendar: str) -> tuple[date, ...]:
    """Declared fixture holidays, after checking the declared definition matches ours."""
    if definition is None:
        return ()
    if not isinstance(definition, dict):
        raise FixtureSchemaError("calendar_definition must be an object")
    reference = _CALENDARS[calendar]()
    expected = {
        "id": calendar,
        "timezone": reference.timezone,
        "session_open_local": reference.session_open_local.strftime("%H:%M"),
        "session_close_local": reference.session_close_local.strftime("%H:%M"),
    }
    for key, value in expected.items():
        if key in definition and definition[key] != value:
            raise FixtureSchemaError(f"calendar_definition.{key} contradicts {calendar}")
    days = definition.get("holidays", [])
    if not isinstance(days, list) or not all(
        isinstance(day, str) and _ISO_DATE.fullmatch(day) for day in days
    ):
        raise FixtureSchemaError("calendar_definition.holidays must list YYYY-MM-DD dates")
    try:
        return tuple(sorted({date.fromisoformat(day) for day in days}))
    except ValueError as exc:
        raise FixtureSchemaError("calendar_definition.holidays has an invalid date") from exc


def load_manifest(dataset_dir: Path) -> DatasetManifest:
    raw = _read(dataset_dir, MANIFEST_FILE)
    try:
        document = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FixtureSchemaError(f"{MANIFEST_FILE} is not valid UTF-8 JSON") from exc
    if not isinstance(document, dict):
        raise FixtureSchemaError(f"{MANIFEST_FILE} must be a JSON object")
    if missing := sorted(_MANIFEST_REQUIRED - document.keys()):
        raise FixtureSchemaError(f"{MANIFEST_FILE} is missing: " + ", ".join(missing))
    calendar = _text(document, "calendar")
    if calendar not in _CALENDARS:
        raise FixtureSchemaError(f"unsupported calendar {calendar[:64]!r}")
    scenarios = document.get("scenarios", [])
    if not isinstance(scenarios, list):
        raise FixtureSchemaError("scenarios must be a list")
    dataset_id, version = _text(document, "dataset_id"), _text(document, "version")
    generator = document.get("generator")
    seed = document.get("seed", generator.get("seed") if isinstance(generator, dict) else None)
    try:
        return DatasetManifest(
            ref=ArtifactRef(
                manifest_id=f"{dataset_id}@{version}",
                content_hash=hashlib.sha256(_canonical_json(document)).hexdigest(),
            ),
            dataset_id=dataset_id,
            version=version,
            calendar_id=calendar,
            holidays=_holidays(document.get("calendar_definition"), calendar),
            files=_files(document["files"]),
            rights=_rights(document["rights"]),
            generator=_describe(generator),
            seed=_describe(seed),
            event_range=_describe(document.get("event_range")),
            availability_range=_describe(document.get("availability_range")),
            scenarios=tuple(
                s if isinstance(s, str) else _canonical_json(s).decode() for s in scenarios
            ),
        )
    except ValidationError as exc:
        raise FixtureSchemaError(f"{MANIFEST_FILE} has invalid values") from exc


def _parse_csv(name: str, data: bytes) -> _Table:
    try:
        text = data.decode("utf-8-sig")
        rows = list(csv.reader(io.StringIO(text, newline=""), strict=True))
    except (UnicodeDecodeError, csv.Error) as exc:
        raise FixtureSchemaError(f"{name}: not well-formed UTF-8 CSV") from exc
    if not rows or not rows[0]:
        raise FixtureSchemaError(f"{name}: header row required")
    header = tuple(column.strip() for column in rows[0])
    if not all(header) or len(set(header)) != len(header):
        raise FixtureSchemaError(f"{name}: blank or duplicate column names")
    return _Table(header, tuple(tuple(row) for row in rows[1:]))


def _verified_tables(dataset_dir: Path, manifest: DatasetManifest) -> dict[str, _Table]:
    tables = {}
    for entry in manifest.files:
        data = _read(dataset_dir, entry.name)
        if hashlib.sha256(data).hexdigest() != entry.sha256:
            raise FixtureIntegrityError(f"{entry.name}: SHA-256 differs from the manifest")
        table = _parse_csv(entry.name, data)
        if len(table.rows) != entry.rows:
            raise FixtureIntegrityError(
                f"{entry.name}: {len(table.rows)} data rows, manifest declares {entry.rows}"
            )
        tables[entry.name] = table
    return tables


def _reference_rows(
    name: str, table: _Table, columns: tuple[tuple[str, ...], tuple[str, ...]]
) -> list[tuple[int, dict[str, str], tuple[tuple[str, str], ...]]]:
    """Rows of a reference file as (position, known values, verbatim extra columns)."""
    required, optional = columns
    if missing := [c for c in required if c not in table.header]:
        raise FixtureSchemaError(f"{name}: missing columns " + ", ".join(missing))
    known = set(required) | set(optional)
    result = []
    for position, row in enumerate(table.rows, start=1):
        if len(row) != len(table.header):
            raise FixtureSchemaError(f"{name} row {position}: wrong number of fields")
        values = {c: v.strip() for c, v in zip(table.header, row, strict=True) if c in known}
        if any(not values[c] for c in required):
            raise FixtureSchemaError(f"{name} row {position}: required value missing")
        extras = tuple((c, v) for c, v in zip(table.header, row, strict=True) if c not in known)
        result.append((position, values, extras))
    return result


def _iso_date(name: str, position: int, value: str) -> date:
    try:
        if _ISO_DATE.fullmatch(value):
            return date.fromisoformat(value)
    except ValueError:
        pass
    raise FixtureSchemaError(f"{name} row {position}: expected a YYYY-MM-DD date")


def _overlap(a: SymbolPeriod, b: SymbolPeriod) -> bool:
    return a.effective_from < (b.effective_to or date.max) and b.effective_from < (
        a.effective_to or date.max
    )


def _instruments(tables: Mapping[str, _Table]) -> dict[str, Instrument]:
    rows: dict[str, tuple[dict[str, str], tuple[tuple[str, str], ...]]] = {}
    for position, values, extras in _reference_rows(
        INSTRUMENTS_FILE, tables[INSTRUMENTS_FILE], _INSTRUMENT_COLUMNS
    ):
        if values["instrument_id"] in rows:
            raise FixtureSchemaError(f"{INSTRUMENTS_FILE} row {position}: duplicate instrument_id")
        rows[values["instrument_id"]] = (values, extras)

    periods: dict[str, list[SymbolPeriod]] = {instrument_id: [] for instrument_id in rows}
    for position, values, extras in _reference_rows(
        SYMBOL_HISTORY_FILE, tables[SYMBOL_HISTORY_FILE], _SYMBOL_COLUMNS
    ):
        if values["instrument_id"] not in rows:
            raise FixtureSchemaError(f"{SYMBOL_HISTORY_FILE} row {position}: unknown instrument")
        effective_to = values.get("effective_to")
        try:
            periods[values["instrument_id"]].append(
                SymbolPeriod(
                    symbol=values["symbol"],
                    effective_from=_iso_date(
                        SYMBOL_HISTORY_FILE, position, values["effective_from"]
                    ),
                    effective_to=(
                        _iso_date(SYMBOL_HISTORY_FILE, position, effective_to)
                        if effective_to
                        else None
                    ),
                    exchange=values.get("exchange") or None,
                    attributes=extras,
                )
            )
        except ValidationError as exc:
            raise FixtureSchemaError(f"{SYMBOL_HISTORY_FILE} row {position}: invalid") from exc

    # One ticker per instrument and one instrument per ticker on any session date.
    by_symbol: dict[str, list[SymbolPeriod]] = {}
    for items in periods.values():
        for period in items:
            by_symbol.setdefault(period.symbol, []).append(period)
    for group in (*periods.values(), *by_symbol.values()):
        if any(_overlap(a, b) for a, b in combinations(group, 2)):
            raise FixtureSchemaError(
                f"{SYMBOL_HISTORY_FILE}: overlapping periods make identity ambiguous"
            )

    instruments = {}
    for instrument_id, (values, extras) in rows.items():
        try:
            instruments[instrument_id] = Instrument(
                instrument_id=instrument_id,
                security_type=SecurityType(values["security_type"].upper()),
                currency=values["currency"].upper(),
                name=values.get("name") or None,
                symbols=tuple(sorted(periods[instrument_id], key=lambda p: p.effective_from)),
                attributes=extras,
            )
        except ValueError as exc:  # includes ValidationError
            raise FixtureSchemaError(
                f"{INSTRUMENTS_FILE}: {instrument_id[:64]!r} has an unsupported security type "
                "or currency; unsupported instruments fail closed"
            ) from exc
    return instruments


def _bar_records(table: _Table, manifest: DatasetManifest) -> tuple[RawRecord, ...]:
    if problems := bar_header_problems(table.header):
        raise FixtureSchemaError(f"{BARS_FILE}: " + "; ".join(problems))
    entry = next(f for f in manifest.files if f.name == BARS_FILE)
    artifact = ArtifactRef(manifest_id=manifest.ref.manifest_id, content_hash=entry.sha256)
    records = []
    for position, row in enumerate(table.rows, start=1):
        names = table.header + tuple(f"_extra_{i}" for i in range(len(table.header), len(row)))
        record_hash = hashlib.sha256(_canonical_json([table.header, row])).hexdigest()
        records.append(
            RawRecord(
                artifact=artifact,
                source=RawRecordRef(stream=BARS_FILE, position=position, record_hash=record_hash),
                fields=tuple(zip(names, row, strict=False)),
                structural_error=(
                    None
                    if len(row) == len(table.header)
                    else f"expected {len(table.header)} fields, found {len(row)}"
                ),
            )
        )
    return tuple(records)


class FixtureReplayProvider(MarketDataProvider):
    """Replays a verified synthetic dataset. Construction fails with FixtureLoadError rather
    than serving anything from a dataset that does not match its manifest."""

    def __init__(self, dataset_dir: str | Path, *, clock: Callable[[], datetime] = now) -> None:
        self._dir = Path(dataset_dir)
        self._clock = clock
        self._manifest = load_manifest(self._dir)
        self._calendar = _CALENDARS[self._manifest.calendar_id](
            frozenset(self._manifest.holidays)
        )
        tables = _verified_tables(self._dir, self._manifest)
        self._instruments = _instruments(tables)
        self._records = _bar_records(tables[BARS_FILE], self._manifest)
        self._provider = f"{FIXTURE_PROVIDER_PREFIX}:{self._manifest.ref.manifest_id}"

    @property
    def manifest(self) -> DatasetManifest:
        return self._manifest

    @property
    def calendar(self) -> FixtureWeekdayCalendarV1:
        return self._calendar

    def capabilities(self) -> ProviderCapabilities:
        unparsed = sorted(f.name for f in self._manifest.files if f.name not in PARSED_FILES)
        return ProviderCapabilities(
            provider=self._provider,
            provenance_class=ProvenanceClass.SYNTHETIC_FIXTURE,
            kinds=(ObservationKind.BAR,),
            bar_intervals=(BarInterval.ONE_DAY,),
            calendar_id=self._calendar.calendar_id,
            session_timezone=self._calendar.timezone,
            identity_mapping=IdentityMapping.STABLE_ID_WITH_EFFECTIVE_DATED_SYMBOLS,
            revision_policy=RevisionPolicy.VERSIONED_RECORD,
            availability_basis=AvailabilityBasis.VERIFIED_HISTORICAL,
            rights=self._manifest.rights,
            rate_limit=None,
            live=False,
            resumable_cursor=True,
            dataset=self._manifest.ref,
            scenarios=self._manifest.scenarios,
            limitations=(
                "REPLAY_ONLY: synthetic replay; no live feed, vendor or entitlement",
                "GENERATOR_DECLARED_AVAILABILITY: available_at is verified only within the "
                "synthetic dataset",
                "NOT_AN_EXCHANGE_CALENDAR: FIXTURE_WEEKDAY_V1 has only declared fixture "
                "holidays and no early closes",
                *(f"VERIFIED_NOT_NORMALIZED: {name}" for name in unparsed),
            ),
        )

    def instruments(self) -> tuple[Instrument, ...]:
        return tuple(self._instruments[key] for key in sorted(self._instruments))

    def read_batch(self, cursor: Cursor | None, max_records: int) -> ProviderBatch:
        if max_records < 1:
            raise ValueError("max_records must be positive")
        start = self._offset(cursor)
        end = min(start + max_records, len(self._records))
        return ProviderBatch(
            provider=self._provider,
            provenance_class=ProvenanceClass.SYNTHETIC_FIXTURE,
            availability_basis=AvailabilityBasis.VERIFIED_HISTORICAL,
            manifest=self._manifest.ref,
            records=self._records[start:end],
            received_at=self._clock(),
            cursor=cursor,
            next_cursor=Cursor(
                provider=self._provider,
                token=f"{_CURSOR_VERSION}:{self._manifest.ref.content_hash}:{end}",
            ),
            exhausted=end == len(self._records),
        )

    def health(self) -> ProviderHealth:
        """Served data is the snapshot verified at load. Degraded means the source on disk has
        since changed or vanished, so a fresh load would be refused or differ."""
        reasons = []
        try:
            if load_manifest(self._dir).ref != self._manifest.ref:
                reasons.append(f"SOURCE_CHANGED: {MANIFEST_FILE}")
        except FixtureLoadError:
            reasons.append(f"SOURCE_INVALID: {MANIFEST_FILE}")
        for entry in self._manifest.files:
            try:
                digest = hashlib.sha256(_read(self._dir, entry.name)).hexdigest()
            except FixtureIntegrityError:
                reasons.append(f"SOURCE_MISSING: {entry.name}")
                continue
            if digest != entry.sha256:
                reasons.append(f"SOURCE_CHANGED: {entry.name}")
        return ProviderHealth(
            provider=self._provider,
            status=HealthStatus.DEGRADED if reasons else HealthStatus.HEALTHY,
            checked_at=self._clock(),
            reasons=tuple(reasons),
        )

    def _offset(self, cursor: Cursor | None) -> int:
        if cursor is None:
            return 0
        match = _CURSOR.fullmatch(cursor.token)
        if (
            cursor.provider != self._provider
            or match is None
            or match.group(1) != self._manifest.ref.content_hash
        ):
            raise CursorMismatch("Cursor does not belong to this dataset")
        offset = int(match.group(2))
        if offset > len(self._records):
            raise CursorMismatch("Cursor is beyond the end of this dataset")
        return offset

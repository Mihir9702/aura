"""Immutable market-data contracts: the implemented subset of 12-contracts MarketObservation.

Only BAR payloads exist in this slice. Every observation carries a provenance class and raw
lineage; rejected input becomes a QuarantineRecord with reason codes and is never dropped.

Time semantics (all instants are timezone-aware and normalized to UTC):

- A bar describes its whole interval, so its ``event_time`` is ``interval_end``.
- ``available_at`` is when the record became knowable. A final bar cannot be knowable before
  its interval ends; a provisional bar cannot be knowable before its interval starts.
- ``published_at`` (optional) is the source's publication time and cannot follow availability.
- ``received_at``/``recorded_at`` are Aura's own receipt times. For imported history they are
  import times, distinct from availability, and they never take part in duplicate detection.
- Point-in-time reads require ``event_time <= as_of`` and ``available_at <= knowledge_cutoff``;
  UNKNOWN availability cannot support an as-of assertion.
"""

import hashlib
import json
import uuid
from datetime import UTC, date, datetime, time
from decimal import Decimal
from enum import Enum, StrEnum
from functools import cache
from typing import Annotated, Literal, Self
from zoneinfo import ZoneInfo

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    PositiveInt,
    StringConstraints,
    model_validator,
)

MARKET_OBSERVATION_SCHEMA_VERSION: Literal[1] = 1
QUARANTINE_SCHEMA_VERSION: Literal[1] = 1
ZERO_VOLUME = "ZERO_VOLUME"

_ID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "urn:aura:data:v1")


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


Instant = Annotated[AwareDatetime, AfterValidator(_utc)]
Identifier = Annotated[str, StringConstraints(min_length=1, max_length=512)]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
CurrencyCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]


class DataContract(BaseModel):
    """Frozen and strict: no coercion (a float price is rejected) and no unknown fields."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class ProvenanceClass(StrEnum):
    """Origin class of observation content.

    Only synthetic fixtures exist in this pass. A vendor class is added together with an
    approved adapter (OD-04); it is never inferred from data.
    """

    SYNTHETIC_FIXTURE = "SYNTHETIC_FIXTURE"


class ObservationKind(StrEnum):
    QUOTE = "QUOTE"
    BAR = "BAR"
    TRADE = "TRADE"
    FUNDAMENTAL = "FUNDAMENTAL"
    NEWS = "NEWS"
    CORPORATE_ACTION = "CORPORATE_ACTION"
    MACRO = "MACRO"


class AvailabilityBasis(StrEnum):
    LIVE_RECEIPT = "LIVE_RECEIPT"
    VERIFIED_HISTORICAL = "VERIFIED_HISTORICAL"
    UNKNOWN = "UNKNOWN"


class BarInterval(StrEnum):
    ONE_DAY = "1d"


class AdjustmentBasis(StrEnum):
    """Price adjustment of a bar. Adjusted series would be separate versioned records."""

    RAW = "RAW"


class SecurityType(StrEnum):
    EQUITY = "EQUITY"
    ETF = "ETF"


class QuarantineReason(StrEnum):
    """Why a record was rejected. A record may carry several reasons."""

    MALFORMED_RECORD = "MALFORMED_RECORD"
    UNKNOWN_FIELD = "UNKNOWN_FIELD"
    MISSING_FIELD = "MISSING_FIELD"
    UNKNOWN_INSTRUMENT = "UNKNOWN_INSTRUMENT"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
    NAIVE_TIMESTAMP = "NAIVE_TIMESTAMP"
    MALFORMED_TIMESTAMP = "MALFORMED_TIMESTAMP"
    INVALID_NUMBER = "INVALID_NUMBER"
    INVALID_ENUM = "INVALID_ENUM"
    INVALID_REVISION = "INVALID_REVISION"
    NON_POSITIVE_PRICE = "NON_POSITIVE_PRICE"
    NEGATIVE_VOLUME = "NEGATIVE_VOLUME"
    HIGH_BELOW_LOW = "HIGH_BELOW_LOW"
    PRICE_OUTSIDE_RANGE = "PRICE_OUTSIDE_RANGE"
    INTERVAL_ORDER = "INTERVAL_ORDER"
    OFF_CALENDAR = "OFF_CALENDAR"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    AVAILABILITY_BEFORE_EVENT = "AVAILABILITY_BEFORE_EVENT"
    AVAILABILITY_BEFORE_PUBLICATION = "AVAILABILITY_BEFORE_PUBLICATION"
    CONFLICTING_DUPLICATE = "CONFLICTING_DUPLICATE"


class ArtifactRef(DataContract):
    manifest_id: Identifier
    content_hash: Sha256Hex


class RawRecordRef(DataContract):
    """Lineage of one raw record: its stream, 1-based position and content hash."""

    stream: Identifier
    position: PositiveInt
    record_hash: Sha256Hex


class BarPayload(DataContract):
    kind: Literal[ObservationKind.BAR] = ObservationKind.BAR
    interval_start: Instant
    interval_end: Instant
    interval: BarInterval
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    final: bool
    adjustment_basis: AdjustmentBasis
    currency: CurrencyCode

    @model_validator(mode="after")
    def _bounds(self) -> Self:
        if self.interval_end <= self.interval_start:
            raise ValueError("interval_end must follow interval_start")
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError("Bar prices must be positive")
        if self.high < self.low:
            raise ValueError("Bar high is below low")
        if not (self.low <= self.open <= self.high and self.low <= self.close <= self.high):
            raise ValueError("Bar open and close must lie within [low, high]")
        if self.volume < 0:
            raise ValueError("Bar volume cannot be negative")
        return self


class MarketObservation(DataContract):
    """One immutable, attributable provider fact. Corrections are new revisions."""

    id: Identifier
    schema_version: Literal[1]
    instrument_id: Identifier
    provider: Identifier
    provider_record_id: Identifier
    revision: PositiveInt
    kind: ObservationKind
    event_time: Instant
    published_at: Instant | None
    received_at: Instant
    available_at: Instant
    recorded_at: Instant
    availability_basis: AvailabilityBasis
    provenance_class: ProvenanceClass
    raw_artifact: ArtifactRef
    raw_record: RawRecordRef
    quality_flags: tuple[str, ...]
    supersedes_id: Identifier | None
    payload: BarPayload

    @model_validator(mode="after")
    def _temporal(self) -> Self:
        if self.kind is not ObservationKind.BAR:
            raise ValueError("Only BAR observations are implemented")
        bar = self.payload
        if self.event_time != bar.interval_end:
            raise ValueError("A bar's event_time is its interval_end")
        if self.published_at is not None and self.published_at > self.available_at:
            raise ValueError("available_at cannot precede published_at")
        earliest = min(self.available_at, self.published_at or self.available_at)
        if earliest < (bar.interval_end if bar.final else bar.interval_start):
            raise ValueError("A bar cannot be knowable before it exists")
        if (
            self.availability_basis is AvailabilityBasis.LIVE_RECEIPT
            and self.available_at < self.received_at
        ):
            raise ValueError("Live availability cannot precede receipt")
        if self.quality_flags != tuple(sorted(set(self.quality_flags))):
            raise ValueError("quality_flags must be sorted and unique")
        return self


class QuarantineRecord(DataContract):
    """A rejected record kept for diagnosis. Raw fields are untrusted data, never instructions."""

    id: Identifier
    schema_version: Literal[1]
    provider: Identifier
    provenance_class: ProvenanceClass
    raw_artifact: ArtifactRef
    raw_record: RawRecordRef
    raw_fields: tuple[tuple[str, str], ...]
    reasons: tuple[QuarantineReason, ...]
    details: tuple[str, ...]
    instrument_id: str | None
    provider_record_id: str | None
    revision: int | None
    conflicts_with: str | None
    rejected_observation: MarketObservation | None
    received_at: Instant
    recorded_at: Instant

    @model_validator(mode="after")
    def _reasoned(self) -> Self:
        if not self.reasons or self.reasons != tuple(sorted(set(self.reasons))):
            raise ValueError("Quarantine needs sorted, unique reason codes")
        return self


class SymbolPeriod(DataContract):
    """A ticker effective on session dates [effective_from, effective_to); open when None."""

    symbol: Identifier
    effective_from: date
    effective_to: date | None
    exchange: str | None
    attributes: tuple[tuple[str, str], ...] = ()

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.effective_to is not None and self.effective_to <= self.effective_from:
            raise ValueError("effective_to must follow effective_from")
        return self

    def covers(self, session_date: date) -> bool:
        return self.effective_from <= session_date and (
            self.effective_to is None or session_date < self.effective_to
        )


class Instrument(DataContract):
    """Stable identity; symbols are effective-dated attributes. Delisted entries stay listed."""

    instrument_id: Identifier
    security_type: SecurityType
    currency: CurrencyCode
    name: str | None
    symbols: tuple[SymbolPeriod, ...]
    attributes: tuple[tuple[str, str], ...] = ()

    def symbol_on(self, session_date: date) -> str | None:
        return next((p.symbol for p in self.symbols if p.covers(session_date)), None)


class TradingSession(DataContract):
    calendar_id: Identifier
    session_date: date
    open: Instant
    close: Instant


FIXTURE_WEEKDAY_V1 = "FIXTURE_WEEKDAY_V1"


@cache
def _new_york() -> ZoneInfo:
    # Loaded lazily: Windows needs the tzdata package, which psycopg already installs there.
    return ZoneInfo("America/New_York")


class FixtureWeekdayCalendarV1:
    """FIXTURE_WEEKDAY_V1: every Monday-Friday except the dataset's declared fixture holidays
    is one 09:30-16:00 America/New_York session.

    A synthetic-fixture calendar only: it has no early closes or exchange rules and is not an
    approved exchange calendar (OD-03). Session instants are DST-aware UTC.
    """

    calendar_id = FIXTURE_WEEKDAY_V1
    timezone = "America/New_York"
    session_open_local = time(9, 30)
    session_close_local = time(16, 0)

    def __init__(self, holidays: frozenset[date] = frozenset()) -> None:
        self.holidays = holidays

    def session(self, session_date: date) -> TradingSession | None:
        if session_date.weekday() >= 5 or session_date in self.holidays:
            return None
        zone = _new_york()
        return TradingSession(
            calendar_id=self.calendar_id,
            session_date=session_date,
            open=datetime.combine(session_date, self.session_open_local, zone).astimezone(UTC),
            close=datetime.combine(session_date, self.session_close_local, zone).astimezone(UTC),
        )

    def session_date_of(self, instant: datetime) -> date:
        if instant.tzinfo is None:
            raise ValueError("Timezone-aware instant required")
        return instant.astimezone(_new_york()).date()


def _stable_id(*parts: str | int) -> str:
    return str(uuid.uuid5(_ID_NAMESPACE, json.dumps(parts, separators=(",", ":"))))


def observation_id(provider: str, provider_record_id: str, revision: int) -> str:
    """Deterministic ID of an identity, so replays and re-deliveries reuse it."""
    return _stable_id("observation", provider, provider_record_id, revision)


def quarantine_id(*parts: str | int) -> str:
    return _stable_id("quarantine", *parts)


def _canonical(value: object) -> object:
    if isinstance(value, Decimal):
        return "0" if value == 0 else format(value.normalize(), "f")
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, BaseModel):
        return {name: _canonical(item) for name, item in value}
    if isinstance(value, tuple | list):
        return [_canonical(item) for item in value]
    return value


_RECEIPT_FIELDS = frozenset({"received_at", "recorded_at", "raw_artifact", "raw_record"})


def content_fingerprint(observation: MarketObservation) -> str:
    """Hash of the provider fact. Receipt times and raw lineage are excluded, and decimals
    compare numerically, so a re-delivery of the same fact is an identical duplicate."""
    fact = {
        name: _canonical(value)
        for name, value in observation
        if name not in _RECEIPT_FIELDS
    }
    encoded = json.dumps(fact, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()

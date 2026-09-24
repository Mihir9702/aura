"""Normalize provider BAR records into immutable observations or reasoned quarantine.

Every raw record yields exactly one outcome: a MarketObservation, or a QuarantineRecord with
every reason code that applies. Nothing is repaired or dropped.

Bar record fields (FIXTURE_BARS_V1; names follow 12-contracts):

- required: ``interval_start``, ``interval_end``, ``open``, ``high``, ``low``, ``close``,
  ``volume``, ``final``, ``adjustment_basis``, ``available_at``, plus ``instrument_id`` and/or
  ``symbol`` (a symbol maps through effective-dated history on the session date).
- optional: ``provider_record_id`` (default ``<instrument_id>/<interval>/<interval_start>``),
  ``revision`` (default 1), ``interval`` (must be ``1d``), ``currency`` (default and required
  match: the instrument's currency), ``published_at``, ``session_date``.

Timestamps must be ISO-8601 with an explicit offset; they are converted to UTC. Decimals must
be finite with at most eight fractional places (``aura.common.amount``).
"""

import re
from collections.abc import Collection, Iterable
from datetime import UTC, date, datetime
from decimal import Decimal

from pydantic import ValidationError

from aura.common import amount
from aura.data.contracts import (
    MARKET_OBSERVATION_SCHEMA_VERSION,
    QUARANTINE_SCHEMA_VERSION,
    ZERO_VOLUME,
    AdjustmentBasis,
    ArtifactRef,
    BarInterval,
    BarPayload,
    DataContract,
    Identifier,
    Instrument,
    MarketObservation,
    ObservationKind,
    QuarantineReason,
    QuarantineRecord,
    SymbolPeriod,
    observation_id,
    quarantine_id,
)
from aura.data.ports import Cursor, ProviderBatch, RawRecord, SessionCalendar

BAR_REQUIRED_FIELDS = frozenset(
    {
        "interval_start",
        "interval_end",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "final",
        "adjustment_basis",
        "available_at",
    }
)
BAR_IDENTITY_FIELDS = frozenset({"instrument_id", "symbol"})
BAR_OPTIONAL_FIELDS = BAR_IDENTITY_FIELDS | {
    "provider_record_id",
    "revision",
    "interval",
    "currency",
    "published_at",
    "session_date",
}
BAR_FIELDS = BAR_REQUIRED_FIELDS | BAR_OPTIONAL_FIELDS

_REVISION = re.compile(r"[1-9][0-9]{0,8}")
_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_PRICE_FIELDS = ("open", "high", "low", "close")


def bar_header_problems(header: Collection[str]) -> tuple[str, ...]:
    """Schema problems with a bar stream header. Empty means the header is acceptable."""
    problems = []
    names = list(header)
    if len(set(names)) != len(names):
        problems.append("duplicate column names")
    if missing := sorted(BAR_REQUIRED_FIELDS - set(names)):
        problems.append("missing required columns: " + ", ".join(missing))
    if not BAR_IDENTITY_FIELDS & set(names):
        problems.append("an instrument_id or symbol column is required")
    if unknown := sorted(set(names) - BAR_FIELDS):
        problems.append("unknown columns: " + ", ".join(name[:64] for name in unknown))
    return tuple(problems)


class NormalizedBatch(DataContract):
    provider: Identifier
    manifest: ArtifactRef
    observations: tuple[MarketObservation, ...]
    quarantined: tuple[QuarantineRecord, ...]
    next_cursor: Cursor
    exhausted: bool


class IdentityIndex:
    """Resolves stable instrument IDs and effective-dated symbols."""

    def __init__(self, instruments: Iterable[Instrument]) -> None:
        self._by_id = {i.instrument_id: i for i in instruments}
        self._by_symbol: dict[str, list[tuple[SymbolPeriod, Instrument]]] = {}
        for instrument in self._by_id.values():
            for period in instrument.symbols:
                self._by_symbol.setdefault(period.symbol, []).append((period, instrument))

    def get(self, instrument_id: str) -> Instrument | None:
        return self._by_id.get(instrument_id)

    def by_symbol(self, symbol: str, session_date: date) -> tuple[Instrument, ...]:
        return tuple(i for p, i in self._by_symbol.get(symbol, ()) if p.covers(session_date))


class _Fields:
    """Parses one raw record, collecting reason codes instead of raising."""

    def __init__(self, record: RawRecord) -> None:
        self.values: dict[str, str] = {}
        self.reasons: set[QuarantineReason] = set()
        self.details: list[str] = []
        if record.structural_error is not None:
            # Values cannot be trusted to line up with columns; interpret nothing.
            self.fail(QuarantineReason.MALFORMED_RECORD, record.structural_error)
            return
        for name, value in record.fields:
            if name in self.values:
                self.fail(QuarantineReason.MALFORMED_RECORD, "duplicate field name")
            self.values[name] = value
        if set(self.values) - BAR_FIELDS:
            self.fail(QuarantineReason.UNKNOWN_FIELD, "record has fields outside FIXTURE_BARS_V1")

    def fail(self, reason: QuarantineReason, detail: str) -> None:
        self.reasons.add(reason)
        self.details.append(detail)

    def text(self, name: str, *, required: bool) -> str | None:
        value = self.values.get(name, "").strip()
        if value:
            return value
        if required:
            self.fail(QuarantineReason.MISSING_FIELD, f"{name}: required value missing")
        return None

    def instant(self, name: str, *, required: bool) -> datetime | None:
        value = self.text(name, required=required)
        if value is None:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            self.fail(QuarantineReason.MALFORMED_TIMESTAMP, f"{name}: not an ISO-8601 timestamp")
            return None
        if parsed.utcoffset() is None:
            self.fail(QuarantineReason.NAIVE_TIMESTAMP, f"{name}: timezone offset missing")
            return None
        return parsed.astimezone(UTC)

    def number(self, name: str) -> Decimal | None:
        value = self.text(name, required=True)
        if value is None:
            return None
        try:
            return amount(value)
        except (ArithmeticError, ValueError):
            self.fail(QuarantineReason.INVALID_NUMBER, f"{name}: not a finite supported decimal")
            return None

    def final(self) -> bool | None:
        value = self.text("final", required=True)
        if value is None:
            return None
        if value.lower() not in {"true", "false"}:
            self.fail(QuarantineReason.INVALID_ENUM, "final: expected true or false")
            return None
        return value.lower() == "true"

    def adjustment_basis(self) -> AdjustmentBasis | None:
        value = self.text("adjustment_basis", required=True)
        if value is None:
            return None
        try:
            return AdjustmentBasis(value.upper())
        except ValueError:
            self.fail(QuarantineReason.INVALID_ENUM, "adjustment_basis: unsupported value")
            return None

    def interval(self) -> BarInterval | None:
        value = self.text("interval", required=False)
        if value is not None and value != BarInterval.ONE_DAY:
            self.fail(QuarantineReason.INVALID_ENUM, "interval: only 1d bars are supported")
            return None
        return BarInterval.ONE_DAY

    def revision(self) -> int | None:
        value = self.text("revision", required=False)
        if value is None:
            return 1
        if not _REVISION.fullmatch(value):
            self.fail(QuarantineReason.INVALID_REVISION, "revision: expected a positive integer")
            return None
        return int(value)

    def session_date(self) -> date | None:
        value = self.text("session_date", required=False)
        if value is None:
            return None
        try:
            if _ISO_DATE.fullmatch(value):
                return date.fromisoformat(value)
        except ValueError:
            pass
        self.fail(QuarantineReason.MALFORMED_TIMESTAMP, "session_date: expected YYYY-MM-DD")
        return None


def _resolve_identity(
    fields: _Fields, index: IdentityIndex, session_date: date | None
) -> Instrument | None:
    instrument_id = fields.text("instrument_id", required=False)
    symbol = fields.text("symbol", required=False)
    if instrument_id is None and symbol is None:
        fields.fail(QuarantineReason.MISSING_FIELD, "instrument_id or symbol required")
        return None
    mapped: Instrument | None = None
    if symbol is not None and session_date is not None:
        matches = index.by_symbol(symbol, session_date)
        if len(matches) == 1:
            mapped = matches[0]
        elif len(matches) > 1:
            fields.fail(QuarantineReason.IDENTITY_MISMATCH, "symbol maps to several instruments")
        elif instrument_id is None:
            fields.fail(
                QuarantineReason.UNKNOWN_INSTRUMENT, "symbol has no mapping on the session date"
            )
    if instrument_id is None:
        return mapped
    instrument = index.get(instrument_id)
    if instrument is None:
        fields.fail(QuarantineReason.UNKNOWN_INSTRUMENT, "instrument_id is not in the master")
        return None
    if symbol is not None and session_date is not None and mapped is not instrument:
        fields.fail(
            QuarantineReason.IDENTITY_MISMATCH,
            "symbol is not effective for instrument_id on the session date",
        )
    return instrument


def _quarantine(
    record: RawRecord,
    batch: ProviderBatch,
    fields: _Fields,
    recorded_at: datetime,
    *,
    instrument: Instrument | None = None,
    record_id: str | None = None,
    revision: int | None = None,
) -> QuarantineRecord:
    return QuarantineRecord(
        id=quarantine_id(
            batch.provider, record.source.stream, record.source.position, record.source.record_hash
        ),
        schema_version=QUARANTINE_SCHEMA_VERSION,
        provider=batch.provider,
        provenance_class=batch.provenance_class,
        raw_artifact=record.artifact,
        raw_record=record.source,
        raw_fields=record.fields,
        reasons=tuple(sorted(fields.reasons)),
        details=tuple(fields.details),
        instrument_id=instrument.instrument_id if instrument is not None else None,
        provider_record_id=record_id,
        revision=revision,
        conflicts_with=None,
        rejected_observation=None,
        received_at=batch.received_at,
        recorded_at=recorded_at,
    )


def _normalize(
    record: RawRecord,
    batch: ProviderBatch,
    index: IdentityIndex,
    calendar: SessionCalendar,
    recorded_at: datetime,
) -> MarketObservation | QuarantineRecord:
    fields = _Fields(record)
    if record.structural_error is not None:
        return _quarantine(record, batch, fields, recorded_at)
    start = fields.instant("interval_start", required=True)
    end = fields.instant("interval_end", required=True)
    available_at = fields.instant("available_at", required=True)
    published_at = fields.instant("published_at", required=False)
    open_, high, low, close = (fields.number(name) for name in _PRICE_FIELDS)
    volume = fields.number("volume")
    final = fields.final()
    basis = fields.adjustment_basis()
    interval = fields.interval()
    revision = fields.revision()
    declared_session = fields.session_date()

    session_date: date | None = None
    if start is not None and end is not None:
        if end <= start:
            fields.fail(QuarantineReason.INTERVAL_ORDER, "interval_end must follow interval_start")
        else:
            session_date = calendar.session_date_of(start)
            session = calendar.session(session_date)
            if session is None or (start, end) != (session.open, session.close):
                fields.fail(
                    QuarantineReason.OFF_CALENDAR, f"not a {calendar.calendar_id} session interval"
                )
            if declared_session is not None and declared_session != session_date:
                fields.fail(
                    QuarantineReason.OFF_CALENDAR, "session_date does not match interval_start"
                )

    instrument = _resolve_identity(fields, index, session_date)

    if open_ is not None and high is not None and low is not None and close is not None:
        if min(open_, high, low, close) <= 0:
            fields.fail(QuarantineReason.NON_POSITIVE_PRICE, "prices must be positive")
        if high < low:
            fields.fail(QuarantineReason.HIGH_BELOW_LOW, "high is below low")
        elif not (low <= open_ <= high and low <= close <= high):
            fields.fail(QuarantineReason.PRICE_OUTSIDE_RANGE, "open/close outside [low, high]")
    if volume is not None and volume < 0:
        fields.fail(QuarantineReason.NEGATIVE_VOLUME, "volume cannot be negative")

    currency = fields.text("currency", required=False)
    if instrument is not None:
        if currency is not None and currency.upper() != instrument.currency:
            fields.fail(QuarantineReason.CURRENCY_MISMATCH, "currency differs from instrument")
        currency = instrument.currency

    if available_at is not None and published_at is not None and published_at > available_at:
        fields.fail(
            QuarantineReason.AVAILABILITY_BEFORE_PUBLICATION, "available_at precedes published_at"
        )
    if final is not None and start is not None and end is not None and available_at is not None:
        earliest = min(available_at, published_at or available_at)
        if earliest < (end if final else start):
            fields.fail(
                QuarantineReason.AVAILABILITY_BEFORE_EVENT,
                "knowable before the bar could exist (final bars: interval_end)",
            )

    record_id = fields.text("provider_record_id", required=False)
    if record_id is None and instrument is not None and start is not None:
        record_id = f"{instrument.instrument_id}/{BarInterval.ONE_DAY}/{start:%Y-%m-%dT%H:%M:%SZ}"

    if (
        fields.reasons
        or instrument is None
        or record_id is None
        or revision is None
        or start is None
        or end is None
        or available_at is None
        or open_ is None
        or high is None
        or low is None
        or close is None
        or volume is None
        or final is None
        or basis is None
        or interval is None
        or currency is None
    ):
        if not fields.reasons:  # defensive: every unparsed value should already have a reason
            fields.fail(QuarantineReason.MALFORMED_RECORD, "unparsed field")
        return _quarantine(
            record,
            batch,
            fields,
            recorded_at,
            instrument=instrument,
            record_id=record_id,
            revision=revision,
        )
    try:
        return MarketObservation(
            id=observation_id(batch.provider, record_id, revision),
            schema_version=MARKET_OBSERVATION_SCHEMA_VERSION,
            instrument_id=instrument.instrument_id,
            provider=batch.provider,
            provider_record_id=record_id,
            revision=revision,
            kind=ObservationKind.BAR,
            event_time=end,
            published_at=published_at,
            received_at=batch.received_at,
            available_at=available_at,
            recorded_at=recorded_at,
            availability_basis=batch.availability_basis,
            provenance_class=batch.provenance_class,
            raw_artifact=record.artifact,
            raw_record=record.source,
            quality_flags=(ZERO_VOLUME,) if volume == 0 else (),
            supersedes_id=None,
            payload=BarPayload(
                interval_start=start,
                interval_end=end,
                interval=interval,
                open=open_,
                high=high,
                low=low,
                close=close,
                volume=volume,
                final=final,
                adjustment_basis=basis,
                currency=currency,
            ),
        )
    except ValidationError:
        fields.fail(QuarantineReason.MALFORMED_RECORD, "failed contract validation")
        return _quarantine(
            record,
            batch,
            fields,
            recorded_at,
            instrument=instrument,
            record_id=record_id,
            revision=revision,
        )


def normalize_bars(
    batch: ProviderBatch,
    *,
    instruments: Iterable[Instrument],
    calendar: SessionCalendar,
    recorded_at: datetime,
) -> NormalizedBatch:
    """Normalize one provider batch; ``len(observations) + len(quarantined)`` equals the
    number of raw records. Duplicate and conflict detection across batches is the store's job
    because it needs every previously accepted observation."""
    if recorded_at.tzinfo is None:
        raise ValueError("Timezone-aware recorded_at required")
    index = IdentityIndex(instruments)
    observations: list[MarketObservation] = []
    quarantined: list[QuarantineRecord] = []
    for record in batch.records:
        outcome = _normalize(record, batch, index, calendar, recorded_at)
        if isinstance(outcome, MarketObservation):
            observations.append(outcome)
        else:
            quarantined.append(outcome)
    return NormalizedBatch(
        provider=batch.provider,
        manifest=batch.manifest,
        observations=tuple(observations),
        quarantined=tuple(quarantined),
        next_cursor=batch.next_cursor,
        exhausted=batch.exhausted,
    )

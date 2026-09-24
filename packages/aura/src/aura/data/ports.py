"""Data ports: what a market-data source and a point-in-time reader must provide.

The synthetic fixture replay provider implements MarketDataProvider now. An approved vendor
adapter (OD-04) implements the same port later; consumers depend only on these interfaces.
"""

from collections.abc import Collection
from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Protocol

from pydantic import StringConstraints

from aura.data.contracts import (
    ArtifactRef,
    AvailabilityBasis,
    BarInterval,
    DataContract,
    Identifier,
    Instant,
    Instrument,
    MarketObservation,
    ObservationKind,
    ProvenanceClass,
    RawRecordRef,
    Sha256Hex,
    TradingSession,
)

NonBlank = Annotated[str, StringConstraints(min_length=1, pattern=r"\S")]


class CursorMismatch(ValueError):
    """A cursor is malformed or belongs to another provider or dataset. Never guess a position."""


class IdentityMapping(StrEnum):
    # Records carry stable instrument IDs; tickers are effective-dated attributes that may
    # also map a record, and a mismatch between the two is quarantined.
    STABLE_ID_WITH_EFFECTIVE_DATED_SYMBOLS = "STABLE_ID_WITH_EFFECTIVE_DATED_SYMBOLS"


class RevisionPolicy(StrEnum):
    # Revisions share provider_record_id; a higher revision supersedes lower ones only once
    # it is available. Nothing is overwritten.
    VERSIONED_RECORD = "VERSIONED_RECORD"


class HealthStatus(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"


class ProviderRights(DataContract):
    license: NonBlank
    third_party_content: bool
    provenance_class: ProvenanceClass
    not_market_data: bool


class ProviderCapabilities(DataContract):
    provider: Identifier
    provenance_class: ProvenanceClass
    kinds: tuple[ObservationKind, ...]
    bar_intervals: tuple[BarInterval, ...]
    calendar_id: Identifier
    session_timezone: Identifier
    identity_mapping: IdentityMapping
    revision_policy: RevisionPolicy
    availability_basis: AvailabilityBasis
    rights: ProviderRights
    rate_limit: str | None
    live: bool
    resumable_cursor: bool
    dataset: ArtifactRef | None
    scenarios: tuple[str, ...]
    limitations: tuple[str, ...]


class ProviderHealth(DataContract):
    provider: Identifier
    status: HealthStatus
    checked_at: Instant
    reasons: tuple[str, ...]


class Cursor(DataContract):
    """Opaque, serializable resume position.

    Persist ``next_cursor`` only after the batch it follows has been committed. Resuming an
    older cursor re-delivers records, which consumers must treat as duplicates.
    """

    provider: Identifier
    token: Identifier


class RawRecord(DataContract):
    """One record exactly as the source delivered it: untrusted strings plus lineage."""

    artifact: ArtifactRef
    source: RawRecordRef
    fields: tuple[tuple[str, str], ...]
    structural_error: str | None = None


class ProviderBatch(DataContract):
    provider: Identifier
    provenance_class: ProvenanceClass
    availability_basis: AvailabilityBasis
    manifest: ArtifactRef
    records: tuple[RawRecord, ...]
    received_at: Instant
    cursor: Cursor | None
    next_cursor: Cursor
    exhausted: bool


class ReadExclusionReason(StrEnum):
    PROVISIONAL = "PROVISIONAL"
    AVAILABILITY_UNKNOWN = "AVAILABILITY_UNKNOWN"
    CONFLICTING_DUPLICATE = "CONFLICTING_DUPLICATE"


class UnavailableReason(StrEnum):
    NO_ELIGIBLE_OBSERVATIONS = "NO_ELIGIBLE_OBSERVATIONS"
    MANIFEST_UNAVAILABLE = "MANIFEST_UNAVAILABLE"


class ReadExclusion(DataContract):
    observation_id: Identifier
    instrument_id: Identifier
    reason: ReadExclusionReason


class InstrumentUnavailable(DataContract):
    instrument_id: Identifier
    reason: UnavailableReason


class AsOfRead(DataContract):
    """Eligible observations plus explicit exclusions and unavailability.

    Records that were not yet knowable at the cutoff are simply absent: reporting them, even
    as counts, would leak the future into the read.
    """

    as_of: Instant
    knowledge_cutoff: Instant
    manifest: ArtifactRef | None
    include_provisional: bool
    observations: tuple[MarketObservation, ...]
    excluded: tuple[ReadExclusion, ...]
    unavailable: tuple[InstrumentUnavailable, ...]
    selection_hash: Sha256Hex


class SessionCalendar(Protocol):
    @property
    def calendar_id(self) -> str: ...

    def session(self, session_date: date) -> TradingSession | None: ...

    def session_date_of(self, instant: datetime) -> date: ...


class MarketDataProvider(Protocol):
    def capabilities(self) -> ProviderCapabilities: ...

    def instruments(self) -> tuple[Instrument, ...]: ...

    def read_batch(self, cursor: Cursor | None, max_records: int) -> ProviderBatch: ...

    def health(self) -> ProviderHealth: ...


class ObservationReader(Protocol):
    def read_as_of(
        self,
        instrument_ids: Collection[str],
        as_of: datetime,
        knowledge_cutoff: datetime,
        manifest: ArtifactRef | None,
        *,
        include_provisional: bool = False,
    ) -> AsOfRead:
        """Return BAR observations with event_time <= as_of and available_at <=
        knowledge_cutoff, taking each record's highest revision knowable at the cutoff.

        Provisional bars are excluded unless ``include_provisional``; close-based reads must
        leave it False. A pinned ``manifest`` restricts the read to that dataset.
        """
        ...

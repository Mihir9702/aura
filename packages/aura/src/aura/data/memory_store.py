"""In-memory ObservationReader for downstream unit tests. It is not operational persistence.

It applies the identity rules a durable store must also enforce:

- Identity is ``(provider, provider_record_id, revision)``. Re-delivering the same fact is an
  identical duplicate and is counted, not stored twice. Every delivered item lands in exactly
  one IngestReport bucket, including re-delivered quarantine records.
- A different fact under an existing identity is quarantined as CONFLICTING_DUPLICATE. The
  accepted observation is never overwritten. Once the conflicting variant is knowable at a
  read's cutoff, that record is excluded (fail closed) instead of silently choosing a winner.
- Reads pick each record's highest revision knowable at the cutoff, so a later correction
  cannot leak into an earlier decision and cannot overwrite what that decision saw.
"""

import hashlib
import json
from collections.abc import Collection
from datetime import UTC, datetime

from aura.common import Conflict
from aura.data.contracts import (
    QUARANTINE_SCHEMA_VERSION,
    ArtifactRef,
    AvailabilityBasis,
    DataContract,
    MarketObservation,
    QuarantineReason,
    QuarantineRecord,
    content_fingerprint,
    quarantine_id,
)
from aura.data.normalize import NormalizedBatch
from aura.data.ports import (
    AsOfRead,
    InstrumentUnavailable,
    ObservationReader,
    ReadExclusion,
    ReadExclusionReason,
    UnavailableReason,
)

_Identity = tuple[str, str, int]


class IngestReport(DataContract):
    accepted: tuple[str, ...]
    duplicates: tuple[str, ...]
    quarantined: tuple[str, ...]
    quarantine_duplicates: tuple[str, ...]


class InMemoryObservationStore(ObservationReader):
    def __init__(self) -> None:
        self._observations: dict[_Identity, MarketObservation] = {}
        self._fingerprints: dict[_Identity, str] = {}
        self._contested: dict[_Identity, list[datetime]] = {}
        self._quarantine: dict[str, QuarantineRecord] = {}
        self._manifests: dict[str, str] = {}

    @property
    def observations(self) -> tuple[MarketObservation, ...]:
        return tuple(sorted(self._observations.values(), key=_order))

    @property
    def quarantine(self) -> tuple[QuarantineRecord, ...]:
        return tuple(self._quarantine.values())

    def ingest(self, batch: NormalizedBatch) -> IngestReport:
        known = self._manifests.setdefault(batch.manifest.manifest_id, batch.manifest.content_hash)
        if known != batch.manifest.content_hash:
            raise Conflict("Manifest ID already registered with different content")
        accepted: list[str] = []
        duplicates: list[str] = []
        quarantined: list[str] = []
        requarantined: list[str] = []
        for record in batch.quarantined:
            (quarantined if self._keep(record) else requarantined).append(record.id)
        for observation in batch.observations:
            identity = (observation.provider, observation.provider_record_id, observation.revision)
            fingerprint = content_fingerprint(observation)
            existing = self._observations.get(identity)
            if existing is None:
                self._observations[identity] = observation
                self._fingerprints[identity] = fingerprint
                accepted.append(observation.id)
            elif self._fingerprints[identity] == fingerprint:
                duplicates.append(observation.id)
            else:
                conflict = _conflict(observation, existing, fingerprint)
                if self._keep(conflict):
                    quarantined.append(conflict.id)
                    self._contested.setdefault(identity, []).append(observation.available_at)
                else:
                    requarantined.append(conflict.id)
        return IngestReport(
            accepted=tuple(accepted),
            duplicates=tuple(duplicates),
            quarantined=tuple(quarantined),
            quarantine_duplicates=tuple(requarantined),
        )

    def _keep(self, record: QuarantineRecord) -> bool:
        return self._quarantine.setdefault(record.id, record) is record

    def read_as_of(
        self,
        instrument_ids: Collection[str],
        as_of: datetime,
        knowledge_cutoff: datetime,
        manifest: ArtifactRef | None,
        *,
        include_provisional: bool = False,
    ) -> AsOfRead:
        if isinstance(instrument_ids, str):
            raise TypeError("instrument_ids must be a collection of IDs, not one string")
        if as_of.tzinfo is None or knowledge_cutoff.tzinfo is None:
            raise ValueError("Timezone-aware as_of and knowledge_cutoff required")
        as_of, knowledge_cutoff = as_of.astimezone(UTC), knowledge_cutoff.astimezone(UTC)
        requested = sorted(set(instrument_ids))
        wanted = set(requested)
        available = manifest is None or self._manifests.get(manifest.manifest_id) == (
            manifest.content_hash
        )
        dataset = manifest.manifest_id if manifest is not None else None
        latest: dict[tuple[str, str], MarketObservation] = {}
        for observation in self._observations.values() if available else ():
            if observation.instrument_id not in wanted or not _knowable(
                observation, as_of, knowledge_cutoff, dataset
            ):
                continue
            record = (observation.provider, observation.provider_record_id)
            current = latest.get(record)
            if current is None or observation.revision > current.revision:
                latest[record] = observation

        selected: list[MarketObservation] = []
        excluded: list[ReadExclusion] = []
        for observation in latest.values():
            reason = self._exclusion(observation, knowledge_cutoff, include_provisional)
            if reason is None:
                selected.append(observation)
            else:
                excluded.append(
                    ReadExclusion(
                        observation_id=observation.id,
                        instrument_id=observation.instrument_id,
                        reason=reason,
                    )
                )
        selected.sort(key=_order)
        excluded.sort(key=lambda e: (e.instrument_id, e.observation_id))
        present = {o.instrument_id for o in selected}
        missing_reason = (
            UnavailableReason.NO_ELIGIBLE_OBSERVATIONS
            if available
            else UnavailableReason.MANIFEST_UNAVAILABLE
        )
        selection = [[o.id, self._fingerprint(o)] for o in selected]
        return AsOfRead(
            as_of=as_of,
            knowledge_cutoff=knowledge_cutoff,
            manifest=manifest,
            include_provisional=include_provisional,
            observations=tuple(selected),
            excluded=tuple(excluded),
            unavailable=tuple(
                InstrumentUnavailable(instrument_id=i, reason=missing_reason)
                for i in requested
                if i not in present
            ),
            selection_hash=hashlib.sha256(
                json.dumps(selection, separators=(",", ":")).encode()
            ).hexdigest(),
        )

    def _fingerprint(self, observation: MarketObservation) -> str:
        return self._fingerprints[
            (observation.provider, observation.provider_record_id, observation.revision)
        ]

    def _exclusion(
        self, observation: MarketObservation, cutoff: datetime, include_provisional: bool
    ) -> ReadExclusionReason | None:
        # The highest knowable revision decides the record; an unusable one is never
        # replaced by an older revision it supersedes.
        if observation.availability_basis is AvailabilityBasis.UNKNOWN:
            return ReadExclusionReason.AVAILABILITY_UNKNOWN
        identity = (observation.provider, observation.provider_record_id, observation.revision)
        if any(at <= cutoff for at in self._contested.get(identity, ())):
            return ReadExclusionReason.CONFLICTING_DUPLICATE
        if not observation.payload.final and not include_provisional:
            return ReadExclusionReason.PROVISIONAL
        return None


def _order(observation: MarketObservation) -> tuple[str, datetime, str, str, int]:
    return (
        observation.instrument_id,
        observation.event_time,
        observation.provider,
        observation.provider_record_id,
        observation.revision,
    )


def _knowable(
    observation: MarketObservation, as_of: datetime, cutoff: datetime, dataset: str | None
) -> bool:
    """Event at or before as_of and, unless availability is UNKNOWN (excluded later, fail
    closed), knowable at the cutoff. UNKNOWN records take part in revision selection so an
    unplaceable correction blocks the record instead of letting a superseded one through."""
    if observation.event_time > as_of:
        return False
    if dataset is not None and observation.raw_artifact.manifest_id != dataset:
        return False
    return (
        observation.availability_basis is AvailabilityBasis.UNKNOWN
        or observation.available_at <= cutoff
    )


def _conflict(
    observation: MarketObservation, existing: MarketObservation, fingerprint: str
) -> QuarantineRecord:
    return QuarantineRecord(
        id=quarantine_id(
            "conflict",
            observation.provider,
            observation.provider_record_id,
            observation.revision,
            fingerprint,
        ),
        schema_version=QUARANTINE_SCHEMA_VERSION,
        provider=observation.provider,
        provenance_class=observation.provenance_class,
        raw_artifact=observation.raw_artifact,
        raw_record=observation.raw_record,
        raw_fields=(),
        reasons=(QuarantineReason.CONFLICTING_DUPLICATE,),
        details=("same provider record and revision as an accepted observation, different fact",),
        instrument_id=observation.instrument_id,
        provider_record_id=observation.provider_record_id,
        revision=observation.revision,
        conflicts_with=existing.id,
        rejected_observation=observation,
        received_at=observation.received_at,
        recorded_at=observation.recorded_at,
    )

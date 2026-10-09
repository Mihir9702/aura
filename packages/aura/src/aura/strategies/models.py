"""Strategies persistence, private to this package (migration s04_strategy_scopes).

Foreign keys, checks and guard triggers live in the migration: a scope's version and
eligibility generation never decrease, any change of state, qualification or activation
advances both, and qualification records are append-only. Composite foreign keys bind a
qualification to exactly one scope's Pod version and horizon version, and an activation to
that qualification and scope. The ORM declares no foreign keys, so callers flush dependent
rows in order.
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from aura.storage import Base


class PodVersionRow(Base):
    __tablename__ = "strategy_pods"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    display_name: Mapped[str] = mapped_column(String)
    description: Mapped[str] = mapped_column(String)
    implementation_status: Mapped[str] = mapped_column(String)


class HorizonRow(Base):
    __tablename__ = "strategy_horizons"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    approval: Mapped[str] = mapped_column(String)
    decision_ref: Mapped[str] = mapped_column(String)
    description: Mapped[str] = mapped_column(String)


class ScopeRow(Base):
    __tablename__ = "strategy_scopes"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    pod_id: Mapped[str] = mapped_column(String)
    pod_version: Mapped[int] = mapped_column(Integer)
    horizon_id: Mapped[str] = mapped_column(String)
    horizon_version: Mapped[int] = mapped_column(Integer)
    universe_ref: Mapped[str | None] = mapped_column(String)
    state: Mapped[str] = mapped_column(String)
    previous_state: Mapped[str | None] = mapped_column(String)
    version: Mapped[int] = mapped_column(Integer)
    eligibility_generation: Mapped[int] = mapped_column(BigInteger)
    qualification_id: Mapped[str | None] = mapped_column(String)
    activation_id: Mapped[str | None] = mapped_column(String)
    reason: Mapped[str] = mapped_column(String)
    actor: Mapped[str] = mapped_column(String)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class QualificationRow(Base):
    __tablename__ = "strategy_qualifications"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    scope_id: Mapped[str] = mapped_column(String)
    pod_id: Mapped[str] = mapped_column(String)
    pod_version: Mapped[int] = mapped_column(Integer)
    horizon_id: Mapped[str] = mapped_column(String)
    horizon_version: Mapped[int] = mapped_column(Integer)
    policy_ref: Mapped[str] = mapped_column(String)
    evidence_refs: Mapped[list[str]] = mapped_column(JSONB)
    limitations: Mapped[list[str]] = mapped_column(JSONB)
    approved_by: Mapped[str] = mapped_column(String)
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ActivationRow(Base):
    __tablename__ = "strategy_activations"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    qualification_id: Mapped[str] = mapped_column(String)
    scope_id: Mapped[str] = mapped_column(String)
    portfolio_id: Mapped[str] = mapped_column(String)
    generation: Mapped[int] = mapped_column(BigInteger)
    state: Mapped[str] = mapped_column(String)
    approved_by: Mapped[str] = mapped_column(String)
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

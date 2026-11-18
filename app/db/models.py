from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin


class User(TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("role IN ('ADMIN','SUPERADMIN')", name="role"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(20), default="ADMIN", index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True)
    token_version: Mapped[int] = mapped_column(Integer, default=0)


class AuthLoginAttempt(Base):
    __tablename__ = "auth_login_attempts"
    key_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Employee(TimestampMixin, Base):
    __tablename__ = "employees"
    __table_args__ = (CheckConstraint("status IN ('ACTIVE','INACTIVE')", name="status"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    job_title: Mapped[str | None] = mapped_column(String(255))
    joined_at: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE", index=True)
    notes: Mapped[str | None] = mapped_column(Text)


class LivestockGroup(TimestampMixin, Base):
    __tablename__ = "livestock_groups"
    __table_args__ = (
        CheckConstraint("species IN ('AYAM','LELE')", name="species"),
        CheckConstraint("status IN ('ACTIVE','CLOSED')", name="status"),
        CheckConstraint(
            "initial_weight_g IS NULL OR (species = 'LELE' AND initial_weight_g > 0)", name="initial_weight"
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    species: Mapped[str] = mapped_column(String(10), index=True)
    code: Mapped[str] = mapped_column(String(60), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    start_date: Mapped[date] = mapped_column(Date, index=True)
    initial_population: Mapped[int] = mapped_column(BigInteger)
    # Lele only: average weight of one fish (grams) on start_date. When set, it is the starting
    # point of the group's first real ADG interval (stocking -> first sampling).
    initial_weight_g: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    location: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE", index=True)
    closed_at: Mapped[date | None] = mapped_column(Date)
    closure_reason: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    daily_records: Mapped[list[DailyRecord]] = relationship(back_populates="group")


class FeedType(TimestampMixin, Base):
    __tablename__ = "feed_types"
    __table_args__ = (
        CheckConstraint("species IN ('AYAM','LELE')", name="species"),
        UniqueConstraint("species", "name"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    code: Mapped[str] = mapped_column(String(60), unique=True, index=True)
    species: Mapped[str] = mapped_column(String(10), index=True)
    name: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)


class DailyRecord(TimestampMixin, Base):
    __tablename__ = "daily_records"
    __table_args__ = (UniqueConstraint("group_id", "record_date"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("livestock_groups.id", ondelete="RESTRICT"))
    record_date: Mapped[date] = mapped_column(Date, index=True)
    mortality: Mapped[int] = mapped_column(BigInteger, default=0)
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    group: Mapped[LivestockGroup] = relationship(back_populates="daily_records")
    feeds: Mapped[list[FeedUsage]] = relationship(cascade="all, delete-orphan")
    eggs: Mapped[list[EggHarvest]] = relationship(cascade="all, delete-orphan")


class FeedUsage(TimestampMixin, Base):
    __tablename__ = "feed_usages"
    __table_args__ = (
        UniqueConstraint("daily_record_id", "feed_type_id"),
        CheckConstraint(
            "morning_kg >= 0 AND noon_kg >= 0 AND evening_kg >= 0 AND quantity_kg >= 0 "
            "AND quantity_kg = morning_kg + noon_kg + evening_kg",
            name="quantities",
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    daily_record_id: Mapped[int] = mapped_column(ForeignKey("daily_records.id", ondelete="CASCADE"))
    feed_type_id: Mapped[int] = mapped_column(ForeignKey("feed_types.id", ondelete="RESTRICT"))
    morning_kg: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=0)
    noon_kg: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=0)
    evening_kg: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=0)
    quantity_kg: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    feed_type: Mapped[FeedType] = relationship()


class EggHarvest(TimestampMixin, Base):
    __tablename__ = "egg_harvests"
    __table_args__ = (
        UniqueConstraint("daily_record_id", "egg_category"),
        CheckConstraint("egg_category IN ('UTUH','PECAH','REJECT')", name="category"),
        CheckConstraint("weight_kg >= 0 AND quantity_pieces >= 0", name="quantities"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    daily_record_id: Mapped[int] = mapped_column(ForeignKey("daily_records.id", ondelete="CASCADE"))
    egg_category: Mapped[str] = mapped_column(String(10), index=True)
    weight_kg: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=0)
    quantity_pieces: Mapped[int] = mapped_column(BigInteger, default=0)


class InventoryOpeningBalance(TimestampMixin, Base):
    __tablename__ = "inventory_opening_balances"
    __table_args__ = (
        CheckConstraint(
            "(item_kind='FEED' AND feed_type_id IS NOT NULL AND egg_category IS NULL AND quantity_pieces IS NULL) OR "
            "(item_kind='EGG' AND feed_type_id IS NULL AND egg_category IN ('UTUH','PECAH','REJECT') AND quantity_pieces IS NOT NULL)",
            name="shape",
        ),
        Index(
            "uq_inventory_opening_feed", "feed_type_id", unique=True, postgresql_where=text("feed_type_id IS NOT NULL")
        ),
        Index(
            "uq_inventory_opening_egg", "egg_category", unique=True, postgresql_where=text("egg_category IS NOT NULL")
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    item_kind: Mapped[str] = mapped_column(String(10), index=True)
    feed_type_id: Mapped[int | None] = mapped_column(ForeignKey("feed_types.id", ondelete="RESTRICT"))
    egg_category: Mapped[str | None] = mapped_column(String(10), index=True)
    quantity_kg: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=0)
    quantity_pieces: Mapped[int | None] = mapped_column(BigInteger)
    effective_date: Mapped[date] = mapped_column(Date, index=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))


class InventoryAdjustment(TimestampMixin, Base):
    __tablename__ = "inventory_adjustments"
    __table_args__ = (
        CheckConstraint(
            "(item_kind='FEED' AND feed_type_id IS NOT NULL AND egg_category IS NULL AND delta_pieces IS NULL) OR "
            "(item_kind='EGG' AND feed_type_id IS NULL AND egg_category IN ('UTUH','PECAH','REJECT') AND delta_pieces IS NOT NULL)",
            name="shape",
        ),
        CheckConstraint("delta_kg <> 0 OR COALESCE(delta_pieces,0) <> 0", name="nonzero"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    item_kind: Mapped[str] = mapped_column(String(10), index=True)
    feed_type_id: Mapped[int | None] = mapped_column(ForeignKey("feed_types.id", ondelete="RESTRICT"))
    egg_category: Mapped[str | None] = mapped_column(String(10), index=True)
    delta_kg: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    delta_pieces: Mapped[int | None] = mapped_column(BigInteger)
    effective_date: Mapped[date] = mapped_column(Date, index=True)
    reason: Mapped[str] = mapped_column(Text)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))


class PopulationAdjustment(TimestampMixin, Base):
    __tablename__ = "population_adjustments"
    __table_args__ = (CheckConstraint("delta_ekor <> 0", name="nonzero"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("livestock_groups.id", ondelete="RESTRICT"))
    delta_ekor: Mapped[int] = mapped_column(BigInteger)
    effective_date: Mapped[date] = mapped_column(Date, index=True)
    reason: Mapped[str] = mapped_column(Text)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))


class ExternalInventorySource(TimestampMixin, Base):
    __tablename__ = "external_inventory_sources"
    __table_args__ = (
        UniqueConstraint("source_service", "source_type", "source_id"),
        CheckConstraint("source_type IN ('FEED_RECEIPT','EGG_SALE')", name="source_type"),
        CheckConstraint("status IN ('ACTIVE','VOIDED')", name="status"),
        CheckConstraint("source_revision > 0", name="revision"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_service: Mapped[str] = mapped_column(String(100))
    source_type: Mapped[str] = mapped_column(String(30), index=True)
    source_id: Mapped[str] = mapped_column(String(255))
    source_revision: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(10), index=True)
    effective_date: Mapped[date] = mapped_column(Date, index=True)
    payload_hash: Mapped[str] = mapped_column(String(64))
    lines: Mapped[list[ExternalInventoryLine]] = relationship(cascade="all, delete-orphan")


class ExternalInventoryLine(TimestampMixin, Base):
    __tablename__ = "external_inventory_lines"
    __table_args__ = (
        UniqueConstraint("source_id", "source_line_id"),
        CheckConstraint(
            "(item_kind='FEED' AND feed_type_id IS NOT NULL AND egg_category IS NULL "
            "AND quantity_pieces IS NULL) OR "
            "(item_kind='EGG' AND feed_type_id IS NULL "
            "AND egg_category IN ('UTUH','PECAH','REJECT') AND quantity_pieces IS NOT NULL)",
            name="shape",
        ),
        CheckConstraint("quantity_kg >= 0 AND COALESCE(quantity_pieces,0) >= 0", name="quantities"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("external_inventory_sources.id", ondelete="CASCADE"), index=True
    )
    source_line_id: Mapped[str] = mapped_column(String(255))
    item_kind: Mapped[str] = mapped_column(String(10), index=True)
    feed_type_id: Mapped[int | None] = mapped_column(ForeignKey("feed_types.id", ondelete="RESTRICT"))
    egg_category: Mapped[str | None] = mapped_column(String(10), index=True)
    quantity_kg: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    quantity_pieces: Mapped[int | None] = mapped_column(BigInteger)
    effective_date: Mapped[date] = mapped_column(Date, index=True)
    feed_type: Mapped[FeedType | None] = relationship()


class LeleSampling(TimestampMixin, Base):
    __tablename__ = "lele_samplings"
    __table_args__ = (UniqueConstraint("group_id", "sampling_date"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("livestock_groups.id", ondelete="RESTRICT"))
    sampling_date: Mapped[date] = mapped_column(Date, index=True)
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    group: Mapped[LivestockGroup] = relationship()
    measurements: Mapped[list[LeleSamplingMeasurement]] = relationship(cascade="all, delete-orphan")
    result: Mapped[LeleSamplingResult | None] = relationship(
        cascade="all, delete-orphan", foreign_keys="LeleSamplingResult.sampling_id"
    )


class LeleSamplingMeasurement(TimestampMixin, Base):
    __tablename__ = "lele_sampling_measurements"
    __table_args__ = (
        UniqueConstraint("sampling_id", "sequence"),
        CheckConstraint("sequence BETWEEN 1 AND 3", name="sequence"),
        CheckConstraint("fish_count = 10", name="fish_count"),
        CheckConstraint("total_weight_kg > 0", name="weight"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    sampling_id: Mapped[int] = mapped_column(ForeignKey("lele_samplings.id", ondelete="CASCADE"))
    sequence: Mapped[int] = mapped_column(SmallInteger)
    fish_count: Mapped[int] = mapped_column(SmallInteger, default=10)
    total_weight_kg: Mapped[Decimal] = mapped_column(Numeric(14, 3))


class LeleSamplingResult(TimestampMixin, Base):
    """Derived per-fish weight and real ADG of one ``lele_samplings`` row (see lele_biomass_service).

    Never written directly: every sampling create/update (and every change of the group's
    ``initial_weight_g``) recomputes the rows of the whole group, so a backdated or edited sampling
    also refreshes the ADG of the sampling after it. The ADG covers the interval ending at this
    sampling and starting at:

    - ``previous_sampling_id``, the group's preceding sampling; or, for the first sampling,
    - the stocking (``previous_sampling_id`` NULL): the group's ``start_date`` and
      ``initial_weight_g``. The ADG is NULL when the group has no initial weight or the first
      sampling is on the start date.
    """

    __tablename__ = "lele_sampling_results"
    __table_args__ = (
        CheckConstraint("fish_count > 0 AND total_weight_kg > 0 AND average_weight_g > 0", name="weights"),
        CheckConstraint(
            "(days_since_previous IS NULL AND weight_gain_g IS NULL AND adg_g IS NULL)"
            " OR (days_since_previous > 0 AND weight_gain_g IS NOT NULL AND adg_g IS NOT NULL)",
            name="interval",
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    sampling_id: Mapped[int] = mapped_column(ForeignKey("lele_samplings.id", ondelete="CASCADE"), unique=True)
    fish_count: Mapped[int] = mapped_column(Integer)
    total_weight_kg: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    # Average weight of one fish, in grams: total_weight_kg * 1000 / fish_count.
    average_weight_g: Mapped[Decimal] = mapped_column(Numeric(12, 3))
    previous_sampling_id: Mapped[int | None] = mapped_column(
        ForeignKey("lele_samplings.id", ondelete="SET NULL"), index=True
    )
    days_since_previous: Mapped[int | None] = mapped_column(Integer)
    # May be negative: sampling 30 fish is noisy, and the data is shown as measured.
    weight_gain_g: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    adg_g: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))


class AuditEvent(TimestampMixin, Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    entity_type: Mapped[str] = mapped_column(String(100), index=True)
    entity_id: Mapped[int] = mapped_column(BigInteger, index=True)
    action: Mapped[str] = mapped_column(String(100), index=True)
    happened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    reason: Mapped[str | None] = mapped_column(Text)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


# --- Lele biomass read model -----------------------------------------------------------------
# Precomputed by the background rebuild (app.features.lele_biomass_jobs) so that
# GET /lele-biomass/{group_id} only reads these tables. Never edited by hand: every row of a group
# is replaced by each rebuild.


class LeleBiomassJob(Base):
    """Outbox of Lele groups whose biomass read model must be rebuilt.

    Inserted in the same transaction as the change (sampling, initial weight, mortality, ...), so a
    committed change can never be missed; deleted once the group has been rebuilt. Several jobs of
    one group are rebuilt once.
    """

    __tablename__ = "lele_biomass_jobs"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("livestock_groups.id", ondelete="CASCADE"), index=True)
    reason: Mapped[str] = mapped_column(String(60))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LeleBiomassSnapshot(Base):
    """One row per Lele group: details, latest sampling and harvest prediction."""

    __tablename__ = "lele_biomass_snapshots"
    __table_args__ = (
        CheckConstraint("baseline_source IS NULL OR baseline_source IN ('SAMPLING','STOCKING')", name="baseline"),
        CheckConstraint("adg_source IS NULL OR adg_source IN ('ACTUAL','DEFAULT')", name="adg_source"),
        CheckConstraint(
            "(baseline_source IS NULL AND baseline_date IS NULL AND baseline_weight_g IS NULL AND adg_source IS NULL"
            " AND adg_g IS NULL AND days_to_target IS NULL AND harvest_date IS NULL AND ready_to_sell IS NULL"
            " AND remaining_weight_g IS NULL AND projected_biomass_kg IS NULL)"
            " OR (baseline_source IS NOT NULL AND baseline_date IS NOT NULL AND baseline_weight_g IS NOT NULL"
            " AND adg_source IS NOT NULL AND adg_g IS NOT NULL AND days_to_target IS NOT NULL"
            " AND harvest_date IS NOT NULL AND ready_to_sell IS NOT NULL AND remaining_weight_g IS NOT NULL"
            " AND projected_biomass_kg IS NOT NULL)",
            name="projection",
        ),
    )
    group_id: Mapped[int] = mapped_column(ForeignKey("livestock_groups.id", ondelete="CASCADE"), primary_key=True)
    # Group details, copied so the endpoint does not read livestock_groups.
    name: Mapped[str] = mapped_column(String(255))
    location: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(20))
    start_date: Mapped[date] = mapped_column(Date)
    closed_at: Mapped[date | None] = mapped_column(Date)
    initial_population: Mapped[int] = mapped_column(BigInteger)
    current_population: Mapped[int] = mapped_column(BigInteger)
    population_as_of: Mapped[date] = mapped_column(Date)
    initial_weight_g: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    notes: Mapped[str | None] = mapped_column(Text)
    # Settings in force when the row was computed.
    target_weight_g: Mapped[Decimal] = mapped_column(Numeric(12, 3))
    default_adg_g: Mapped[Decimal] = mapped_column(Numeric(12, 3))
    # Latest sampling (NULL without samplings).
    latest_sampling_date: Mapped[date | None] = mapped_column(Date)
    latest_weight_g: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    latest_population: Mapped[int | None] = mapped_column(BigInteger)
    latest_biomass_kg: Mapped[Decimal | None] = mapped_column(Numeric(18, 3))
    # Harvest prediction from the baseline (latest sampling, else stocking) with the latest real
    # ADG, or the default ADG when there is no positive real ADG. NULL without any known weight.
    baseline_source: Mapped[str | None] = mapped_column(String(10))
    baseline_date: Mapped[date | None] = mapped_column(Date)
    baseline_weight_g: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    adg_source: Mapped[str | None] = mapped_column(String(10))
    adg_g: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    days_to_target: Mapped[int | None] = mapped_column(Integer)
    harvest_date: Mapped[date | None] = mapped_column(Date)
    ready_to_sell: Mapped[bool | None] = mapped_column(Boolean)
    remaining_weight_g: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    projected_biomass_kg: Mapped[Decimal | None] = mapped_column(Numeric(18, 3))
    # Chart bounds: the X axis ends on chart_end_date (the harvest date, capped); Y from 0 to y_max_g.
    chart_end_date: Mapped[date] = mapped_column(Date)
    y_max_g: Mapped[Decimal] = mapped_column(Numeric(12, 3))
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LeleBiomassChartDate(Base):
    """X axis of a group's weight chart, with the measured weight/biomass on measured dates."""

    __tablename__ = "lele_biomass_chart_dates"
    group_id: Mapped[int] = mapped_column(
        ForeignKey("lele_biomass_snapshots.group_id", ondelete="CASCADE"), primary_key=True
    )
    chart_date: Mapped[date] = mapped_column(Date, primary_key=True)
    day: Mapped[int] = mapped_column(Integer)
    # Week number on the weekly dates; NULL on a sampling/harvest date between two weeks.
    week: Mapped[int | None] = mapped_column(Integer)
    is_sampling: Mapped[bool] = mapped_column(Boolean)
    is_harvest_date: Mapped[bool] = mapped_column(Boolean)
    # Measured on this date (stocking or sampling); NULL otherwise.
    actual_weight_g: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    population: Mapped[int | None] = mapped_column(BigInteger)
    biomass_kg: Mapped[Decimal | None] = mapped_column(Numeric(18, 3))


class LeleBiomassChartSeries(Base):
    """One ADG line of a group's weight chart: the default ADG or a real ADG."""

    __tablename__ = "lele_biomass_chart_series"
    __table_args__ = (
        UniqueConstraint("group_id", "position"),
        CheckConstraint("type IN ('DEFAULT','REAL')", name="type"),
        CheckConstraint("period_from IS NULL OR period_from IN ('SAMPLING','STOCKING')", name="period_from"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("lele_biomass_snapshots.group_id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(SmallInteger)
    name: Mapped[str] = mapped_column(String(60))
    type: Mapped[str] = mapped_column(String(10))
    is_latest: Mapped[bool] = mapped_column(Boolean)
    adg_g: Mapped[Decimal] = mapped_column(Numeric(12, 3))
    # Where the line starts, and when it reaches the target (NULL if it never does).
    start_date: Mapped[date | None] = mapped_column(Date)
    start_weight_g: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    days_to_target: Mapped[int | None] = mapped_column(Integer)
    projected_sell_date: Mapped[date | None] = mapped_column(Date)
    # Real ADG only: the interval it was measured over.
    period_from: Mapped[str | None] = mapped_column(String(10))
    period_from_date: Mapped[date | None] = mapped_column(Date)
    period_to_date: Mapped[date | None] = mapped_column(Date)
    period_days: Mapped[int | None] = mapped_column(Integer)
    period_from_weight_g: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    period_to_weight_g: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    period_weight_gain_g: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))


class LeleBiomassChartValue(Base):
    """Weight of one fish on one chart date for one ADG line (only the dates the line covers)."""

    __tablename__ = "lele_biomass_chart_values"
    series_id: Mapped[int] = mapped_column(
        ForeignKey("lele_biomass_chart_series.id", ondelete="CASCADE"), primary_key=True
    )
    chart_date: Mapped[date] = mapped_column(Date, primary_key=True)
    weight_g: Mapped[Decimal] = mapped_column(Numeric(12, 3))

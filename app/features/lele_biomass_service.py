"""Lele biomass, average daily gain (ADG) and sell-date projection.

Definitions (weights are per fish, in grams):

- **average weight** of a sampling = sum of its measurements' ``total_weight_kg`` * 1000 / sum of
  their ``fish_count`` (3 x 10 fish), e.g. 0.300 + 0.350 + 0.400 kg over 30 fish = 35.000 g.
- **stocking weight** = the group's ``initial_weight_g``: weight of one fish on ``start_date``,
  entered on the livestock group (optional).
- **real ADG** of a sampling = (its average weight - the previous weight) / days between the two
  dates, where the previous weight is the group's preceding sampling or, for the first sampling,
  the stocking weight. Without a stocking weight the first sampling has no real ADG, so a group
  with N samplings has N real ADGs with a stocking weight and N - 1 without.
- **default ADG** = ``settings.lele_default_adg_g`` (1.3 g/day), used while no real ADG exists.
- **harvest prediction**: from the baseline (the latest sampling, or the stocking when there is
  no sampling yet), days to target = ceil((``settings.lele_target_weight_g`` - baseline weight) /
  ADG) and the harvest date is the baseline date plus those days. The ADG is the latest real ADG
  (the current growth rate); the default ADG is only a fallback while there is no positive real
  ADG. The harvest date is the last date of the chart and of the weight estimate.
- **biomass** = population * average weight.

Two stores, both rebuilt for a whole group at a time:

- ``lele_sampling_results`` (per-sampling weight and real ADG): rebuilt by
  ``refresh_group_results`` inside every sampling write and stocking weight change, so the
  sampling API returns it immediately and inserting/editing a sampling in the middle of the
  history also corrects the ADG of the sampling after it.
- the biomass read model (``lele_biomass_snapshots`` and ``lele_biomass_chart_*``): rebuilt in the
  background by ``rebuild_snapshot`` (see ``lele_biomass_jobs``) after every change that affects
  it, and the only thing ``GET /lele-biomass/{group_id}`` reads (``biomass_response``).
"""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

from sqlalchemy import delete, func, insert, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.decimal import decimal_string
from app.db.models import (
    LeleBiomassChartDate,
    LeleBiomassChartSeries,
    LeleBiomassChartValue,
    LeleBiomassSnapshot,
    LeleSampling,
    LeleSamplingMeasurement,
    LeleSamplingResult,
    LivestockGroup,
)
from app.features.common import now_utc, today_local
from app.features.farm_service import population
from app.features.lele_biomass_queue import has_pending

GRAMS = Decimal("0.001")
ADG_ACTUAL = "ACTUAL"
ADG_DEFAULT = "DEFAULT"
# Start of an ADG interval / baseline of a projection.
FROM_SAMPLING = "SAMPLING"
FROM_STOCKING = "STOCKING"


def grams(value: Decimal) -> Decimal:
    return value.quantize(GRAMS, rounding=ROUND_HALF_UP)


def average_weight_g(total_weight_kg: Decimal, fish_count: int) -> Decimal:
    """Weight of one fish in grams from a batch weighed together."""
    if fish_count <= 0:
        raise ValueError("fish_count must be positive")
    return grams(Decimal(total_weight_kg) * 1000 / fish_count)


def daily_gain_g(previous_weight_g: Decimal, weight_g: Decimal, days: int) -> Decimal:
    if days <= 0:
        raise ValueError("days must be positive")
    return grams((weight_g - previous_weight_g) / days)


def biomass_kg(population_count: int, weight_g: Decimal) -> Decimal:
    return (Decimal(population_count) * weight_g / 1000).quantize(GRAMS, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Projection:
    adg_g: Decimal
    days_to_target: int
    projected_sell_date: date


def project_sell_date(weight_g: Decimal, sampled_on: date, adg_g: Decimal, target_g: Decimal) -> Projection | None:
    """When the fish reach ``target_g`` growing ``adg_g`` per day from ``weight_g`` on ``sampled_on``.

    Returns None when the fish cannot reach the target at that rate (ADG not positive). A group that
    already reached the target projects to the sampling date itself (0 days).
    """
    remaining = target_g - weight_g
    if remaining <= 0:
        return Projection(adg_g, 0, sampled_on)
    if adg_g <= 0:
        return None
    days = int((remaining / adg_g).to_integral_value(rounding=ROUND_CEILING))
    return Projection(adg_g, days, sampled_on + timedelta(days=days))


def refresh_group_results(db: Session, group_id: int) -> list[LeleSamplingResult]:
    """Rebuild ``lele_sampling_results`` for every sampling of ``group_id``, oldest first.

    Call after the sampling and its measurements (or the group's ``initial_weight_g``) are flushed;
    it reads measurement totals with SQL so it does not depend on possibly stale ORM collections.
    Rows whose values do not change are not rewritten. Idempotent.
    """
    group = db.get(LivestockGroup, group_id)
    totals = db.execute(
        select(
            LeleSampling.id,
            LeleSampling.sampling_date,
            func.sum(LeleSamplingMeasurement.total_weight_kg),
            func.sum(LeleSamplingMeasurement.fish_count),
        )
        .join(LeleSamplingMeasurement, LeleSamplingMeasurement.sampling_id == LeleSampling.id)
        .where(LeleSampling.group_id == group_id)
        .group_by(LeleSampling.id, LeleSampling.sampling_date)
        .order_by(LeleSampling.sampling_date, LeleSampling.id)
    ).all()
    sampling_ids = [row[0] for row in totals]
    existing = {
        row.sampling_id: row
        for row in db.scalars(select(LeleSamplingResult).where(LeleSamplingResult.sampling_id.in_(sampling_ids)))
    }
    results: list[LeleSamplingResult] = []
    # (sampling id or None for the stocking, date, weight of one fish) the next interval starts at.
    previous: tuple[int | None, date, Decimal] | None = None
    if group is not None and group.initial_weight_g is not None:
        previous = (None, group.start_date, Decimal(group.initial_weight_g))
    for sampling_id, sampling_date, total_kg, fish_count in totals:
        weight = average_weight_g(Decimal(total_kg), int(fish_count))
        result = existing.get(sampling_id) or LeleSamplingResult(sampling_id=sampling_id)
        result.fish_count = int(fish_count)
        result.total_weight_kg = Decimal(total_kg)
        result.average_weight_g = weight
        # No interval: no stocking weight before the first sampling, or sampled on the start date.
        if previous is None or (sampling_date - previous[1]).days <= 0:
            result.previous_sampling_id = None
            result.days_since_previous = None
            result.weight_gain_g = None
            result.adg_g = None
        else:
            previous_id, previous_date, previous_weight = previous
            days = (sampling_date - previous_date).days
            result.previous_sampling_id = previous_id
            result.days_since_previous = days
            result.weight_gain_g = weight - previous_weight
            result.adg_g = daily_gain_g(previous_weight, weight, days)
        db.add(result)
        results.append(result)
        previous = (sampling_id, sampling_date, weight)
    db.flush()
    return results


# --- Read side ------------------------------------------------------------------------------


def _amount(value: Decimal | None) -> str | None:
    return decimal_string(value)


def result_out(result: LeleSamplingResult | None) -> dict[str, object] | None:
    """Per-sampling derived values, embedded in the ``lele-samplings`` responses."""
    if result is None:
        return None
    return {
        "fish_count": result.fish_count,
        "total_weight_kg": _amount(result.total_weight_kg),
        "average_weight_g": _amount(result.average_weight_g),
        # Where the ADG interval starts: the previous sampling, or the stocking (group start date
        # and initial weight) for the first sampling. None when there is no real ADG.
        "adg_from": adg_from(result),
        "previous_sampling_id": result.previous_sampling_id,
        "days_since_previous": result.days_since_previous,
        "weight_gain_g": _amount(result.weight_gain_g),
        "adg_g": _amount(result.adg_g),
    }


def adg_from(result: LeleSamplingResult) -> str | None:
    if result.adg_g is None:
        return None
    return FROM_SAMPLING if result.previous_sampling_id is not None else FROM_STOCKING


def group_history(db: Session, group_id: int) -> list[tuple[LeleSampling, LeleSamplingResult]]:
    """The group's samplings that have results, oldest first."""
    rows = db.execute(
        select(LeleSampling, LeleSamplingResult)
        .join(LeleSamplingResult, LeleSamplingResult.sampling_id == LeleSampling.id)
        .where(LeleSampling.group_id == group_id)
        .order_by(LeleSampling.sampling_date, LeleSampling.id)
    ).all()
    return [(sampling, result) for sampling, result in rows]


# --- Chart ----------------------------------------------------------------------------------

WEEK_DAYS = 7
# The chart ends on the harvest date; a very small real ADG (e.g. 0.01 g/day) would put it years
# ahead, so it is cut at this many days after the start date.
CHART_HORIZON_DAYS = 365
SERIES_ACTUAL = "ACTUAL"
SERIES_DEFAULT = "DEFAULT"
SERIES_REAL = "REAL"


def chart_dates(start: date, end: date, extra: set[date]) -> list[date]:
    """Weekly dates from ``start`` (start, +7, +14, ...) up to ``end``, plus ``end`` itself and every
    date of ``extra`` (stocking/samplings) between the two that does not fall on a weekly date."""
    end = max(end, start)
    weekly = {start + timedelta(days=WEEK_DAYS * week) for week in range((end - start).days // WEEK_DAYS + 1)}
    return sorted(weekly | {day for day in extra if start <= day <= end} | {end})


def line_values(dates: list[date], anchor_date: date, anchor_weight: Decimal, adg_g: Decimal) -> dict[date, Decimal]:
    """Weight of one fish on every date from ``anchor_date`` on, growing ``adg_g`` per day from
    ``anchor_weight``. Values may pass the target weight; a shrinking line stops before zero."""
    values: dict[date, Decimal] = {}
    for day in dates:
        if day < anchor_date:
            continue
        weight = grams(anchor_weight + adg_g * (day - anchor_date).days)
        if weight <= 0:
            break
        values[day] = weight
    return values


@dataclass(frozen=True)
class _Line:
    name: str
    type: str
    adg_g: Decimal
    start_date: date
    start_weight_g: Decimal
    projection: Projection | None
    is_latest: bool = False
    result: LeleSamplingResult | None = None  # real ADG: the interval it was measured over


# --- Background rebuild (write side) ---------------------------------------------------------


READ_MODEL = (LeleBiomassSnapshot, LeleBiomassChartDate, LeleBiomassChartSeries, LeleBiomassChartValue)


def rebuild_snapshot(db: Session, group_id: int, today: date | None = None) -> bool:
    """Recomputes the whole biomass read model of one group and replaces its rows (does not commit).
    Removes the read model of a group that no longer exists or is not Lele; returns whether one was
    built.

    Rows are written with Core statements and any loaded read-model objects are detached first, so
    the session never holds stale copies of rows replaced here (the old chart rows go through the
    database's ON DELETE CASCADE, which the ORM does not track)."""
    for instance in [obj for obj in db.identity_map.values() if isinstance(obj, READ_MODEL)]:
        db.expunge(instance)
    db.execute(
        delete(LeleBiomassSnapshot)
        .where(LeleBiomassSnapshot.group_id == group_id)
        .execution_options(synchronize_session=False)
    )
    group = db.get(LivestockGroup, group_id)
    if group is None or group.species != "LELE":
        return False

    target = settings.lele_target_weight_g
    default_adg = settings.lele_default_adg_g
    start = group.start_date
    # A closed group is described as it was on its closing day.
    as_of = group.closed_at if group.closed_at else (today or today_local())
    current_population = population(db, group, as_of)
    stocking_weight = Decimal(group.initial_weight_g) if group.initial_weight_g is not None else None
    history = group_history(db, group_id)

    # Measured points: the stocking and every sampling, with the population on that date.
    measured: dict[date, tuple[Decimal, int]] = {}
    if stocking_weight is not None:
        measured[start] = (stocking_weight, population(db, group, start))
    for sampling, result in history:
        measured[sampling.sampling_date] = (result.average_weight_g, population(db, group, sampling.sampling_date))
    sampling_dates = {sampling.sampling_date for sampling, _ in history}

    # Baseline: the latest sampling, else the stocking. Without either there is nothing to project.
    baseline: tuple[str, date, Decimal] | None = None
    latest: LeleSamplingResult | None = history[-1][1] if history else None
    if history:
        baseline = (FROM_SAMPLING, history[-1][0].sampling_date, history[-1][1].average_weight_g)
    elif stocking_weight is not None:
        baseline = (FROM_STOCKING, start, stocking_weight)

    # Harvest prediction, anchored on the latest real ADG; the default ADG only as a fallback.
    harvest: Projection | None = None
    adg_source: str | None = None
    lines: list[_Line] = []
    if baseline is not None:
        _, baseline_date, baseline_weight = baseline
        if latest is not None and latest.adg_g is not None and latest.adg_g > 0:
            harvest, adg_source = project_sell_date(baseline_weight, baseline_date, latest.adg_g, target), ADG_ACTUAL
        else:
            harvest, adg_source = project_sell_date(baseline_weight, baseline_date, default_adg, target), ADG_DEFAULT
        lines.append(
            _Line(
                "Default ADG",
                SERIES_DEFAULT,
                default_adg,
                baseline_date,
                baseline_weight,
                project_sell_date(baseline_weight, baseline_date, default_adg, target),
            )
        )
    # Every real ADG, oldest first: "Real ADG #1", "#2", ...; the highest number is the latest one,
    # the one the harvest prediction uses. Each line starts at the sampling that closed its interval.
    real = [(sampling, result) for sampling, result in history if result.adg_g is not None]
    for number, (sampling, result) in enumerate(real, start=1):
        adg = Decimal(str(result.adg_g))
        weight = result.average_weight_g
        lines.append(
            _Line(
                f"Real ADG #{number}",
                SERIES_REAL,
                adg,
                sampling.sampling_date,
                weight,
                project_sell_date(weight, sampling.sampling_date, adg, target),
                is_latest=result is latest,
                result=result,
            )
        )

    # X axis: weekly from the start date up to the harvest date (the chart's last date).
    if harvest is not None:
        end = min(harvest.projected_sell_date, start + timedelta(days=CHART_HORIZON_DAYS))
    else:
        end = start
    end = max([end, *sampling_dates])
    dates = chart_dates(start, end, set(measured))
    values = [line_values(dates, line.start_date, line.start_weight_g, line.adg_g) for line in lines]
    y_max = max([target, *(weight for weight, _ in measured.values()), *(v for line in values for v in line.values())])

    latest_sampling = history[-1][0] if history else None
    latest_measure = measured[latest_sampling.sampling_date] if latest_sampling else None
    snapshot: dict[str, object] = dict(
        group_id=group.id,
        name=group.name,
        location=group.location,
        status=group.status,
        start_date=start,
        closed_at=group.closed_at,
        initial_population=group.initial_population,
        current_population=current_population,
        population_as_of=as_of,
        initial_weight_g=stocking_weight,
        notes=group.notes,
        target_weight_g=target,
        default_adg_g=default_adg,
        latest_sampling_date=latest_sampling.sampling_date if latest_sampling else None,
        latest_weight_g=latest_measure[0] if latest_measure else None,
        latest_population=latest_measure[1] if latest_measure else None,
        latest_biomass_kg=biomass_kg(latest_measure[1], latest_measure[0]) if latest_measure else None,
        chart_end_date=end,
        y_max_g=y_max,
        computed_at=now_utc(),
    )
    if baseline is not None and harvest is not None:
        baseline_source, baseline_date, baseline_weight = baseline
        snapshot.update(
            baseline_source=baseline_source,
            baseline_date=baseline_date,
            baseline_weight_g=baseline_weight,
            adg_source=adg_source,
            adg_g=harvest.adg_g,
            days_to_target=harvest.days_to_target,
            harvest_date=harvest.projected_sell_date,
            ready_to_sell=baseline_weight >= target,
            remaining_weight_g=max(target - baseline_weight, Decimal("0")),
            projected_biomass_kg=biomass_kg(current_population, max(baseline_weight, target)),
        )
    db.execute(insert(LeleBiomassSnapshot).values(**snapshot))

    harvest_date = harvest.projected_sell_date if harvest else None
    db.execute(
        insert(LeleBiomassChartDate),
        [
            {
                "group_id": group.id,
                "chart_date": day,
                "day": (day - start).days,
                "week": (day - start).days // WEEK_DAYS if (day - start).days % WEEK_DAYS == 0 else None,
                "is_sampling": day in sampling_dates,
                "is_harvest_date": day == harvest_date,
                "actual_weight_g": measured[day][0] if day in measured else None,
                "population": measured[day][1] if day in measured else None,
                "biomass_kg": biomass_kg(measured[day][1], measured[day][0]) if day in measured else None,
            }
            for day in dates
        ],
    )
    for position, (line, line_points) in enumerate(zip(lines, values, strict=True), start=1):
        row: dict[str, object] = {
            "group_id": group.id,
            "position": position,
            "name": line.name,
            "type": line.type,
            "is_latest": line.is_latest,
            "adg_g": line.adg_g,
            "start_date": line.start_date,
            "start_weight_g": line.start_weight_g,
            "days_to_target": line.projection.days_to_target if line.projection else None,
            "projected_sell_date": line.projection.projected_sell_date if line.projection else None,
        }
        if line.result is not None:
            days = int(line.result.days_since_previous or 0)
            gain = Decimal(str(line.result.weight_gain_g))
            row.update(
                period_from=adg_from(line.result),
                period_from_date=line.start_date - timedelta(days=days),
                period_to_date=line.start_date,
                period_days=days,
                period_from_weight_g=line.start_weight_g - gain,
                period_to_weight_g=line.start_weight_g,
                period_weight_gain_g=gain,
            )
        series_id = db.execute(
            insert(LeleBiomassChartSeries).values(**row).returning(LeleBiomassChartSeries.id)
        ).scalar_one()
        if line_points:
            db.execute(
                insert(LeleBiomassChartValue),
                [
                    {"series_id": series_id, "chart_date": day, "weight_g": weight}
                    for day, weight in line_points.items()
                ],
            )
    return True


# --- API response (read side): reads the read model only --------------------------------------


def biomass_response(db: Session, group_id: int, today: date | None = None) -> dict[str, object] | None:
    """The biomass page of one Lele group from its precomputed rows, or None if it has none yet."""
    snapshot = db.get(LeleBiomassSnapshot, group_id)
    if snapshot is None:
        return None
    dates = db.scalars(
        select(LeleBiomassChartDate)
        .where(LeleBiomassChartDate.group_id == group_id)
        .order_by(LeleBiomassChartDate.chart_date)
    ).all()
    series = db.scalars(
        select(LeleBiomassChartSeries)
        .where(LeleBiomassChartSeries.group_id == group_id)
        .order_by(LeleBiomassChartSeries.position)
    ).all()
    points: dict[int, dict[date, Decimal]] = {line.id: {} for line in series}
    if series:
        for series_id, chart_date, weight in db.execute(
            select(
                LeleBiomassChartValue.series_id, LeleBiomassChartValue.chart_date, LeleBiomassChartValue.weight_g
            ).where(LeleBiomassChartValue.series_id.in_(list(points)))
        ):
            points[series_id][chart_date] = weight

    start = snapshot.start_date
    # Only date arithmetic on stored values: "today" moves without any write, so it is not stored.
    reference = snapshot.closed_at if snapshot.closed_at else (today or today_local())
    samplings = [
        {
            "sampling_date": row.chart_date.isoformat(),
            "day": row.day,
            "average_weight_g": _amount(row.actual_weight_g),
            "population": row.population,
            "biomass_kg": _amount(row.biomass_kg),
        }
        for row in dates
        if row.is_sampling
    ]
    return {
        "group": {
            "name": snapshot.name,
            "location": snapshot.location,
            "status": snapshot.status,
            "start_date": start.isoformat(),
            "closed_at": snapshot.closed_at.isoformat() if snapshot.closed_at else None,
            "age_days": max((reference - start).days, 0),
            "initial_population": snapshot.initial_population,
            "current_population": snapshot.current_population,
            "initial_weight_g": _amount(snapshot.initial_weight_g),
            "notes": snapshot.notes,
        },
        "target_weight_g": _amount(snapshot.target_weight_g),
        "latest_sampling": samplings[-1] if samplings else None,
        "projection": _projection_response(snapshot),
        "estimate": _estimate_response(snapshot, reference),
        "samplings": samplings,
        "chart": {
            "y_min_g": _amount(Decimal("0")),
            "y_max_g": _amount(snapshot.y_max_g),
            "target_weight_g": _amount(snapshot.target_weight_g),
            "end_date": snapshot.chart_end_date.isoformat(),
            "dates": [
                {
                    "date": row.chart_date.isoformat(),
                    "day": row.day,
                    "week": row.week,
                    "is_sampling": row.is_sampling,
                    "is_harvest_date": row.is_harvest_date,
                }
                for row in dates
            ],
            "series": [
                _series_response(
                    "Actual weight",
                    SERIES_ACTUAL,
                    [row.actual_weight_g for row in dates],
                ),
                # Always present, so the chart keeps the same lines; empty without any known weight.
                *(
                    []
                    if any(line.type == SERIES_DEFAULT for line in series)
                    else [
                        {
                            **_series_response("Default ADG", SERIES_DEFAULT, [None] * len(dates)),
                            "adg_g": _amount(snapshot.default_adg_g),
                        }
                    ]
                ),
                *(
                    _series_response(
                        line.name,
                        line.type,
                        [points[line.id].get(row.chart_date) for row in dates],
                        line,
                    )
                    for line in series
                ),
            ],
        },
        "sync": {"computed_at": snapshot.computed_at.isoformat(), "pending": has_pending(db, group_id)},
    }


def _projection_response(snapshot: LeleBiomassSnapshot) -> dict[str, object] | None:
    if snapshot.baseline_date is None or snapshot.harvest_date is None:
        return None
    return {
        "baseline": {
            "source": snapshot.baseline_source,
            "date": snapshot.baseline_date.isoformat(),
            "average_weight_g": _amount(snapshot.baseline_weight_g),
        },
        "ready_to_sell": snapshot.ready_to_sell,
        "remaining_weight_g": _amount(snapshot.remaining_weight_g),
        "adg_source": snapshot.adg_source,
        "adg_g": _amount(snapshot.adg_g),
        "days_to_target": snapshot.days_to_target,
        "projected_sell_date": snapshot.harvest_date.isoformat(),
        "projected_biomass_kg": _amount(snapshot.projected_biomass_kg),
    }


def _estimate_response(snapshot: LeleBiomassSnapshot, reference: date) -> dict[str, object] | None:
    """Weight and biomass on ``reference`` (today, or the closing date), growing the baseline
    weight by the prediction's ADG, but never past the harvest date."""
    baseline_date, weight, adg, harvest_date = (
        snapshot.baseline_date,
        snapshot.baseline_weight_g,
        snapshot.adg_g,
        snapshot.harvest_date,
    )
    if baseline_date is None or weight is None or adg is None or harvest_date is None:
        return None
    as_of = min(reference, harvest_date)
    days = max((as_of - baseline_date).days, 0)
    estimated = grams(weight + adg * days)
    return {
        "as_of": as_of.isoformat(),
        "capped_at_harvest_date": reference > harvest_date,
        "adg_source": snapshot.adg_source,
        "days_since_baseline": days,
        "average_weight_g": _amount(estimated),
        "population": snapshot.current_population,
        "biomass_kg": _amount(biomass_kg(snapshot.current_population, estimated)),
    }


def _series_response(
    name: str, series_type: str, values: list[Decimal | None], line: LeleBiomassChartSeries | None = None
) -> dict[str, object]:
    period = None
    if line is not None and line.period_from is not None:
        period = {
            "from": line.period_from,
            "from_date": line.period_from_date.isoformat() if line.period_from_date else None,
            "to_date": line.period_to_date.isoformat() if line.period_to_date else None,
            "days": line.period_days,
            "from_weight_g": _amount(line.period_from_weight_g),
            "to_weight_g": _amount(line.period_to_weight_g),
            "weight_gain_g": _amount(line.period_weight_gain_g),
        }
    return {
        "name": name,
        "type": series_type,
        "is_latest": line.is_latest if line else False,
        "adg_g": _amount(line.adg_g) if line else None,
        # Where the line starts: the sampling (or stocking) its ADG is projected from.
        "start_date": line.start_date.isoformat() if line and line.start_date else None,
        "start_weight_g": _amount(line.start_weight_g) if line else None,
        # When this line reaches the target (may be after the chart's end). None if it never does.
        "days_to_target": line.days_to_target if line else None,
        "projected_sell_date": line.projected_sell_date.isoformat() if line and line.projected_sell_date else None,
        "period": period,
        "values": [_amount(value) for value in values],
    }

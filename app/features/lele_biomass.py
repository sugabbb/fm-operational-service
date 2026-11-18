from fastapi import APIRouter, status
from fastapi.responses import JSONResponse

from app.core.dependencies import ActiveUser, DbSession
from app.core.errors import not_found
from app.features.lele_biomass_queue import has_pending
from app.features.lele_biomass_service import biomass_response

router = APIRouter(tags=["Lele Biomass"])


@router.get(
    "/lele-biomass/{group_id}",
    response_model=None,
    responses={202: {"description": "The group's biomass is still being computed; retry shortly."}},
)
def biomass_detail(group_id: int, _: ActiveUser, db: DbSession) -> dict[str, object] | JSONResponse:
    """Biomass page of one Lele group: details, biomass, harvest prediction and the weekly weight
    chart (actual weight, default ADG and every real ADG).

    Reads only the precomputed read model (``lele_biomass_snapshots`` / ``lele_biomass_chart_*``),
    which is rebuilt in the background after every change; ``sync`` tells when it was computed and
    whether a newer rebuild is still queued."""
    data = biomass_response(db, group_id)
    if data is not None:
        return {"data": data}
    if has_pending(db, group_id):
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={"code": "BIOMASS_PENDING", "detail": "The group's biomass is being computed; retry shortly."},
        )
    raise not_found("Lele group")

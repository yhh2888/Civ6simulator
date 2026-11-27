"""API request and response schemas."""

from app.schemas.city_schema import CityCreate, TileOwnership
from app.schemas.game_schema import (
    BuilderActionRequest,
    FoundCityRequest,
    ForceEntityRequest,
    ForceUnitCreateRequest,
    ProductionQueueRequest,
    ProductionReorderRequest,
    ResearchAvailabilityRequest,
    ResearchSelectRequest,
    UnitMoveRequest,
)
from app.schemas.snapshot_schema import SaveMapRequest

COMPATIBILITY_SCHEMAS = {
    schema.__name__: schema
    for schema in (
        CityCreate,
        TileOwnership,
        SaveMapRequest,
        ProductionQueueRequest,
        ProductionReorderRequest,
        UnitMoveRequest,
        BuilderActionRequest,
        FoundCityRequest,
        ResearchSelectRequest,
        ResearchAvailabilityRequest,
        ForceUnitCreateRequest,
        ForceEntityRequest,
    )
}

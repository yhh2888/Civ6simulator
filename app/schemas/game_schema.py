from pydantic import BaseModel, Field


class ProductionQueueRequest(BaseModel):
    category: str
    target_id: str
    x: int | None = None
    y: int | None = None


class ProductionReorderRequest(BaseModel):
    queue_id: str
    position: int = Field(ge=0)


class UnitMoveRequest(BaseModel):
    x: int
    y: int


class BuilderActionRequest(BaseModel):
    improvement_id: str | None = None


class FoundCityRequest(BaseModel):
    name: str = Field(default="New City", min_length=1, max_length=80)


class ResearchSelectRequest(BaseModel):
    target_id: str


class ResearchAvailabilityRequest(BaseModel):
    unlocked: bool


class ForceUnitCreateRequest(BaseModel):
    unit_type: str = Field(min_length=1)
    owner_city_id: str = Field(min_length=1)
    x: int | None = None
    y: int | None = None


class ForceEntityRequest(BaseModel):
    category: str
    entity_id: str = Field(min_length=1)
    city_id: str = Field(min_length=1)
    x: int | None = None
    y: int | None = None

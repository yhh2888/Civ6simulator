from pydantic import BaseModel, Field


class CityCreate(BaseModel):
    name: str = "New City"
    x: int
    y: int
    population: int | None = Field(default=None, ge=1)
    civilization: str | None = None
    leader: str | None = None


class TileOwnership(BaseModel):
    owned: bool

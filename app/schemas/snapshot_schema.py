from pydantic import BaseModel, Field


class SaveMapRequest(BaseModel):
    name: str = Field(min_length=1, max_length=84)

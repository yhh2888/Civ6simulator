from typing import TypedDict


class District(TypedDict, total=False):
    id: str
    x: int
    y: int
    yields: dict[str, float]

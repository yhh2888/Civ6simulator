from typing import TypedDict


class Wonder(TypedDict, total=False):
    id: str
    x: int
    y: int
    yields: dict[str, float]

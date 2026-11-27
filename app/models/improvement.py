from typing import TypedDict


class Improvement(TypedDict, total=False):
    id: str
    yields: dict[str, float]

from typing import TypedDict


class Yield(TypedDict, total=False):
    Food: float
    Production: float
    Gold: float
    Science: float
    Culture: float
    Faith: float

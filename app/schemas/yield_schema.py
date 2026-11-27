from typing import TypedDict

from app.models.yield_model import Yield


class YieldCalculationResponse(TypedDict, total=False):
    totals: Yield
    breakdown: dict[str, Yield]
    tiles: list[dict]
    districts: list[dict]
    buildings: list[dict]

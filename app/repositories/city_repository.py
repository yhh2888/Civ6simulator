"""City persistence access."""

from app.repositories.map_repository import map_repository


class CityRepository:
    def get_all(self) -> dict:
        return map_repository.get_cities()

    def save_all(self, cities: dict, *, atomic: bool = True) -> None:
        map_repository.save_cities(cities, atomic=atomic)


city_repository = CityRepository()

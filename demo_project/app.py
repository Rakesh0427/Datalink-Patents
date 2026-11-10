"""Application integration boundary."""

from demo_project.controller import update_valve
from demo_project.sensor import convert_temperature


def process_reading(raw: int, limit: float) -> None:
    temperature = convert_temperature(raw)
    update_valve(temperature, limit)

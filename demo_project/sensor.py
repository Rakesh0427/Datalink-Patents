"""Sensor conversion component."""


def convert_temperature(raw: int) -> float:
    """Convert a raw sensor count into a temperature."""
    return raw * 0.1

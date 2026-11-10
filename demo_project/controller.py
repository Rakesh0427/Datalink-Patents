"""Controller component."""

from demo_project.actuator import actuator_close, actuator_open


def update_valve(temperature: float, limit: float) -> None:
    """Open the valve when temperature reaches the configured limit."""
    if temperature >= limit:
        actuator_open()
    else:
        actuator_close()

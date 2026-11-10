"""Actuator component."""


_is_open = False


def actuator_open() -> None:
    """Request opening the valve."""
    global _is_open
    _is_open = True


def actuator_close() -> None:
    """Request closing the valve."""
    global _is_open
    _is_open = False


def is_open() -> bool:
    return _is_open

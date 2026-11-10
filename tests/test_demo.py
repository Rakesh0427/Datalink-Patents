import unittest

from demo_project import actuator
from demo_project.app import process_reading


class TemperatureValveTests(unittest.TestCase):
    """Example requirements-based scenarios for the integration flow."""

    def setUp(self):
        actuator.actuator_close()

    def test_below_limit_keeps_valve_closed(self):
        process_reading(raw=100, limit=20.0)
        self.assertFalse(actuator.is_open())

    def test_at_limit_opens_valve(self):
        process_reading(raw=200, limit=20.0)
        self.assertTrue(actuator.is_open())

    def test_above_limit_opens_valve(self):
        process_reading(raw=250, limit=20.0)
        self.assertTrue(actuator.is_open())


if __name__ == "__main__":
    unittest.main()

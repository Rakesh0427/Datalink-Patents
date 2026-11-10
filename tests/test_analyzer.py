import unittest
from pathlib import Path

from coupling_guard.analyzer import analyze, build_report


REPO = Path(__file__).resolve().parents[1]


class AnalyzerTests(unittest.TestCase):
    def test_changed_sensor_impacts_controller_and_actuator(self):
        result = analyze(
            REPO,
            REPO / "coupling.json",
            changed=["demo_project/sensor.py"],
        )
        impacted = set(result.impacted_nodes)
        self.assertIn("demo_project.sensor.convert_temperature", impacted)
        self.assertIn("demo_project.app.process_reading", impacted)
        self.assertIn("demo_project.controller.update_valve", impacted)
        self.assertIn("demo_project.actuator.actuator_open", impacted)

    def test_report_maps_impact_to_requirements_and_tests(self):
        result = analyze(
            REPO,
            REPO / "coupling.json",
            changed=["demo_project/sensor.py"],
        )
        report = build_report(result)
        self.assertIn("REQ-TEMP-014", report["affected_requirements"])
        self.assertIn("TC-TEMP-LIMIT", report["recommended_tests"])
        edge_types = {kind for edge in report["edges"] for kind in edge["coupling_types"]}
        self.assertIn("data", edge_types)
        self.assertIn("control", edge_types)

    def test_unrelated_file_is_not_a_changed_function(self):
        result = analyze(
            REPO,
            REPO / "coupling.json",
            changed=["README.md"],
        )
        self.assertEqual(result.changed_nodes, [])


if __name__ == "__main__":
    unittest.main()

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from ccdc_local.analyzer import MAX_MERMAID_EDGES, MAX_MERMAID_FUNCTIONS, analyze_change, _mermaid


class CxxAnalyzerTests(unittest.TestCase):
    def test_c_and_cpp_change_produces_data_control_and_flow_report(self):
        if not shutil.which("git"):
            self.skipTest("git is required for the local repository integration test")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            (root / "src" / "sensor").mkdir(parents=True)
            (root / "src" / "control").mkdir(parents=True)
            (root / "src" / "output").mkdir(parents=True)
            (root / "src" / "app").mkdir(parents=True)
            (root / "src" / "sensor" / "sensor.h").write_text("float convert_temperature(int raw);\n")
            (root / "src" / "sensor" / "sensor.c").write_text(
                "float convert_temperature(int raw) { return raw * 0.1f; }\n"
            )
            (root / "src" / "output" / "actuator.h").write_text(
                "void actuator_open();\nvoid actuator_close();\n"
            )
            (root / "src" / "output" / "actuator.cpp").write_text(
                '#include "actuator.h"\nvoid actuator_open() {}\nvoid actuator_close() {}\n'
            )
            (root / "src" / "control" / "controller.h").write_text(
                "void update_valve(float temperature, float limit);\n"
            )
            (root / "src" / "control" / "controller.cpp").write_text(
                '#include "controller.h"\n#include "../output/actuator.h"\n'
                "void update_valve(float temperature, float limit) {\n"
                "  if (temperature >= limit) actuator_open(); else actuator_close();\n"
                "}\n"
            )
            (root / "src" / "app" / "app.cpp").write_text(
                '#include "../sensor/sensor.h"\n#include "../control/controller.h"\n'
                "void process(int raw) { update_valve(convert_temperature(raw), 20.0f); }\n"
            )
            config = {
                "components": {
                    "Sensor": ["src/sensor"],
                    "Control": ["src/control"],
                    "Output": ["src/output"],
                    "Application": ["src/app"],
                },
                "requirements": {"REQ-TEMP": ["src/sensor/sensor.c::convert_temperature#1"]},
                "tests": {"TC-TEMP": ["REQ-TEMP"]},
            }
            (root / "ccdc.json").write_text(json.dumps(config))
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "CCDC Test"], cwd=root, check=True)
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "SCR-12 baseline"], cwd=root, check=True)
            base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
            sensor = root / "src" / "sensor" / "sensor.c"
            sensor.write_text("float convert_temperature(int raw) { return raw * 0.01f; }\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "SCR-12 change calibration"], cwd=root, check=True)
            head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
            output = Path(temp) / "report"
            issue = {"key": "SCR-12", "summary": "Change calibration", "updated": "today", "status": "Open"}
            progress_events = []
            files = analyze_change(
                root, base, head, issue, output, root / "ccdc.json",
                progress=lambda percent, message: progress_events.append((percent, message)),
            )
            report = json.loads(files["json"].read_text())
            self.assertIn("src/sensor/sensor.c::convert_temperature#1", report["changed_functions"])
            self.assertIn("TC-TEMP", report["recommended_tests"])
            self.assertTrue(any(edge["cross_component"] and "data" in edge["coupling_types"] for edge in report["edges"]))
            self.assertTrue(any("control" in edge["coupling_types"] for edge in report["edges"]))
            self.assertTrue(files["mermaid"].exists())
            self.assertTrue(files["mermaid_svg"].exists())
            self.assertTrue(files["mermaid_png"].exists())
            self.assertIn("<svg", files["mermaid_svg"].read_text())
            png_bytes = files["mermaid_png"].read_bytes()
            self.assertTrue(png_bytes.startswith(b"\x89PNG\r\n\x1a\n"))
            self.assertLessEqual(int.from_bytes(png_bytes[16:20], "big"), 2400)
            self.assertLessEqual(int.from_bytes(png_bytes[20:24], "big"), 1800)
            self.assertTrue(files["diff"].exists())
            markdown = files["markdown"].read_text()
            self.assertIn("## At a glance", markdown)
            self.assertIn("CC edges — Control Coupling", markdown)
            self.assertIn("DC edges — Data Coupling", markdown)
            self.assertIn("Mermaid.js", markdown)
            determinate = [percent for percent, _message in progress_events if percent is not None]
            self.assertEqual(determinate[-1], 99)
            self.assertEqual(determinate, sorted(determinate))
            self.assertTrue(any("parsed" in message and "/" in message for _percent, message in progress_events))
            self.assertTrue(any("baseline source blobs" in message for _percent, message in progress_events))

    def test_mermaid_diagram_caps_large_graphs_and_reports_omitted_content(self):
        names = [f"src/module_{index:03}::function_{index:03}#1" for index in range(300)]
        report = {
            "changed_functions": [names[150]],
            "impacted_functions": names,
            "edges": [
                {
                    "caller": names[index],
                    "callee": names[index + 1],
                    "coupling_types": ["data"],
                    "state": "unchanged",
                    "path": f"src/module_{index:03}.cpp",
                    "line": index + 1,
                }
                for index in range(len(names) - 1)
            ],
        }
        source = _mermaid(report)
        node_lines = [line for line in source.splitlines() if line.startswith("  N") and '["' in line]
        edge_lines = [line for line in source.splitlines() if " -->|" in line]
        self.assertEqual(len(node_lines), MAX_MERMAID_FUNCTIONS)
        self.assertLessEqual(len(edge_lines), MAX_MERMAID_EDGES)
        self.assertIn("Diagram limit:", source)
        self.assertIn(f"{len(names) - MAX_MERMAID_FUNCTIONS} functions", source)
        self.assertIn("Edge Details / JSON", source)


if __name__ == "__main__":
    unittest.main()

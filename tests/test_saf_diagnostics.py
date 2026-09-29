import json
import tempfile
import unittest
from pathlib import Path
from saf_desktop.diagnostics import write_incident


class DiagnosticTests(unittest.TestCase):
    def test_report_does_not_include_exception_message(self):
        with tempfile.TemporaryDirectory() as directory:
            try:
                raise RuntimeError("PRIVATE_TEST_SECRET")
            except RuntimeError as error:
                path = write_incident(type(error), error, error.__traceback__, directory)
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("PRIVATE_TEST_SECRET", text)
            self.assertNotIn(":", path.name)
            report = json.loads(text)
            self.assertEqual(report["exception_type"], "RuntimeError")
            self.assertEqual(report["schema_version"], 1)
            self.assertTrue(report["frames"])

    def test_unique_reports(self):
        with tempfile.TemporaryDirectory() as directory:
            first = write_incident(ValueError, ValueError(), None, directory)
            second = write_incident(ValueError, ValueError(), None, directory)
            self.assertNotEqual(first, second)
            self.assertEqual(len(list(Path(directory).glob("*.json"))), 2)


if __name__ == "__main__":
    unittest.main()

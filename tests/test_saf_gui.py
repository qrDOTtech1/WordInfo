import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QTabWidget
from saf_desktop.__main__ import MainWindow


class DesktopSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_window_opens_without_trading_imports(self):
        window = MainWindow()
        try:
            window.show()
            self.app.processEvents()
            self.assertTrue(window.isVisible())
            self.assertIn("SAF Engine", window.windowTitle())
            self.assertIsInstance(window.centralWidget(), QTabWidget)
            self.assertEqual(window.centralWidget().count(), 5)
            self.assertFalse(any(name == "core" or name.startswith("core.") for name in sys.modules))
            self.assertFalse(any(name == "real_control" or name.startswith("real_control.") for name in sys.modules))
        finally:
            window.close()
            window.deleteLater()
            self.app.processEvents()


if __name__ == "__main__":
    unittest.main()

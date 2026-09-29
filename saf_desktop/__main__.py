"""Launch with python -m saf_desktop. Does not import the trading engine."""
import sys
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel, QMainWindow, QTabWidget, QVBoxLayout, QWidget
from . import __version__
from .diagnostics import install_exception_hook


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"SAF Engine {__version__} | Set and Forget")
        self.resize(1180, 760)
        tabs = QTabWidget()
        pages = {
            "Vue d'ensemble": ("SAF ENGINE", "Socle desktop — moteur non connecté", "Aucun ordre envoyé. Aucun résultat financier simulé ou inventé."),
            "Stratégies": ("Stratégies", "Migration à auditer", "La version existante reste intacte. Référence et challenger seront séparés."),
            "Expériences": ("Référence / Challenger", "Aucune expérience en cours", "Les comparaisons utiliseront des données identiques et des paramètres versionnés."),
            "Diagnostic": ("Diagnostic local", "Rapports minimaux enregistrés localement", "Pas de capture des variables d'environnement. Pas d'envoi GitHub automatique dans cette version."),
            "Configuration": ("Configuration", "Mode réel désactivé", "Les budgets, permissions et fournisseurs IA seront configurés avant toute connexion au moteur.")
        }
        for name, texts in pages.items():
            page = QWidget()
            layout = QVBoxLayout(page)
            layout.setContentsMargins(32, 32, 32, 32)
            for index, text in enumerate(texts):
                label = QLabel(text)
                label.setWordWrap(True)
                label.setTextFormat(Qt.TextFormat.PlainText)
                label.setObjectName("heading" if index == 0 else "body")
                layout.addWidget(label)
            layout.addStretch()
            tabs.addTab(page, name)
        self.setCentralWidget(tabs)
        self.statusBar().showMessage("Socle desktop | Trading non connecté | Diagnostics locaux uniquement")


def main():
    install_exception_hook()
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet("QWidget { background: #101827; color: #e5edf7; font-size: 14px; } QLabel#heading { font-size: 30px; font-weight: 600; color: #5eead4; } QLabel#body { padding: 10px 0; } QTabBar::tab { background: #182338; padding: 14px 20px; } QTabBar::tab:selected { background: #28405a; } QStatusBar { color: #94a3b8; }")
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

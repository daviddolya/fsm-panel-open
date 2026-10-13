"""Render the real Qt interface with synthetic accounts, then cleanly close it."""

import argparse
import os
import tempfile
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication

from fsm_panel.adapters.fakes import FakeGame, FakeLease, MemorySecretStore
from fsm_panel.core import AccountRepository, Coordinator
from fsm_panel.ui.window import MainWindow


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    app = QApplication([])
    # The offscreen Qt plugin does not enumerate Windows system fonts.
    # Use an installed font for previews; it is not copied into the build.
    if not QFontDatabase.families() and os.name == "nt":
        font_path = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts/segoeui.ttf"
        font_id = QFontDatabase.addApplicationFont(str(font_path))
        if font_id < 0:
            raise RuntimeError("preview_system_font_unavailable")
        app.setFont(QFont(QFontDatabase.applicationFontFamilies(font_id)[0], 10))
    with tempfile.TemporaryDirectory(prefix="fsm-preview-") as temporary:
        with AccountRepository(Path(temporary) / "accounts.sqlite") as repo:
            window = MainWindow(
                repo,
                Coordinator(repo, game=FakeGame(), lease=FakeLease()),
                MemorySecretStore(),
                Path(temporary),
                simulate=True,
            )
            window.show()
            timer = QTimer()

            def ready() -> None:
                if not window.ready:
                    return
                timer.stop()
                for _ in range(4):
                    window.add_sample()
                window.table.selectAll()
                window.start_selected()
                QTimer.singleShot(500, capture)

            def capture() -> None:
                for index, name in (
                    (0, "accounts"),
                    (1, "settings"),
                    (2, "trades"),
                    (3, "license"),
                ):
                    window.tabs.setCurrentIndex(index)
                    app.processEvents()
                    if not window.grab().save(str(args.output / f"{name}.png")):
                        raise RuntimeError("preview_write_failed")
                window.close()

            timer.timeout.connect(ready)
            timer.start(20)
            QTimer.singleShot(10000, window.close)
            app.exec()
    print(args.output.resolve())


if __name__ == "__main__":
    main()

"""Exercise the actual synthetic desktop/worker slice in a packaged process."""

import base64
import io
import json
import time
from pathlib import Path

from PIL import Image
from PySide6.QtCore import QTimer

from fsm_panel.adapters.account_config import load_config_templates
from fsm_panel.adapters.gsi_config import load_gsi_template
from fsm_panel.notifications.images import compose_item_images

from .window import MainWindow


def verify_item_composite() -> None:
    """Load the packaged PNG decoder/encoder with fixed synthetic source bytes."""
    first = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8AgCAAEEgER"
        "3CXTfwAAAABJRU5ErkJggg=="
    )
    second = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAD0lEQVR4nGNgYPgPRmAK"
        "ABf2A/1+6zfzAAAAAElFTkSuQmCC"
    )
    composite = compose_item_images(first, second)
    with Image.open(io.BytesIO(composite.png)) as decoded:
        if (
            (composite.width, composite.height) != (2, 1)
            or decoded.format != "PNG"
            or decoded.mode != "RGBA"
            or decoded.size != (2, 1)
            or decoded.getpixel((0, 0)) != (255, 0, 0, 17)
            or decoded.getpixel((1, 0)) != (0, 0, 255, 255)
        ):
            raise RuntimeError("item_composite_smoke_failed")


class DesktopSmoke:
    def __init__(self, window: MainWindow, report: Path) -> None:
        self.window, self.report = window, report
        self.passed = False
        self._checks_passed = False
        self._item_composite_verified = False
        self._code = "smoke_interrupted"
        self.step = 0
        self.deadline = time.monotonic() + 40
        self.timer = QTimer(window)
        self.timer.timeout.connect(self.tick)
        window.bridge.shutdown_finished.connect(self.finished_shutdown)
        self.timer.start(25)

    def finish(self, passed: bool, code: str) -> None:
        self._checks_passed, self._code = passed, code
        self.timer.stop()
        self.window.close()

    def finished_shutdown(self) -> None:
        self.timer.stop()
        bridge = self.window.bridge
        worker = self.window.trades.worker
        process = worker._process if worker else None
        clean = (
            bridge.shutdown_error is None
            and not bridge._thread.is_alive()
            and process is not None
            and process.returncode == 0
        )
        self.passed = self._checks_passed and clean
        code = self._code if clean or not self._checks_passed else "smoke_cleanup_failed"
        self.report.parent.mkdir(parents=True, exist_ok=True)
        self.report.write_text(
            json.dumps(
                {
                    "passed": self.passed,
                    "code": code,
                    "step": self.step,
                    "mode": "simulation",
                    "live_steam": "not_run",
                    "shutdown_verified": clean,
                    "config_templates_verified": self.step > 0,
                    "gsi_template_verified": self.step > 0,
                    "item_composite_verified": self._item_composite_verified,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def tick(self) -> None:
        try:
            self.advance()
        except Exception:
            self.finish(False, "smoke_failed")

    def advance(self) -> None:
        window, panel = self.window, self.window.trades
        if time.monotonic() > self.deadline:
            self.finish(False, "smoke_timeout")
            return
        if self.step == 0 and window.ready:
            # Exercise resource lookup/hash verification from the frozen package,
            # not merely the build machine's source checkout.
            load_config_templates()
            load_gsi_template()
            verify_item_composite()
            self._item_composite_verified = True
            if window.repository.list_accounts():
                self.finish(False, "smoke_requires_empty_data_directory")
                return
            window.add_sample()
            window.table.selectRow(0)
            window.start_button.click()
            self.step = 1
        elif self.step == 1 and window.repository.list_accounts()[0].state.value == "running":
            panel.inventory_button.click()
            self.step = 2
        elif self.step == 2 and panel.inventory_button.isEnabled():
            if "1 предметов" not in panel.result.toPlainText():
                raise RuntimeError("inventory_smoke_failed")
            panel.send_button.click()
            self.step = 3
        elif self.step == 3 and panel.send_button.isEnabled():
            if ": sent;" not in panel.result.toPlainText():
                raise RuntimeError("trade_smoke_failed")
            panel.reconcile_button.click()
            self.step = 4
        elif self.step == 4 and panel.reconcile_button.isEnabled():
            effects = window.repository.list_effects()
            if len(effects) != 1 or ": sent;" not in panel.result.toPlainText().splitlines()[-1]:
                raise RuntimeError("reconciliation_smoke_failed")
            window.table.selectRow(0)
            window.stop_selected()
            self.step = 5
        elif self.step == 5 and window.repository.list_accounts()[0].state.value == "stopped":
            self.finish(True, "desktop_worker_roundtrip_passed")

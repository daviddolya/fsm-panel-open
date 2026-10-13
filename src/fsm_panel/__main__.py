"""Launch the desktop. Real mode fails closed; simulation requires --simulate."""

import argparse
import json
import os
import sys
from pathlib import Path

from fsm_license.crypto import b64decode
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from fsm_panel.adapters.fakes import FakeGame, FakeLease, MemorySecretStore
from fsm_panel.adapters.secrets import DPAPISecretStore
from fsm_panel.application import LicenseAdmission
from fsm_panel.core import AccountRepository, Coordinator
from fsm_panel.licensing import LicenseClient
from fsm_panel.licensing.device import WindowsDpapiDeviceStore
from fsm_panel.ui.window import MainWindow


def main() -> int:
    parser = argparse.ArgumentParser(description="FSM Panel Open — research build")
    parser.add_argument(
        "--simulate", action="store_true", help="Explicit local simulator; no Steam activity"
    )
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument(
        "--license-config",
        type=Path,
        help="Pinned service origin and verification keys; no activation secret",
    )
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="Open UI and close automatically for packaging validation",
    )
    args = parser.parse_args()
    if args.simulate and args.license_config:
        parser.error("--simulate and --license-config select different environments")
    default_root = (
        Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local/share"))) / "FSMPanelOpen"
    )
    data_dir = (
        args.data_dir or default_root / ("simulation" if args.simulate else "production")
    ).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    client = None
    if args.license_config:
        config = json.loads(args.license_config.read_text(encoding="utf-8"))
        client = LicenseClient(
            base_url=config["base_url"],
            store=WindowsDpapiDeviceStore(data_dir / "device.dpapi"),
            public_keys={key: b64decode(value) for key, value in config["public_keys"].items()},
            issuer=config["issuer"],
            audience=config["audience"],
            allow_insecure_http=config.get("allow_local_http", False),
        )
    repository = AccountRepository(data_dir / "accounts.sqlite")
    secrets = MemorySecretStore() if args.simulate else DPAPISecretStore(data_dir / "secrets")
    coordinator = Coordinator(
        repository,
        game=FakeGame() if args.simulate else None,
        lease=FakeLease() if args.simulate else LicenseAdmission(client) if client else None,
    )
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("FSM Panel Open")
    window = MainWindow(repository, coordinator, secrets, data_dir, simulate=args.simulate)
    if client:
        window.attach_license(client, config["base_url"])
    window.show()
    if args.smoke_test:
        QTimer.singleShot(700, window.close)
    try:
        return app.exec()
    finally:
        repository.close()


if __name__ == "__main__":
    raise SystemExit(main())

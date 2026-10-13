"""Desktop licensing boundary. Gate admission only; always allow stop/export/recovery."""

from .client import LicenseClient
from .device import DeviceIdentity, DeviceStore, MemoryDeviceStore, WindowsDpapiDeviceStore
from .gate import LocalLeaseGate
from .models import LeaseClaims, LeaseGate, LicenseUnavailable

__all__ = [
    "DeviceIdentity",
    "DeviceStore",
    "LeaseClaims",
    "LeaseGate",
    "LicenseClient",
    "LicenseUnavailable",
    "LocalLeaseGate",
    "MemoryDeviceStore",
    "WindowsDpapiDeviceStore",
]

"""Opt-in, local GSI telemetry; never a game-action adapter."""

from .http import GsiServer, create_gsi_app
from .receiver import GameStateReceiver, GameStateSnapshot, GsiError

__all__ = ["GameStateReceiver", "GameStateSnapshot", "GsiError", "GsiServer", "create_gsi_app"]

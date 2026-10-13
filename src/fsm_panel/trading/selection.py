"""Import the embedded script's selected-item shape without passing paths to Node."""

import json
from pathlib import Path

from .models import Asset, TradeError


def normalize_selected_assets(value: object) -> tuple[Asset, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= 500:
        raise TradeError("invalid_request")
    assets: dict[tuple[int, str, str], Asset] = {}
    try:
        for item in value:
            if not isinstance(item, dict):
                raise ValueError
            app = item.get("app_id", item.get("appid"))
            context = item.get("context_id", item.get("contextid", "2"))
            asset_id = item.get("asset_id", item.get("assetid", item.get("id")))
            amount = item.get("amount", 1)
            if any(type(number) is bool for number in (app, context, asset_id, amount)):
                raise ValueError
            if not isinstance(app, int | str) or not str(app).isascii() or not str(app).isdecimal():
                raise ValueError
            # Canonical decimal text only; unlike parseInt this never accepts '730garbage'.
            asset = Asset(int(app), str(context), str(asset_id), amount)
            key = (asset.app_id, asset.context_id, asset.asset_id)
            if key in assets and assets[key].amount != asset.amount:
                raise ValueError
            assets[key] = asset
    except TypeError, ValueError, TradeError:
        raise TradeError("invalid_request") from None
    return tuple(assets.values())


def load_selected_assets(path: str | Path) -> tuple[Asset, ...]:
    try:
        with Path(path).open("rb") as source:
            data = source.read(262_145)
        if len(data) > 262_144:
            raise ValueError
        return normalize_selected_assets(json.loads(data))
    except OSError, ValueError, UnicodeError:
        raise TradeError("invalid_request") from None

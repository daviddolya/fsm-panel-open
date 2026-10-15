"""Inert traces of two recovered co-op state helpers, with no live dependencies."""

import math
from collections.abc import Generator
from dataclasses import dataclass
from typing import cast

LEGACY_REGIONS_MAX_AGE = 60.0
LEGACY_REGIONS_REFRESH_TARGET = "self.bkcfWJfVrPJw"

type Number = int | float


class CoopStatePlanError(ValueError):
    def __init__(self) -> None:
        super().__init__("coop_state_plan_response_invalid")


@dataclass(frozen=True)
class IteratorRef:
    token: str


@dataclass(frozen=True)
class GameKey:
    token: str


@dataclass(frozen=True)
class GameEntry:
    token: str


@dataclass(frozen=True)
class IterationEnd:
    """Normal exhaustion, distinct from an injected iterator failure."""


@dataclass(frozen=True)
class RegionsRef:
    """Opaque identity for the returned regions object; no contents are inspected."""

    token: str


@dataclass(frozen=True)
class RegionsCache:
    stamp: Number
    regions: RegionsRef


@dataclass(frozen=True)
class CacheMissing:
    """getattr used its default; distinct from an invalid stored None value."""


@dataclass(frozen=True)
class ThreadRef:
    token: str


type Argument = (
    str
    | Number
    | bool
    | tuple[str, ...]
    | IteratorRef
    | GameKey
    | GameEntry
    | RegionsCache
    | ThreadRef
)


@dataclass(frozen=True)
class CoopStateRequest:
    operation: str
    arguments: tuple[Argument, ...] = ()
    keywords: tuple[tuple[str, Argument], ...] = ()


def _ack(value: object) -> None:
    if value is not None:
        raise CoopStatePlanError()


def _number(value: object) -> Number:
    if type(value) not in (int, float):
        raise CoopStatePlanError()
    if type(value) is float and not math.isfinite(value):
        raise CoopStatePlanError()
    return cast(Number, value)


def _string(value: object) -> str:
    if type(value) is not str:
        raise CoopStatePlanError()
    return cast(str, value)


def _reference[T: (IteratorRef, GameKey, GameEntry, RegionsRef, ThreadRef)](
    value: object, expected: type[T]
) -> T:
    if type(value) is not expected:
        raise CoopStatePlanError()
    reference = cast(T, value)
    _string(reference.token)
    return reference


def iter_legacy_all_in_menu() -> Generator[CoopStateRequest, object, bool]:
    """Describe the global helper's length, iteration and per-entry catch scopes.

    The supplied iterator yields GameKey or IterationEnd. Each entry lookup is
    deferred and returns a GameEntry identity for that lookup's path observation.
    Invalid protocol values always propagate, even inside legacy catch regions.
    """
    length = yield CoopStateRequest("game_info_length")
    if type(length) is not int or cast(int, length) < 0:
        raise CoopStatePlanError()
    if length == 0:
        return False
    try:
        iterator = _reference((yield CoopStateRequest("game_info_iterator")), IteratorRef)
        while True:
            try:
                item = yield CoopStateRequest("next_game_info_key", (iterator,))
            except CoopStatePlanError:
                raise
            except StopIteration:
                return True
            if type(item) is IterationEnd:
                return True
            key = _reference(item, GameKey)
            try:
                now = _number((yield CoopStateRequest("read_time", ("entry_age",))))
                entry = _reference(
                    (yield CoopStateRequest("read_game_info_entry", (key,))), GameEntry
                )
                stamp = _number(
                    (yield CoopStateRequest("read_entry_path", (entry, ("provider", "timestamp"))))
                )
                if _number(now - stamp) > 35:
                    continue
                entry = _reference(
                    (yield CoopStateRequest("read_game_info_entry", (key,))), GameEntry
                )
                map_name = _string(
                    (yield CoopStateRequest("read_entry_path", (entry, ("map", "name"))))
                )
                if "_" in map_name:
                    return False
                entry = _reference(
                    (yield CoopStateRequest("read_game_info_entry", (key,))), GameEntry
                )
                activity = _string(
                    (yield CoopStateRequest("read_entry_path", (entry, ("player", "activity"))))
                )
                if activity != "menu":
                    return False
            except CoopStatePlanError:
                raise
            except Exception:
                pass
    except CoopStatePlanError:
        raise
    except Exception:
        return False


def iter_legacy_regions(
    max_age: Number = LEGACY_REGIONS_MAX_AGE,
) -> Generator[CoopStateRequest, object, RegionsRef]:
    """Describe cache read, optional refresh-thread start and old-object return.

    The cache observation returns RegionsCache or CacheMissing. The request's
    default cache carries a fresh RegionsRef object for the empty-list default.
    No region contents or real threads are accessed; no source recovery is added.
    """
    age_limit = _number(max_age)
    default_cache = RegionsCache(0.0, RegionsRef("default-empty-regions"))
    try:
        observed = yield CoopStateRequest(
            "get_regions_cache", ("_srt_regions_cache", default_cache)
        )
    except CoopStatePlanError:
        raise
    except AttributeError:
        # Models getattr's built-in default semantics, not a source-level catch.
        observed = CacheMissing()
    if type(observed) is CacheMissing:
        cache = default_cache
    elif type(observed) is RegionsCache:
        cache = cast(RegionsCache, observed)
    else:
        raise CoopStatePlanError()
    stamp = _number(cache.stamp)
    regions = _reference(cache.regions, RegionsRef)
    now = _number((yield CoopStateRequest("read_time", ("regions_age",))))
    if _number(now - stamp) > age_limit:
        try:
            busy = yield CoopStateRequest("get_regions_busy", ("_srt_regions_busy", False))
        except CoopStatePlanError:
            raise
        except AttributeError:
            busy = False
        if type(busy) is not bool:
            raise CoopStatePlanError()
        if not busy:
            _ack((yield CoopStateRequest("write_regions_busy", (True,))))
            observed_thread = yield CoopStateRequest(
                "construct_thread",
                keywords=(("target", LEGACY_REGIONS_REFRESH_TARGET), ("daemon", True)),
            )
            thread = _reference(observed_thread, ThreadRef)
            _ack((yield CoopStateRequest("start_thread", (thread,))))
    return regions

"""Inert trace of the recovered co-op autoaccept caller; no effects are executed.

Observations and acknowledgements are supplied by a synthetic driver. Helper
policy, scheduling, thread creation, co-op transport and input remain external.
"""

import math
from collections.abc import Generator
from dataclasses import dataclass
from typing import cast

LEGACY_INITIAL_MIN_ACCEPT_DELAY = 4.0
LEGACY_DISPATCH_TARGET = "self.VIXswEwxFlBY"

type Number = int | float
type Scalar = str | Number | bool | None
type Value = Scalar | tuple[Value, ...]


class CoopAcceptPlanError(ValueError):
    def __init__(self) -> None:
        super().__init__("coop_accept_plan_response_invalid")


@dataclass(frozen=True)
class CoopRef:
    """Identity token for a non-None captured co-op object, even if false-valued."""

    token: str


@dataclass(frozen=True)
class ThreadRef:
    """Identity token for the result of the described Thread construction."""

    token: str


type Argument = Value | frozenset[str] | CoopRef | ThreadRef


@dataclass(frozen=True)
class CoopAcceptRequest:
    operation: str
    arguments: tuple[Argument, ...] = ()
    keywords: tuple[tuple[str, Argument], ...] = ()


type Trace = Generator[CoopAcceptRequest, object]


def _ack(value: object) -> None:
    if value is not None:
        raise CoopAcceptPlanError()


def _string(value: object) -> str:
    if type(value) is not str:
        raise CoopAcceptPlanError()
    return cast(str, value)


def _number(value: object) -> Number:
    if type(value) not in (int, float):
        raise CoopAcceptPlanError()
    if type(value) is float and not math.isfinite(value):
        raise CoopAcceptPlanError()
    return cast(Number, value)


def _scalar(value: object) -> Scalar:
    if type(value) in (str, bool) or value is None:
        return cast(Scalar, value)
    return _number(value)


def _value(value: object) -> Value:
    if type(value) is tuple:
        for item in cast(tuple[object, ...], value):
            _value(item)
        return cast(tuple[Value, ...], value)
    return _scalar(value)


def _tuple(value: object) -> tuple[object, ...]:
    if type(value) is not tuple:
        raise CoopAcceptPlanError()
    return cast(tuple[object, ...], value)


def _falsey(value: object) -> bool:
    # Exact builtin containers are inspected only for length. No user hooks run.
    if type(value) in (tuple, list, dict):
        return len(cast(tuple[object, ...], value)) == 0
    return not _scalar(value)


def _steamid(account: object) -> str:
    if _falsey(account):
        return ""
    if type(account) is not dict:
        raise CoopAcceptPlanError()
    fields = cast(dict[object, object], account)
    # Validate keys before lookup so a hostile key cannot run equality hooks.
    for key in fields:
        _string(key)
    return str(_scalar(fields.get("steamid", "")))


def _servers(value: object) -> frozenset[str]:
    if type(value) is not frozenset:
        raise CoopAcceptPlanError()
    for item in cast(frozenset[object], value):
        _string(item)
    return cast(frozenset[str], value)


def _effect(operation: str, *arguments: Argument) -> Trace:
    _ack((yield CoopAcceptRequest(operation, arguments)))


def _log(event: str, **fields: Argument) -> Trace:
    _ack((yield CoopAcceptRequest("coop_log", (event,), tuple(fields.items()))))


def iter_legacy_coop_accept(coop_tickets: object) -> Trace:
    """Describe the complete immediate caller with late observations and no catch.

    Tickets must be an exact tuple of three-item tuples. Validation happens only
    when the original reaches that value. Read requests have the response schema
    in docs/coop-accept-plan.md; effect requests require None. Any thrown exception
    or invalid response terminates the trace without rollback or further effects.
    """
    coop_response = yield CoopAcceptRequest("read_coop")
    if coop_response is None:
        return
    if type(coop_response) is not CoopRef:
        raise CoopAcceptPlanError()
    coop = cast(CoopRef, coop_response)
    _string(coop.token)
    original = _tuple(coop_tickets)
    tickets: list[tuple[str, str]] = []
    seen_at: dict[str, Number] = {}
    for row in original:
        columns = _tuple(row)
        if len(columns) != 3:
            raise CoopAcceptPlanError()
        login, ticket_server, logged_at = columns
        if _falsey(ticket_server):
            continue
        ticket_server = _string(ticket_server)
        login = _string(login)
        steamid = _steamid((yield CoopAcceptRequest("read_launched_account", (login,))))
        if steamid:
            tickets.append((steamid, ticket_server))
            timestamp = 0 if _falsey(logged_at) else _number(logged_at)
            seen_at[ticket_server] = max(seen_at.get(ticket_server, 0), timestamp)
    normalized = tuple(tickets)
    yield from _effect("write_coop_servers", frozenset(server for _, server in tickets))
    in_menu = _value((yield CoopAcceptRequest("observe_all_in_menu")))
    regions = _value((yield CoopAcceptRequest("observe_regions")))
    if tickets or original:
        short_servers = set()
        for _, ticket_server in tickets:
            short_servers.add(_string((yield CoopAcceptRequest("short_id", (ticket_server,)))))
        yield from _log(
            "TICKETS",
            found=len(original),
            sent=len(tickets),
            in_menu=in_menu,
            servers=tuple(sorted(short_servers)),
            regions=regions or "any",
        )
    yield from _effect("report_tickets", coop, normalized, in_menu, regions)
    server_response = yield CoopAcceptRequest("consume_accept", (coop,))
    if server_response is None:
        return
    server = _string(server_response)
    if not _value((yield CoopAcceptRequest("read_setting", ("AUTOACCEPTGAME",)))):
        return
    already_accepted = yield CoopAcceptRequest("accepted_games_contains", (server,))
    if type(already_accepted) is not bool:
        raise CoopAcceptPlanError()
    if already_accepted:
        return
    if _servers((yield CoopAcceptRequest("read_coop_servers"))) != frozenset((server,)):
        short_server = _string((yield CoopAcceptRequest("short_id", (server,))))
        current = _servers((yield CoopAcceptRequest("read_coop_servers")))
        short_current = []
        for item in current:
            short_current.append(_string((yield CoopAcceptRequest("short_id", (item,)))))
        yield from _log("ACCEPT_REFUSED", server=short_server, current=tuple(sorted(short_current)))
        yield from _effect("leave_match", coop, "match_cancelled")
        return
    waited: Number = 0.0
    if seen_at.get(server):
        now = _number((yield CoopAcceptRequest("read_time", ("ticket_age",))))
        waited = _number(now - seen_at.get(server, 0))
    outer_minimum = _number((yield CoopAcceptRequest("read_global", ("MIN_ACCEPT_DELAY",))))
    inner_minimum = _number((yield CoopAcceptRequest("read_global", ("MIN_ACCEPT_DELAY",))))
    delay = _number(min(outer_minimum, max(0.0, _number(inner_minimum - waited))))
    yield from _effect("append_accepted_game", server)
    activated = _number((yield CoopAcceptRequest("read_time", ("activation_timestamp",))))
    yield from _effect("write_setting", "LAST_AUTOACCEPTER_ACTIVATION", activated)
    last_accepted = _number(
        (yield CoopAcceptRequest("read_time", ("last_accepted_game_timestamp",)))
    )
    yield from _effect("write_setting", "last_accepted_game", last_accepted)
    short_server = _string((yield CoopAcceptRequest("short_id", (server,))))
    enabled_for_log = _value((yield CoopAcceptRequest("read_setting", ("ENABLE_AUTOACCEPT",))))
    yield from _log(
        "ACCEPTING",
        server=short_server,
        enabled=enabled_for_log,
        age=round(waited, 1),
        wait=round(delay, 1),
    )
    if _value((yield CoopAcceptRequest("read_setting", ("ENABLE_AUTOACCEPT",)))):
        yield from _effect(
            "application_log", "[A.Accepter] Co-op match confirmed by server. Accepting..."
        )
        thread_response = yield CoopAcceptRequest(
            "construct_thread",
            keywords=(
                ("target", LEGACY_DISPATCH_TARGET),
                ("args", (delay, server)),
                ("daemon", True),
            ),
        )
        if type(thread_response) is not ThreadRef:
            raise CoopAcceptPlanError()
        thread = cast(ThreadRef, thread_response)
        _string(thread.token)
        yield from _effect("start_thread", thread)

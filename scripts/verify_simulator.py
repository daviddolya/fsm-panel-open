"""Repeat 1/10/25/50 synthetic sessions; never launches Steam or proves game parity."""

import argparse
import asyncio
import json
import platform
import tempfile
import time
from pathlib import Path

import psutil

from fsm_panel.adapters.fakes import FakeGame, FakeLease
from fsm_panel.core import AccountRepository, AccountState, Coordinator


def process_metrics() -> dict:
    process = psutil.Process()
    return {
        "rss_bytes": process.memory_info().rss,
        "handles": process.num_handles() if hasattr(process, "num_handles") else None,
        "threads": process.num_threads(),
        "children": len(process.children(recursive=True)),
        "async_tasks": len(asyncio.all_tasks()),
    }


async def exercise(clients: int, cycles: int, directory: Path) -> dict:
    before = process_metrics()
    timings = []
    game, lease = FakeGame(launch_delay=0.001), FakeLease()
    with AccountRepository(directory / f"load-{clients}.sqlite") as repo:
        accounts = [repo.add_account(f"synthetic_{number}") for number in range(clients)]
        ids = [account.id for account in accounts]
        coordinator = Coordinator(repo, game=game, lease=lease, max_concurrency=5)
        try:
            for iteration in range(cycles):
                # A confirmed pre-launch error is safe to retry after cleanup.
                game.fail_next(ids[iteration % clients])
                began = time.monotonic()
                result = await coordinator.start_batch(ids)
                assert all(account.state == AccountState.RUNNING for account in result)
                assert len(game.running) == clients
                await asyncio.gather(*(coordinator.pause(account_id) for account_id in ids))
                await asyncio.gather(*(coordinator.resume(account_id) for account_id in ids))
                lease.allowed = False
                await coordinator.check_leases()
                assert not game.running
                assert all(
                    account.state == AccountState.STOPPED for account in repo.list_accounts()
                )
                denied = await coordinator.start(ids[0])
                assert denied.state == AccountState.BLOCKED
                lease.allowed = True
                timings.append(time.monotonic() - began)
        finally:
            await coordinator.close()
        assert game.max_launching <= 5
        assert not repo.owned_processes()
    await asyncio.sleep(0)
    after = process_metrics()
    assert after["async_tasks"] == before["async_tasks"]
    assert after["children"] == before["children"]
    return {
        "clients": clients,
        "cycles": cycles,
        "maximum_concurrent_launches": game.max_launching,
        "cycle_seconds_max": round(max(timings), 4),
        "before": before,
        "after": after,
        "assertions": "passed",
    }


async def run(cycles: int) -> dict:
    with tempfile.TemporaryDirectory(prefix="fsm-simulator-") as directory:
        rows = [await exercise(size, cycles, Path(directory)) for size in (1, 10, 25, 50)]
    return {
        "scope": "synthetic lifecycle only; not Steam/CS2 performance or parity",
        "platform": platform.platform(),
        "python": platform.python_version(),
        "real_clients": 0,
        "duration_24h_tested": False,
        "runs": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cycles", type=int, default=10)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.cycles <= 10000:
        parser.error("--cycles must be 1..10000")
    result = asyncio.run(run(args.cycles))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "assertions": "passed",
                "clients": [1, 10, 25, 50],
                "cycles": args.cycles,
                "real_clients": 0,
            }
        )
    )


if __name__ == "__main__":
    main()

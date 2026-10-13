import asyncio
import sys
import threading

import psutil
import pytest

from fsm_panel.adapters.fakes import FakeProcessBackend
from fsm_panel.adapters.processes import OwnedProcessController
from fsm_panel.core import AccountRepository, StateConflict, ValidationError


@pytest.fixture
def setup_processes():
    with AccountRepository() as repo:
        account = repo.add_account("synthetic_process")
        backend = FakeProcessBackend()
        controller = OwnedProcessController(repo, spawn=backend.spawn, lookup=backend.lookup)
        yield repo, account, backend, controller


async def test_process_is_owned_and_shell_is_never_enabled(setup_processes):
    repo, account, backend, controller = setup_processes
    owned = await controller.launch(account.id, [sys.executable, "literal & metacharacter"])
    assert len(repo.owned_processes(account.id)) == 1
    assert backend.spawn_options[-1]["shell"] is False
    assert backend.spawn_options[-1]["close_fds"] is True
    process = backend.lookup(owned.pid)
    await controller.stop(account.id)
    assert process.terminated
    assert repo.owned_processes() == []


async def test_reused_pid_is_never_killed(setup_processes):
    repo, account, backend, controller = setup_processes
    owned = await controller.launch(account.id, [sys.executable])
    unrelated = backend.reuse_pid(owned.pid)
    await controller.stop(account.id)
    assert unrelated.alive and not unrelated.terminated and not unrelated.killed
    assert repo.owned_processes() == []


async def test_children_and_hung_owned_process_are_cleaned(setup_processes):
    repo, account, backend, controller = setup_processes
    owned = await controller.launch(account.id, [sys.executable])
    parent = backend.lookup(owned.pid)
    child = backend.spawn([sys.executable])
    child.hung = True
    parent.descendants.append(child)
    unrelated = backend.spawn([sys.executable])
    await controller.stop(account.id)
    assert not parent.alive and child.killed
    assert unrelated.alive
    assert repo.owned_processes() == []


async def test_changed_executable_or_missing_pid_is_not_signalled(setup_processes):
    repo, account, backend, controller = setup_processes
    owned = await controller.launch(account.id, [sys.executable])
    changed = backend.lookup(owned.pid)
    changed.executable = str(sys.executable) + "-different"
    await controller.stop(account.id)
    assert changed.alive and not changed.terminated
    another = await controller.launch(account.id, [sys.executable])
    backend.lookup(another.pid).alive = False
    await controller.stop(account.id)
    assert not repo.owned_processes()


async def test_access_denial_keeps_recovery_record(setup_processes):
    repo, account, backend, controller = setup_processes
    owned = await controller.launch(account.id, [sys.executable])
    process = backend.lookup(owned.pid)

    def denied():
        raise psutil.AccessDenied(owned.pid)

    process.terminate = denied
    with pytest.raises(StateConflict, match="cleanup_required"):
        await controller.stop(account.id)
    assert len(repo.owned_processes()) == 1 and process.alive


async def test_cancel_during_spawn_waits_for_ownership_then_cleans(setup_processes):
    repo, account, backend, _ = setup_processes
    entered, release = threading.Event(), threading.Event()

    def slow_spawn(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return backend.spawn(*args, **kwargs)

    controller = OwnedProcessController(repo, spawn=slow_spawn, lookup=backend.lookup)
    launch = asyncio.create_task(controller.launch(account.id, [sys.executable]))
    assert await asyncio.to_thread(entered.wait, 2)
    launch.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await launch
    assert repo.owned_processes() == []
    assert not any(process.alive for process in backend.processes.values())


async def test_recovery_rechecks_persisted_ownership(tmp_path):
    path = tmp_path / "processes.sqlite"
    backend = FakeProcessBackend()
    with AccountRepository(path) as repo:
        account = repo.add_account("synthetic")
        controller = OwnedProcessController(repo, spawn=backend.spawn, lookup=backend.lookup)
        owned = await controller.launch(account.id, [sys.executable])
    with AccountRepository(path) as repo:
        controller = OwnedProcessController(repo, spawn=backend.spawn, lookup=backend.lookup)
        await controller.recover()
        assert not backend.processes[owned.pid].alive
        assert repo.owned_processes() == []


@pytest.mark.parametrize(
    "argv", ["python -c unsafe", [], ["relative.exe"], ["missing\x00.exe"], [1]]
)
async def test_invalid_argv_never_spawns(setup_processes, argv):
    _, account, backend, controller = setup_processes
    with pytest.raises(ValidationError):
        await controller.launch(account.id, argv)
    assert not backend.processes


async def test_failed_persistence_cleans_new_process(setup_processes, monkeypatch):
    repo, account, backend, controller = setup_processes

    def fail(*args):
        raise OSError("synthetic disk fault")

    monkeypatch.setattr(repo, "remember_process", fail)
    with pytest.raises(StateConflict, match="owned_process_launch_failed"):
        await controller.launch(account.id, [sys.executable])
    assert all(not process.alive for process in backend.processes.values())


async def test_cancelled_stop_keeps_new_launch_out_of_cleanup_window(setup_processes):
    repo, account, backend, controller = setup_processes
    owned = await controller.launch(account.id, [sys.executable])
    original = backend.lookup(owned.pid)
    entered, release = threading.Event(), threading.Event()

    def slow_terminate():
        entered.set()
        assert release.wait(timeout=5)
        original.alive = False

    original.terminate = slow_terminate
    stop = asyncio.create_task(controller.stop(account.id))
    assert await asyncio.to_thread(entered.wait, 2)
    stop.cancel()
    launch = asyncio.create_task(controller.launch(account.id, [sys.executable]))
    await asyncio.sleep(0)
    assert not launch.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await stop
    fresh = await launch
    assert backend.lookup(fresh.pid).alive
    assert [record["pid"] for record in repo.owned_processes()] == [fresh.pid]
    await controller.stop(account.id)

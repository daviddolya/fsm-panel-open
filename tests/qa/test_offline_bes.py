"""Independent BES ownership, recovery, admission and read-only Win32 checks.

No original application or BES is executed. The only real Win32 queries inspect
the pytest Python process; all window messages and target effects are synthetic.
"""

import asyncio
import ctypes
import os
import struct
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

from fsm_panel.adapters import bes
from fsm_panel.adapters.bes import (
    BesError,
    BesJournal,
    BesSession,
    BesWindow,
    OwnedBesService,
    ProcessIdentity,
    ReviewedBesExecutable,
    WindowsBesDriver,
)
from fsm_panel.adapters.processes import OwnedProcess
from fsm_panel.core import AccountRepository, AccountState, StateConflict
from fsm_panel.core.models import Admission
from fsm_panel.cpu import CpuLimitRuntime


@pytest.mark.parametrize("change", ["stopping", "closing", "disabled", "unrecovered"])
async def test_initial_admission_rechecks_state_before_any_limit(tmp_path, change):
    entered, release = asyncio.Event(), asyncio.Event()
    delivered = []

    class WaitingLease:
        async def admit(self, account_id):
            entered.set()
            await release.wait()
            return Admission(True)

    class Service:
        async def limit(self, target, percent, *, executable=None):
            delivered.append(target.token)
            return SimpleNamespace(state="delivered")

        async def stop(self, account_id):
            pass

        async def recover(self):
            pass

    with AccountRepository(tmp_path / "accounts.sqlite") as repository:
        account = repository.add_account("synthetic admission race")
        repository.update_state(account.id, AccountState.RUNNING)
        repository.remember_process(
            "synthetic-owned", account.id, 101, 10.0, str(tmp_path / "synthetic-target.exe")
        )
        coordinator = SimpleNamespace(repository=repository, lease=WaitingLease())
        runtime = CpuLimitRuntime(coordinator, Service())
        await runtime.recover()
        pending = asyncio.create_task(runtime.limit("synthetic-owned", 25, "synthetic-BES.exe"))
        await asyncio.wait_for(entered.wait(), 2)
        if change == "stopping":
            repository.update_state(account.id, AccountState.STOPPING)
        elif change == "closing":
            runtime.begin_close()
        elif change == "disabled":
            runtime.enabled = False
        else:
            runtime._recovered = False
        release.set()
        with pytest.raises(StateConflict) as error:
            await pending
        assert not delivered, "a new BES command must not start after admission becomes invalid"
        assert str(error.value) == "cpu_admission_denied"


@pytest.mark.parametrize("layout", ["original_marker", "hardlink"])
def test_reviewed_hash_cannot_authorize_writable_original_evidence(tmp_path, monkeypatch, layout):
    distribution = tmp_path / "original evidence"
    executable = distribution / "BES" / "BES.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"inert synthetic image; never executed")
    if layout == "original_marker":
        (distribution / "Panel.exe").write_bytes(b"inert original marker; never executed")
    else:
        os.link(executable, tmp_path / "linked deployment.exe")
    digest = SimpleNamespace(hexdigest=lambda: bes.SHIPPED_BES_SHA256)
    monkeypatch.setattr(bes.hashlib, "file_digest", lambda *args: digest)
    before = executable.read_bytes()
    with pytest.raises(BesError):
        bes.verify_executable(ReviewedBesExecutable(executable))
    assert executable.read_bytes() == before
    assert not (executable.parent / "bes.ini").exists()


class RecoveryDriver:
    """Pre-existing journal replay only; this double cannot spawn or limit."""

    def __init__(self, session):
        self.session = session
        self.identities = {
            item.pid: ProcessIdentity(item.pid, item.create_time, item.executable)
            for item in (session.target, session.instance)
        }
        self.operations = []
        self.worker_present = False
        self.target_resumed = True
        self.probed_targets = []

    def identity(self, pid):
        return self.identities.get(pid)

    @contextmanager
    def guard(self, owned):
        yield self.identity(owned.pid) == ProcessIdentity(
            owned.pid, owned.create_time, owned.executable
        )

    def window(self, handle):
        return BesWindow(handle, self.session.instance.pid)

    def threads(self, pid):
        assert pid == self.session.instance.pid
        return self.session.baseline_threads + (((987, 111),) if self.worker_present else ())

    def resumed(self, target):
        self.probed_targets.append(target)
        return self.target_resumed

    def send_stop(self, window, timeout):
        self.operations.append("stop")
        assert window.pid == self.session.instance.pid

    def send_copydata(self, window, payload, timeout):
        assert struct.unpack_from("<I", payload)[0] == bes.BES_COMMAND_EXIT
        assert window.pid == self.session.instance.pid
        self.operations.append("exit")
        self.identities.pop(window.pid)
        return 0


@pytest.fixture
def recovery_context(tmp_path):
    with AccountRepository(tmp_path / "accounts.sqlite") as repository:
        account = repository.add_account("synthetic recovery")
        target = OwnedProcess("target", account.id, 101, 10.0, str(tmp_path / "game.exe"))
        instance = OwnedProcess("bes", account.id, 202, 20.0, str(tmp_path / "BES.exe"))
        session = BesSession(
            "session",
            target,
            instance,
            window_handle=808,
            baseline_threads=((303, 404),),
            state="delivered",
            reduction_percent=30,
            may_have_limited=True,
        )
        with BesJournal(tmp_path / "bes.sqlite") as journal:
            journal.save(session)
            driver = RecoveryDriver(session)
            service = OwnedBesService(
                repository,
                None,
                journal,
                driver=driver,
                command_timeout=0.01,
                poll_interval=0.001,
            )
            yield SimpleNamespace(
                repository=repository,
                journal=journal,
                session=session,
                driver=driver,
                service=service,
            )


async def test_failed_journal_removal_after_exit_is_retryable_without_another_message(
    recovery_context, monkeypatch
):
    ctx = recovery_context
    real_remove = ctx.journal.remove

    def deny_remove(token):
        raise OSError("synthetic private storage diagnostic")

    monkeypatch.setattr(ctx.journal, "remove", deny_remove)
    with pytest.raises(BesError) as error:
        await ctx.service.recover()
    assert "synthetic private" not in str(error.value)
    assert ctx.driver.operations == ["stop", "exit"]
    assert ctx.service.sessions()[0].state == "cleanup_required"
    monkeypatch.setattr(ctx.journal, "remove", real_remove)
    await ctx.service.recover()
    assert not ctx.service.sessions()
    assert ctx.driver.operations == ["stop", "exit"]


async def test_failed_resumed_commit_preserves_owner_and_prevents_exit(
    recovery_context, monkeypatch
):
    ctx = recovery_context
    real_save = ctx.journal.save

    def deny_resumed(session):
        if session.state == "resumed":
            raise OSError("synthetic disk full")
        return real_save(session)

    monkeypatch.setattr(ctx.journal, "save", deny_resumed)
    with pytest.raises(BesError, match="journal_write_failed"):
        await ctx.service.recover()
    assert ctx.driver.operations == ["stop"]
    assert ctx.service.sessions()[0].state == "cleanup_required"
    assert ctx.service.sessions()[0].may_have_limited
    monkeypatch.setattr(ctx.journal, "save", real_save)
    await ctx.service.recover()
    assert ctx.driver.operations == ["stop", "stop", "exit"]
    assert not ctx.service.sessions()


async def test_reused_owner_is_not_addressed_or_forgotten_when_resume_unknown(recovery_context):
    ctx = recovery_context
    old_owner = ctx.session.instance
    ctx.driver.identities[old_owner.pid] = ProcessIdentity(
        old_owner.pid, old_owner.create_time + 1, old_owner.executable
    )
    ctx.driver.target_resumed = None
    with pytest.raises(BesError, match="owner_lost_resume_unconfirmed"):
        await ctx.service.recover()
    assert not ctx.driver.operations
    assert ctx.service.sessions()[0].target == ctx.session.target


async def test_dead_target_still_requires_limiter_workers_to_leave(recovery_context):
    ctx = recovery_context
    ctx.driver.identities.pop(ctx.session.target.pid)
    ctx.driver.worker_present = True
    with pytest.raises(BesError, match="resume_unconfirmed"):
        await ctx.service.recover()
    assert ctx.driver.operations == ["stop"]
    assert ctx.service.sessions()
    # Simulate a reused target PID. Recovery must never probe its thread counts.
    ctx.driver.identities[ctx.session.target.pid] = ProcessIdentity(
        ctx.session.target.pid, 99.0, ctx.session.target.executable
    )
    ctx.driver.worker_present = False
    await ctx.service.recover()
    assert ctx.driver.operations == ["stop", "stop", "exit"]
    assert not ctx.driver.probed_targets
    assert not ctx.service.sessions()


@pytest.mark.skipif(os.name != "nt", reason="Windows ABI and read-only self-process query")
def test_native_bindings_query_only_current_python_process():
    driver = WindowsBesDriver()
    identity = driver.identity(os.getpid())
    assert identity is not None and identity.pid == os.getpid()
    own_python = OwnedProcess(
        "qa-self", "qa-only", identity.pid, identity.create_time, identity.executable
    )
    kernel, user, nt, windows = driver._native()
    assert ctypes.sizeof(kernel.OpenProcess.restype) == ctypes.sizeof(ctypes.c_void_p)
    assert ctypes.sizeof(kernel.OpenThread.restype) == ctypes.sizeof(ctypes.c_void_p)
    assert ctypes.sizeof(user.SendMessageTimeoutW.restype) == ctypes.sizeof(ctypes.c_void_p)
    assert ctypes.sizeof(nt.NtQueryInformationThread.restype) == 4
    assert nt.NtQueryInformationThread.restype(-1).value < 0
    assert ctypes.sizeof(windows.DWORD) == ctypes.sizeof(windows.ULONG) == 4
    with driver.guard(own_python) as valid:
        assert valid
        threads = driver.threads(identity.pid)
        assert threads and all(tid > 0 and created > 0 for tid, created in threads)
        assert driver.resumed(own_python) is True
    with driver.guard(replace(own_python, create_time=identity.create_time - 1)) as valid:
        assert not valid


@pytest.mark.skipif(os.name != "nt", reason="Windows ctypes scalar and FILETIME layout")
@pytest.mark.parametrize(
    "fault", ["owner", "times", "status", "length", "suspended", "snapshot", "open"]
)
def test_thread_probe_closes_handles_and_fails_closed(tmp_path, monkeypatch, fault):
    from ctypes import wintypes

    driver = WindowsBesDriver()
    closed, access_masks, probes = [], [], []
    native_handle = 2**40 + 9
    calls = 0

    def threads():
        nonlocal calls
        calls += 1
        return [SimpleNamespace(id=72 if fault == "snapshot" and calls > 1 else 71)]

    def open_thread(access, inherit, tid):
        access_masks.append(access)
        assert inherit is False and tid == 71
        return 0 if fault == "open" else native_handle

    def get_times(handle, created, exited, kernel, user):
        assert handle == native_handle
        created._obj.dwLowDateTime = 100
        return fault != "times"

    def query(handle, info_class, count, size, length):
        probes.append(info_class)
        assert handle == native_handle and info_class == 35 and size == 4
        count._obj.value = 1 if fault == "suspended" else 0
        length._obj.value = 3 if fault == "length" else 4
        return -1073741822 if fault == "status" else 0

    kernel = SimpleNamespace(
        OpenThread=open_thread,
        GetProcessIdOfThread=lambda handle: 999 if fault == "owner" else 42,
        GetThreadTimes=get_times,
        CloseHandle=lambda handle: closed.append(handle),
    )
    nt = SimpleNamespace(NtQueryInformationThread=query)
    monkeypatch.setattr(driver, "_native", lambda: (kernel, None, nt, wintypes))
    monkeypatch.setattr(bes.psutil, "Process", lambda pid: SimpleNamespace(threads=threads))

    @contextmanager
    def pinned_target(target):
        yield True

    monkeypatch.setattr(driver, "guard", pinned_target)
    target = OwnedProcess("target", "account", 42, 10.0, str(tmp_path / "synthetic.exe"))
    result = driver.resumed(target)
    assert result is (False if fault == "suspended" else None)
    assert access_masks == [0x0840]  # Query rights only; no suspend/resume permission.
    assert closed == ([] if fault == "open" else [native_handle])
    if fault in {"owner", "times", "open"}:
        assert not probes

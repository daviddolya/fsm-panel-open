"""Synthetic protocol/lifecycle tests: never launch BES or suspend a real process."""

import asyncio
import ctypes
import hashlib
import os
import re
import struct
import threading
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from fsm_panel.adapters import bes
from fsm_panel.adapters.bes import (
    BES_COMMAND_EXIT,
    BES_COMMAND_PARSE,
    BES_COPYDATA_SIZE,
    SHIPPED_BES_SHA256,
    BesError,
    BesJournal,
    BesWindow,
    OwnedBesService,
    ProcessIdentity,
    ReviewedBesExecutable,
    WindowsBesDriver,
    copydata_payload,
    launch_argv,
    limit_command,
    verify_executable,
)
from fsm_panel.adapters.processes import OwnedProcess
from fsm_panel.core import AccountRepository, ValidationError


class SyntheticDriver:
    def __init__(self, target):
        self.identities = {
            target.pid: ProcessIdentity(target.pid, target.create_time, target.executable)
        }
        self.window_map = {}
        self.argv = []
        self.operations = []
        self.baselines = {}
        self.active = {}
        self.resume_status = {target.pid: True}
        self.spawn_mode = "normal"
        self.exit_hangs = False
        self.stop_hangs = False
        self.worker_hangs = False
        self.resume_hangs = False
        self.parse_result = 1
        self.parse_error = None
        self.block_parse = None
        self.block_stop = None
        self.before_parse = None

    def identity(self, pid):
        return self.identities.get(pid)

    @contextmanager
    def guard(self, process):
        current = self.identity(process.pid)
        yield current is not None and (
            current.create_time == process.create_time
            and os.path.normcase(current.executable) == os.path.normcase(process.executable)
        )

    def spawn(self, argv):
        self.argv.append(tuple(argv))
        pid = 1000 + len(self.argv)
        identity = ProcessIdentity(pid, 100.0 + pid, os.path.normcase(str(Path(argv[0]).resolve())))
        self.identities[pid] = identity
        self.baselines[pid] = ((pid * 10, 5),)
        if self.spawn_mode == "normal":
            self.window_map[pid * 100] = BesWindow(pid * 100, pid)
        elif self.spawn_mode == "foreign":
            self.window_map[pid * 100] = BesWindow(pid * 100, 666)
        elif self.spawn_mode == "exited":
            del self.identities[pid]
        return identity

    def windows(self, pid):
        # Intentionally includes foreign windows: the service must filter them.
        return list(self.window_map.values())

    def window(self, handle):
        return self.window_map.get(handle)

    def threads(self, pid):
        return self.baselines[pid] + (((pid * 10 + 1, 6),) if pid in self.active else ())

    def resumed(self, target):
        return self.resume_status[target.pid]

    def send_copydata(self, window, payload, timeout):
        assert window == self.window_map.get(window.handle), "foreign HWND must never be addressed"
        command = struct.unpack_from("<I", payload)[0]
        if command == BES_COMMAND_EXIT:
            assert window.pid not in self.active, "must not exit a live limiter worker"
            self.operations.append(("exit", window.pid))
            if not self.exit_hangs:
                del self.identities[window.pid]
                del self.window_map[window.handle]
            return 0  # Actual BES returns FALSE for its EXITNOW handler.
        assert command == BES_COMMAND_PARSE
        text = payload[16:].decode("utf-16-le").rstrip("\x00")
        assert "--unlimit" not in text and "--exitnow" not in text
        pid = int(re.search(r"PID:(\d+)", text)[1])
        if self.before_parse:
            self.before_parse()
        self.operations.append(("parse", window.pid, pid, text))
        self.active[window.pid] = pid
        self.resume_status[pid] = False
        if self.block_parse:
            entered, release = self.block_parse
            entered.set()
            assert release.wait(5)
        if self.parse_error:
            raise self.parse_error
        return self.parse_result

    def send_stop(self, window, timeout):
        assert window == self.window_map.get(window.handle)
        self.operations.append(("stop", window.pid))
        if self.block_stop:
            entered, release = self.block_stop
            entered.set()
            assert release.wait(5)
        if self.stop_hangs:
            raise TimeoutError("synthetic private detail")
        if self.worker_hangs:
            return
        pid = self.active.pop(window.pid, None)
        if pid is not None and not self.resume_hangs:
            self.resume_status[pid] = True


@pytest.fixture
def rig(tmp_path, monkeypatch):
    target_path = str((tmp_path / "synthetic-target.exe").resolve())
    executable = tmp_path / "reviewed-deployment" / "BES.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"synthetic file; no executable code")
    # Lifecycle tests use an explicit fake verifier; production verification is
    # independently tested below without running even the synthetic file.
    monkeypatch.setattr(bes, "verify_executable", lambda reviewed: executable.resolve())
    with AccountRepository(tmp_path / "accounts.sqlite") as repo:
        account = repo.add_account("synthetic_bes")
        target = OwnedProcess("owned-target", account.id, 42, 10.0, os.path.normcase(target_path))
        repo.remember_process(
            target.token, target.account_id, target.pid, target.create_time, target.executable
        )
        with BesJournal(tmp_path / "bes.sqlite") as journal:
            driver = SyntheticDriver(target)
            service = OwnedBesService(
                repo,
                ReviewedBesExecutable(executable),
                journal,
                driver=driver,
                startup_timeout=0.02,
                command_timeout=0.02,
                poll_interval=0.001,
            )
            yield SimpleNamespace(
                repo=repo,
                account=account,
                target=target,
                executable=executable,
                journal=journal,
                driver=driver,
                service=service,
            )


def test_wire_contract_is_fixed_width_unicode_and_launch_is_idle():
    assert launch_argv("C:/reviewed/BES.exe") == (
        "C:/reviewed/BES.exe",
        "--selfish",
        "--allow-multi",
        "--minimize",
    )
    command = limit_command(123, 37)
    assert command == '"BES.exe" "PID:123" 37;100;0'
    payload = copydata_payload(command)
    assert len(payload) == BES_COPYDATA_SIZE == 2064
    assert struct.unpack_from("<IIII", payload) == (0xBECD0001, 0, 0, 0)
    assert payload[16:].decode("utf-16-le").rstrip("\x00") == command
    assert struct.unpack_from("<IIII", copydata_payload(exit_now=True)) == (0xBECD0000, 0, 0, 0)
    assert len(copydata_payload("\U0001f642" * 511)) == 2064
    with pytest.raises(ValidationError, match="too_long"):
        copydata_payload("\U0001f642" * 512)
    with pytest.raises(ValidationError, match="invalid_command"):
        copydata_payload("bad\x00command")
    with pytest.raises(ValidationError, match="invalid_command"):
        copydata_payload("\ud800")


@pytest.mark.parametrize("percent", [0, 100, -1, 1.0, True, "50", None])
def test_only_integer_reduction_percent_1_to_99(percent):
    with pytest.raises(ValidationError, match="reduction_percent"):
        limit_command(42, percent)


@pytest.mark.parametrize("pid", [0, -1, True, 2**32, 2.5, "123"])
def test_invalid_pid_cannot_become_an_image_or_switch(pid):
    with pytest.raises(ValidationError, match="invalid_pid"):
        limit_command(pid, 50)


def test_hash_validation_is_exact_and_never_spawns(tmp_path, monkeypatch):
    executable = tmp_path / "BES.exe"
    executable.write_bytes(b"synthetic")
    with pytest.raises(BesError, match="hash_mismatch"):
        verify_executable(ReviewedBesExecutable(executable))
    with pytest.raises(BesError, match="reviewed_executable_required"):
        verify_executable(
            ReviewedBesExecutable(executable, hashlib.sha256(b"synthetic").hexdigest())
        )
    monkeypatch.setattr(
        hashlib, "file_digest", lambda *_: SimpleNamespace(hexdigest=lambda: SHIPPED_BES_SHA256)
    )
    assert verify_executable(ReviewedBesExecutable(executable)) == executable.resolve()


async def test_limit_update_unlimit_and_stop_are_addressed_and_idempotent(rig):
    # A foreign BES must be untouched even though window enumeration returns it.
    rig.driver.window_map[99] = BesWindow(99, 999)
    session = await rig.service.limit(rig.target, 25)
    assert session.state == "delivered" and session.session_id == session.token
    assert len(rig.repo.owned_processes()) == 1  # BES is not in the force-kill controller.
    updated = await rig.service.limit(rig.target, 67)
    assert updated.token == session.token and updated.reduction_percent == 67
    assert len(rig.driver.argv) == 1
    await rig.service.unlimit(rig.target)
    assert rig.service.sessions()[0].state == "resumed"
    assert session.instance.pid in rig.driver.identities
    await rig.service.unlimit(rig.target)
    await rig.service.stop(rig.account.id)
    await rig.service.stop(rig.account.id)
    assert rig.service.sessions() == []
    assert rig.driver.window_map == {99: BesWindow(99, 999)}
    assert all(operation[1] == session.instance.pid for operation in rig.driver.operations)
    assert [operation[0] for operation in rig.driver.operations] == [
        "parse",
        "parse",
        "stop",
        "stop",
        "stop",
        "exit",
    ]


async def test_unregistered_or_forged_target_never_spawns(rig):
    for target in (
        replace(rig.target, token="foreign"),
        replace(rig.target, create_time=11),
        replace(rig.target, account_id="foreign"),
    ):
        with pytest.raises(BesError, match="target_not_owned"):
            await rig.service.limit(target, 30)
    assert not rig.driver.argv


async def test_reused_target_pid_blocks_update_then_owned_bes_can_stop(rig):
    session = await rig.service.limit(rig.target, 30)
    rig.driver.identities[42] = ProcessIdentity(42, 999.0, rig.target.executable)
    with pytest.raises(BesError, match="target_identity_changed"):
        await rig.service.limit(rig.target, 40)
    await rig.service.stop(rig.account.id)
    assert rig.driver.identities[42].create_time == 999.0
    assert rig.service.sessions() == []
    assert len([op for op in rig.driver.operations if op[0] == "parse"]) == 1
    assert session.instance.pid not in rig.driver.identities


async def test_reused_bes_pid_is_never_signalled_and_unknown_resume_is_retained(rig):
    session = await rig.service.limit(rig.target, 30)
    rig.driver.identities[session.instance.pid] = ProcessIdentity(
        session.instance.pid, 9999.0, session.instance.executable
    )
    before = list(rig.driver.operations)
    with pytest.raises(BesError, match="owner_lost_resume_unconfirmed"):
        await rig.service.stop(rig.account.id)
    assert rig.driver.operations == before
    assert rig.service.sessions()[0].state == "cleanup_required"
    rig.driver.resume_status[42] = True
    await rig.service.recover()
    assert rig.driver.operations == before and rig.service.sessions() == []


@pytest.mark.parametrize("changed", ["pid", "class", "missing"])
async def test_foreign_or_reused_window_handles_are_not_signalled(rig, changed):
    session = await rig.service.limit(rig.target, 30)
    window = rig.driver.window_map[session.window_handle]
    if changed == "missing":
        del rig.driver.window_map[window.handle]
    else:
        rig.driver.window_map[window.handle] = replace(
            window, **({"pid": 1234} if changed == "pid" else {"class_name": "foreign"})
        )
    before = list(rig.driver.operations)
    with pytest.raises(BesError, match="window_ownership_changed"):
        await rig.service.stop(rig.account.id)
    assert rig.driver.operations == before
    assert rig.service.sessions()[0].error_code == "bes_window_ownership_changed"


@pytest.mark.parametrize("mode", ["missing", "foreign", "exited"])
async def test_startup_timeout_or_exit_keeps_idle_recovery_identity(rig, mode):
    rig.driver.spawn_mode = mode
    with pytest.raises(BesError, match="startup"):
        await rig.service.limit(rig.target, 30)
    session = rig.service.sessions()[0]
    assert session.state == "cleanup_required" and not session.may_have_limited
    assert not rig.driver.operations
    if mode != "exited":
        rig.driver.window_map.clear()
        window = BesWindow(12345, session.instance.pid)
        rig.driver.window_map[window.handle] = window
    await rig.service.recover()
    assert not rig.service.sessions()


@pytest.mark.parametrize("failure", ["stop_hangs", "worker_hangs", "resume_hangs"])
async def test_unconfirmed_resume_never_exits_and_can_be_retried(rig, failure):
    await rig.service.limit(rig.target, 30)
    setattr(rig.driver, failure, True)
    with pytest.raises(BesError):
        await rig.service.stop(rig.account.id)
    assert rig.service.sessions()[0].state == "cleanup_required"
    assert not any(op[0] == "exit" for op in rig.driver.operations)
    setattr(rig.driver, failure, False)
    rig.driver.resume_status[42] = True
    await rig.service.recover()
    assert not rig.service.sessions()


async def test_exit_timeout_retains_instance_for_retry_without_force_kill(rig):
    await rig.service.limit(rig.target, 30)
    rig.driver.exit_hangs = True
    with pytest.raises(BesError, match="exit_timeout"):
        await rig.service.stop(rig.account.id)
    assert rig.service.sessions()
    rig.driver.exit_hangs = False
    await rig.service.recover()
    assert not rig.service.sessions()


async def test_missing_resume_probe_refuses_to_start(rig):
    rig.driver.resume_status[42] = None
    with pytest.raises(BesError, match="resume_probe_required"):
        await rig.service.limit(rig.target, 30)
    assert not rig.driver.argv


async def test_reply_or_transport_failure_is_uncertain_not_success(rig):
    rig.driver.parse_result = 0
    with pytest.raises(BesError, match="not_acknowledged"):
        await rig.service.limit(rig.target, 30)
    assert rig.service.sessions()[0].may_have_limited
    with pytest.raises(BesError, match="recovery_required"):
        await rig.service.limit(rig.target, 30)
    await rig.service.recover()
    rig.driver.parse_error = TimeoutError("private upstream data")
    with pytest.raises(BesError) as error:
        await rig.service.limit(rig.target, 30)
    assert str(error.value) == error.value.code == "bes_command_failed_cleanup_required"
    await rig.service.recover()


async def test_limit_is_journalled_before_external_command(rig):
    def observe():
        saved = rig.journal.sessions()[0]
        assert saved.state == "unknown" and saved.may_have_limited
        assert saved.window_handle and saved.baseline_threads

    rig.driver.before_parse = observe
    await rig.service.limit(rig.target, 30)
    await rig.service.stop(rig.account.id)


async def test_persistence_failure_cannot_send_limit_and_in_memory_identity_is_recoverable(
    rig, monkeypatch
):
    original = rig.journal.save
    monkeypatch.setattr(rig.journal, "save", lambda _: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(BesError, match="journal_write_failed"):
        await rig.service.limit(rig.target, 30)
    assert not rig.driver.operations
    assert len(rig.service.sessions()) == 1
    monkeypatch.setattr(rig.journal, "save", original)
    await rig.service.recover()
    assert not rig.service.sessions()


async def test_cancelled_limit_waits_for_command_then_resumes_and_exits(rig):
    entered, release = threading.Event(), threading.Event()
    rig.driver.block_parse = (entered, release)
    task = asyncio.create_task(rig.service.limit(rig.target, 30))
    assert await asyncio.to_thread(entered.wait, 2)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not rig.service.sessions() and rig.driver.resume_status[42]
    assert [op[0] for op in rig.driver.operations] == ["parse", "stop", "exit"]


async def test_cancelled_cleanup_failure_is_actionable_not_hidden_by_cancellation(rig):
    entered, release = threading.Event(), threading.Event()
    rig.driver.block_parse = (entered, release)
    rig.driver.stop_hangs = True
    task = asyncio.create_task(rig.service.limit(rig.target, 30))
    assert await asyncio.to_thread(entered.wait, 2)
    task.cancel()
    release.set()
    with pytest.raises(BesError, match="cleanup_required"):
        await task
    assert rig.service.sessions()[0].state == "cleanup_required"
    rig.driver.stop_hangs = False
    await rig.service.recover()


async def test_cancelled_stop_serializes_following_limit(rig):
    await rig.service.limit(rig.target, 30)
    entered, release = threading.Event(), threading.Event()
    rig.driver.block_stop = (entered, release)
    stop = asyncio.create_task(rig.service.stop(rig.account.id))
    assert await asyncio.to_thread(entered.wait, 2)
    stop.cancel()
    following = asyncio.create_task(rig.service.limit(rig.target, 40))
    await asyncio.sleep(0)
    assert not following.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await stop
    session = await following
    assert session.state == "delivered" and len(rig.driver.argv) == 2
    await rig.service.stop(rig.account.id)


async def test_recovery_after_reopening_journal_needs_no_executable_configuration(rig, tmp_path):
    session = await rig.service.limit(rig.target, 30)
    rig.journal.close()
    with BesJournal(tmp_path / "bes.sqlite") as recovered:
        assert recovered.sessions()[0] == session
        service = OwnedBesService(
            rig.repo, None, recovered, driver=rig.driver, command_timeout=0.02, poll_interval=0.001
        )
        rig.executable.unlink()
        await service.recover()
        assert not service.sessions()


async def test_per_call_reviewed_executable_and_identity_access_error(rig):
    service = OwnedBesService(
        rig.repo, None, rig.journal, driver=rig.driver, command_timeout=0.02, poll_interval=0.001
    )
    with pytest.raises(BesError, match="reviewed_executable_required"):
        await service.limit(rig.target, 30)
    session = await service.limit(rig.target, 30, executable=ReviewedBesExecutable(rig.executable))
    original = rig.driver.identity
    rig.driver.identity = lambda _: (_ for _ in ()).throw(psutil.AccessDenied())
    with pytest.raises(BesError, match="cleanup_required"):
        await service.stop(rig.account.id)
    assert service.sessions()[0].token == session.token
    rig.driver.identity = original
    await service.recover()


def test_two_journal_owners_cannot_race_and_corruption_fails_closed(tmp_path):
    path = tmp_path / "bes.sqlite"
    with BesJournal(path) as journal:
        with pytest.raises(BesError, match="busy"):
            BesJournal(path)
        journal._db.execute("INSERT INTO bes_sessions VALUES ('bad','{}')")
        journal._db.commit()
        with pytest.raises(BesError, match="journal_invalid"):
            journal.sessions()


@pytest.mark.parametrize("which", ["target", "instance"])
async def test_changed_executable_is_never_addressed(rig, which):
    session = await rig.service.limit(rig.target, 30)
    identity = rig.target if which == "target" else session.instance
    rig.driver.identities[identity.pid] = ProcessIdentity(
        identity.pid, identity.create_time, str(Path(identity.executable).with_name("foreign.exe"))
    )
    before = list(rig.driver.operations)
    with pytest.raises(BesError, match="identity_changed"):
        await rig.service.limit(rig.target, 40)
    assert rig.driver.operations == before


async def test_reused_thread_identity_cannot_count_as_idle_baseline(rig):
    session = await rig.service.limit(rig.target, 30)
    rig.driver.baselines[session.instance.pid] = ((session.instance.pid * 10, 999),)
    with pytest.raises(BesError, match="resume_unconfirmed"):
        await rig.service.stop(rig.account.id)
    assert not any(op[0] == "exit" for op in rig.driver.operations)
    rig.driver.baselines[session.instance.pid] = session.baseline_threads
    await rig.service.recover()


async def test_stop_only_its_account_and_recovery_attempts_other_accounts_after_failure(rig):
    account = rig.repo.add_account("synthetic_other")
    target = replace(rig.target, token="other-target", account_id=account.id, pid=43)
    rig.repo.remember_process(
        target.token, target.account_id, target.pid, target.create_time, target.executable
    )
    rig.driver.identities[target.pid] = ProcessIdentity(
        target.pid, target.create_time, target.executable
    )
    rig.driver.resume_status[target.pid] = True
    first = await rig.service.limit(rig.target, 30)
    second = await rig.service.limit(target, 35)
    await rig.service.stop(rig.account.id)
    assert second.instance.pid in rig.driver.active
    assert rig.service.sessions() == [second]
    first = await rig.service.limit(rig.target, 30)
    rig.driver.window_map[second.window_handle] = BesWindow(second.window_handle, 999)
    with pytest.raises(BesError, match="ownership_changed"):
        await rig.service.recover()
    assert first.instance.pid not in rig.driver.identities
    assert [s.token for s in rig.service.sessions()] == [second.token]


def test_win32_message_boundary_preserves_payload_and_zero_exit_reply(monkeypatch):
    """Calls only a fake ctypes API; validates native packing without a real HWND."""
    driver = WindowsBesDriver()
    window = BesWindow(999, 100)
    calls = []

    class NativeCopyData(ctypes.Structure):
        _fields_ = [
            ("identifier", ctypes.c_size_t),
            ("length", ctypes.c_uint32),
            ("data", ctypes.c_void_p),
        ]

    def fake_send(hwnd, message, wparam, lparam, flags, timeout, result):
        calls.append((hwnd, message, wparam, flags, timeout))
        if message == 0x4A:
            packet = ctypes.cast(lparam, ctypes.POINTER(NativeCopyData)).contents
            assert packet.identifier == 0xBA1EC0DE and packet.length == 2064
            received = ctypes.string_at(packet.data, packet.length)
            assert received == copydata_payload(exit_now=True)
        else:
            assert message == 0x111 and wparam == 30004 and lparam == 0
        result._obj.value = 0
        return 1

    user = SimpleNamespace(SendMessageTimeoutW=fake_send)
    monkeypatch.setattr(driver, "_native", lambda: (None, user, None, None))
    monkeypatch.setattr(driver, "window", lambda _: window)
    assert driver.send_copydata(window, copydata_payload(exit_now=True), 1.5) == 0
    driver.send_stop(window, 0.1)
    assert calls == [(999, 0x4A, 0, 0x23, 1500), (999, 0x111, 30004, 0x23, 100)]
    monkeypatch.setattr(driver, "window", lambda _: replace(window, pid=101))
    with pytest.raises(BesError, match="ownership_changed"):
        driver.send_stop(window, 0.1)
    assert len(calls) == 2
    monkeypatch.setattr(driver, "window", lambda _: window)
    user.SendMessageTimeoutW = lambda *_: 0
    with pytest.raises(BesError, match="message_timeout_or_failed"):
        driver.send_stop(window, 0.1)

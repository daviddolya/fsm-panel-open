import concurrent.futures

import pytest

from fsm_panel.core import Account, AccountRepository, AccountState, StateConflict, ValidationError


def test_snapshot_and_journal_survive_reopen_atomically(tmp_path):
    path = tmp_path / "state.sqlite"
    with AccountRepository(path) as repo:
        account = repo.add_account("synthetic_account")
        event = repo.update_state(account.id, AccountState.RUNNING, expected_revision=0)
        assert event.sequence == 2
        with pytest.raises(StateConflict):
            repo.update_state(account.id, AccountState.FAILED, expected_revision=0)
    with AccountRepository(path) as repo:
        assert repo.get(account.id).state == AccountState.RUNNING
        assert repo.get(account.id).revision == 1
        assert [item.state for item in repo.events(account_id=account.id)] == [
            AccountState.IDLE,
            AccountState.RUNNING,
        ]
        assert len(repo.events(after=1)) == 1


def test_duplicate_casefold_and_error_messages_hide_login():
    with AccountRepository() as repo:
        repo.add_account("synthetic_ä")
        with pytest.raises(ValidationError, match="duplicate_account") as error:
            repo.add_account("SYNTHETIC_Ä")
        assert "synthetic" not in str(error.value)
        assert len(repo.events()) == 1


def test_cross_thread_reads_and_writes_preserve_revisions():
    with AccountRepository() as repo:
        account = repo.add_account("synthetic_thread")

        def writer():
            for _ in range(100):
                repo.update_state(account.id, AccountState.RUNNING)

        def reader():
            for _ in range(100):
                assert len(repo.list_accounts()) == 1
                events = repo.events()
                assert len({event.sequence for event in events}) == len(events)

        with concurrent.futures.ThreadPoolExecutor() as executor:
            futures = [executor.submit(writer), executor.submit(reader), executor.submit(reader)]
            for future in futures:
                future.result()
        assert repo.get(account.id).revision == 100
        assert len(repo.events()) == 101


@pytest.mark.parametrize("login", ["", " leading", "trailing ", "a\x00b", "a\nb", "x" * 129])
def test_invalid_accounts_are_not_written(login):
    with AccountRepository() as repo:
        with pytest.raises(ValidationError):
            repo.add_account(login)
        assert repo.list_accounts() == []


def test_missing_account_and_invalid_codes_are_safe():
    with AccountRepository() as repo:
        with pytest.raises(KeyError, match="account_not_found"):
            repo.get("synthetic_missing")
        account = repo.add_account("synthetic")
        with pytest.raises(ValidationError):
            repo.update_state(account.id, AccountState.FAILED, code="Exception with password")
        with pytest.raises(ValidationError):
            repo.events(limit=0)
        assert repo.get(account.id).revision == 0


def test_failed_public_mutations_do_not_add_partial_state():
    with AccountRepository() as repo:
        with pytest.raises(ValidationError, match="new_account_must_be_idle"):
            repo.add_accounts([Account("synthetic", "synthetic", state=AccountState.RUNNING)])
        assert not repo.list_accounts()
        with pytest.raises(KeyError):
            repo.update_state("synthetic_missing", AccountState.RUNNING)
        account = repo.add_account("synthetic")
        with pytest.raises(ValidationError):
            repo.record_retry(account.id, True)
        assert len(repo.events()) == 1

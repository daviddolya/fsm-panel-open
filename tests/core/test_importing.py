import pytest

from fsm_panel.adapters.fakes import MemorySecretStore
from fsm_panel.core import AccountRepository, ValidationError, import_accounts


def test_import_keeps_password_out_of_sqlite_events_and_repr(tmp_path):
    path = tmp_path / "accounts.sqlite"
    secrets = MemorySecretStore()
    with AccountRepository(path) as repo:
        result = import_accounts("synthetic_1:synthetic_password:with_colon", repo, secrets)
        assert result.count == 1
        account = result.accounts[0]
        assert secrets.get(account.secret_ref) == b"synthetic_password:with_colon"
        assert "synthetic_password" not in repr(result)
        assert "synthetic_password" not in repr(repo.events())
    assert b"synthetic_password" not in path.read_bytes()


def test_csv_quotes_and_display_names():
    with AccountRepository() as repo:
        result = import_accounts(
            'login,password,display_name\nsynthetic,"test,pass",Example\n',
            repo,
            MemorySecretStore(),
            format="csv",
        )
        assert result.accounts[0].display_name == "Example"


@pytest.mark.parametrize(
    "text,format",
    [
        ("synthetic:pass\nbad_line", "lines"),
        ("synthetic:pass\nSYNTHETIC:other", "lines"),
        ("synthetic:", "lines"),
        ("", "lines"),
        ("synthetic:pass", "unknown"),
        ("login,unknown\nsynthetic,test", "csv"),
        ("login,password\nsynthetic,pass,extra", "csv"),
        ("login,password\nsynthetic", "csv"),
        ("login,password\nsynthetic,", "csv"),
    ],
)
def test_invalid_import_is_all_or_nothing_and_never_echoes_credentials(text, format):
    secrets = MemorySecretStore()
    with AccountRepository() as repo:
        with pytest.raises(ValidationError) as error:
            import_accounts(text, repo, secrets, format=format)
        assert "pass" not in str(error.value).replace("password", "")
        assert repo.list_accounts() == []
        assert "entries=0" in repr(secrets)


def test_secret_failure_rolls_back_written_entries_and_account_transaction():
    class FailingStore(MemorySecretStore):
        def put(self, reference, value):
            super().put(reference, value)
            if value == b"synthetic_second":
                raise RuntimeError("secret synthetic_second")

    secrets = FailingStore()
    with AccountRepository() as repo:
        with pytest.raises(ValidationError, match="account_import_failed"):
            import_accounts(
                "synthetic_1:synthetic_first\nsynthetic_2:synthetic_second", repo, secrets
            )
        assert repo.list_accounts() == []
        assert "entries=0" in repr(secrets)


def test_existing_duplicate_does_not_overwrite_secret():
    secrets = MemorySecretStore()
    with AccountRepository() as repo:
        result = import_accounts("synthetic:old_synthetic", repo, secrets)
        with pytest.raises(ValidationError, match="duplicate_account"):
            import_accounts("SYNTHETIC:new_synthetic", repo, secrets)
        assert secrets.get(result.accounts[0].secret_ref) == b"old_synthetic"


def test_import_ignores_blank_lines_but_rejects_excessive_payload():
    with AccountRepository() as repo:
        result = import_accounts("\nsynthetic:synthetic_password\n\n", repo, MemorySecretStore())
        assert result.count == 1
        with pytest.raises(ValidationError, match="account_import_too_large"):
            import_accounts(" " * (2 * 1024 * 1024 + 1), repo, MemorySecretStore())

"""Explicit reconstruction import formats; never parse or log live account fixtures."""

import csv
import io
import uuid
from dataclasses import dataclass, field

from .contracts import SecretStore
from .models import Account, ValidationError
from .repository import AccountRepository, _validate_account


@dataclass(frozen=True, slots=True)
class ImportResult:
    accounts: tuple[Account, ...]
    count: int


@dataclass(frozen=True, slots=True)
class _Credential:
    login: str = field(repr=False)
    password: str = field(repr=False)
    display_name: str = field(default="", repr=False)


def _parse(text: str, format: str) -> list[_Credential]:
    if len(text.encode("utf-8")) > 2 * 1024 * 1024:
        raise ValidationError("account_import_too_large")
    credentials: list[_Credential] = []
    if format == "lines":
        for line_number, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            login, separator, password = line.partition(":")
            if not separator or not login or not password:
                raise ValidationError(f"invalid_account_line:{line_number}")
            credentials.append(_Credential(login, password))
    elif format == "csv":
        try:
            reader = csv.DictReader(io.StringIO(text), strict=True)
            if (
                reader.fieldnames is None
                or set(reader.fieldnames)
                not in (
                    {"login", "password"},
                    {"login", "password", "display_name"},
                )
                or len(set(reader.fieldnames)) != len(reader.fieldnames)
            ):
                raise ValidationError("invalid_account_csv_headers")
            for line_number, row in enumerate(reader, 2):
                if None in row or any(value is None for value in row.values()):
                    raise ValidationError(f"invalid_account_line:{line_number}")
                credentials.append(
                    _Credential(row["login"], row["password"], row.get("display_name", ""))
                )
        except csv.Error:
            raise ValidationError("invalid_account_csv") from None
    else:
        raise ValidationError("unknown_account_import_format")
    if not credentials or len(credentials) > 10000:
        raise ValidationError("invalid_account_import_count")
    seen: set[str] = set()
    for index, credential in enumerate(credentials, 1):
        _validate_account(Account(str(index), credential.login, credential.display_name))
        if (
            not credential.password
            or len(credential.password) > 4096
            or "\x00" in credential.password
        ):
            raise ValidationError(f"invalid_account_password:{index}")
        if credential.login.casefold() in seen:
            raise ValidationError(f"duplicate_account_line:{index}")
        seen.add(credential.login.casefold())
    return credentials


def import_accounts(
    text: str,
    repository: AccountRepository,
    secrets: SecretStore,
    *,
    format: str = "lines",
) -> ImportResult:
    """Validate the complete import, protect passwords, then commit metadata atomically.

    Formats are `login:password` (password may contain colons) and CSV with exact
    login,password headers and optional display_name. Caller owns clearing UI input.
    Python strings cannot be reliably erased; this API does not promise memory wiping.
    """
    credentials = _parse(text, format)
    existing = {account.login.casefold() for account in repository.list_accounts()}
    if any(credential.login.casefold() in existing for credential in credentials):
        raise ValidationError("duplicate_account")
    accounts: list[Account] = []
    protected: list[str] = []
    try:
        for credential in credentials:
            account_id = str(uuid.uuid4())
            reference = f"account-{account_id}"
            # Register before put so rollback also handles a store that writes then raises.
            protected.append(reference)
            secrets.put(reference, credential.password.encode("utf-8"))
            accounts.append(
                Account(account_id, credential.login, credential.display_name, secret_ref=reference)
            )
        repository.add_accounts(accounts)
    except Exception:
        cleanup_failed = False
        for reference in protected:
            try:
                secrets.delete(reference)
            except Exception:
                cleanup_failed = True
        code = "account_import_cleanup_required" if cleanup_failed else "account_import_failed"
        raise ValidationError(code) from None
    return ImportResult(tuple(accounts), len(accounts))

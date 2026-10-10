"""
Account management for Cove Launcher.

Supports three account types:
  * ``msa``     -- real Microsoft accounts. Only the *refresh token* is
                   persisted (inside the encrypted vault); short-lived access
                   tokens are obtained on demand and never written to disk.
  * ``ely``     -- Ely.by accounts via their OAuth2 device-code flow. The
                   refresh token is persisted in the vault; the OAuth access
                   token doubles as the Minecraft session token (see
                   :mod:`launcher.elyby`).
  * ``offline`` -- developer/offline mode with a deterministic UUID.
"""

from __future__ import annotations

import uuid as uuidlib
from dataclasses import dataclass, field, asdict

from minecraft_launcher_lib import microsoft_account as mll_ms

from . import elyby
from .constants import APP_NAME, APP_VERSION, ELY_SCOPES
from .security import Vault


class AuthError(Exception):
    """Raised when Microsoft authentication/refresh fails."""


def offline_uuid(username: str) -> str:
    """
    Deterministic offline-mode UUID, mirroring vanilla's derivation
    (MD5 of ``OfflinePlayer:<name>``).
    """
    digest_name = f"OfflinePlayer:{username}".encode("utf-8")
    return str(uuidlib.uuid3(uuidlib.NAMESPACE_OID, digest_name.hex()))


@dataclass
class Account:
    uuid: str
    name: str
    type: str = "offline"            # "msa" | "ely" | "offline"
    refresh_token: str | None = None # msa/ely only; lives ONLY in the vault
    added: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(raw: dict) -> "Account":
        return Account(
            uuid=raw.get("uuid", ""),
            name=raw.get("name", ""),
            type=raw.get("type", "offline"),
            refresh_token=raw.get("refresh_token"),
            added=raw.get("added", ""),
        )


_VAULT_KEY_ACCOUNTS = "accounts"
_VAULT_KEY_CURRENT = "current_account_uuid"


class AccountStore:
    """CRUD + token lifecycle for launcher accounts, backed by the vault."""

    def __init__(self, vault: Vault) -> None:
        self._vault = vault
        self._access_cache: dict[str, str] = {}  # uuid -> access_token (RAM only)

    # -- internal ------------------------------------------------------------
    def _read_accounts(self) -> list[Account]:
        raw_list = self._vault.get(_VAULT_KEY_ACCOUNTS, []) or []
        return [Account.from_dict(raw) for raw in raw_list]

    def _write_accounts(self, accounts: list[Account], current: str | None = None) -> None:
        # NOTE: ``current=None`` means "leave the stored selection unchanged".
        # Deleting the key here used to reset the active account every time a
        # refresh token was rotated at launch.
        data = self._vault.read()
        data[_VAULT_KEY_ACCOUNTS] = [acc.to_dict() for acc in accounts]
        if current is not None:
            data[_VAULT_KEY_CURRENT] = current
        self._vault.write(data)

    # -- CRUD ------------------------------------------------------------------
    def list(self) -> list[Account]:
        return self._read_accounts()

    def get(self, account_uuid: str) -> Account | None:
        for acc in self._read_accounts():
            if acc.uuid == account_uuid:
                return acc
        return None

    def current(self) -> Account | None:
        current_uuid = self._vault.get(_VAULT_KEY_CURRENT)
        accounts = self._read_accounts()
        if not accounts:
            return None
        for acc in accounts:
            if acc.uuid == current_uuid:
                return acc
        return accounts[0]

    def set_current(self, account_uuid: str) -> None:
        if self.get(account_uuid) is None:
            raise KeyError(account_uuid)
        data = self._vault.read()
        data[_VAULT_KEY_CURRENT] = account_uuid
        self._vault.write(data)

    def add_offline(self, name: str) -> Account:
        name = name.strip() or "Player"
        account = Account(uuid=offline_uuid(name), name=name, type="offline")
        accounts = [a for a in self._read_accounts() if a.uuid != account.uuid]
        accounts.append(account)
        self._write_accounts(accounts, current=account.uuid)
        return account

    def add_microsoft(self, profile: dict) -> Account:
        """
        Persist a completed Microsoft login.

        ``profile`` is the CompleteLoginResponse dict from
        ``microsoft_account.complete_login/complete_refresh``.
        """
        account = Account(
            uuid=str(profile.get("id", "")),
            name=str(profile.get("name", "")),
            type="msa",
            refresh_token=str(profile.get("refresh_token", "")),
        )
        if not account.uuid or not account.refresh_token:
            raise AuthError("Microsoft profile incomplete")
        accounts = [a for a in self._read_accounts() if a.uuid != account.uuid]
        accounts.append(account)
        self._write_accounts(accounts, current=account.uuid)
        return account

    def add_elyby(self, profile: dict) -> Account:
        """
        Persist a completed Ely.by login.

        ``profile`` is the dict emitted by
        :class:`launcher.workers_net.ElyLoginThread`:
        ``{uuid, username, access_token, refresh_token}``. Only the refresh
        token is stored (encrypted via the vault); the access token kept in
        the RAM cache and refreshed on demand.
        """
        account = Account(
            uuid=str(profile.get("uuid", "")),
            name=str(profile.get("username", "")),
            type="ely",
            refresh_token=str(profile.get("refresh_token", "")),
        )
        if not account.uuid or not account.name or not account.refresh_token:
            raise AuthError("Ely.by profile incomplete")
        accounts = [a for a in self._read_accounts() if a.uuid != account.uuid]
        accounts.append(account)
        self._write_accounts(accounts, current=account.uuid)
        if profile.get("access_token"):
            self._access_cache[account.uuid] = str(profile["access_token"])
        return account

    def remove(self, account_uuid: str) -> None:
        accounts = [a for a in self._read_accounts() if a.uuid != account_uuid]
        data = self._vault.read()
        was_current = data.get(_VAULT_KEY_CURRENT) == account_uuid
        data[_VAULT_KEY_ACCOUNTS] = [acc.to_dict() for acc in accounts]
        if was_current:
            data[_VAULT_KEY_CURRENT] = (
                accounts[0].uuid if accounts else None
            )
        self._vault.write(data)
        self._access_cache.pop(account_uuid, None)

    # -- tokens ------------------------------------------------------------------
    def ensure_access_token(self, account: Account) -> tuple[str, str, str]:
        """
        Return ``(name, uuid, access_token)`` for the account.

        For MSA and Ely.by accounts a fresh access token is obtained when no
        cached value exists (tokens live ~24 h; refreshing at launch is what
        Prism-style launchers do). Cached tokens live in RAM only and are
        never written to disk or logs.
        """
        if account.type == "ely":
            return self._ensure_ely_access_token(account)
        if account.type != "msa":
            return account.name, account.uuid, ""

        cached = self._access_cache.get(account.uuid)
        if cached:
            return account.name, account.uuid, cached

        if not account.refresh_token:
            raise AuthError("Account has no stored refresh token")

        try:
            profile = mll_ms.complete_refresh(
                client_id=self._azure_client_id(),
                client_secret=None,
                redirect_uri=None,
                refresh_token=account.refresh_token,
            )
        except Exception as exc:
            raise AuthError(f"Microsoft login refresh failed: {exc}") from exc

        # Rotate stored refresh token (Microsoft may issue a new one).
        account.refresh_token = str(profile.get("refresh_token", account.refresh_token))
        accounts = [
            account if a.uuid == account.uuid else a
            for a in self._read_accounts()
        ]
        self._write_accounts(accounts)

        token = str(profile["access_token"])
        self._access_cache[account.uuid] = token
        return str(profile.get("name", account.name)), account.uuid, token

    def _ensure_ely_access_token(self, account: Account) -> tuple[str, str, str]:
        """Refresh the Ely.by OAuth access token when the RAM cache is empty."""
        cached = self._access_cache.get(account.uuid)
        if cached:
            return account.name, account.uuid, cached

        if not account.refresh_token:
            raise AuthError("Ely.by account has no stored refresh token")

        try:
            tokens = elyby.refresh_access_token(
                client_id=self._elyby_client_id(),
                refresh_token=account.refresh_token,
                scope=ELY_SCOPES,
            )
        except elyby.ElyByError as exc:
            raise AuthError(f"Ely.by login refresh failed: {exc}") from exc

        token = str(tokens.get("access_token", ""))
        if not token:
            raise AuthError("Ely.by login refresh returned no access token")

        # Ely.by issues a fresh refresh token on every refresh; rotate it.
        account.refresh_token = str(
            tokens.get("refresh_token", account.refresh_token)
        )
        accounts = [
            account if a.uuid == account.uuid else a
            for a in self._read_accounts()
        ]
        self._write_accounts(accounts)

        self._access_cache[account.uuid] = token
        return account.name, account.uuid, token

    # -- settings passthrough ------------------------------------------------------
    @staticmethod
    def _azure_client_id() -> str:
        from .constants import DEFAULT_AZURE_CLIENT_ID
        return DEFAULT_AZURE_CLIENT_ID

    def _elyby_client_id(self) -> str:
        """Ely.by OAuth client id; a vault override beats the built-in one."""
        from .constants import ELY_CLIENT_ID

        override = str(self._vault.get("ely_client_id", "") or "").strip()
        return override or ELY_CLIENT_ID

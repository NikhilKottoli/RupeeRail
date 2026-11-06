"""Safe key loading. Secrets are read as text inside Python only, never logged.

Precedence for each role:
  1. its own env var (QUOTE_SIGNER_PRIVATE_KEY / PAYMASTER_PRIVATE_KEY / SETTLER_PRIVATE_KEY);
  2. SEED_PHRASE (env, else the repo-root .env read as text) at HD path m/44'/60'/0'/0/0.

Every error message names the variable but never includes its value, and exceptions are
raised `from None` so a library error that echoes the input cannot leak through a traceback.
Only public addresses ever leave this module in printable form.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from eth_account import Account
from eth_account.signers.local import LocalAccount

HD_PATH = "m/44'/60'/0'/0/0"
_PK_RE = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")
_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


class KeyLoadError(RuntimeError):
    """Raised with a message that never contains secret material."""


def read_env_file(path: str | Path, name: str) -> str | None:
    """Returns one variable from a dotenv file, parsed as text (never sourced or executed)."""
    p = Path(path)
    if not p.is_file():
        return None
    for line in p.read_text(encoding="utf-8").splitlines():
        m = _LINE_RE.match(line)
        if not m or m.group(1) != name:
            continue
        value = m.group(2)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        return value.strip() or None
    return None


def _lookup(name: str, env_file: str | Path | None) -> str | None:
    v = os.environ.get(name)
    if v:
        return v.strip()
    return read_env_file(env_file, name) if env_file else None


def account_from_private_key(value: str, var_name: str) -> LocalAccount:
    if not _PK_RE.match(value):
        raise KeyLoadError(f"{var_name} is not a 32-byte hex private key (value not shown)")
    try:
        return Account.from_key(value if value.startswith("0x") else "0x" + value)
    except Exception:
        raise KeyLoadError(f"{var_name} could not be parsed as a private key (value not shown)") from None


def account_from_mnemonic(phrase: str, var_name: str = "SEED_PHRASE", path: str = HD_PATH) -> LocalAccount:
    words = phrase.split()
    if len(words) not in (12, 15, 18, 21, 24) or not all(w.isalpha() and w.islower() for w in words):
        raise KeyLoadError(f"{var_name} is not a 12-24 word lowercase BIP-39 phrase (value not shown)")
    Account.enable_unaudited_hdwallet_features()
    try:
        return Account.from_mnemonic(" ".join(words), account_path=path)
    except Exception:
        raise KeyLoadError(f"{var_name} failed BIP-39 validation (value not shown)") from None


def load_role(override_var: str, env_file: str | Path | None) -> LocalAccount:
    pk = _lookup(override_var, env_file)
    if pk:
        return account_from_private_key(pk, override_var)
    phrase = _lookup("SEED_PHRASE", env_file)
    if not phrase:
        raise KeyLoadError(f"set {override_var} or SEED_PHRASE (env or repo-root .env)")
    return account_from_mnemonic(phrase)


def load_user_key(env_file: str | Path | None, var_name: str = "OTHER_WALLET_PRIVATE_KEY") -> LocalAccount:
    pk = _lookup(var_name, env_file)
    if not pk:
        raise KeyLoadError(f"{var_name} is not set (env or repo-root .env)")
    return account_from_private_key(pk, var_name)


@dataclass
class Keys:
    quote_signer: LocalAccount
    paymaster: LocalAccount
    settler: LocalAccount

    def addresses(self) -> dict[str, str]:
        return {
            "quoteSigner": self.quote_signer.address,
            "paymaster": self.paymaster.address,
            "settler": self.settler.address,
        }

    def __repr__(self) -> str:  # never show key material
        return f"Keys({self.addresses()})"


def load_keys(env_file: str | Path | None) -> Keys:
    return Keys(
        quote_signer=load_role("QUOTE_SIGNER_PRIVATE_KEY", env_file),
        paymaster=load_role("PAYMASTER_PRIVATE_KEY", env_file),
        settler=load_role("SETTLER_PRIVATE_KEY", env_file),
    )

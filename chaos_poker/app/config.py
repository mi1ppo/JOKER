"""Settings from the environment / .env (section 15.1). Secrets are never logged or repr'ed."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _int(value: str | None, default: int) -> int:
    try:
        return int(value) if value not in (None, "") else default
    except ValueError:
        return default


@dataclass(frozen=True)
class GigaChatConfig:
    auth_key: str = field(default="", repr=False)
    scope: str = "GIGACHAT_API_PERS"
    oauth_url: str = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
    api_base: str = "https://api.giga.chat/v1"  # as in the spec; check against the current GigaChat docs
    model: str = "GigaChat-2"
    ca_bundle: str = "certs/russian_trusted_root_ca.pem"
    verify_ssl: bool = True
    token_budget: int = 200_000

    @property
    def enabled(self) -> bool:
        return bool(self.auth_key)

    @property
    def verify(self) -> str | bool:
        """Value for httpx `verify=`: the Russian Ministry CA bundle (only for GigaChat requests)."""
        if not self.verify_ssl:
            return False
        path = Path(self.ca_bundle)
        if not path.is_absolute():
            path = PROJECT_DIR / path
        return str(path) if path.exists() else True

    @property
    def ca_bundle_missing(self) -> bool:
        path = Path(self.ca_bundle)
        if not path.is_absolute():
            path = PROJECT_DIR / path
        return self.verify_ssl and not path.exists()

    @classmethod
    def from_env(cls) -> "GigaChatConfig":
        env = os.environ
        defaults = cls()
        return cls(
            auth_key=(env.get("GIGACHAT_AUTH_KEY") or "").strip(),
            scope=env.get("GIGACHAT_SCOPE") or defaults.scope,
            oauth_url=env.get("GIGACHAT_OAUTH_URL") or defaults.oauth_url,
            api_base=(env.get("GIGACHAT_API_BASE") or defaults.api_base).rstrip("/"),
            model=env.get("GIGACHAT_MODEL") or defaults.model,
            ca_bundle=env.get("GIGACHAT_CA_BUNDLE") or defaults.ca_bundle,
            verify_ssl=_bool(env.get("GIGACHAT_VERIFY_SSL"), True),
            token_budget=_int(env.get("GIGACHAT_TOKEN_BUDGET"), defaults.token_budget),
        )

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List

import yaml


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class OutputConfig:
    root: Path


@dataclass(frozen=True)
class TimeConfig:
    timezone: str
    workday_start: str  # HH:MM


@dataclass(frozen=True)
class ImapConfig:
    host: str
    port: int
    ssl: bool
    folders: list[str]


@dataclass(frozen=True)
class IdentityConfig:
    primary_address: str
    aliases: list[str]


@dataclass(frozen=True)
class SecretsConfig:
    provider: str
    reference: str


@dataclass(frozen=True)
class AccountConfig:
    id: str
    imap: ImapConfig
    identity: IdentityConfig
    secrets: SecretsConfig


@dataclass(frozen=True)
class SuppressRules:
    senders: List[str] = field(default_factory=list)
    subjects: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class ArrivalOnlyRules:
    senders: List[str] = field(default_factory=list)
    subjects: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class RulesConfig:
    high_priority_senders: List[str] = field(default_factory=list)
    collapse_automated: bool = True
    suppress: SuppressRules = field(default_factory=SuppressRules)
    arrival_only: ArrivalOnlyRules = field(default_factory=ArrivalOnlyRules)


@dataclass(frozen=True)
class TicketsConfig:
    enabled: bool
    plugins: list[str]


@dataclass(frozen=True)
class UnrepliedRuleConfig:
    id: str
    target_addresses: list[str] = field(default_factory=list)
    unreplied_after_minutes: int = 60
    lookback_days: int = 14
    notify_cooldown_minutes: int = 60


@dataclass(frozen=True)
class UnrepliedWatchConfig:
    enabled: bool = False
    rules: list[UnrepliedRuleConfig] = field(default_factory=list)


@dataclass(frozen=True)
class WatchConfig:
    ingest_lookback_days: int = 7
    unreplied: UnrepliedWatchConfig = field(default_factory=UnrepliedWatchConfig)


@dataclass(frozen=True)
class AppConfig:
    rootdir: Path
    time: TimeConfig
    accounts: list[AccountConfig]
    rules: RulesConfig
    tickets: TicketsConfig
    watch: WatchConfig

    def state_db_path(self) -> Path:
        return self.rootdir / ".mailtriage" / "state.db"


def _require(d: dict[str, Any], k: str) -> Any:
    if k not in d:
        raise ConfigError(f"Missing required key: {k}")
    return d[k]


def _reject_unknown(d: dict[str, Any], allowed: set[str], context: str) -> None:
    unknown = set(d.keys()) - allowed
    if unknown:
        keys = ", ".join(sorted(unknown))
        raise ConfigError(f"Unknown key(s) in {context}: {keys}")


def load_config(path: Path) -> AppConfig:
    if not path.exists():
        raise ConfigError(f"Config not found: {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ConfigError("Config must be a YAML mapping at top-level")

    _reject_unknown(
        raw, {"output", "time", "accounts", "rules", "tickets", "watch"}, "root config"
    )

    output_raw = raw.get("output")
    _reject_unknown(output_raw, {"root"}, "output")
    if not output_raw or "root" not in output_raw:
        raise ConfigError("output.root is required and must be an absolute path")

    root = Path(output_raw["root"])
    if not root.is_absolute():
        raise ConfigError("output.root must be an absolute path")

    # rootdir = Path(_require(raw, "rootdir")).expanduser()
    time_raw = _require(raw, "time")
    accounts_raw = _require(raw, "accounts")
    rules_raw = _require(raw, "rules")
    tickets_raw = raw.get("tickets", {"enabled": False, "plugins": []})
    watch_raw = raw.get("watch", {}) or {}
    if not isinstance(watch_raw, dict):
        raise ConfigError("watch must be a mapping")
    _reject_unknown(watch_raw, {"unreplied", "ingest_lookback_days"}, "watch")

    unreplied_raw = watch_raw.get("unreplied", {}) or {}
    if not isinstance(unreplied_raw, dict):
        raise ConfigError("watch.unreplied must be a mapping")
    _reject_unknown(
        unreplied_raw,
        {"enabled", "rules"},
        "watch.unreplied",
    )

    rules_raw = unreplied_raw.get("rules") or []
    if not isinstance(rules_raw, list):
        raise ConfigError("watch.unreplied.rules must be a list")

    rules: list[UnrepliedRuleConfig] = []
    for rr in rules_raw:
        if not isinstance(rr, dict):
            raise ConfigError("each watch.unreplied.rules entry must be a mapping")
        _reject_unknown(
            rr,
            {
                "id",
                "target_addresses",
                "unreplied_after_minutes",
                "lookback_days",
                "notify_cooldown_minutes",
            },
            "watch.unreplied.rules[]",
        )
        rid = str(_require(rr, "id")).strip()
        if not rid:
            raise ConfigError("watch.unreplied.rules[].id must be non-empty")

        target_addresses = rr.get("target_addresses") or []
        if not isinstance(target_addresses, list) or not all(
            isinstance(x, str) for x in target_addresses
        ):
            raise ConfigError(
                "watch.unreplied.rules[].target_addresses must be a list of strings"
            )

        rules.append(
            UnrepliedRuleConfig(
                id=rid,
                target_addresses=[str(x) for x in target_addresses],
                unreplied_after_minutes=int(rr.get("unreplied_after_minutes", 60) or 60),
                lookback_days=int(rr.get("lookback_days", 14) or 14),
                notify_cooldown_minutes=int(
                    rr.get("notify_cooldown_minutes", 60) or 60
                ),
            )
        )

    unreplied_watch = UnrepliedWatchConfig(
        enabled=bool(unreplied_raw.get("enabled", False)),
        rules=rules,
    )
    ingest_lookback_days = int(watch_raw.get("ingest_lookback_days", 7) or 7)
    watch_cfg = WatchConfig(ingest_lookback_days=ingest_lookback_days, unreplied=unreplied_watch)

    if not isinstance(time_raw, dict):
        raise ConfigError("time must be a mapping")
    _reject_unknown(time_raw, {"timezone", "workday_start"}, "time")
    time_cfg = TimeConfig(
        timezone=str(_require(time_raw, "timezone")),
        workday_start=str(_require(time_raw, "workday_start")),
    )

    if not isinstance(accounts_raw, list) or not accounts_raw:
        raise ConfigError("accounts must be a non-empty list")

    accounts: list[AccountConfig] = []
    for a in accounts_raw:
        if not isinstance(a, dict):
            raise ConfigError("each account must be a mapping")
        _reject_unknown(a, {"id", "imap", "identity", "secrets"}, "account")

        imap_raw = _require(a, "imap")
        identity_raw = _require(a, "identity")
        secrets_raw = _require(a, "secrets")

        if not isinstance(imap_raw, dict):
            raise ConfigError("account.imap must be a mapping")
        _reject_unknown(imap_raw, {"host", "port", "ssl", "folders"}, "account.imap")
        folders = imap_raw.get("folders") or ["INBOX"]
        if not isinstance(folders, list) or not all(
            isinstance(x, str) for x in folders
        ):
            raise ConfigError("account.imap.folders must be a list of strings")
        imap_cfg = ImapConfig(
            host=str(_require(imap_raw, "host")),
            port=int(_require(imap_raw, "port")),
            ssl=bool(_require(imap_raw, "ssl")),
            folders=[str(x) for x in folders],
        )

        if not isinstance(identity_raw, dict):
            raise ConfigError("account.identity must be a mapping")
        _reject_unknown(
            identity_raw, {"primary_address", "aliases"}, "account.identity"
        )
        aliases = identity_raw.get("aliases") or []
        if not isinstance(aliases, list) or not all(
            isinstance(x, str) for x in aliases
        ):
            raise ConfigError("account.identity.aliases must be a list of strings")
        identity_cfg = IdentityConfig(
            primary_address=str(_require(identity_raw, "primary_address")),
            aliases=[str(x) for x in aliases],
        )

        if not isinstance(secrets_raw, dict):
            raise ConfigError("account.secrets must be a mapping")
        _reject_unknown(secrets_raw, {"provider", "reference"}, "account.secrets")
        secrets_cfg = SecretsConfig(
            provider=str(_require(secrets_raw, "provider")),
            reference=str(_require(secrets_raw, "reference")),
        )

        accounts.append(
            AccountConfig(
                id=str(_require(a, "id")),
                imap=imap_cfg,
                identity=identity_cfg,
                secrets=secrets_cfg,
            )
        )

    if not isinstance(rules_raw, dict):
        raise ConfigError("rules must be a mapping")
    _reject_unknown(
        rules_raw,
        {"high_priority_senders", "collapse_automated", "suppress", "arrival_only"},
        "rules",
    )

    suppress_raw = rules_raw.get("suppress", {}) or {}
    arrival_raw = rules_raw.get("arrival_only", {}) or {}
    _reject_unknown(suppress_raw, {"senders", "subjects"}, "rules.suppress")
    _reject_unknown(arrival_raw, {"senders", "subjects"}, "rules.arrival_only")

    hp = rules_raw.get("high_priority_senders") or []
    if not isinstance(hp, list) or not all(isinstance(x, str) for x in hp):
        raise ConfigError("rules.high_priority_senders must be a list of strings")
    rules_cfg = RulesConfig(
        high_priority_senders=[str(x) for x in hp],
        collapse_automated=bool(rules_raw.get("collapse_automated", True)),
        suppress=SuppressRules(
            senders=[str(x) for x in suppress_raw.get("senders", [])],
            subjects=[str(x) for x in suppress_raw.get("subjects", [])],
        ),
        arrival_only=ArrivalOnlyRules(
            senders=[str(x) for x in arrival_raw.get("senders", [])],
            subjects=[str(x) for x in arrival_raw.get("subjects", [])],
        ),
    )

    if not isinstance(tickets_raw, dict):
        raise ConfigError("tickets must be a mapping")
    _reject_unknown(tickets_raw, {"enabled", "plugins"}, "tickets")
    plugins = tickets_raw.get("plugins") or []
    if not isinstance(plugins, list) or not all(isinstance(x, str) for x in plugins):
        raise ConfigError("tickets.plugins must be a list of strings")
    tickets_cfg = TicketsConfig(
        enabled=bool(tickets_raw.get("enabled", False)),
        plugins=[str(x) for x in plugins],
    )

    return AppConfig(
        rootdir=root,
        time=time_cfg,
        accounts=accounts,
        rules=rules_cfg,
        tickets=tickets_cfg,
        watch=watch_cfg,
    )

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ── Section dataclasses ───────────────────────────────────────────────

@dataclass
class Tier1Config:
    min_spread: float = 0.03
    max_spread: float = 0.40
    max_confidence_threshold: float = 0.95
    min_confidence_threshold: float = 0.05
    min_dollar_volume: int = 100
    min_update_rate: float = 1.0
    min_expiry_seconds: int = 1800
    max_imbalance_deviation: float = 0.35
    min_trades_for_imbalance: int = 20
    consecutive_passes_required: int = 5
    consecutive_fails_allowed: int = 3


@dataclass
class CooldownConfig:
    first_demotion_seconds: int = 900
    second_demotion_seconds: int = 7200
    max_demotions_before_blacklist: int = 3


@dataclass
class EvalConfig:
    interval_seconds: int = 60


@dataclass
class MetadataConfig:
    refresh_interval_seconds: int = 300


@dataclass
class WebSocketConfig:
    trade_buffer_size: int = 5000


@dataclass
class PathsConfig:
    ws_out_dir: str = "data/kalshi/ws"
    transition_log_dir: str = "data/kalshi/transitions"
    metadata_out_dir: str = "data/kalshi/metadata"


@dataclass
class Config:
    tier1: Tier1Config = field(default_factory=Tier1Config)
    cooldowns: CooldownConfig = field(default_factory=CooldownConfig)
    evaluation: EvalConfig = field(default_factory=EvalConfig)
    metadata: MetadataConfig = field(default_factory=MetadataConfig)
    websocket: WebSocketConfig = field(default_factory=WebSocketConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)


# ── Type checking ─────────────────────────────────────────────────────

def _check_type(path: str, value: Any, default: Any) -> Any:
    """Validate and coerce type of a config value against its default."""
    if isinstance(default, float):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        raise TypeError(f"Config error: {path} must be numeric, got {type(value).__name__}: {value!r}")

    if isinstance(default, int):
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, float) and value == int(value):
            return int(value)
        raise TypeError(f"Config error: {path} must be int, got {type(value).__name__}: {value!r}")

    if isinstance(default, str):
        if isinstance(value, str):
            return value
        raise TypeError(f"Config error: {path} must be str, got {type(value).__name__}: {value!r}")

    return value


# ── Section map ───────────────────────────────────────────────────────

_SECTION_CLASSES = {
    "tier1": Tier1Config,
    "cooldowns": CooldownConfig,
    "evaluation": EvalConfig,
    "metadata": MetadataConfig,
    "websocket": WebSocketConfig,
    "paths": PathsConfig,
}


def _load_section(section_name: str, cls: type, raw: dict) -> Any:
    """Load one config section, filling missing keys with defaults."""
    defaults = cls()
    kwargs = {}
    for f in fields(cls):
        if f.name in raw:
            kwargs[f.name] = _check_type(f"{section_name}.{f.name}", raw[f.name], getattr(defaults, f.name))
        else:
            kwargs[f.name] = getattr(defaults, f.name)
            logger.warning("Config: missing key %s.%s, using default: %r", section_name, f.name, kwargs[f.name])
    return cls(**kwargs)


# ── Public API ────────────────────────────────────────────────────────

def load_config(path: str | Path = "config.json") -> Config:
    """Load configuration from a JSON file.

    Missing keys → use defaults with a warning.
    Wrong types → raise ``TypeError`` immediately.
    Missing file → use all defaults and write the file for reference.
    """
    p = Path(path)
    if not p.exists():
        logger.info("Config file %s not found, using all defaults.", p)
        cfg = Config()
        write_default_config(p)
        return cfg

    with p.open() as f:
        raw = json.load(f)

    sections = {}
    for name, cls in _SECTION_CLASSES.items():
        section_raw = raw.get(name, {})
        if name not in raw:
            logger.warning("Config: missing section '%s', using all defaults for it.", name)
        sections[name] = _load_section(name, cls, section_raw)

    return Config(**sections)


def write_default_config(path: str | Path = "config.json") -> None:
    """Write a default config file for reference."""
    cfg = Config()
    d = {}
    for name in _SECTION_CLASSES:
        section = getattr(cfg, name)
        d[name] = {f.name: getattr(section, f.name) for f in fields(section)}
    p = Path(path)
    with p.open("w", encoding="utf-8") as f:
        json.dump(d, f, indent=4)
        f.write("\n")
    logger.info("Wrote default config to %s", p)


def config_summary(cfg: Config) -> str:
    """Return a human-readable summary of a Config for startup printing."""
    lines = []
    for name in _SECTION_CLASSES:
        section = getattr(cfg, name)
        for f in fields(section):
            lines.append(f"  {name}.{f.name} = {getattr(section, f.name)!r}")
    return "\n".join(lines)

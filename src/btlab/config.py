"""YAML configuration loading with readable (French) validation errors."""

from __future__ import annotations

from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel, ValidationError

M = TypeVar("M", bound=BaseModel)


class ConfigError(ValueError):
    """Invalid or unreadable configuration. The message is meant for the user."""


def _format_loc(loc: tuple[Any, ...]) -> str:
    return ".".join(str(part) for part in loc) or "(racine)"


def format_validation_error(err: ValidationError, source: str) -> str:
    lines = [f"Configuration invalide : {source}"]
    for item in err.errors():
        msg = item["msg"]
        if msg.startswith("Value error, "):
            msg = msg[len("Value error, ") :]
        lines.append(f"  - champ « {_format_loc(item['loc'])} » : {msg}")
    return "\n".join(lines)


def parse_model(model: type[M], data: Any, source: str) -> M:
    try:
        return model.model_validate(data)
    except ValidationError as err:
        raise ConfigError(format_validation_error(err, source)) from None


def read_yaml(path: Path) -> Any:
    if not path.is_file():
        raise ConfigError(f"Fichier de configuration introuvable : {path}")
    try:
        with path.open(encoding="utf-8") as fh:
            return yaml.safe_load(fh)
    except yaml.YAMLError as err:
        raise ConfigError(f"YAML illisible dans {path} : {err}") from None


def load_model(model: type[M], path: Path) -> M:
    return parse_model(model, read_yaml(path), str(path))


def dump_yaml(data: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, allow_unicode=True, sort_keys=False)
    tmp.replace(path)

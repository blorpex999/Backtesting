"""Project locations.

Every path derives from the project root, which is found by walking up from the
current directory (or from this package) to the folder holding ``pyproject.toml``
and ``configs/periods.yaml``. ``BTLAB_ROOT`` overrides it (used by the tests),
``BTLAB_DATA_DIR`` moves the (large, gitignored) data folder elsewhere.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT_ENV = "BTLAB_ROOT"
DATA_ENV = "BTLAB_DATA_DIR"


def _is_project_root(path: Path) -> bool:
    return (path / "pyproject.toml").is_file() and (path / "configs" / "periods.yaml").is_file()


def project_root() -> Path:
    env = os.environ.get(ROOT_ENV)
    if env:
        return Path(env).resolve()
    cwd = Path.cwd().resolve()
    here = Path(__file__).resolve()
    for candidate in [cwd, *cwd.parents, *here.parents]:
        if _is_project_root(candidate):
            return candidate
    raise RuntimeError(
        "Racine du projet introuvable : lancez la commande depuis le dossier du projet "
        f"ou définissez la variable d'environnement {ROOT_ENV}."
    )


@dataclass(frozen=True)
class Paths:
    root: Path
    data: Path

    @classmethod
    def from_root(cls, root: Path | None = None) -> Paths:
        root = (root or project_root()).resolve()
        data_env = os.environ.get(DATA_ENV)
        data = Path(data_env).resolve() if data_env else root / "data"
        return cls(root=root, data=data)

    # --- versioned configuration -------------------------------------------------
    @property
    def configs(self) -> Path:
        return self.root / "configs"

    @property
    def instruments_dir(self) -> Path:
        return self.configs / "instruments"

    @property
    def periods_file(self) -> Path:
        return self.configs / "periods.yaml"

    @property
    def data_settings_file(self) -> Path:
        return self.configs / "data.yaml"

    @property
    def registry_db(self) -> Path:
        return self.root / "registry" / "runs.db"

    # --- gitignored working data -------------------------------------------------
    @property
    def raw(self) -> Path:
        return self.data / "raw"

    @property
    def parquet(self) -> Path:
        return self.data / "parquet"

    @property
    def quality(self) -> Path:
        return self.data / "quality"

    @property
    def logs(self) -> Path:
        return self.data / "logs"

    @property
    def calendar(self) -> Path:
        return self.data / "calendar"

    @property
    def reports(self) -> Path:
        return self.root / "reports"

    @property
    def qc_reports(self) -> Path:
        return self.reports / "qc"

    @property
    def tools(self) -> Path:
        return self.root / ".tools"


def get_paths() -> Paths:
    return Paths.from_root()

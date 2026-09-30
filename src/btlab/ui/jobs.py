"""Background jobs launched from the UI (``python -m btlab ...``), with a log file each."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class Job:
    label: str
    args: list[str]
    log_path: Path
    proc: subprocess.Popen
    started_at: datetime = field(default_factory=datetime.now)

    @property
    def returncode(self) -> int | None:
        return self.proc.poll()

    @property
    def running(self) -> bool:
        return self.returncode is None

    @property
    def status(self) -> str:
        code = self.returncode
        if code is None:
            return "en cours"
        return "terminé" if code == 0 else f"échec (code {code})"

    def tail(self, lines: int = 40) -> str:
        if not self.log_path.exists():
            return ""
        text = self.log_path.read_text(encoding="utf-8", errors="replace")
        return "\n".join(text.splitlines()[-lines:])


class JobManager:
    def __init__(self, root: Path, log_dir: Path):
        self.root = root
        self.log_dir = log_dir
        self._jobs: list[Job] = []

    def start(self, label: str, args: list[str]) -> Job:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        log_path = self.log_dir / f"{stamp}-{args[0]}-{args[1] if len(args) > 1 else ''}.log"
        env = {
            **os.environ,
            "BTLAB_ROOT": str(self.root),
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
        }
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        with log_path.open("w", encoding="utf-8") as log:
            log.write(f"$ btlab {' '.join(args)}\n")
            log.flush()
            proc = subprocess.Popen(
                [sys.executable, "-m", "btlab", *args],
                cwd=self.root,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=flags,
            )
        job = Job(label, args, log_path, proc)
        self._jobs.insert(0, job)
        return job

    def jobs(self) -> list[Job]:
        return list(self._jobs)

    def any_running(self) -> bool:
        return any(j.running for j in self._jobs)

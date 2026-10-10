"""Run store: one directory per run holding every stage's artifacts."""
from __future__ import annotations

import json
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable, Iterator

_RUN_ID = re.compile(r"^\d{4}-W\d{2}$")


def current_run_id(today: date | None = None) -> str:
    """ISO year-week id, e.g. `2026-W41`."""
    year, week, _ = (today or date.today()).isocalendar()
    return f"{year}-W{week:02d}"


def previous_run_id(run_id: str) -> str:
    """The ISO week before `run_id`, e.g. `2026-W41` -> `2026-W40`."""
    year, week = run_id.split("-W")
    monday = date.fromisocalendar(int(year), int(week), 1)
    return current_run_id(monday - timedelta(weeks=1))


class RunStore:
    def __init__(self, runs_dir: Path | str, run_id: str | None = None) -> None:
        run_id = run_id or current_run_id()
        if not _RUN_ID.match(run_id):
            raise ValueError(f"Invalid run id {run_id!r}; expected YYYY-Www, e.g. 2026-W41")
        self.run_id = run_id
        self.dir = Path(runs_dir) / run_id
        self.dir.mkdir(parents=True, exist_ok=True)

    def path(self, name: str) -> Path:
        return self.dir / name

    def exists(self, name: str) -> bool:
        return self.path(name).is_file()

    def write_json(self, name: str, data: Any) -> Path:
        path = self.path(name)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        return path

    def read_json(self, name: str) -> Any:
        return json.loads(self.path(name).read_text())

    def write_jsonl(self, name: str, rows: Iterable[Any]) -> Path:
        path = self.path(name)
        with path.open("w") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return path

    def read_jsonl(self, name: str) -> Iterator[Any]:
        with self.path(name).open() as fh:
            for line in fh:
                if line.strip():
                    yield json.loads(line)

    def write_text(self, name: str, text: str) -> Path:
        path = self.path(name)
        path.write_text(text)
        return path

    def read_text(self, name: str) -> str:
        return self.path(name).read_text()

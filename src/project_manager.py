from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
import json


def projects_dir(root_dir: str | Path) -> Path:
    p = Path(root_dir) / "workspace" / "projects"
    p.mkdir(parents=True, exist_ok=True)
    return p


def safe_project_name(name: str) -> str:
    text = str(name).strip() or "proyecto"
    for ch in '<>:"/\\|?*':
        text = text.replace(ch, "_")
    return text[:120]


def list_projects(root_dir: str | Path) -> list[Path]:
    return sorted(projects_dir(root_dir).glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)


def save_project(root_dir: str | Path, name: str, config: dict[str, Any]) -> Path:
    p = projects_dir(root_dir) / f"{safe_project_name(name)}.json"
    payload = dict(config)
    payload["project_name"] = name
    payload["saved_at"] = datetime.now().isoformat(timespec="seconds")
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return p


def load_project(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def delete_project(path: str | Path) -> None:
    Path(path).unlink(missing_ok=True)

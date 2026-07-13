from __future__ import annotations

from datetime import datetime
from typing import Any
import uuid


def create_job(config: dict[str, Any]) -> dict[str, Any]:
    job = dict(config)
    job.setdefault("id", str(uuid.uuid4())[:8])
    job.setdefault("created_at", datetime.now().isoformat(timespec="seconds"))
    job.setdefault("status", "Pendiente")
    job.setdefault("output_file", None)
    job.setdefault("error", None)
    return job


def job_label(job: dict[str, Any]) -> str:
    return f"{job.get('id')} · {job.get('consulta')} · {', '.join(job.get('selected_scenarios') or [])} · {', '.join(job.get('selected_cases') or [])}"


def jobs_to_records(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records = []
    for j in jobs:
        records.append({
            "ID": j.get("id"),
            "Estado": j.get("status"),
            "Consulta": j.get("consulta"),
            "Escenarios": ", ".join(j.get("selected_scenarios") or []),
            "Casos": ", ".join(j.get("selected_cases") or []),
            "Progreso": j.get("progress", 0),
            "Paso actual": j.get("current_step"),
            "Creado": j.get("created_at"),
            "Inicio": j.get("started_at"),
            "Fin": j.get("finished_at"),
            "Archivo": j.get("output_file"),
            "Cache": j.get("cache_hit"),
            "Detención solicitada": bool(j.get("cancel_requested")),
            "Error": j.get("error"),
        })
    return records

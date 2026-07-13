from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any
import json
import traceback

import pandas as pd


def _safe_text(value: Any) -> str:
    text = str(value) if value is not None else ""
    for ch in '<>:"/\\|?*':
        text = text.replace(ch, '_')
    return text[:120] or "sin_nombre"


def summarize_dataframe(df: pd.DataFrame | None) -> dict[str, Any]:
    if df is None:
        return {"rows": 0, "columns": 0, "column_names": []}
    return {
        "rows": int(len(df)),
        "columns": int(len(df.columns)),
        "column_names": [str(c) for c in df.columns],
    }


def build_log_payload(
    *,
    app_version: str,
    job: dict[str, Any],
    steps: list[str],
    status: str,
    output_file: str | None = None,
    export_fmt: str | None = None,
    preview_df: pd.DataFrame | None = None,
    error: Exception | None = None,
    timings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "app_version": app_version,
        "status": status,
        "consulta": job.get("consulta"),
        "modo": job.get("modo"),
        "escenarios": job.get("selected_scenarios"),
        "casos": job.get("selected_cases"),
        "parametros": job.get("params", {}),
        "plexos": {
            "api_path": job.get("api_path"),
            "sample": job.get("sample"),
            "phase": job.get("phase"),
            "period_yearly": job.get("period_yearly"),
            "period_hourly": job.get("period_hourly"),
            "series_type": job.get("series_type"),
        },
        "exportacion": {
            "output_file": output_file,
            "export_fmt": export_fmt,
            "export_pref": job.get("export_pref"),
            "row_threshold": job.get("row_threshold"),
        },
        "resultado": summarize_dataframe(preview_df),
        "steps": steps,
        "timings": timings or {},
    }
    if error is not None:
        payload["error"] = {
            "type": type(error).__name__,
            "message": str(error),
            "traceback": traceback.format_exc(),
        }
    return payload


def write_execution_log(output_dir: str | Path, payload: dict[str, Any]) -> tuple[Path, Path]:
    base_out = Path(output_dir)
    if base_out.name.lower() == "outputs" and base_out.parent.name.lower() == "workspace":
        out = base_out.parent / "logs"
    else:
        out = base_out / "logs"
    out.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    consulta = _safe_text(payload.get("consulta", "consulta"))
    base = out / f"log_{timestamp}_{consulta}"
    json_path = base.with_suffix(".json")
    txt_path = base.with_suffix(".txt")
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    lines = [
        f"App PLEXOS - log de ejecución",
        f"Fecha: {payload.get('timestamp')}",
        f"Versión: {payload.get('app_version')}",
        f"Estado: {payload.get('status')}",
        f"Consulta: {payload.get('consulta')}",
        f"Modo: {payload.get('modo')}",
        f"Escenarios: {payload.get('escenarios')}",
        f"Casos: {payload.get('casos')}",
        f"Archivo salida: {payload.get('exportacion', {}).get('output_file')}",
        f"Formato: {payload.get('exportacion', {}).get('export_fmt')}",
        "",
        "Perfil de tiempo (segundos):",
        json.dumps(payload.get("timings", {}), ensure_ascii=False, indent=2),
        "",
        "Columnas resultado:",
        ", ".join(payload.get("resultado", {}).get("column_names", [])),
        "",
        "Pasos:",
    ]
    lines += [f"- {s}" for s in payload.get("steps", [])]
    if payload.get("error"):
        lines += ["", "Error:", json.dumps(payload["error"], ensure_ascii=False, indent=2)]
    txt_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, txt_path

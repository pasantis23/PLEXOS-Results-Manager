
from __future__ import annotations

from pathlib import Path
from typing import Any
from datetime import datetime
import json
import zipfile

import pandas as pd


def catalog_dir(root_dir: str | Path) -> Path:
    p = Path(root_dir) / "workspace" / "catalog"
    p.mkdir(parents=True, exist_ok=True)
    return p


def catalog_path(root_dir: str | Path) -> Path:
    return catalog_dir(root_dir) / "results_index.csv"


def _safe_len(value: Any) -> int | None:
    try:
        return int(len(value))
    except Exception:
        return None


def _file_meta(path: str | Path | None) -> dict[str, Any]:
    if not path:
        return {"output_exists": False}
    p = Path(path)
    payload = {"output_file": str(p), "output_name": p.name, "output_suffix": p.suffix.lower(), "output_exists": p.exists()}
    if p.exists():
        try:
            payload["output_size_mb"] = round(p.stat().st_size / (1024 * 1024), 2)
            payload["output_mtime"] = datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds")
            if p.suffix.lower() == ".zip":
                with zipfile.ZipFile(p) as zf:
                    names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
                payload["zip_csv_count"] = len(names)
                payload["zip_csv_preview"] = ", ".join(names[:5])
        except Exception:
            pass
    return payload


def record_result(root_dir: str | Path, job: dict[str, Any], result: dict[str, Any]) -> Path:
    path = catalog_path(root_dir)
    output_file = result.get("output_file")
    params = job.get("params") or {}
    selected_files = job.get("selected_files") or []
    row = {
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "consulta": job.get("consulta"),
        "modo": job.get("modo"),
        "escenarios": ", ".join(job.get("selected_scenarios") or []),
        "casos": ", ".join(job.get("selected_cases") or []),
        "years": ", ".join(str(x) for x in (params.get("years") or [])),
        "samples": ", ".join(str(x) for x in (params.get("samples") or [])),
        "modo_generacion": params.get("modo_generacion"),
        "export_fmt": result.get("export_fmt"),
        "partition_mode": job.get("partition_mode"),
        "solution_files": _safe_len(selected_files),
        "cache_hit": bool(result.get("cache_hit", False)),
        "cache_key": result.get("cache_key"),
        "timings_json": json.dumps(result.get("timings") or {}, ensure_ascii=False),
    }
    row.update(_file_meta(output_file))
    df_new = pd.DataFrame([row])
    if path.exists():
        try:
            df = pd.read_csv(path)
            df = pd.concat([df, df_new], ignore_index=True)
        except Exception:
            df = df_new
    else:
        df = df_new
    # Evita crecimiento infinito del catálogo en sesiones largas.
    df = df.tail(5000)
    df.to_csv(path, index=False, encoding="utf-8-sig")
    return path


def load_catalog(root_dir: str | Path) -> pd.DataFrame:
    path = catalog_path(root_dir)
    if not path.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(path)
        if "saved_at" in df.columns:
            df = df.sort_values("saved_at", ascending=False)
        return df
    except Exception:
        return pd.DataFrame()


def summarize_catalog(df: pd.DataFrame) -> dict[str, Any]:
    if df is None or df.empty:
        return {"total": 0}
    return {
        "total": len(df),
        "consultas": int(df.get("consulta", pd.Series(dtype=object)).nunique()),
        "archivos_existentes": int(df.get("output_exists", pd.Series(dtype=bool)).astype(str).str.lower().isin(["true", "1"]).sum()),
        "ultimo": str(df.iloc[0].get("saved_at", "")),
    }

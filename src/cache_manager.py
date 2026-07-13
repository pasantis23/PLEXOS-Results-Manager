from __future__ import annotations

from pathlib import Path
from typing import Any
from datetime import datetime, date
import hashlib
import json

import pandas as pd

VOLATILE_KEYS = {
    "id", "created_at", "started_at", "finished_at", "status", "output_file", "error",
    "log_json", "log_txt", "progress", "current_step", "steps", "timings", "_future", "cache_hit",
}


def cache_dir(root_dir: str | Path) -> Path:
    p = Path(root_dir) / "workspace" / "cache"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _solution_file_payload(item: Any) -> dict[str, Any]:
    path = Path(getattr(item, "path", ""))
    payload = {
        "scenario": getattr(item, "scenario", None),
        "case": getattr(item, "case", None),
        "path": str(path),
        "file_name": getattr(item, "file_name", path.name),
    }
    try:
        if path.exists():
            st = path.stat()
            payload["size"] = int(st.st_size)
            payload["mtime"] = int(st.st_mtime)
    except Exception:
        pass
    return payload


def _normalize(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k): _normalize(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0])) if str(k) not in VOLATILE_KEYS}
    if isinstance(value, (list, tuple, set)):
        return [_normalize(v) for v in value]
    if all(hasattr(value, attr) for attr in ("scenario", "case", "path")):
        return _solution_file_payload(value)
    return str(value)


def canonical_job(job: dict[str, Any]) -> dict[str, Any]:
    payload = {k: v for k, v in job.items() if k not in VOLATILE_KEYS}
    return _normalize(payload)


def job_cache_key(job: dict[str, Any]) -> str:
    payload = canonical_job(job)
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _pkl(path: Path) -> Path:
    return path.with_suffix(".pkl")



def _cache_safe_scalar(value: Any) -> Any:
    """Convierte objetos no serializables por pickle, por ejemplo System.DateTime de .NET."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    if isinstance(value, (str, int, float, bool, datetime, date, pd.Timestamp)):
        return value

    # Objetos .NET expuestos por pythonnet, como System.DateTime, suelen tener ToString().
    # Para cache interno basta una representación estable. No se modifican las salidas Power BI.
    if hasattr(value, "ToString"):
        try:
            return value.ToString()
        except Exception:
            return str(value)

    return str(value)


def _make_cache_safe_df(df: pd.DataFrame) -> pd.DataFrame:
    """Devuelve una copia serializable para cache interno.

    No modifica el DataFrame original ni las salidas CSV/XLSX. Evita errores como:
    TypeError: cannot pickle 'DateTime' object.
    """
    out = df.copy()
    for col in out.columns:
        if pd.api.types.is_object_dtype(out[col]):
            out[col] = out[col].map(_cache_safe_scalar)
            col_lower = str(col).lower()
            if col_lower in {"fecha", "date", "datetime", "_date"} or "fecha" in col_lower or "date" in col_lower:
                converted = pd.to_datetime(out[col], errors="coerce")
                # Solo reemplaza si la conversión funcionó razonablemente.
                if converted.notna().sum() >= max(1, int(out[col].notna().sum() * 0.5)):
                    out[col] = converted
    return out


def load_result(root_dir: str | Path, cache_key: str) -> dict[str, Any] | None:
    base = cache_dir(root_dir) / cache_key
    meta_path = base / "metadata.json"
    if not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        output_file = Path(meta.get("output_file", ""))
        if not output_file.exists():
            return None
        preview_path = base / "preview_df.pkl"
        resumen_path = base / "resumen.pkl"
        extra_dir = base / "extra_tables"
        preview_df = pd.read_pickle(preview_path) if preview_path.exists() else pd.DataFrame()
        resumen = pd.read_pickle(resumen_path) if resumen_path.exists() else None
        extra_tables: dict[str, pd.DataFrame] = {}
        if extra_dir.exists():
            for p in extra_dir.glob("*.pkl"):
                try:
                    extra_tables[p.stem] = pd.read_pickle(p)
                except Exception:
                    pass
        return {
            "preview_df": preview_df,
            "resumen": resumen,
            "extra_tables": extra_tables,
            "output_file": output_file,
            "export_fmt": meta.get("export_fmt", ""),
            "cache_hit": True,
            "cache_key": cache_key,
            "timings": meta.get("timings", {}),
        }
    except Exception:
        return None


def save_result(root_dir: str | Path, cache_key: str, result: dict[str, Any], job: dict[str, Any] | None = None) -> Path:
    base = cache_dir(root_dir) / cache_key
    base.mkdir(parents=True, exist_ok=True)
    preview_df = result.get("preview_df")
    if isinstance(preview_df, pd.DataFrame):
        _make_cache_safe_df(preview_df).to_pickle(base / "preview_df.pkl")
    resumen = result.get("resumen")
    if isinstance(resumen, pd.DataFrame):
        _make_cache_safe_df(resumen).to_pickle(base / "resumen.pkl")
    extra = result.get("extra_tables") or {}
    if extra:
        extra_dir = base / "extra_tables"
        extra_dir.mkdir(exist_ok=True)
        for name, df in extra.items():
            if isinstance(df, pd.DataFrame):
                safe = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in str(name))[:80] or "tabla"
                _make_cache_safe_df(df).to_pickle(extra_dir / f"{safe}.pkl")
    meta = {
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "cache_key": cache_key,
        "output_file": str(result.get("output_file")),
        "export_fmt": result.get("export_fmt"),
        "timings": result.get("timings", {}),
        "job": canonical_job(job or {}),
    }
    (base / "metadata.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return base


def cache_index(root_dir: str | Path) -> pd.DataFrame:
    rows = []
    for meta_path in cache_dir(root_dir).glob("*/metadata.json"):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            job = meta.get("job", {})
            rows.append({
                "cache_key": meta.get("cache_key"),
                "saved_at": meta.get("saved_at"),
                "consulta": job.get("consulta"),
                "escenarios": ", ".join(job.get("selected_scenarios") or []),
                "casos": ", ".join(job.get("selected_cases") or []),
                "output_file": meta.get("output_file"),
                "export_fmt": meta.get("export_fmt"),
            })
        except Exception:
            pass
    return pd.DataFrame(rows).sort_values("saved_at", ascending=False) if rows else pd.DataFrame()

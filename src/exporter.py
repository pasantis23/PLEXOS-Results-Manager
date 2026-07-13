from __future__ import annotations

from pathlib import Path
from io import BytesIO
from zipfile import ZipFile, ZIP_DEFLATED
import re
import pandas as pd

INVALID_SHEET_CHARS = r"[\[\]\*\?/\\:]"
EXCEL_MAX_ROWS = 1_048_576


def safe_sheet_name(name: str, fallback: str = "Hoja") -> str:
    name = re.sub(INVALID_SHEET_CHARS, "_", str(name)).strip()
    return (name or fallback)[:31]


def safe_filename(name: str, fallback: str = "archivo") -> str:
    name = re.sub(r"[^A-Za-z0-9_\-.áéíóúÁÉÍÓÚñÑ ]+", "_", str(name)).strip()
    name = name.replace(" ", "_")
    return name or fallback


def export_by_scenario(
    dfs_by_scenario: dict[str, pd.DataFrame],
    output_path: str | Path,
    resumen: pd.DataFrame | None = None,
    extra_sheets: dict[str, pd.DataFrame] | None = None,
) -> Path:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        if resumen is not None:
            resumen.to_excel(writer, sheet_name="RESUMEN", index=False)
        if extra_sheets:
            for sheet, df in extra_sheets.items():
                df.to_excel(writer, sheet_name=safe_sheet_name(sheet), index=False)
        for scenario, df in dfs_by_scenario.items():
            df.to_excel(writer, sheet_name=safe_sheet_name(scenario), index=False)
    return output_path


def export_single_df(df: pd.DataFrame, output_path: str | Path, sheet_name: str = "RESUMEN") -> Path:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name=safe_sheet_name(sheet_name), index=False)
    return output_path


def choose_export_format(df: pd.DataFrame, requested: str = "Automático", row_threshold: int = 500_000) -> str:
    """Retorna 'xlsx' o 'csv_zip'. CSV se entrega como ZIP para poder incluir resumen/hojas extra."""
    requested_norm = str(requested).strip().lower()
    n_rows = len(df) if df is not None else 0
    if requested_norm == "xlsx":
        # Excel no permite más de 1.048.576 filas por hoja. Dejamos margen.
        if n_rows >= EXCEL_MAX_ROWS:
            return "csv_zip"
        return "xlsx"
    if requested_norm in {"csv", "csv zip", "csv_zip"}:
        return "csv_zip"
    # Automático: CSV ZIP para tablas horarias o resultados muy grandes.
    return "csv_zip" if n_rows >= int(row_threshold) else "xlsx"


def export_csv_zip(
    df: pd.DataFrame,
    output_path: str | Path,
    resumen: pd.DataFrame | None = None,
    extra_tables: dict[str, pd.DataFrame] | None = None,
    split_by_scenario: bool = False,
) -> Path:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with ZipFile(output_path, mode="w", compression=ZIP_DEFLATED) as zf:
        def write_csv(name: str, data: pd.DataFrame):
            csv_bytes = data.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
            zf.writestr(safe_filename(name), csv_bytes)

        if resumen is not None and not resumen.empty:
            write_csv("RESUMEN.csv", resumen)

        if extra_tables:
            for name, data in extra_tables.items():
                if data is not None and not data.empty:
                    write_csv(f"{name}.csv", data)

        if split_by_scenario and df is not None and not df.empty and "Escenario" in df.columns:
            for scenario, data in df.groupby("Escenario"):
                write_csv(f"Resultados_{scenario}.csv", data.reset_index(drop=True))
        else:
            write_csv("RESULTADOS.csv", df if df is not None else pd.DataFrame())

    return output_path

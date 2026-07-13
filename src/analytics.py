from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import pandas as pd

SCENARIO_COLS = ["Escenario", "Scenario"]
CASE_COLS = ["Caso", "caso", "Case"]
YEAR_COLS = ["Fiscal Year", "Year", "Año", "Anio"]
DATE_COLS = ["Fecha", "Date", "Datetime", "_date"]
LINE_COLS = ["LineName", "Línea", "Line", "child_name"]
FLOW_COLS = ["Flow", "Flow_mean", "Flow_sample 1", "Flow_sample 2", "Flow_sample 3"]
IMPORT_LIMIT_COLS = ["Import Limit [MW]", "Import Limit"]
EXPORT_LIMIT_COLS = ["Export Limit [MW]", "Export Limit"]


def first_existing(columns: Iterable[str], candidates: list[str]) -> str | None:
    cols = list(columns)
    for c in candidates:
        if c in cols:
            return c
    return None


def scenario_case_label(df: pd.DataFrame, fallback: str) -> str:
    """Etiqueta legible para gráficos comparativos: Escenario - caso."""
    scen_col = first_existing(df.columns, SCENARIO_COLS)
    case_col = first_existing(df.columns, CASE_COLS)
    scen = None
    case = None
    if scen_col and not df.empty:
        vals = [str(x) for x in df[scen_col].dropna().unique().tolist()]
        scen = vals[0] if len(vals) == 1 else ("varios escenarios" if len(vals) > 1 else None)
    if case_col and not df.empty:
        vals = [str(x) for x in df[case_col].dropna().unique().tolist()]
        case = vals[0] if len(vals) == 1 else ("varios casos" if len(vals) > 1 else None)
    if scen and case:
        return f"{scen} - {case}"
    if scen:
        return scen
    if case:
        return case
    return fallback


def infer_flow_cols(df: pd.DataFrame) -> tuple[str | None, str | None, str | None, str | None]:
    line_col = first_existing(df.columns, LINE_COLS)
    flow_cols = [c for c in df.columns if str(c).startswith("Flow_") or c == "Flow"]
    flow_col = "Flow_mean" if "Flow_mean" in flow_cols else (flow_cols[0] if flow_cols else None)
    import_col = first_existing(df.columns, IMPORT_LIMIT_COLS)
    export_col = first_existing(df.columns, EXPORT_LIMIT_COLS)
    return line_col, flow_col, import_col, export_col


def infer_line_directions(line_name: str | None) -> tuple[str, str]:
    """Infere sentido positivo y negativo a partir del nombre de la línea.

    Si el nombre viene como 'Nodo A --> Nodo B', entonces:
    - flujo positivo: Nodo A --> Nodo B
    - flujo negativo: Nodo B --> Nodo A
    """
    if line_name is None:
        return ("Flujo positivo / sentido asociado a Import Limit", "Flujo negativo / sentido asociado a Export Limit")
    raw = str(line_name).strip()
    separators = ["-->", "->", "→", "=>"]
    for sep in separators:
        if sep in raw:
            parts = [x.strip() for x in raw.split(sep, 1)]
            if len(parts) == 2 and parts[0] and parts[1]:
                a, b = parts
                return (f"{a} --> {b}", f"{b} --> {a}")
    return ("Flujo positivo / sentido asociado a Import Limit", "Flujo negativo / sentido asociado a Export Limit")


def add_signed_flow_utilization(
    df: pd.DataFrame,
    flow_col: str,
    import_col: str | None,
    export_col: str | None,
    positive_direction: str = "Flujo positivo / sentido asociado a Import Limit",
    negative_direction: str = "Flujo negativo / sentido asociado a Export Limit",
) -> pd.DataFrame:
    """Calcula utilización de líneas respetando el signo del flujo.

    Convención implementada:
    - Flow >= 0 se contrasta contra Import Limit.
    - Flow < 0 se contrasta contra Export Limit.

    La etiqueta de dirección queda parametrizada para que el usuario pueda documentar si el signo
    corresponde, por ejemplo, a Norte→Centro o Centro→Norte según la convención de la línea.
    """
    out = df.copy()
    out[flow_col] = pd.to_numeric(out[flow_col], errors="coerce")
    out["_flow_abs_mw"] = out[flow_col].abs()
    out["_flow_sign"] = out[flow_col].apply(lambda x: "positivo" if pd.notna(x) and x >= 0 else ("negativo" if pd.notna(x) else "sin dato"))
    out["_direction"] = out["_flow_sign"].map({"positivo": positive_direction, "negativo": negative_direction}).fillna("sin dato")

    import_series = pd.Series(pd.NA, index=out.index, dtype="Float64")
    export_series = pd.Series(pd.NA, index=out.index, dtype="Float64")
    if import_col and import_col in out.columns:
        import_series = pd.to_numeric(out[import_col], errors="coerce").abs().astype("Float64")
    if export_col and export_col in out.columns:
        export_series = pd.to_numeric(out[export_col], errors="coerce").abs().astype("Float64")

    out["_limit_used_mw"] = import_series.where(out[flow_col] >= 0, export_series)
    out["_limit_source"] = import_col or "Import Limit"
    out.loc[out[flow_col] < 0, "_limit_source"] = export_col or "Export Limit"
    out["_utilization_pct"] = out["_flow_abs_mw"] / out["_limit_used_mw"].replace(0, pd.NA) * 100
    out["_over_limit"] = out["_utilization_pct"] > 100
    out["_missing_limit"] = out["_limit_used_mw"].isna() | (out["_limit_used_mw"] == 0)
    return out


def transmission_utilization_summary(
    df: pd.DataFrame,
    line_col: str,
    flow_col: str,
    import_col: str | None,
    export_col: str | None,
    positive_direction: str = "Flujo positivo / sentido asociado a Import Limit",
    negative_direction: str = "Flujo negativo / sentido asociado a Export Limit",
) -> tuple[pd.DataFrame, pd.DataFrame]:
    enriched = add_signed_flow_utilization(df, flow_col, import_col, export_col, positive_direction, negative_direction)
    if line_col not in enriched.columns:
        return enriched, pd.DataFrame()

    def dominant_direction(s: pd.Series) -> str:
        vc = s.dropna().astype(str).value_counts()
        return vc.index[0] if not vc.empty else "sin dato"

    summary = enriched.groupby(line_col, as_index=False).agg(
        Flujo_abs_promedio_MW=("_flow_abs_mw", "mean"),
        Flujo_abs_max_MW=("_flow_abs_mw", "max"),
        Utilizacion_promedio_pct=("_utilization_pct", "mean"),
        Utilizacion_maxima_pct=("_utilization_pct", "max"),
        P95_utilizacion_pct=("_utilization_pct", lambda x: x.quantile(0.95)),
        Horas_sobre_70=("_utilization_pct", lambda x: int((x > 70).sum())),
        Horas_sobre_80=("_utilization_pct", lambda x: int((x > 80).sum())),
        Horas_sobre_90=("_utilization_pct", lambda x: int((x > 90).sum())),
        Horas_sobre_limite=("_over_limit", lambda x: int(x.sum())),
        Horas_flujo_positivo=("_flow_sign", lambda x: int((x == "positivo").sum())),
        Horas_flujo_negativo=("_flow_sign", lambda x: int((x == "negativo").sum())),
        Horas_limite_faltante=("_missing_limit", lambda x: int(x.sum())),
        Sentido_dominante=("_direction", dominant_direction),
    )
    summary["Sentido_dominante"] = summary["Sentido_dominante"].fillna("sin dato")
    summary = summary.sort_values("P95_utilizacion_pct", ascending=False)
    return enriched, summary


def critical_hours(
    df: pd.DataFrame,
    value_col: str,
    date_col: str,
    object_col: str | None = None,
    top_n: int = 100,
    abs_value: bool = True,
) -> pd.DataFrame:
    work = df.copy()
    work[value_col] = pd.to_numeric(work[value_col], errors="coerce")
    work[date_col] = pd.to_datetime(work[date_col], errors="coerce")
    work = work.dropna(subset=[date_col, value_col])
    if abs_value:
        work["_ranking_value"] = work[value_col].abs()
    else:
        work["_ranking_value"] = work[value_col]
    cols = [date_col, value_col, "_ranking_value"] + ([object_col] if object_col and object_col in work.columns else [])
    return work[cols].sort_values("_ranking_value", ascending=False).head(top_n).rename(columns={"_ranking_value": "Valor ranking"})

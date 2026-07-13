from __future__ import annotations

from pathlib import Path
from datetime import datetime
from typing import Any
import json

import pandas as pd

from src.analytics import transmission_utilization_summary, infer_flow_cols

SCENARIO_COLS = ["Escenario", "Scenario"]
CASE_COLS = ["Caso", "caso", "Case"]
YEAR_COLS = ["Fiscal Year", "Year", "Año", "Anio"]
DATE_COLS = ["Fecha", "Date", "Datetime", "_date"]
OBJECT_COLS = ["Central", "Barra", "LineName", "Línea", "category_name", "child_name"]


def reports_dir(root_dir: str | Path) -> Path:
    p = Path(root_dir) / "workspace" / "reports"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _first_existing(columns, candidates):
    for c in candidates:
        if c in columns:
            return c
    return None


def _numeric_cols(df: pd.DataFrame) -> list[str]:
    out = []
    for c in df.columns:
        # Fechas no son métricas agregables; evita errores como datetime64 sum.
        if pd.api.types.is_datetime64_any_dtype(df[c]):
            continue
        if pd.api.types.is_numeric_dtype(df[c]):
            out.append(c)
        else:
            s = pd.to_numeric(df[c], errors="coerce")
            if s.notna().any() and s.notna().mean() > 0.75:
                out.append(c)
    return out


def _choose_metric(work: pd.DataFrame, metric: str | None, numeric: list[str]) -> str | None:
    """Elige una métrica numérica segura.

    Si el usuario selecciona por error una columna de fecha/texto, se ignora y se usa
    una métrica numérica disponible. Esto evita sumar datetime64 en reportes.
    """
    if metric and metric in work.columns and metric in numeric:
        return metric
    preferred = [
        "Total Cost", "Generation [MWh]", "Energy Curtailed [MWh]", "Capacidad (c/ batería)",
        "Load_mean", "Battery Load_mean", "Flow_mean", "Value", "Units", "Comparado", "Delta"
    ]
    return next((c for c in preferred if c in numeric), numeric[0] if numeric else None)


def _coerce_numeric_metric(work: pd.DataFrame, metric: str) -> pd.Series:
    """Devuelve la métrica como serie numérica o NaN si no es agregable."""
    if pd.api.types.is_datetime64_any_dtype(work[metric]):
        return pd.Series(pd.NA, index=work.index, dtype="Float64")
    return pd.to_numeric(work[metric], errors="coerce")


def generate_findings(df: pd.DataFrame, consulta: str | None = None, metric: str | None = None) -> dict[str, Any]:
    consulta = consulta or "Salida PLEXOS"
    if df is None or df.empty:
        return {"title": f"Reporte automático de hallazgos - {consulta}", "findings": ["No hay datos disponibles para generar hallazgos."], "tables": {}}
    work = df.copy()
    scen_col = _first_existing(work.columns, SCENARIO_COLS)
    case_col = _first_existing(work.columns, CASE_COLS)
    year_col = _first_existing(work.columns, YEAR_COLS)
    date_col = _first_existing(work.columns, DATE_COLS)
    object_col = _first_existing(work.columns, OBJECT_COLS)
    numeric = _numeric_cols(work)
    requested_metric = metric
    metric = _choose_metric(work, metric, numeric)
    findings = [f"Se analizaron {len(work):,} filas y {len(work.columns):,} columnas de la consulta **{consulta}**."]
    tables: dict[str, pd.DataFrame] = {}
    if requested_metric and requested_metric != metric:
        findings.append(f"La métrica solicitada **{requested_metric}** no es numérica agregable; se usó **{metric}**.")
    if metric and metric in work.columns:
        work[metric] = _coerce_numeric_metric(work, metric)
        if work[metric].notna().sum() == 0:
            findings.append(f"La métrica **{metric}** no contiene valores numéricos válidos para agregar.")
            return {"title": f"Reporte automático de hallazgos - {consulta}", "metric": metric, "findings": findings, "tables": tables}
        findings.append(f"La métrica principal usada para el reporte es **{metric}**.")
        dims = [c for c in [scen_col, case_col] if c]
        if dims:
            agg = work.groupby(dims, as_index=False)[metric].sum().sort_values(metric, ascending=False)
            tables["ranking_escenario_caso"] = agg
            top = agg.iloc[0]
            label = " / ".join(str(top[c]) for c in dims)
            findings.append(f"El mayor valor acumulado de **{metric}** se observa en **{label}**, con {top[metric]:,.2f}.")
        if year_col:
            work[year_col] = pd.to_numeric(work[year_col], errors="coerce")
            annual_dims = [year_col] + dims
            annual = work.dropna(subset=[year_col]).groupby(annual_dims, as_index=False)[metric].sum().sort_values(year_col)
            tables["serie_anual"] = annual
            if not annual.empty:
                total_year = annual.groupby(year_col, as_index=False)[metric].sum().sort_values(year_col)
                first = total_year.iloc[0]
                last = total_year.iloc[-1]
                delta = last[metric] - first[metric]
                findings.append(f"Entre {int(first[year_col])} y {int(last[year_col])}, **{metric}** cambia en {delta:,.2f} en el agregado de la selección.")
        if date_col:
            work[date_col] = pd.to_datetime(work[date_col], errors="coerce")
            ts = work.dropna(subset=[date_col]).groupby(date_col, as_index=False)[metric].sum().sort_values(date_col)
            tables["serie_horaria_agregada"] = ts.head(10000)
            if not ts.empty:
                peak = ts.loc[ts[metric].idxmax()]
                findings.append(f"El máximo horario agregado de **{metric}** es {peak[metric]:,.2f}, observado en {peak[date_col]}.")
        if object_col:
            obj = work.groupby(object_col, as_index=False)[metric].sum().sort_values(metric, ascending=False).head(20)
            tables[f"top_{object_col}"] = obj
            if not obj.empty:
                findings.append(f"El objeto con mayor valor acumulado de **{metric}** es **{obj.iloc[0][object_col]}**, con {obj.iloc[0][metric]:,.2f}.")
        line_col, flow_guess, import_col, export_col = infer_flow_cols(work)
        flow_cols = [c for c in work.columns if str(c).startswith("Flow_") or c == "Flow"]
        fcol = metric if metric in flow_cols else flow_guess
        if fcol and line_col and (import_col or export_col):
            enriched, util = transmission_utilization_summary(
                work,
                line_col=line_col,
                flow_col=fcol,
                import_col=import_col,
                export_col=export_col,
                positive_direction="Flujo positivo / sentido asociado a Import Limit",
                negative_direction="Flujo negativo / sentido asociado a Export Limit",
            )
            tables["utilizacion_transmision_con_signo"] = util
            if not util.empty:
                top_util = util.iloc[0]
                findings.append(
                    f"La línea/objeto con mayor P95 de utilización es **{top_util[line_col]}**, "
                    f"con P95={top_util['P95_utilizacion_pct']:,.2f}%. "
                    "La utilización se calculó preservando el signo del flujo: flujo positivo contra Import Limit y flujo negativo contra Export Limit."
                )
                critical = util[(util["P95_utilizacion_pct"] >= 80) | (util["Horas_sobre_90"] > 0)].copy()
                if not critical.empty:
                    tables["semaforo_transmision"] = critical
                    findings.append(f"Se identifican {len(critical):,} líneas/objetos con P95 ≥ 80% o con horas sobre 90% de utilización.")
                under = util[(util["Utilizacion_promedio_pct"] < 10) & (util["Horas_sobre_80"] == 0)].copy()
                if not under.empty:
                    tables["lineas_subutilizadas"] = under
                    findings.append(f"Se identifican {len(under):,} líneas/objetos con utilización promedio menor a 10% y sin horas sobre 80%.")
            if date_col:
                enriched[date_col] = pd.to_datetime(enriched[date_col], errors="coerce")
                crit_hours = enriched.dropna(subset=[date_col, "_utilization_pct"]).sort_values("_utilization_pct", ascending=False)
                cols = [c for c in [date_col, line_col, fcol, "_direction", "_limit_used_mw", "_limit_source", "_utilization_pct"] if c in crit_hours.columns]
                tables["horas_criticas_transmision"] = crit_hours[cols].head(100)
    else:
        findings.append("No se identificaron columnas numéricas suficientes para construir hallazgos cuantitativos.")
    return {"title": f"Reporte automático de hallazgos - {consulta}", "metric": metric, "findings": findings, "tables": tables}


def to_markdown(report: dict[str, Any]) -> str:
    lines = [f"# {report.get('title', 'Reporte automático de hallazgos')}", ""]
    metric = report.get("metric")
    if metric:
        lines += [f"**Métrica principal:** {metric}", ""]
    lines.append("## Hallazgos")
    for item in report.get("findings", []):
        lines.append(f"- {item}")
    for name, df in (report.get("tables") or {}).items():
        lines += ["", f"## {name}"]
        if isinstance(df, pd.DataFrame) and not df.empty:
            try:
                lines.append(df.head(20).to_markdown(index=False))
            except Exception:
                lines.append("```text")
                lines.append(df.head(20).to_string(index=False))
                lines.append("```")
        else:
            lines.append("Sin datos.")
    return "\n".join(lines)


def write_report(root_dir: str | Path, report: dict[str, Any]) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = reports_dir(root_dir) / f"hallazgos_{ts}.md"
    path.write_text(to_markdown(report), encoding="utf-8")
    return path

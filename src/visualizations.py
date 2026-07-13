from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Any
import zipfile
import hashlib
import json

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from src.analytics import transmission_utilization_summary, critical_hours, infer_line_directions


DATE_COLS = ["Fecha", "Date", "Datetime", "_date"]
YEAR_COLS = ["Fiscal Year", "Year", "Año", "Anio"]
SCENARIO_COLS = ["Escenario", "Scenario"]
CASE_COLS = ["Caso", "caso", "Case"]


def _first_existing(columns: list[str] | pd.Index, candidates: list[str]) -> str | None:
    cols = list(columns)
    for c in candidates:
        if c in cols:
            return c
    return None


def _numeric_cols(df: pd.DataFrame, exclude: set[str] | None = None) -> list[str]:
    exclude = exclude or set()
    out: list[str] = []
    for c in df.columns:
        if c in exclude:
            continue
        if pd.api.types.is_numeric_dtype(df[c]):
            out.append(c)
        else:
            s = pd.to_numeric(df[c], errors="coerce")
            if s.notna().any() and s.notna().mean() > 0.75:
                out.append(c)
    return out


def _coerce_numeric(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    out = df.copy()
    for c in cols:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")
    return out


def _filter_df(df: pd.DataFrame, key_prefix: str = "viz") -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    work = df.copy()
    scen_col = _first_existing(work.columns, SCENARIO_COLS)
    case_col = _first_existing(work.columns, CASE_COLS)
    with st.expander("Filtros de visualización", expanded=True):
        cols = st.columns(3)
        if scen_col:
            opts = sorted([str(x) for x in work[scen_col].dropna().unique().tolist()])
            selected = cols[0].multiselect("Escenario", opts, default=opts[: min(len(opts), 8)], key=f"{key_prefix}_scenario")
            if selected:
                work = work[work[scen_col].astype(str).isin(selected)]
        if case_col:
            opts = sorted([str(x) for x in work[case_col].dropna().unique().tolist()])
            selected = cols[1].multiselect("Caso", opts, default=opts[: min(len(opts), 8)], key=f"{key_prefix}_case")
            if selected:
                work = work[work[case_col].astype(str).isin(selected)]
        year_col = _first_existing(work.columns, YEAR_COLS)
        if year_col:
            years = sorted(pd.to_numeric(work[year_col], errors="coerce").dropna().astype(int).unique().tolist())
            if len(years) > 1:
                ymin, ymax = cols[2].select_slider(
                    "Años",
                    options=years,
                    value=(min(years), max(years)),
                    key=f"{key_prefix}_years",
                )
                work = work[
                    (pd.to_numeric(work[year_col], errors="coerce") >= ymin)
                    & (pd.to_numeric(work[year_col], errors="coerce") <= ymax)
                ]
            elif len(years) == 1:
                cols[2].info(f"Año disponible: {years[0]}")
                work = work[pd.to_numeric(work[year_col], errors="coerce") == years[0]]
    return work


def _format_tooltips(fig: go.Figure) -> go.Figure:
    for trace in fig.data:
        orientation = getattr(trace, "orientation", None)
        trace_type = getattr(trace, "type", "")
        if trace_type == "bar" and orientation == "h":
            trace.hovertemplate = "%{y}<br>%{x:,.2f}<extra>%{fullData.name}</extra>"
        elif trace_type in {"scatter", "bar"}:
            trace.hovertemplate = "%{x}<br>%{y:,.2f}<extra>%{fullData.name}</extra>"
    return fig


def _format_year_xaxis(fig: go.Figure, df: pd.DataFrame, year_col: str | None) -> go.Figure:
    if not year_col or year_col not in df.columns:
        return fig
    years = sorted(pd.to_numeric(df[year_col], errors="coerce").dropna().astype(int).unique().tolist())
    if years:
        fig.update_xaxes(tickmode="array", tickvals=years, ticktext=[str(y) for y in years], tickformat="d")
    return fig


def _format_numeric_axes(fig: go.Figure) -> go.Figure:
    # Solo se fuerza formato numérico en el eje Y.
    # No se aplica a X porque puede ser fecha/hora; si se fuerza ",.2f",
    # Plotly muestra literalmente ",.2f" o rompe el eje temporal.
    fig.update_yaxes(tickformat=",.2f")
    return fig


def _plotly(fig: go.Figure, key: str | None = None, year_df: pd.DataFrame | None = None, year_col: str | None = None):
    fig.update_layout(margin=dict(l=10, r=10, t=60, b=10), hovermode="x unified")
    _format_tooltips(fig)
    _format_numeric_axes(fig)
    if year_df is not None and year_col is not None:
        _format_year_xaxis(fig, year_df, year_col)
    st.plotly_chart(fig, width="stretch", key=key)


def _pick_tech_col(df: pd.DataFrame) -> str | None:
    for c in ["Tipo", "Tipo 2", "Technology", "Tecnología", "Tecnologia"]:
        if c in df.columns:
            return c
    return None


def _cost_visuals(df: pd.DataFrame, resumen: pd.DataFrame | None = None):
    st.markdown("### Costos del sistema")
    work = _filter_df(df, "cost")
    if work.empty:
        st.info("No hay datos para graficar.")
        return
    comp_cols = [c for c in ["Cost of Unserved Energy", "Total Generation Cost", "Annualized Build Cost Gen", "Annualized Build Cost Batt", "Annualized Build Cost Gx", "Annualized Build Cost Sx"] if c in work.columns]
    if not comp_cols or "Fiscal Year" not in work.columns:
        st.info("La tabla no contiene las columnas mínimas para gráficos de costos.")
        return
    work = _coerce_numeric(work, comp_cols)
    work["Total Cost"] = work[comp_cols].sum(axis=1)
    group_cols = ["Fiscal Year"] + [c for c in ["Escenario", "Caso"] if c in work.columns]
    annual = work.groupby(group_cols, as_index=False)[["Total Cost"] + comp_cols].sum()
    metric = st.selectbox("Métrica de costo", ["Total Cost"] + comp_cols, key="cost_metric")
    k1, k2, k3 = st.columns(3)
    k1.metric("Total acumulado", f"{annual[metric].sum():,.2f}")
    k2.metric("Máximo anual", f"{annual[metric].max():,.2f}")
    k3.metric("Promedio anual", f"{annual[metric].mean():,.2f}")
    fig = px.line(annual, x="Fiscal Year", y=metric, color="Caso" if "Caso" in annual.columns else None, line_dash="Escenario" if "Escenario" in annual.columns else None, markers=True, title=f"Evolución anual - {metric}")
    _plotly(fig, "cost_line", year_df=annual, year_col="Fiscal Year")

    if "Caso" in annual.columns:
        total_case = annual.groupby([c for c in ["Escenario", "Caso"] if c in annual.columns], as_index=False)[metric].sum()
        fig2 = px.bar(total_case, x="Caso", y=metric, color="Escenario" if "Escenario" in total_case.columns else None, barmode="group", title=f"Comparación acumulada - {metric}")
        _plotly(fig2, "cost_bar")
        st.markdown("#### Delta contra caso base")
        cases = sorted(total_case["Caso"].astype(str).unique().tolist())
        base_case = st.selectbox("Caso base", cases, key="cost_base_case")
        dims = [c for c in ["Escenario"] if c in total_case.columns]
        base = total_case[total_case["Caso"].astype(str) == base_case][dims + [metric]].rename(columns={metric: "Base"})
        comp = total_case.merge(base, on=dims, how="left") if dims else total_case.assign(Base=float(total_case.loc[total_case["Caso"].astype(str) == base_case, metric].sum()))
        comp["Delta"] = comp[metric] - comp["Base"]
        fig3 = px.bar(comp, x="Caso", y="Delta", color="Escenario" if "Escenario" in comp.columns else None, barmode="group", title=f"Delta acumulado respecto de {base_case}")
        _plotly(fig3, "cost_delta")

    with st.expander("Diagnóstico de drivers de costo", expanded=False):
        st.caption("Descompone el costo total en sus componentes principales para identificar qué variable explica el resultado.")
        dims = [c for c in ["Escenario", "Caso"] if c in work.columns]
        driver = work.groupby(dims, as_index=False)[comp_cols].sum() if dims else pd.DataFrame([work[comp_cols].sum().to_dict()])
        driver["Total componentes"] = driver[comp_cols].sum(axis=1)
        long = driver.melt(id_vars=dims, value_vars=comp_cols, var_name="Componente", value_name="Valor")
        st.dataframe(driver.head(100), width="stretch")
        fig_driver = px.bar(long, x="Componente", y="Valor", color="Caso" if "Caso" in long.columns else None, barmode="group", facet_col="Escenario" if "Escenario" in long.columns else None, title="Descomposición acumulada por componente")
        _plotly(fig_driver, "cost_drivers")

        if "Cost of Unserved Energy" in comp_cols:
            ens = work.groupby(dims, as_index=False)["Cost of Unserved Energy"].sum() if dims else pd.DataFrame({"Cost of Unserved Energy": [work["Cost of Unserved Energy"].sum()]})
            ens_alert = ens[pd.to_numeric(ens["Cost of Unserved Energy"], errors="coerce") > 0]
            if not ens_alert.empty:
                st.warning("Se detecta Cost of Unserved Energy mayor a cero en al menos una combinación escenario/caso. Revisar horas o nodos críticos asociados.")
                st.dataframe(ens_alert, width="stretch")


def _generation_visuals(df: pd.DataFrame):
    st.markdown("### Generación de energía")
    work = _filter_df(df, "gen")
    if work.empty:
        st.info("No hay datos para graficar.")
        return
    year_col = _first_existing(work.columns, YEAR_COLS)
    date_col = _first_existing(work.columns, DATE_COLS)
    value_cols = [c for c in work.columns if c == "Generation [MWh]" or c.startswith("Generation_") or c == "Generation"]
    value_cols = value_cols or _numeric_cols(work, exclude={year_col or "", date_col or ""})
    if not value_cols:
        st.info("No se encontraron columnas de generación para graficar.")
        return
    value_col = st.selectbox("Columna de generación", value_cols, key="gen_value")
    work = _coerce_numeric(work, [value_col])
    tech_col = _pick_tech_col(work)
    if year_col:
        dims = [year_col] + [c for c in ["Escenario", "Caso", "caso"] if c in work.columns] + ([tech_col] if tech_col else [])
        annual = work.groupby(dims, as_index=False)[value_col].sum()
        color = tech_col if tech_col else ("Caso" if "Caso" in annual.columns else ("caso" if "caso" in annual.columns else None))
        fig = px.area(annual, x=year_col, y=value_col, color=color, line_group="Escenario" if "Escenario" in annual.columns and color != "Escenario" else None, title="Generación anual agregada")
        _plotly(fig, "gen_area", year_df=annual, year_col=year_col)
    elif date_col:
        work[date_col] = pd.to_datetime(work[date_col], errors="coerce")
        hourly = work.groupby(date_col, as_index=False)[value_col].sum().sort_values(date_col)
        fig = px.line(hourly, x=date_col, y=value_col, title="Generación horaria agregada")
        _plotly(fig, "gen_hourly")

    if "Central" in work.columns:
        topn = st.slider("Top centrales", 5, 30, 15, key="gen_topn")
        top_dims = ["Central"] + ([tech_col] if tech_col else [])
        top = work.groupby(top_dims, as_index=False)[value_col].sum().sort_values(value_col, ascending=False).head(topn)
        fig2 = px.bar(top.sort_values(value_col), x=value_col, y="Central", orientation="h", color=tech_col if tech_col in top.columns else None, title=f"Top {topn} centrales por generación")
        _plotly(fig2, "gen_top")


def _capacity_visuals(df: pd.DataFrame):
    st.markdown("### Capacidad instalada")
    work = _filter_df(df, "cap")
    if work.empty:
        st.info("No hay datos para graficar.")
        return
    year_col = _first_existing(work.columns, YEAR_COLS)
    candidates = ["Capacidad (c/ batería)", "Installed Capacity", "Installed Capacity [MW]", "Units", "Max Units Built"]
    value_cols = [c for c in candidates if c in work.columns] or _numeric_cols(work, exclude={year_col or ""})
    if not value_cols:
        st.info("No se encontraron columnas de capacidad para graficar.")
        return
    value_col = st.selectbox("Columna de capacidad", value_cols, key="cap_value")
    work = _coerce_numeric(work, [value_col])
    tech_col = _pick_tech_col(work)
    if year_col:
        color = tech_col if tech_col else ("Escenario" if "Escenario" in work.columns else None)
        dims = [year_col] + ([color] if color else [])
        annual = work.groupby(dims, as_index=False)[value_col].sum()
        fig = px.area(annual, x=year_col, y=value_col, color=color, title="Capacidad instalada por año")
        _plotly(fig, "cap_area", year_df=annual, year_col=year_col)
        try:
            sort_cols = ([color] if color else []) + [year_col]
            annual_sorted = annual.sort_values(sort_cols).copy()
            group_for_delta = [color] if color else []
            annual_sorted["Nueva capacidad"] = annual_sorted.groupby(group_for_delta)[value_col].diff().fillna(annual_sorted[value_col]) if group_for_delta else annual_sorted[value_col].diff().fillna(annual_sorted[value_col])
            fig_delta = px.bar(annual_sorted, x=year_col, y="Nueva capacidad", color=color, title="Nueva capacidad anual estimada")
            _plotly(fig_delta, "cap_delta", year_df=annual_sorted, year_col=year_col)
        except Exception:
            pass
    if "Central" in work.columns:
        topn = st.slider("Top centrales por capacidad", 5, 30, 15, key="cap_topn")
        top_dims = ["Central"] + ([tech_col] if tech_col else [])
        top = work.groupby(top_dims, as_index=False)[value_col].max().sort_values(value_col, ascending=False).head(topn)
        fig2 = px.bar(top.sort_values(value_col), x=value_col, y="Central", orientation="h", color=tech_col if tech_col in top.columns else None, title=f"Top {topn} centrales por capacidad")
        _plotly(fig2, "cap_top")


def _load_visuals(df: pd.DataFrame):
    st.markdown("### Demanda de energía")
    work = _filter_df(df, "load")
    if work.empty:
        st.info("No hay datos para graficar.")
        return
    date_col = _first_existing(work.columns, DATE_COLS)
    value_cols = [c for c in work.columns if c.startswith("Load_") or c.startswith("Battery Load_") or c in {"Load", "Battery Load"}]
    if not value_cols:
        value_cols = _numeric_cols(work, exclude={date_col or ""})
    if not value_cols or not date_col:
        st.info("La tabla no contiene Fecha y columnas Load/Battery Load para graficar.")
        return
    value_col = st.selectbox("Columna de demanda", value_cols, key="load_value")
    work = _coerce_numeric(work, [value_col])
    work[date_col] = pd.to_datetime(work[date_col], errors="coerce")
    series = work.groupby(date_col, as_index=False)[value_col].sum().sort_values(date_col)
    k1, k2, k3 = st.columns(3)
    k1.metric("Energía agregada", f"{series[value_col].sum():,.2f}")
    k2.metric("Peak horario", f"{series[value_col].max():,.2f}")
    k3.metric("Promedio horario", f"{series[value_col].mean():,.2f}")
    fig = px.line(series, x=date_col, y=value_col, title="Demanda horaria agregada")
    _plotly(fig, "load_line")
    duration = series[[value_col]].sort_values(value_col, ascending=False).reset_index(drop=True)
    duration["Hora ordenada"] = duration.index + 1
    fig_ldc = px.line(duration, x="Hora ordenada", y=value_col, title="Curva de duración de demanda")
    _plotly(fig_ldc, "load_duration")
    barra_col = "Barra" if "Barra" in work.columns else ("Node" if "Node" in work.columns else None)
    if barra_col:
        topn = st.slider("Top barras por peak", 5, 30, 15, key="load_topn")
        peak = work.groupby(barra_col, as_index=False)[value_col].max().sort_values(value_col, ascending=False).head(topn)
        fig2 = px.bar(peak.sort_values(value_col), x=value_col, y=barra_col, orientation="h", title=f"Top {topn} barras por demanda máxima")
        _plotly(fig2, "load_peak")


def _build_aggregated_flow_ts(work_line: pd.DataFrame, date_col: str, selected_flow_cols: list[str], limit_import: str | None, limit_export: str | None) -> pd.DataFrame:
    agg_cols = selected_flow_cols + ([limit_import] if limit_import else []) + ([limit_export] if limit_export else [])
    temp = work_line.copy()
    for c in agg_cols:
        if c in temp.columns:
            temp[c] = pd.to_numeric(temp[c], errors="coerce")
    ts = temp.groupby(date_col, as_index=False)[agg_cols].sum().sort_values(date_col)
    if limit_import and limit_import in ts.columns:
        ts[limit_import] = ts[limit_import].abs()
    if limit_export and limit_export in ts.columns:
        ts[limit_export] = -ts[limit_export].abs()
    return ts


def _flow_visuals(df: pd.DataFrame):
    st.markdown("### Flujos de transmisión")
    work = _filter_df(df, "flow")
    if work.empty:
        st.info("No hay datos para graficar.")
        return
    line_col = "LineName" if "LineName" in work.columns else ("Línea" if "Línea" in work.columns else None)
    date_col = _first_existing(work.columns, DATE_COLS)
    flow_cols = [c for c in work.columns if c.startswith("Flow_") or c == "Flow"]
    if not flow_cols:
        flow_cols = _numeric_cols(work, exclude={date_col or ""})
    if not flow_cols:
        st.info("No se encontraron columnas de flujo para graficar.")
        return

    default_flow_cols = flow_cols[: min(2, len(flow_cols))]
    selected_flow_cols = st.multiselect("Columnas de flujo a graficar", flow_cols, default=default_flow_cols, key="flow_value_multi")
    if not selected_flow_cols:
        st.info("Selecciona al menos una columna de flujo.")
        return
    work = _coerce_numeric(work, selected_flow_cols)

    limit_import = next((c for c in ["Import Limit [MW]", "Import Limit"] if c in work.columns), None)
    limit_export = next((c for c in ["Export Limit [MW]", "Export Limit"] if c in work.columns), None)

    selected_lines: list[str] = []
    if line_col:
        opts = sorted([str(x) for x in work[line_col].dropna().unique().tolist()])
        selected_lines = st.multiselect("Líneas a agregar", opts, default=opts[:1], key="flow_lines") if opts else []
        work_line = work[work[line_col].astype(str).isin(selected_lines)].copy() if selected_lines else work.copy()
    else:
        work_line = work.copy()

    auto_pos, auto_neg = infer_line_directions(selected_lines[0]) if len(selected_lines) == 1 else (
        "Flujo positivo / sentido asociado a Import Limit",
        "Flujo negativo / sentido asociado a Export Limit",
    )

    with st.expander("Convención de signo y sentido del flujo", expanded=False):
        st.caption(
            "Si la línea se nombra como 'Nodo A --> Nodo B', entonces el flujo positivo se interpreta como A --> B y el flujo negativo como B --> A. "
            "Los positivos se contrastan contra Import Limit y los negativos contra Export Limit."
        )
        positive_direction = st.text_input("Etiqueta para flujo positivo", value=auto_pos, key="flow_positive_direction")
        negative_direction = st.text_input("Etiqueta para flujo negativo", value=auto_neg, key="flow_negative_direction")
        if selected_lines:
            map_rows = []
            for ln in selected_lines[:50]:
                pos, neg = infer_line_directions(ln)
                map_rows.append({"Línea": ln, "Sentido flujo positivo": pos, "Sentido flujo negativo": neg})
            st.dataframe(pd.DataFrame(map_rows), width="stretch")

    max_abs = max(pd.to_numeric(work_line[c], errors="coerce").abs().max() for c in selected_flow_cols)
    mean_abs = sum(pd.to_numeric(work_line[c], errors="coerce").abs().mean() for c in selected_flow_cols) / len(selected_flow_cols)
    first_flow = selected_flow_cols[0]
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Flujo absoluto máximo", f"{max_abs:,.2f}")
    k2.metric("Flujo absoluto promedio", f"{mean_abs:,.2f}")
    k3.metric("Registros flujo positivo", f"{int((pd.to_numeric(work_line[first_flow], errors='coerce') >= 0).sum()):,}")
    k4.metric("Registros flujo negativo", f"{int((pd.to_numeric(work_line[first_flow], errors='coerce') < 0).sum()):,}")

    if date_col:
        work_line[date_col] = pd.to_datetime(work_line[date_col], errors="coerce")
        ts = _build_aggregated_flow_ts(work_line, date_col, selected_flow_cols, limit_import, limit_export)
        title = "Flujo horario agregado de líneas seleccionadas" if selected_lines else "Flujo horario"
        fig = go.Figure()
        if limit_import and limit_import in ts.columns:
            fig.add_trace(go.Scatter(x=ts[date_col], y=ts[limit_import], mode="lines", name="Límite Importación [MW]"))
        if limit_export and limit_export in ts.columns:
            fig.add_trace(go.Scatter(x=ts[date_col], y=ts[limit_export], mode="lines", name="Límite Exportación [MW]"))
        for c in selected_flow_cols:
            label = c.replace("Flow_", "Flujo_") if c.startswith("Flow_") else c
            fig.add_trace(go.Scatter(x=ts[date_col], y=ts[c], mode="lines", name=f"{label} [MW]"))
        fig.add_hline(y=0, line_dash="dot")
        fig.update_layout(title=title, yaxis_title="Flujo Tx [MW]")
        fig.update_xaxes(type="date", tickformat="%Y-%m-%d<br>%H:%M")
        _plotly(fig, "flow_linechart")
        st.caption("Si seleccionas más de una línea, la app suma flujos y límites por timestamp para construir la serie agregada.")

    if line_col:
        topn = st.slider("Top líneas por flujo máximo absoluto", 5, 30, 15, key="flow_topn")
        aux = work.copy()
        aux["_abs_flow"] = pd.to_numeric(aux[first_flow], errors="coerce").abs()
        top = aux.groupby(line_col, as_index=False)["_abs_flow"].max().sort_values("_abs_flow", ascending=False).head(topn)
        fig2 = px.bar(top.sort_values("_abs_flow"), x="_abs_flow", y=line_col, orientation="h", title=f"Top {topn} líneas por flujo máximo absoluto")
        _plotly(fig2, "flow_top")

        if limit_import or limit_export:
            st.markdown("#### Utilización respecto de límite según signo")
            st.caption(
                "Cálculo: Flow positivo → Import Limit; Flow negativo → Export Limit. "
                "Si la línea está nombrada como 'Nodo A --> Nodo B', un flujo negativo se interpreta como 'Nodo B --> Nodo A'."
            )
            # resumen por línea individual
            enriched, util = transmission_utilization_summary(
                work_line, line_col=line_col, flow_col=first_flow, import_col=limit_import, export_col=limit_export,
                positive_direction=positive_direction, negative_direction=negative_direction,
            )
            st.dataframe(util.head(50), width="stretch")
            fig3 = px.bar(util.head(topn).sort_values("P95_utilizacion_pct"), x="P95_utilizacion_pct", y=line_col, orientation="h", title=f"Top {topn} líneas por P95 de utilización (%)")
            _plotly(fig3, "flow_util_p95")

            # resumen agregado de la selección
            if date_col and selected_lines:
                ts_for_util = _build_aggregated_flow_ts(work_line, date_col, [first_flow], limit_import, limit_export)
                ts_for_util["Selección de líneas"] = " + ".join(selected_lines[:3]) + (" ..." if len(selected_lines) > 3 else "")
                _, agg_util = transmission_utilization_summary(
                    ts_for_util,
                    line_col="Selección de líneas",
                    flow_col=first_flow,
                    import_col=limit_import,
                    export_col=limit_export,
                    positive_direction=positive_direction,
                    negative_direction=negative_direction,
                )
                if not agg_util.empty:
                    st.markdown("##### Resumen agregado de líneas seleccionadas")
                    st.dataframe(agg_util, width="stretch")

            semaforo = util.copy()
            semaforo["Semáforo"] = "Revisión normal"
            semaforo.loc[semaforo["P95_utilizacion_pct"] >= 90, "Semáforo"] = "Crítica: P95 ≥ 90%"
            semaforo.loc[(semaforo["P95_utilizacion_pct"] < 90) & (semaforo["P95_utilizacion_pct"] >= 80), "Semáforo"] = "Alta: P95 ≥ 80%"
            semaforo.loc[(semaforo["Utilizacion_promedio_pct"] < 10) & (semaforo["Horas_sobre_80"] == 0), "Semáforo"] = "Subutilizada"
            with st.expander("Semáforo técnico de transmisión", expanded=False):
                st.dataframe(semaforo.head(100), width="stretch")

            if date_col:
                with st.expander("Horas críticas de flujo/utilización", expanded=False):
                    top_h = st.slider("Cantidad de horas críticas", 10, 200, 50, key="flow_critical_n")
                    criterio = st.selectbox("Criterio", ["Utilización", "Flujo absoluto"], key="flow_critical_metric")
                    # usar serie agregada para horas críticas cuando hay varias líneas seleccionadas
                    if selected_lines:
                        ts_enriched = ts_for_util.copy()
                        ts_enriched = ts_enriched.rename(columns={first_flow: first_flow})
                        from src.analytics import add_signed_flow_utilization
                        ts_enriched = add_signed_flow_utilization(ts_enriched, first_flow, limit_import, limit_export, positive_direction, negative_direction)
                        crit_col = "_utilization_pct" if criterio == "Utilización" else first_flow
                        crit = critical_hours(ts_enriched, crit_col, date_col, object_col=None, top_n=top_h, abs_value=(criterio == "Flujo absoluto"))
                        keep_extra = [c for c in ["_direction", "_limit_used_mw", "_limit_source", "_utilization_pct"] if c in ts_enriched.columns]
                        if keep_extra:
                            crit = crit.merge(ts_enriched[[date_col] + keep_extra].drop_duplicates(), on=[date_col], how="left")
                    else:
                        crit_col = "_utilization_pct" if criterio == "Utilización" else first_flow
                        crit = critical_hours(enriched, crit_col, date_col, object_col=line_col, top_n=top_h, abs_value=(criterio == "Flujo absoluto"))
                        keep_extra = [c for c in ["_direction", "_limit_used_mw", "_limit_source", "_utilization_pct"] if c in enriched.columns]
                        if keep_extra:
                            crit = crit.merge(enriched[[date_col, line_col] + keep_extra].drop_duplicates(), on=[date_col, line_col], how="left")
                    st.dataframe(crit, width="stretch")
        else:
            st.info("No se encontraron columnas de Import Limit / Export Limit para calcular utilización con signo.")


def _restrictions_visuals(df: pd.DataFrame):
    st.markdown("### Restricciones")
    work = _filter_df(df, "res")
    if work.empty:
        st.info("No hay datos para graficar.")
        return
    value_cols = [c for c in work.columns if c.startswith("Value") or c.startswith("Decision") or c.startswith("Units") or c in {"value", "Valor"}]
    value_cols = value_cols or _numeric_cols(work)
    if not value_cols:
        st.info("No se encontraron columnas numéricas para graficar restricciones.")
        return
    value_col = st.selectbox("Columna de valor", value_cols, key="res_value")
    work = _coerce_numeric(work, [value_col])
    cat_col = "category_name" if "category_name" in work.columns else ("Category" if "Category" in work.columns else None)
    if cat_col:
        top = work.groupby(cat_col, as_index=False)[value_col].sum().sort_values(value_col, ascending=False).head(30)
        fig = px.bar(top.sort_values(value_col), x=value_col, y=cat_col, orientation="h", title="Variables/restricciones por valor acumulado")
        _plotly(fig, "res_bar")
    date_col = _first_existing(work.columns, DATE_COLS)
    if date_col:
        work[date_col] = pd.to_datetime(work[date_col], errors="coerce")
        ts = work.groupby(date_col, as_index=False)[value_col].sum().sort_values(date_col)
        fig2 = px.line(ts, x=date_col, y=value_col, title="Evolución horaria de restricciones")
        _plotly(fig2, "res_line")


def _tx_plan_visuals(df: pd.DataFrame, resumen: pd.DataFrame | None = None):
    st.markdown("### Optimización de transmisión")
    work = resumen.copy() if resumen is not None and not resumen.empty else df.copy()
    work = _filter_df(work, "txplan") if not work.empty else work
    if work.empty:
        st.info("No hay datos para graficar.")
        return
    year_like = [c for c in work.columns if "año" in c.lower() or "year" in c.lower()]
    numeric = _numeric_cols(work)
    value_cols = year_like or numeric
    if not value_cols:
        st.info("No se encontraron columnas de año/activación para graficar.")
        return
    value_col = st.selectbox("Columna para graficar", value_cols, key="txplan_value")
    obj_col = "LineName" if "LineName" in work.columns else ("Línea" if "Línea" in work.columns else ("Line" if "Line" in work.columns else None))
    if obj_col:
        plot = work[[obj_col, value_col] + (["Escenario"] if "Escenario" in work.columns else [])].copy()
        plot[value_col] = pd.to_numeric(plot[value_col], errors="coerce")
        plot = plot.dropna(subset=[value_col]).head(60)
        fig = px.bar(plot.sort_values(value_col), x=value_col, y=obj_col, color="Escenario" if "Escenario" in plot.columns else None, orientation="h", title="Activación / resultado por línea")
        _plotly(fig, "txplan_bar")
    elif "Escenario" in work.columns:
        plot = work.groupby("Escenario", as_index=False)[value_col].count().rename(columns={value_col: "Cantidad"})
        fig = px.bar(plot, x="Escenario", y="Cantidad", title="Cantidad de registros por escenario")
        _plotly(fig, "txplan_count")


def _custom_visuals(df: pd.DataFrame):
    st.markdown("### Consulta personalizada")
    work = _filter_df(df, "custom")
    if work.empty:
        st.info("No hay datos para graficar.")
        return
    date_col = _first_existing(work.columns, DATE_COLS)
    year_col = _first_existing(work.columns, YEAR_COLS)
    exclude = {date_col or "", year_col or ""}
    value_cols = _numeric_cols(work, exclude=exclude)
    if not value_cols:
        st.info("No se encontraron columnas numéricas para graficar.")
        return
    value_col = st.selectbox("Columna de valor", value_cols, key="custom_value")
    work = _coerce_numeric(work, [value_col])
    x_col = date_col or year_col
    if x_col:
        if x_col == date_col:
            work[x_col] = pd.to_datetime(work[x_col], errors="coerce")
        ts = work.groupby(x_col, as_index=False)[value_col].sum().sort_values(x_col)
        fig = px.line(ts, x=x_col, y=value_col, title=f"Evolución de {value_col}")
        _plotly(fig, "custom_line", year_df=ts if x_col == year_col else None, year_col=year_col if x_col == year_col else None)
    cat_candidates = [c for c in ["child_name", "Central", "Barra", "LineName", "category_name"] if c in work.columns]
    if cat_candidates:
        cat = st.selectbox("Categoría para ranking", cat_candidates, key="custom_cat")
        top = work.groupby(cat, as_index=False)[value_col].sum().sort_values(value_col, ascending=False).head(20)
        fig2 = px.bar(top.sort_values(value_col), x=value_col, y=cat, orientation="h", title=f"Ranking por {cat}")
        _plotly(fig2, "custom_rank")



def _cache_dir() -> Path:
    root = Path("workspace") / "cache" / "uploaded_tables"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_cache_key(*parts: str) -> str:
    raw = "|".join([str(p) for p in parts])
    return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()[:24]


def _read_cached_or_build(cache_key: str, builder, label: str) -> tuple[pd.DataFrame, str]:
    cache_path = _cache_dir() / f"{cache_key}.parquet"
    meta_path = _cache_dir() / f"{cache_key}.json"
    if cache_path.exists():
        with st.spinner(f"Leyendo cache rápido para {label}..."):
            try:
                df = pd.read_parquet(cache_path)
                return df, f"{label} (cache parquet)"
            except Exception:
                cache_path.unlink(missing_ok=True)
                meta_path.unlink(missing_ok=True)

    with st.spinner(f"Cargando {label}. Esto puede tardar varios minutos si es XLSX grande..."):
        df = builder()

    # Cache interno solo para acelerar visualización. No modifica salidas Power BI.
    try:
        df.to_parquet(cache_path, index=False)
        meta_path.write_text(json.dumps({"label": label, "rows": int(len(df)), "columns": list(df.columns)}, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return df, label



def _ensure_scenario_from_sheet(df: pd.DataFrame, sheet_name: str) -> pd.DataFrame:
    """Agrega columna Escenario usando el nombre de hoja si no existe."""
    out = df.copy()
    if "Escenario" not in out.columns and "Scenario" not in out.columns:
        out["Escenario"] = sheet_name
    return out


def _sheet_cache_path(file_key: str, sheet_name: str) -> Path:
    safe_sheet = "".join(ch if ch.isalnum() or ch in ("_", "-", ".") else "_" for ch in str(sheet_name))[:80]
    root = _cache_dir() / "xlsx_sheets" / file_key
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{safe_sheet}.parquet"


def _read_excel_sheets_with_cache(
    xlsx_source,
    sheet_names: list[str],
    file_key: str,
    label: str,
    source_kind: str = "uploaded",
) -> tuple[pd.DataFrame, str]:
    """Lee una o varias hojas de Excel y cachea cada hoja como Parquet.

    xlsx_source puede ser bytes o Path. El cache es interno para análisis/visualización.
    """
    if not sheet_names:
        return pd.DataFrame(), label

    frames: list[pd.DataFrame] = []
    progress = st.progress(0, text="Preparando lectura de hojas...")
    status = st.empty()

    for i, sheet in enumerate(sheet_names, start=1):
        cache_path = _sheet_cache_path(file_key, sheet)
        status.info(f"Hoja {i}/{len(sheet_names)}: {sheet}")

        if cache_path.exists():
            try:
                df_sheet = pd.read_parquet(cache_path)
                frames.append(df_sheet)
                progress.progress(i / len(sheet_names), text=f"Hoja {i}/{len(sheet_names)} desde cache: {sheet}")
                continue
            except Exception:
                cache_path.unlink(missing_ok=True)

        # Primera lectura: Excel puede tardar bastante, sobre todo con openpyxl.
        if source_kind == "local":
            df_sheet = pd.read_excel(xlsx_source, sheet_name=sheet)
        else:
            df_sheet = pd.read_excel(BytesIO(xlsx_source), sheet_name=sheet)

        df_sheet = _ensure_scenario_from_sheet(df_sheet, sheet)

        try:
            df_sheet.to_parquet(cache_path, index=False)
        except Exception:
            pass

        frames.append(df_sheet)
        progress.progress(i / len(sheet_names), text=f"Hoja {i}/{len(sheet_names)} leída: {sheet}")

    progress.empty()
    status.empty()

    if not frames:
        return pd.DataFrame(), label

    with st.spinner("Consolidando hojas seleccionadas..."):
        out = pd.concat(frames, ignore_index=True, sort=False)

    return out, f"{label} / {len(sheet_names)} hoja(s)"


def _select_excel_sheets(sheet_names: list[str], key_prefix: str) -> list[str]:
    """Interfaz para seleccionar una, varias o todas las hojas."""
    if not sheet_names:
        return []

    mode = st.radio(
        "Modo de carga del Excel",
        ["Una hoja", "Hojas seleccionadas", "Todas las hojas"],
        horizontal=True,
        key=f"{key_prefix}_xlsx_load_mode",
    )

    if mode == "Una hoja":
        sheet = st.selectbox("Hoja a cargar", sheet_names, key=f"{key_prefix}_single_sheet")
        return [sheet]

    if mode == "Hojas seleccionadas":
        default = sheet_names[: min(len(sheet_names), 5)]
        selected = st.multiselect(
            "Hojas a cargar",
            sheet_names,
            default=default,
            key=f"{key_prefix}_multi_sheet",
        )
        return selected

    st.warning(
        "Cargar todas las hojas puede tardar varios minutos y consumir bastante memoria. "
        "La primera lectura crea cache Parquet; las siguientes serán más rápidas."
    )
    confirm = st.checkbox("Confirmo cargar todas las hojas", key=f"{key_prefix}_all_confirm")
    return sheet_names if confirm else []



def _data_csvs_from_zip(csvs: list[str]) -> list[str]:
    """Filtra CSV auxiliares para visualización; deja resultados de datos."""
    out = []
    for n in csvs:
        base = Path(n).name.lower()
        if base.startswith("diccionario") or base.startswith("manifest") or base.startswith("metadata"):
            continue
        out.append(n)
    return out or csvs


def _select_zip_csvs(csvs: list[str], key_prefix: str) -> list[str]:
    data_csvs = _data_csvs_from_zip(csvs)
    if not data_csvs:
        return []
    mode = st.radio(
        "Modo de carga del ZIP",
        ["Un CSV", "CSV seleccionados", "Todos los CSV de datos"],
        horizontal=True,
        key=f"{key_prefix}_zip_mode",
    )
    if mode == "Un CSV":
        selected = st.selectbox("CSV dentro del ZIP", data_csvs, key=f"{key_prefix}_zip_single")
        return [selected]
    if mode == "CSV seleccionados":
        default = data_csvs[: min(len(data_csvs), 5)]
        return st.multiselect("CSV a cargar", data_csvs, default=default, key=f"{key_prefix}_zip_multi")
    st.warning("Cargar todos los CSV puede consumir mucha memoria si el ZIP es particionado y grande. Para gráficos, selecciona solo los escenarios/casos necesarios.")
    confirm = st.checkbox("Confirmo cargar todos los CSV de datos", key=f"{key_prefix}_zip_all_confirm")
    return data_csvs if confirm else []


def _read_zip_csvs_with_cache(source, selected_csvs: list[str], cache_key: str, label: str, source_kind: str) -> tuple[pd.DataFrame, str]:
    if not selected_csvs:
        return pd.DataFrame(), label

    def build():
        frames = []
        progress = st.progress(0, text="Leyendo CSV del ZIP...")
        if source_kind == "local":
            zctx = zipfile.ZipFile(source)
        else:
            zctx = zipfile.ZipFile(BytesIO(source))
        with zctx as zf:
            for i, name in enumerate(selected_csvs, start=1):
                with zf.open(name) as f:
                    frames.append(pd.read_csv(f))
                progress.progress(i / len(selected_csvs), text=f"CSV {i}/{len(selected_csvs)}: {Path(name).name}")
        progress.empty()
        return pd.concat(frames, ignore_index=True, sort=False) if frames else pd.DataFrame()

    return _read_cached_or_build(cache_key, build, f"{label} / {len(selected_csvs)} CSV")


def _read_local_table(path_str: str) -> tuple[pd.DataFrame, str]:
    path = Path(path_str.strip().strip('"'))
    if not path.exists():
        st.error("La ruta local indicada no existe.")
        return pd.DataFrame(), path_str
    name = path.name.lower()
    stat_key = f"{path.resolve()}|{path.stat().st_size}|{int(path.stat().st_mtime)}"

    if name.endswith(".csv"):
        cache_key = _safe_cache_key("local_csv", stat_key)
        return _read_cached_or_build(cache_key, lambda: pd.read_csv(path), path.name)

    if name.endswith(".parquet"):
        with st.spinner("Leyendo Parquet..."):
            return pd.read_parquet(path), path.name

    if name.endswith(".xlsx"):
        with st.spinner("Leyendo lista de hojas del Excel..."):
            xls = pd.ExcelFile(path)
        selected_sheets = _select_excel_sheets(xls.sheet_names, "local")
        if not selected_sheets:
            st.info("Selecciona al menos una hoja para cargar.")
            return pd.DataFrame(), path.name
        file_key = _safe_cache_key("local_xlsx_file", stat_key)
        return _read_excel_sheets_with_cache(
            path,
            selected_sheets,
            file_key=file_key,
            label=path.name,
            source_kind="local",
        )

    if name.endswith(".zip"):
        with zipfile.ZipFile(path) as zf:
            csvs = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        selected_csvs = _select_zip_csvs(csvs, "local")
        if not selected_csvs:
            st.info("Selecciona al menos un CSV del ZIP para cargar.")
            return pd.DataFrame(), path.name
        cache_key = _safe_cache_key("local_zip", stat_key, "|".join(selected_csvs))
        return _read_zip_csvs_with_cache(path, selected_csvs, cache_key, path.name, source_kind="local")

    st.error("Formato no soportado. Usa CSV, XLSX, ZIP con CSV o Parquet.")
    return pd.DataFrame(), path.name


def _read_uploaded_table(uploaded) -> tuple[pd.DataFrame, str]:
    name = uploaded.name.lower()
    size_mb = getattr(uploaded, "size", 0) / (1024 * 1024)
    if size_mb >= 100:
        st.warning(
            f"Archivo grande detectado ({size_mb:,.1f} MB). "
            "Si es XLSX puede tardar varios minutos. Para análisis rápido se recomienda CSV/ZIP CSV, Parquet o cargar desde ruta local."
        )

    # UploadedFile se lee desde memoria del navegador. Para archivos grandes, se cachea a Parquet después de la primera lectura.
    data = uploaded.getvalue()
    file_hash = hashlib.sha256(data[:2_000_000] + str(len(data)).encode()).hexdigest()[:24]

    if name.endswith(".csv"):
        cache_key = _safe_cache_key("upload_csv", uploaded.name, file_hash)
        return _read_cached_or_build(cache_key, lambda: pd.read_csv(BytesIO(data)), uploaded.name)

    if name.endswith(".xlsx"):
        with st.spinner("Leyendo lista de hojas del Excel..."):
            xls = pd.ExcelFile(BytesIO(data))
        selected_sheets = _select_excel_sheets(xls.sheet_names, "uploaded")
        if not selected_sheets:
            st.info("Selecciona al menos una hoja para cargar.")
            return pd.DataFrame(), uploaded.name
        file_key = _safe_cache_key("upload_xlsx_file", uploaded.name, file_hash)
        return _read_excel_sheets_with_cache(
            data,
            selected_sheets,
            file_key=file_key,
            label=uploaded.name,
            source_kind="uploaded",
        )

    if name.endswith(".zip"):
        with zipfile.ZipFile(BytesIO(data)) as zf:
            csvs = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        selected_csvs = _select_zip_csvs(csvs, "uploaded")
        if not selected_csvs:
            st.info("Selecciona al menos un CSV del ZIP para cargar.")
            return pd.DataFrame(), uploaded.name
        cache_key = _safe_cache_key("upload_zip", uploaded.name, file_hash, "|".join(selected_csvs))
        return _read_zip_csvs_with_cache(data, selected_csvs, cache_key, uploaded.name, source_kind="uploaded")

    return pd.DataFrame(), uploaded.name


def render_visualization_tab(last_result: dict[str, Any] | None = None):
    st.header("Visualización y revisión rápida")
    st.caption("Las visualizaciones usan copias de las tablas procesadas o archivos cargados. No modifican los Excel/CSV compatibles con Power BI.")

    source_mode = st.radio(
        "Fuente de datos",
        ["Último procesamiento", "Cargar archivo exportado", "Cargar desde ruta local"],
        horizontal=True,
    )
    consulta = None
    df = pd.DataFrame()
    resumen = None
    if source_mode == "Último procesamiento":
        if not last_result or last_result.get("df") is None:
            st.info("Ejecuta primero un procesamiento o carga un archivo exportado.")
            return
        consulta = last_result.get("consulta", "Consulta personalizada PLEXOS")
        df = last_result.get("df", pd.DataFrame())
        resumen = last_result.get("resumen")
        st.success(f"Usando último resultado: {consulta}")
        if last_result.get("output_file"):
            st.caption(f"Archivo asociado: {Path(str(last_result.get('output_file'))).name}")
    elif source_mode == "Cargar archivo exportado":
        st.caption("Límite configurado para carga manual: 1024 MB por archivo. Para XLSX grandes puedes cargar una hoja, varias hojas o todas. La primera lectura crea cache Parquet por hoja.")
        uploaded = st.file_uploader("Cargar CSV, XLSX o ZIP de CSV", type=["csv", "xlsx", "zip"], max_upload_size=1024)
        if uploaded is None:
            st.info("Carga un archivo generado por la app para visualizarlo.")
            return
        df, label = _read_uploaded_table(uploaded)
        st.success(f"Archivo cargado: {label}")
        consulta = st.selectbox("Tipo de salida a visualizar", [
            "Costos del sistema",
            "Generación de energía",
            "Capacidad instalada",
            "Demanda de energía (Load)",
            "Flujos de transmisión",
            "Restricciones",
            "Optimización de transmisión (Plan de Transmisión - Units)",
            "Consulta personalizada PLEXOS",
        ])
    else:
        st.caption("Recomendado para archivos grandes: pega la ruta local del CSV, XLSX, ZIP o Parquet. En XLSX puedes cargar una hoja, varias hojas o todas y se cachea por hoja en Parquet.")
        path_str = st.text_input("Ruta local del archivo", placeholder=r"C:\ruta\Tx_Flow_260624.xlsx")
        if not path_str:
            st.info("Indica una ruta local para cargar el archivo.")
            return
        df, label = _read_local_table(path_str)
        if df.empty:
            return
        st.success(f"Archivo cargado: {label}")
        consulta = st.selectbox("Tipo de salida a visualizar", [
            "Costos del sistema",
            "Generación de energía",
            "Capacidad instalada",
            "Demanda de energía (Load)",
            "Flujos de transmisión",
            "Restricciones",
            "Optimización de transmisión (Plan de Transmisión - Units)",
            "Consulta personalizada PLEXOS",
        ], key="local_tipo_salida")

    if df is None or df.empty:
        st.warning("La tabla está vacía.")
        return

    st.caption(f"Filas disponibles para visualización: {len(df):,} | Columnas: {len(df.columns):,}")
    with st.expander("Vista de datos usada para graficar", expanded=False):
        st.dataframe(df.head(1000), width="stretch")

    if consulta == "Costos del sistema":
        _cost_visuals(df, resumen)
    elif consulta == "Generación de energía":
        _generation_visuals(df)
    elif consulta == "Capacidad instalada":
        _capacity_visuals(df)
    elif consulta == "Demanda de energía (Load)":
        _load_visuals(df)
    elif consulta == "Flujos de transmisión":
        _flow_visuals(df)
    elif consulta == "Restricciones":
        _restrictions_visuals(df)
    elif consulta == "Optimización de transmisión (Plan de Transmisión - Units)":
        _tx_plan_visuals(df, resumen)
    else:
        _custom_visuals(df)

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Any
import zipfile

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from src.analytics import scenario_case_label, transmission_utilization_summary, infer_flow_cols

SCENARIO_COLS = ["Escenario", "Scenario"]
CASE_COLS = ["Caso", "caso", "Case"]
YEAR_COLS = ["Fiscal Year", "Year", "Año", "Anio"]
DATE_COLS = ["Fecha", "Date", "Datetime", "_date"]
OBJECT_COLS = ["Central", "Barra", "LineName", "Línea", "category_name", "child_name"]


def _first_existing(columns, candidates):
    cols = list(columns)
    for c in candidates:
        if c in cols:
            return c
    return None


def _numeric_cols(df: pd.DataFrame, exclude: set[str] | None = None) -> list[str]:
    exclude = exclude or set()
    out = []
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


def _read_uploaded_table(uploaded) -> tuple[pd.DataFrame, str]:
    name = uploaded.name.lower()
    if name.endswith(".csv"):
        return pd.read_csv(uploaded), uploaded.name
    if name.endswith(".xlsx"):
        data = uploaded.read()
        xls = pd.ExcelFile(BytesIO(data))
        sheet = st.selectbox("Hoja a cargar", xls.sheet_names, key=f"cmp_sheet_{uploaded.name}")
        return pd.read_excel(BytesIO(data), sheet_name=sheet), f"{uploaded.name} / {sheet}"
    if name.endswith(".zip"):
        data = uploaded.read()
        with zipfile.ZipFile(BytesIO(data)) as zf:
            csvs = [n for n in zf.namelist() if n.lower().endswith(".csv")]
            selected = st.selectbox("CSV dentro del ZIP", csvs, key=f"cmp_zip_{uploaded.name}")
            with zf.open(selected) as f:
                return pd.read_csv(f), f"{uploaded.name} / {selected}"
    return pd.DataFrame(), uploaded.name


def _plotly(fig: go.Figure, key: str):
    fig.update_layout(margin=dict(l=10, r=10, t=60, b=10), hovermode="x unified")
    for trace in fig.data:
        trace.hovertemplate = "%{x}<br>%{y:,.2f}<extra>%{fullData.name}</extra>"
    st.plotly_chart(fig, width="stretch", key=key)


def _select_pair(df: pd.DataFrame, label: str, key: str) -> tuple[pd.DataFrame, str]:
    scen_col = _first_existing(df.columns, SCENARIO_COLS)
    case_col = _first_existing(df.columns, CASE_COLS)
    work = df.copy()
    st.markdown(f"**{label}**")
    c1, c2 = st.columns(2)
    selected_scen = None
    selected_case = None
    if scen_col:
        opts = sorted([str(x) for x in work[scen_col].dropna().unique()])
        selected_scen = c1.selectbox("Escenario", opts, key=f"{key}_scen") if opts else None
        if selected_scen is not None:
            work = work[work[scen_col].astype(str) == selected_scen]
    if case_col:
        opts = sorted([str(x) for x in work[case_col].dropna().unique()])
        selected_case = c2.selectbox("Caso", opts, key=f"{key}_case") if opts else None
        if selected_case is not None:
            work = work[work[case_col].astype(str) == selected_case]
    human_label = scenario_case_label(work, label)
    return work, human_label

def render_comparator_tab(last_result: dict[str, Any] | None = None):
    st.header("Comparador entre escenarios/casos")
    st.caption("Compara dos subconjuntos de una misma salida procesada. No modifica los archivos compatibles con Power BI.")

    source = st.radio("Fuente de datos", ["Último procesamiento", "Cargar archivo"], horizontal=True, key="cmp_source")
    if source == "Último procesamiento":
        if not last_result or last_result.get("df") is None or last_result.get("df").empty:
            st.info("Ejecuta un procesamiento o carga un archivo exportado.")
            return
        df = last_result.get("df").copy()
        st.success(f"Usando último resultado: {last_result.get('consulta')}")
    else:
        uploaded = st.file_uploader("Cargar CSV, XLSX o ZIP de CSV", type=["csv", "xlsx", "zip"], key="cmp_upload", max_upload_size=1024)
        if uploaded is None:
            st.info("Carga un archivo para comparar.")
            return
        df, label = _read_uploaded_table(uploaded)
        st.success(f"Archivo cargado: {label}")

    if df.empty:
        st.warning("La tabla está vacía.")
        return

    year_col = _first_existing(df.columns, YEAR_COLS)
    date_col = _first_existing(df.columns, DATE_COLS)
    x_col = year_col or date_col
    exclude = {x_col or ""}
    metric_cols = _numeric_cols(df, exclude=exclude)
    if not metric_cols:
        st.warning("No se encontraron columnas numéricas comparables.")
        return

    metric = st.selectbox("Métrica a comparar", metric_cols, key="cmp_metric")
    object_candidates = [c for c in OBJECT_COLS if c in df.columns]
    group_mode = st.radio("Nivel de comparación", ["Agregado", "Por objeto"], horizontal=True, key="cmp_group_mode")
    object_col = None
    if group_mode == "Por objeto" and object_candidates:
        object_col = st.selectbox("Objeto", object_candidates, key="cmp_object_col")

    left_col, right_col = st.columns(2)
    with left_col:
        base, base_label_raw = _select_pair(df, "Selección base", "cmp_base")
    with right_col:
        comp, comp_label_raw = _select_pair(df, "Selección comparada", "cmp_comp")

    # Evita colisiones de nombres cuando el usuario deja seleccionada la misma
    # combinación Escenario-Caso en base y comparado. Sin este ajuste, pandas
    # agrega sufijos _x/_y al hacer el merge y luego no encuentra las columnas
    # esperadas para calcular el delta.
    if str(base_label_raw) == str(comp_label_raw):
        base_label = f"{base_label_raw} (base)"
        comp_label = f"{comp_label_raw} (comparado)"
    else:
        base_label = str(base_label_raw)
        comp_label = str(comp_label_raw)

    delta_label = f"Δ ({comp_label}) - ({base_label})"

    for d in (base, comp):
        d[metric] = pd.to_numeric(d[metric], errors="coerce")
        if year_col:
            d[year_col] = pd.to_numeric(d[year_col], errors="coerce").astype("Int64")
        if date_col:
            d[date_col] = pd.to_datetime(d[date_col], errors="coerce")

    dims = []
    if x_col:
        dims.append(x_col)
    if object_col:
        dims.append(object_col)
    if not dims:
        base_val = base[metric].sum()
        comp_val = comp[metric].sum()
        result = pd.DataFrame({"Métrica": [metric], base_label: [base_val], comp_label: [comp_val], delta_label: [comp_val - base_val]})
    else:
        b = base.groupby(dims, as_index=False)[metric].sum().rename(columns={metric: base_label})
        c = comp.groupby(dims, as_index=False)[metric].sum().rename(columns={metric: comp_label})
        result = b.merge(c, on=dims, how="outer").fillna(0)

        # Guardia defensiva: si por algún motivo pandas dejó sufijos (_x/_y),
        # se recuperan como columnas base/comparado antes de calcular delta.
        if base_label not in result.columns:
            candidates = [col for col in result.columns if str(col).startswith(base_label)]
            if candidates:
                result[base_label] = pd.to_numeric(result[candidates[0]], errors="coerce").fillna(0)
        if comp_label not in result.columns:
            candidates = [col for col in result.columns if str(col).startswith(comp_label)]
            if candidates:
                result[comp_label] = pd.to_numeric(result[candidates[0]], errors="coerce").fillna(0)

        if base_label not in result.columns or comp_label not in result.columns:
            st.error("No fue posible construir las columnas base/comparada para calcular el delta. Revisa que las selecciones contengan datos para la métrica escogida.")
            st.dataframe(result.head(1000), width="stretch")
            return

        result[delta_label] = pd.to_numeric(result[comp_label], errors="coerce").fillna(0) - pd.to_numeric(result[base_label], errors="coerce").fillna(0)
        result["Delta %"] = result.apply(lambda r: None if r[base_label] == 0 else (r[delta_label] / r[base_label] * 100), axis=1)

    st.subheader("Resultado comparativo")
    st.dataframe(result.head(1000), width="stretch")

    if not result.empty and {base_label, comp_label, delta_label}.issubset(result.columns):
        total_base = pd.to_numeric(result[base_label], errors="coerce").sum()
        total_comp = pd.to_numeric(result[comp_label], errors="coerce").sum()
        total_delta = total_comp - total_base
        pct = None if total_base == 0 else total_delta / total_base * 100
        m1, m2, m3, m4 = st.columns(4)
        m1.metric(base_label, f"{total_base:,.2f}")
        m2.metric(comp_label, f"{total_comp:,.2f}")
        m3.metric("Delta", f"{total_delta:,.2f}")
        m4.metric("Delta %", "—" if pct is None else f"{pct:,.2f}%")

        if object_col and x_col and len(result) <= 20000:
            with st.expander("Mapa de calor del delta", expanded=False):
                heat = result.pivot_table(index=object_col, columns=x_col, values=delta_label, aggfunc="sum", fill_value=0)
                if len(heat) > 40:
                    top_objects = result.assign(abs_delta=result[delta_label].abs()).groupby(object_col)["abs_delta"].sum().sort_values(ascending=False).head(40).index
                    heat = heat.loc[heat.index.intersection(top_objects)]
                fig_h = px.imshow(heat, aspect="auto", title=f"Delta por {object_col} y {x_col}", color_continuous_scale="RdBu_r")
                st.plotly_chart(fig_h, width="stretch", key="cmp_heatmap")
        if object_col and any(str(c).startswith("Flow_") or c == "Flow" for c in df.columns):
            with st.expander("Diagnóstico de transmisión para las selecciones", expanded=False):
                line_col, flow_guess, import_col, export_col = infer_flow_cols(df)
                flow_metric = metric if metric == flow_guess or str(metric).startswith("Flow_") else flow_guess
                if line_col and flow_metric and (import_col or export_col):
                    pos_dir = st.text_input("Etiqueta flujo positivo", value="Flujo positivo / sentido asociado a Import Limit", key="cmp_pos_dir")
                    neg_dir = st.text_input("Etiqueta flujo negativo", value="Flujo negativo / sentido asociado a Export Limit", key="cmp_neg_dir")
                    _, util_b = transmission_utilization_summary(base, line_col, flow_metric, import_col, export_col, pos_dir, neg_dir)
                    _, util_c = transmission_utilization_summary(comp, line_col, flow_metric, import_col, export_col, pos_dir, neg_dir)
                    util_b = util_b.rename(columns={"P95_utilizacion_pct": f"P95 {base_label}", "Utilizacion_promedio_pct": f"Promedio {base_label}"})
                    util_c = util_c.rename(columns={"P95_utilizacion_pct": f"P95 {comp_label}", "Utilizacion_promedio_pct": f"Promedio {comp_label}"})
                    util_cmp = util_b[[line_col, f"P95 {base_label}", f"Promedio {base_label}"]].merge(
                        util_c[[line_col, f"P95 {comp_label}", f"Promedio {comp_label}"]], on=line_col, how="outer"
                    ).fillna(0)
                    util_cmp["Delta P95"] = util_cmp[f"P95 {comp_label}"] - util_cmp[f"P95 {base_label}"]
                    st.dataframe(util_cmp.sort_values("Delta P95", key=lambda x: x.abs(), ascending=False).head(100), width="stretch")
                else:
                    st.info("Para este diagnóstico se requieren columnas de línea, flujo e Import/Export Limit.")

    csv = result.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
    st.download_button("Descargar comparación CSV", data=csv, file_name="comparacion_plexos.csv", mime="text/csv")

    if x_col and not object_col:
        plot = result.sort_values(x_col)
        fig = px.line(plot, x=x_col, y=[base_label, comp_label, delta_label], markers=True, title=f"Comparación agregada - {metric}")
        _plotly(fig, "cmp_line")
    elif object_col:
        topn = st.slider("Top objetos por delta absoluto", 5, 30, 15, key="cmp_topn")
        aux = result.copy()
        aux["abs_delta"] = aux[delta_label].abs()
        top = aux.groupby(object_col, as_index=False)[delta_label].sum()
        top["abs_delta"] = top[delta_label].abs()
        top = top.sort_values("abs_delta", ascending=False).head(topn)
        fig = px.bar(top.sort_values(delta_label), x=delta_label, y=object_col, orientation="h", title=f"Top {topn} diferencias por {object_col}")
        _plotly(fig, "cmp_bar")

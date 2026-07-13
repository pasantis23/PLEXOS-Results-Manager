
from __future__ import annotations

from pathlib import Path
from typing import Iterable, Any
import zipfile
import tempfile

import pandas as pd
import streamlit as st
import plotly.express as px

from .exporter import safe_filename
from .analytics import infer_line_directions

PREVIEW_ROWS = 1000
CHUNKSIZE = 300_000

SCENARIO_CANDIDATES = ["Escenario", "Scenario", "scenario"]
CASE_CANDIDATES = ["Case", "caso", "Caso", "case"]
DATE_CANDIDATES = ["Fecha", "Date", "date", "_date"]
LINE_CANDIDATES = ["LineName", "Line", "Línea", "Linea", "child_name"]
YEAR_CANDIDATES = ["Fiscal Year", "Year", "Año", "Anio"]
TYPE_CANDIDATES = ["Tipo", "Tipo 2", "Technology", "Tecnología", "Tecnologia"]


def _find_col(df: pd.DataFrame, candidates: list[str]) -> str | None:
    cols = {str(c).lower(): c for c in df.columns}
    for c in candidates:
        if c.lower() in cols:
            return cols[c.lower()]
    return None


def _numeric_cols(df: pd.DataFrame, prefixes: tuple[str, ...] = ()) -> list[str]:
    out = []
    for c in df.columns:
        if prefixes and not any(str(c).startswith(p) for p in prefixes):
            continue
        s = pd.to_numeric(df[c], errors="coerce")
        if s.notna().any():
            out.append(c)
    return out


def _read_table(path: Path, sheet: str | None = None, zip_member: str | None = None, nrows: int | None = None) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return pd.read_csv(path, nrows=nrows)
    if suffix == ".xlsx":
        return pd.read_excel(path, sheet_name=sheet or 0, nrows=nrows)
    if suffix == ".zip":
        with zipfile.ZipFile(path) as zf:
            members = [n for n in zf.namelist() if n.lower().endswith(".csv")]
            if not members:
                return pd.DataFrame()
            member = zip_member or members[0]
            with zf.open(member) as f:
                return pd.read_csv(f, nrows=nrows)
    return pd.DataFrame()


def _iter_csv_sources(path: Path, selected_members: list[str] | None = None):
    suffix = path.suffix.lower()
    if suffix == ".csv":
        yield path.name, path
    elif suffix == ".zip":
        with zipfile.ZipFile(path) as zf:
            members = [n for n in zf.namelist() if n.lower().endswith(".csv")]
            if selected_members:
                allowed = set(selected_members)
                members = [m for m in members if m in allowed]
            for m in members:
                with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as tmp:
                    tmp.write(zf.read(m))
                    tmp_path = Path(tmp.name)
                try:
                    yield m, tmp_path
                finally:
                    tmp_path.unlink(missing_ok=True)


def inspect_file(path_str: str) -> tuple[Path | None, pd.DataFrame, dict[str, Any]]:
    if not path_str or not path_str.strip():
        return None, pd.DataFrame(), {}
    path = Path(path_str.strip().strip('"'))
    if not path.exists():
        return path, pd.DataFrame(), {"error": "La ruta no existe"}
    meta: dict[str, Any] = {"archivo": path.name, "extension": path.suffix.lower(), "size_mb": round(path.stat().st_size / (1024*1024), 2)}
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as zf:
            members = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        meta["csv_en_zip"] = len(members)
        meta["csv_preview"] = members[:8]
        df = _read_table(path, zip_member=members[0] if members else None, nrows=5000)
    elif path.suffix.lower() == ".xlsx":
        xls = pd.ExcelFile(path)
        meta["hojas"] = xls.sheet_names
        df = pd.read_excel(path, sheet_name=xls.sheet_names[0], nrows=5000)
    else:
        df = _read_table(path, nrows=5000)
    meta["columnas"] = list(df.columns)
    meta["columnas_detectadas"] = {
        "escenario": _find_col(df, SCENARIO_CANDIDATES),
        "caso": _find_col(df, CASE_CANDIDATES),
        "fecha": _find_col(df, DATE_CANDIDATES),
        "linea": _find_col(df, LINE_CANDIDATES),
        "anio": _find_col(df, YEAR_CANDIDATES),
        "tipo": _find_col(df, TYPE_CANDIDATES),
    }
    return path, df, meta


def _std_keys(df: pd.DataFrame) -> tuple[str | None, str | None]:
    return _find_col(df, SCENARIO_CANDIDATES), _find_col(df, CASE_CANDIDATES)


def summarize_costs(path: Path, sheet: str | None = None) -> pd.DataFrame:
    df = _read_table(path, sheet=sheet)
    if df.empty:
        return pd.DataFrame()
    sc, ca = _std_keys(df)
    if sc is None or ca is None:
        return pd.DataFrame()
    year = _find_col(df, YEAR_CANDIDATES)
    num = [c for c in df.columns if c not in {year} and pd.to_numeric(df[c], errors="coerce").notna().any()]
    keep = [sc, ca] + ([year] if year else []) + num
    work = df[keep].copy()
    for c in num:
        work[c] = pd.to_numeric(work[c], errors="coerce")
    agg = work.groupby([sc, ca], dropna=False)[num].sum(numeric_only=True).reset_index()
    return agg.rename(columns={sc: "Escenario", ca: "Case"})


def summarize_generation(path: Path, value_col: str | None = None, member_limit: int = 200) -> pd.DataFrame:
    # Para ZIP particionados o CSV grandes se resume por chunks. Para XLSX se lee completo porque normalmente es anual o acotado.
    if path.suffix.lower() == ".xlsx":
        df = _read_table(path)
        return _summarize_generation_df(df, value_col)
    rows = []
    sources = list(_iter_csv_sources(path))[:member_limit]
    for name, csv_path in sources:
        for chunk in pd.read_csv(csv_path, chunksize=CHUNKSIZE):
            part = _summarize_generation_df(chunk, value_col)
            if not part.empty:
                rows.append(part)
    if not rows:
        return pd.DataFrame()
    out = pd.concat(rows, ignore_index=True)
    group_cols = [c for c in ["Escenario", "Case", "Grupo"] if c in out.columns]
    val = "Generation [MWh]"
    return out.groupby(group_cols, dropna=False)[val].sum().reset_index()


def _summarize_generation_df(df: pd.DataFrame, value_col: str | None = None) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    sc, ca = _std_keys(df)
    if sc is None or ca is None:
        return pd.DataFrame()
    type_col = _find_col(df, TYPE_CANDIDATES)
    value_candidates = [value_col] if value_col else []
    value_candidates += [c for c in df.columns if "Generation" in str(c) or "Generacion" in str(c) or "Generación" in str(c)]
    value_candidates += _numeric_cols(df)
    value_candidates = [c for c in value_candidates if c and c in df.columns]
    if not value_candidates:
        return pd.DataFrame()
    val = value_candidates[0]
    work = pd.DataFrame({"Escenario": df[sc], "Case": df[ca], "Generation [MWh]": pd.to_numeric(df[val], errors="coerce")})
    work["Grupo"] = df[type_col].astype(str) if type_col else "Total"
    return work.groupby(["Escenario", "Case", "Grupo"], dropna=False)["Generation [MWh]"].sum().reset_index()


def summarize_restrictions(path: Path, member_limit: int = 200) -> pd.DataFrame:
    rows = []
    if path.suffix.lower() == ".xlsx":
        df = _read_table(path)
        return _summarize_restrictions_df(df)
    for name, csv_path in list(_iter_csv_sources(path))[:member_limit]:
        for chunk in pd.read_csv(csv_path, chunksize=CHUNKSIZE):
            part = _summarize_restrictions_df(chunk)
            if not part.empty:
                rows.append(part)
    if not rows:
        return pd.DataFrame()
    out = pd.concat(rows, ignore_index=True)
    return out.groupby(["Escenario", "Case", "category_name"], dropna=False).agg(
        horas_activas=("horas_activas", "sum"), magnitud_total=("magnitud_total", "sum")
    ).reset_index()


def _summarize_restrictions_df(df: pd.DataFrame) -> pd.DataFrame:
    sc, ca = _std_keys(df)
    cat = "category_name" if "category_name" in df.columns else None
    if df.empty or sc is None or ca is None or cat is None:
        return pd.DataFrame()
    value_cols = [c for c in df.columns if str(c).startswith("Value_")]
    if not value_cols:
        value_cols = _numeric_cols(df)
    if not value_cols:
        return pd.DataFrame()
    vals = df[value_cols].apply(pd.to_numeric, errors="coerce").abs().sum(axis=1)
    work = pd.DataFrame({"Escenario": df[sc], "Case": df[ca], "category_name": df[cat], "magnitud_total": vals, "horas_activas": vals.gt(0).astype(int)})
    return work.groupby(["Escenario", "Case", "category_name"], dropna=False).sum(numeric_only=True).reset_index()


def summarize_flow(path: Path, line_filter: list[str] | None = None, flow_col: str | None = None, member_limit: int = 200) -> pd.DataFrame:
    rows = []
    if path.suffix.lower() == ".xlsx":
        df = _read_table(path)
        return _summarize_flow_df(df, line_filter, flow_col)
    for name, csv_path in list(_iter_csv_sources(path))[:member_limit]:
        for chunk in pd.read_csv(csv_path, chunksize=CHUNKSIZE):
            part = _summarize_flow_df(chunk, line_filter, flow_col)
            if not part.empty:
                rows.append(part)
    if not rows:
        return pd.DataFrame()
    out = pd.concat(rows, ignore_index=True)
    group_cols = ["Escenario", "Case", "LineName"]
    return out.groupby(group_cols, dropna=False).agg(
        registros=("registros", "sum"),
        flujo_abs_promedio=("flujo_abs_sum", lambda x: x.sum()),
        flujo_abs_max=("flujo_abs_max", "max"),
        horas_80=("horas_80", "sum"),
        horas_90=("horas_90", "sum"),
        horas_pos=("horas_pos", "sum"),
        horas_neg=("horas_neg", "sum"),
        util_sum=("util_sum", "sum"),
        util_max=("util_max", "max"),
    ).reset_index().pipe(_finalize_flow_summary)


def _summarize_flow_df(df: pd.DataFrame, line_filter: list[str] | None = None, flow_col: str | None = None) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    sc, ca = _std_keys(df)
    line = _find_col(df, LINE_CANDIDATES)
    if sc is None or ca is None or line is None:
        return pd.DataFrame()
    if line_filter:
        df = df[df[line].astype(str).isin(set(line_filter))]
        if df.empty:
            return pd.DataFrame()
    flow_candidates = [flow_col] if flow_col else []
    flow_candidates += [c for c in df.columns if str(c).startswith("Flow_")]
    flow_candidates += [c for c in df.columns if str(c).lower() in {"flow", "flujo"}]
    flow_candidates = [c for c in flow_candidates if c and c in df.columns]
    if not flow_candidates:
        return pd.DataFrame()
    fc = flow_candidates[0]
    flow = pd.to_numeric(df[fc], errors="coerce")
    imp = pd.to_numeric(df.get("Import Limit [MW]", pd.Series(index=df.index, dtype=float)), errors="coerce").abs()
    exp = pd.to_numeric(df.get("Export Limit [MW]", pd.Series(index=df.index, dtype=float)), errors="coerce").abs()
    denom = imp.where(flow >= 0, exp).replace(0, pd.NA)
    util = (flow.abs() / denom).astype("float64")
    work = pd.DataFrame({
        "Escenario": df[sc], "Case": df[ca], "LineName": df[line].astype(str),
        "registros": 1,
        "flujo_abs_sum": flow.abs(),
        "flujo_abs_max": flow.abs(),
        "horas_80": util.ge(0.8).fillna(False).astype(int),
        "horas_90": util.ge(0.9).fillna(False).astype(int),
        "horas_pos": flow.gt(0).fillna(False).astype(int),
        "horas_neg": flow.lt(0).fillna(False).astype(int),
        "util_sum": util.fillna(0),
        "util_max": util,
    })
    return work.groupby(["Escenario", "Case", "LineName"], dropna=False).agg(
        registros=("registros", "sum"), flujo_abs_sum=("flujo_abs_sum", "sum"), flujo_abs_max=("flujo_abs_max", "max"),
        horas_80=("horas_80", "sum"), horas_90=("horas_90", "sum"), horas_pos=("horas_pos", "sum"), horas_neg=("horas_neg", "sum"),
        util_sum=("util_sum", "sum"), util_max=("util_max", "max")
    ).reset_index()


def _finalize_flow_summary(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if "flujo_abs_sum" in out.columns:
        out["flujo_abs_promedio"] = out["flujo_abs_sum"] / out["registros"].replace(0, pd.NA)
    else:
        out["flujo_abs_promedio"] = out["flujo_abs_promedio"] / out["registros"].replace(0, pd.NA)
    out["util_promedio"] = out["util_sum"] / out["registros"].replace(0, pd.NA)
    out["sentido_dominante"] = out.apply(lambda r: "positivo" if r.get("horas_pos",0) >= r.get("horas_neg",0) else "negativo", axis=1)
    out["diagnostico"] = out.apply(_flow_diagnosis, axis=1)
    return out.drop(columns=[c for c in ["flujo_abs_sum", "util_sum"] if c in out.columns])


def _flow_diagnosis(r: pd.Series) -> str:
    registros = max(float(r.get("registros", 0) or 0), 1.0)
    p90 = float(r.get("horas_90", 0) or 0) / registros
    p80 = float(r.get("horas_80", 0) or 0) / registros
    util = float(r.get("util_promedio", 0) or 0)
    if p90 > 0.05 or float(r.get("util_max", 0) or 0) >= 1.0:
        return "Crítica / revisar límite"
    if p80 > 0.05 or util >= 0.6:
        return "Uso alto recurrente"
    if util < 0.1 and float(r.get("horas_80", 0) or 0) == 0:
        return "Subutilizada"
    return "Uso medio/bajo"


def _entity_selector(summary: pd.DataFrame, label: str, key: str) -> tuple[str, str]:
    if summary.empty:
        return "", ""
    pairs = summary[["Escenario", "Case"]].drop_duplicates().astype(str)
    labels = [f"{r.Escenario} | {r.Case}" for r in pairs.itertuples(index=False)]
    if not labels:
        return "", ""
    selected = st.selectbox(label, labels, key=key)
    sc, ca = selected.split(" | ", 1)
    return sc, ca


def _delta_table(summary: pd.DataFrame, base: tuple[str, str], comp: tuple[str, str], value_cols: list[str], by: list[str] | None = None) -> pd.DataFrame:
    if summary.empty or not value_cols:
        return pd.DataFrame()
    by = by or []
    b = summary[(summary["Escenario"].astype(str) == base[0]) & (summary["Case"].astype(str) == base[1])]
    c = summary[(summary["Escenario"].astype(str) == comp[0]) & (summary["Case"].astype(str) == comp[1])]
    if by:
        b = b[by + value_cols].groupby(by, dropna=False).sum(numeric_only=True).reset_index()
        c = c[by + value_cols].groupby(by, dropna=False).sum(numeric_only=True).reset_index()
        out = pd.merge(b, c, on=by, how="outer", suffixes=("_base", "_comp")).fillna(0)
    else:
        bvals = b[value_cols].sum(numeric_only=True).to_frame().T.add_suffix("_base")
        cvals = c[value_cols].sum(numeric_only=True).to_frame().T.add_suffix("_comp")
        out = pd.concat([bvals, cvals], axis=1)
    for col in value_cols:
        out[f"Δ {col}"] = out.get(f"{col}_comp", 0) - out.get(f"{col}_base", 0)
        denom = out.get(f"{col}_base", 0).replace(0, pd.NA) if isinstance(out.get(f"{col}_base", 0), pd.Series) else pd.NA
        try:
            out[f"Δ% {col}"] = out[f"Δ {col}"] / denom
        except Exception:
            pass
    return out


def render_decision_comparator_tab():
    st.header("Análisis técnico para decisión")
    st.caption("Compara escenarios/casos con foco en drivers: costos, generación, transmisión y restricciones. Lee datos ya exportados; no consulta PLEXOS ni reprocesa soluciones.")

    with st.expander("1) Cargar archivos de salida", expanded=True):
        c1, c2 = st.columns(2)
        with c1:
            cost_path = st.text_input("Ruta Costos sistema (XLSX/CSV/ZIP)", key="adv_cost_path")
            gen_path = st.text_input("Ruta Generación anual u horaria (XLSX/CSV/ZIP)", key="adv_gen_path")
        with c2:
            flow_path = st.text_input("Ruta Flujos Tx, opcional (XLSX/CSV/ZIP)", key="adv_flow_path")
            rest_path = st.text_input("Ruta Restricciones, opcional (XLSX/CSV/ZIP)", key="adv_rest_path")
        st.info("Para archivos grandes usa rutas locales. La lectura se resume por chunks cuando el archivo es CSV/ZIP para evitar cargar todo en memoria.")

    summaries: dict[str, pd.DataFrame] = {}
    metas = []
    if cost_path.strip():
        path, sample, meta = inspect_file(cost_path)
        metas.append({"tipo": "Costos", **meta})
        if path and path.exists() and st.button("Resumir costos", key="sum_costs"):
            summaries["costos"] = summarize_costs(path)
            st.session_state["adv_sum_costos"] = summaries["costos"]
        summaries["costos"] = st.session_state.get("adv_sum_costos", pd.DataFrame())
    if gen_path.strip():
        path, sample, meta = inspect_file(gen_path)
        metas.append({"tipo": "Generación", **meta})
        if path and path.exists() and st.button("Resumir generación", key="sum_gen"):
            summaries["generacion"] = summarize_generation(path)
            st.session_state["adv_sum_generacion"] = summaries["generacion"]
        summaries["generacion"] = st.session_state.get("adv_sum_generacion", pd.DataFrame())
    if flow_path.strip():
        path, sample, meta = inspect_file(flow_path)
        metas.append({"tipo": "Flujos Tx", **meta})
        line_col = meta.get("columnas_detectadas", {}).get("linea") if meta else None
        line_options = []
        if isinstance(sample, pd.DataFrame) and not sample.empty and line_col in sample.columns:
            line_options = sorted(sample[line_col].astype(str).dropna().unique().tolist())[:500]
        selected_lines = st.multiselect("Líneas a analizar en Tx", line_options, default=line_options[:1], key="adv_flow_lines") if line_options else []
        if path and path.exists() and st.button("Resumir flujos Tx", key="sum_flow"):
            summaries["flow"] = summarize_flow(path, line_filter=selected_lines or None)
            st.session_state["adv_sum_flow"] = summaries["flow"]
        summaries["flow"] = st.session_state.get("adv_sum_flow", pd.DataFrame())
    if rest_path.strip():
        path, sample, meta = inspect_file(rest_path)
        metas.append({"tipo": "Restricciones", **meta})
        if path and path.exists() and st.button("Resumir restricciones", key="sum_rest"):
            summaries["restricciones"] = summarize_restrictions(path)
            st.session_state["adv_sum_restricciones"] = summaries["restricciones"]
        summaries["restricciones"] = st.session_state.get("adv_sum_restricciones", pd.DataFrame())

    if metas:
        with st.expander("Diagnóstico de archivos cargados", expanded=False):
            st.dataframe(pd.DataFrame(metas), width="stretch")

    available = [k for k, v in summaries.items() if isinstance(v, pd.DataFrame) and not v.empty]
    if not available:
        st.warning("Carga y resume al menos un archivo. El comparador no inventa resultados: solo compara datos disponibles.")
        return

    # Base/comparado desde la primera tabla disponible.
    ref = summaries[available[0]]
    st.subheader("2) Selección base vs comparado")
    c1, c2 = st.columns(2)
    with c1:
        base = _entity_selector(ref, "Caso base", "adv_base")
    with c2:
        comp = _entity_selector(ref, "Caso comparado", "adv_comp")
    if not all(base + comp):
        st.info("Selecciona caso base y comparado.")
        return

    st.subheader("3) Lectura ejecutiva basada en datos")
    bullets = []

    if not summaries.get("costos", pd.DataFrame()).empty:
        cost = summaries["costos"]
        value_cols = [c for c in cost.columns if c not in {"Escenario", "Case"} and pd.api.types.is_numeric_dtype(cost[c])]
        dt = _delta_table(cost, base, comp, value_cols)
        if not dt.empty:
            st.markdown("**Costos y drivers**")
            st.dataframe(dt.T.rename(columns={0: "Valor"}), width="stretch")
            delta_cols = [c for c in dt.columns if c.startswith("Δ ")]
            if delta_cols:
                impacts = dt[delta_cols].iloc[0].sort_values(key=lambda s: s.abs(), ascending=False)
                top = impacts.index[0].replace("Δ ", "")
                val = impacts.iloc[0]
                direction = "aumenta" if val > 0 else "disminuye"
                bullets.append(f"El principal cambio económico detectado es `{top}`, que {direction} en {val:,.2f} respecto del caso base.")
                fig = px.bar(impacts.reset_index(), x="index", y=0, title="Deltas de costos por componente")
                st.plotly_chart(fig, width="stretch")

    if not summaries.get("generacion", pd.DataFrame()).empty:
        gen = summaries["generacion"]
        dt = _delta_table(gen, base, comp, ["Generation [MWh]"], by=["Grupo"])
        if not dt.empty:
            st.markdown("**Generación**")
            st.dataframe(dt.sort_values("Δ Generation [MWh]", key=lambda s: s.abs(), ascending=False).head(20), width="stretch")
            top = dt.sort_values("Δ Generation [MWh]", key=lambda s: s.abs(), ascending=False).head(1)
            if not top.empty:
                grp = top.iloc[0].get("Grupo")
                val = top.iloc[0].get("Δ Generation [MWh]", 0)
                direction = "sube" if val > 0 else "baja"
                bullets.append(f"La mayor variación de generación se observa en `{grp}`: {direction} {val:,.2f} MWh.")
            fig = px.bar(dt.sort_values("Δ Generation [MWh]", key=lambda s: s.abs(), ascending=False).head(15), x="Grupo", y="Δ Generation [MWh]", title="Cambio de generación por grupo")
            st.plotly_chart(fig, width="stretch")

    if not summaries.get("flow", pd.DataFrame()).empty:
        flow = summaries["flow"]
        st.markdown("**Transmisión**")
        base_flow = flow[(flow["Escenario"].astype(str)==base[0]) & (flow["Case"].astype(str)==base[1])]
        comp_flow = flow[(flow["Escenario"].astype(str)==comp[0]) & (flow["Case"].astype(str)==comp[1])]
        show = pd.concat([base_flow.assign(Comparación="Base"), comp_flow.assign(Comparación="Comparado")], ignore_index=True)
        st.dataframe(show.sort_values(["diagnostico", "util_max"], ascending=[True, False]).head(50), width="stretch")
        critical = comp_flow[comp_flow["diagnostico"].astype(str).str.contains("Crítica|alto", case=False, na=False)]
        if not critical.empty:
            line = critical.sort_values("util_max", ascending=False).iloc[0]
            bullets.append(f"En transmisión, `{line['LineName']}` queda como `{line['diagnostico']}` con utilización máxima {line.get('util_max',0):.2%} y {int(line.get('horas_90',0))} registros sobre 90%.")
        else:
            bullets.append("No se detecta una línea crítica en la muestra de transmisión resumida; revisar si se filtraron las líneas relevantes.")

    if not summaries.get("restricciones", pd.DataFrame()).empty:
        rest = summaries["restricciones"]
        dt = _delta_table(rest, base, comp, ["horas_activas", "magnitud_total"], by=["category_name"])
        if not dt.empty:
            st.markdown("**Restricciones**")
            st.dataframe(dt.sort_values("Δ horas_activas", key=lambda s: s.abs(), ascending=False).head(20), width="stretch")
            top = dt.sort_values("Δ horas_activas", key=lambda s: s.abs(), ascending=False).head(1)
            if not top.empty:
                cat = top.iloc[0].get("category_name")
                val = top.iloc[0].get("Δ horas_activas", 0)
                direction = "aumentan" if val > 0 else "disminuyen"
                bullets.append(f"Las restricciones `{cat}` {direction} en {val:,.0f} registros activos respecto del caso base.")

    st.subheader("Conclusión preliminar")
    if bullets:
        for b in bullets:
            st.markdown(f"- {b}")
    else:
        st.info("No hay suficientes columnas estándar para emitir lectura. Revisa diagnóstico de archivos y columnas detectadas.")
    st.caption("La conclusión es preliminar y depende de los archivos cargados. La app evita procesar todo en memoria; para Flow y Restricciones usa resúmenes por chunks.")

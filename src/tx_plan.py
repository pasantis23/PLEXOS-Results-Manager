from __future__ import annotations

from pathlib import Path
from typing import Callable
import pandas as pd

from .plexos_api import PlexosEnv, query_result_to_df, get_sample_id_list, as_number, parse_plexos_date, is_plexos_day_id_error
from .discovery import SolutionFile
from .demo_data import demo_tx_plan


def _contiguous_year_blocks(years: list[int] | None) -> list[tuple[int, int]]:
    years_int = sorted({int(y) for y in years or []})
    if not years_int:
        return []
    blocks: list[tuple[int, int]] = []
    start = prev = years_int[0]
    for y in years_int[1:]:
        if y == prev + 1:
            prev = y
        else:
            blocks.append((start, prev))
            start = prev = y
    blocks.append((start, prev))
    return blocks


def _filter_df_years(df: pd.DataFrame, years: list[int] | None, date_col: str = "_date") -> pd.DataFrame:
    years_int = sorted({int(y) for y in years or []})
    if not years_int or df.empty or date_col not in df.columns:
        return df
    parsed = parse_plexos_date(df[date_col])
    return df[parsed.dt.year.isin(years_int)].copy()



ProgressCallback = Callable[[int, int, str], None]

LINE_META_COLS = ["LineName", "Tipo", "Zona", "Regional", "Max Flow", "VI (MMUSD)"]
LINE_META_COLS_STAGE2 = ["LineName", "Tipo", "Zona", "Max Flow", "Regional", "VI (MMUSD)"]


def process_line_units(sol_file: str | Path, model_name: str, env: PlexosEnv, sample: str = "mean", phase: str = "LTPlan", period_yearly: str = "FiscalYear", series_type: str = "Values", years: list[int] | None = None) -> pd.DataFrame:
    """Replica process_line_units del notebook: LineName, Fiscal Year, category_name, Units, Model."""
    sol = env.open_solution(sol_file)
    try:
        collections = sol.FetchAllCollectionIds()
        sample_id = get_sample_id_list(sol, [sample])
        prop_id = sol.PropertyName2EnumId("System", "Line", "Lines", "Units")
        cols = ["collection_name", "child_name", "category_name", "_date", "value"]
        years_int = sorted({int(y) for y in years or []})
        blocks = _contiguous_year_blocks(years_int)

        def _query(start_date, end_date):
            return sol.QueryToList(
                env.phase(phase),
                collections["SystemLines"],
                "", "",
                env.period(period_yearly),
                env.series_type(series_type),
                str(prop_id),
                start_date, end_date,
                "0",
                sample_id,
            )

        frames: list[pd.DataFrame] = []
        fallback_all = False
        if blocks:
            for y0, y1 in blocks:
                attempts = [(f"01-01-{y0}", f"31-12-{y1}")]
                attempts += [(f"01-01-{y0}", f"{d:02d}-12-{y1}") for d in (30, 29, 28)]
                ok = False
                for start_date, end_date in attempts:
                    try:
                        res = _query(start_date, end_date)
                        df_block = query_result_to_df(res, cols, env.String) if res else pd.DataFrame(columns=cols)
                        if df_block.empty:
                            fallback_all = True
                            ok = False
                            break
                        frames.append(df_block)
                        ok = True
                        break
                    except Exception as exc:
                        if is_plexos_day_id_error(exc):
                            continue
                        fallback_all = True
                        ok = False
                        break
                if fallback_all or not ok:
                    fallback_all = True
                    break
        if not blocks or fallback_all:
            results = _query(None, None)
            df = query_result_to_df(results, cols, env.String) if results else pd.DataFrame(columns=cols)
        else:
            df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=cols)
        df = _filter_df_years(df, years_int)
    finally:
        sol.Close()

    if df.empty:
        return pd.DataFrame(columns=["LineName", "Fiscal Year", "category_name", "Units", "Model"])
    df["_date"] = parse_plexos_date(df["_date"])
    df["Fiscal Year"] = df["_date"].dt.year
    df["Units"] = as_number(df["value"]).round(2)
    df.rename(columns={"child_name": "LineName"}, inplace=True)
    df = df[["LineName", "Fiscal Year", "category_name", "Units"]]
    df["Model"] = model_name
    return df


def read_line_dictionary(path_diccionario: str | Path | None, sheet_name: str = "Cap_Tx") -> pd.DataFrame:
    if not path_diccionario:
        return pd.DataFrame()
    path = Path(path_diccionario)
    if not path.exists():
        return pd.DataFrame()
    df_plan = pd.read_excel(path, sheet_name=sheet_name, engine="openpyxl")
    df_plan = df_plan.rename(columns={
        "Línea": "LineName",
        "Linea": "LineName",
        "Line": "LineName",
        "Line Name": "LineName",
    })
    keep = [c for c in LINE_META_COLS if c in df_plan.columns]
    if "LineName" not in keep:
        return pd.DataFrame()
    return df_plan[keep].drop_duplicates("LineName")


def merge_line_dictionary(df: pd.DataFrame, path_diccionario: str | Path | None, sheet_name: str = "Cap_Tx") -> pd.DataFrame:
    df_plan = read_line_dictionary(path_diccionario, sheet_name)
    if df.empty or df_plan.empty:
        return df
    return df.merge(df_plan, on="LineName", how="left")


def filter_category(df: pd.DataFrame, category: str | None = None) -> pd.DataFrame:
    if df.empty or not category or "category_name" not in df.columns:
        return df
    return df[df["category_name"].astype(str).eq(str(category))].reset_index(drop=True)


def pivot_units_by_model(all_models: dict[str, pd.DataFrame], category_filter: str | None) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    for scenario, df in all_models.items():
        df_filt = filter_category(df, category_filter)
        index_cols = [c for c in ["LineName", "Tipo", "Zona", "Regional", "Max Flow", "VI (MMUSD)"] if c in df_filt.columns]
        if "LineName" not in index_cols:
            index_cols = ["LineName"]
        df_piv = df_filt.pivot_table(index=index_cols, columns="Fiscal Year", values="Units", fill_value=0).reset_index()
        out[scenario] = df_piv
    return out


def build_resumen_original(
    all_models: dict[str, pd.DataFrame],
    df_plan: pd.DataFrame,
    category_filter: str | None = "Sistema-Tx-Evaluacion_Opt",
    threshold: float = 0.3,
    floor_interregional: int = 2032,
    floor_zonal: int = 2030,
) -> pd.DataFrame:
    if df_plan.empty:
        # Si no hay diccionario, crear base desde líneas presentes.
        line_names = sorted({line for df in all_models.values() for line in df.get("LineName", pd.Series(dtype=str)).dropna().unique()})
        df_plan = pd.DataFrame({"LineName": line_names})

    base_cols = [c for c in ["LineName", "Tipo", "Zona", "Max Flow", "VI (MMUSD)", "Regional"] if c in df_plan.columns]
    if "LineName" not in base_cols:
        base_cols = ["LineName"]
    lines = df_plan.drop_duplicates("LineName").set_index("LineName")
    final_rows = []
    for line, attrs in lines.iterrows():
        regionalidad = attrs.get("Regional", "") if hasattr(attrs, "get") else ""
        piso = floor_interregional if str(regionalidad).strip() == "Interregional" else floor_zonal
        row = {"LineName": line}
        for col in ["Tipo", "Zona", "Max Flow", "VI (MMUSD)", "Regional"]:
            if col in attrs.index:
                row[col] = attrs[col]
        for scenario, df_model in all_models.items():
            df_sc = filter_category(df_model, category_filter)
            act = df_sc[df_sc["Units"] > threshold].groupby("LineName")["Fiscal Year"].min()
            if line in act.index:
                real_year = int(act[line])
                row[scenario] = real_year if real_year >= piso else piso
            else:
                row[scenario] = ""
        final_rows.append(row)

    df_final = pd.DataFrame(final_rows)
    scenario_cols = list(all_models.keys())
    if scenario_cols:
        mask_all_empty = df_final[scenario_cols].eq("").all(axis=1)
        df_final = df_final[~mask_all_empty]
    # Orden como notebook: LineName, Tipo, Zona, Max Flow, VI, Regional + escenarios si existen.
    preferred = ["LineName", "Tipo", "Zona", "Max Flow", "VI (MMUSD)", "Regional"]
    cols = [c for c in preferred if c in df_final.columns] + [c for c in scenario_cols if c in df_final.columns]
    return df_final[cols].reset_index(drop=True)


def build_plexos_plan_from_summary(
    df_resumen: pd.DataFrame,
    parent_object: str = "SING-SIC",
    category: str = "Transmission Expansion",
    scenario_prefix: str = "9_PlanOptimoTx",
) -> dict[str, pd.DataFrame]:
    meta_cols = ["LineName", "Tipo", "Zona", "Max Flow", "Regional", "VI (MMUSD)"]
    scenario_cols = [c for c in df_resumen.columns if c not in meta_cols]
    out: dict[str, pd.DataFrame] = {}
    for scen in scenario_cols:
        rows = []
        for _, r in df_resumen.iterrows():
            val = r.get(scen)
            if pd.isna(val) or (isinstance(val, str) and val.strip() == ""):
                continue
            try:
                activation_year = int(float(val))
            except Exception:
                continue
            rows.append({
                "Collection": "Lines",
                "Parent Object": parent_object,
                "Child Object": r.get("LineName"),
                "Property": "Units",
                "Value": 1,
                "Data File": "",
                "Units": "-",
                "Band": 1,
                "Date From": f"01-01-{activation_year}",
                "Date To": "",
                "Timeslice": "",
                "Action": "=",
                "Expression": "",
                "Scenario": f"{scenario_prefix}_{scen}",
                "Memo": "",
                "Category": category,
            })
        if rows:
            out[scen] = pd.DataFrame(rows)
    return out


def run(
    files: list[SolutionFile],
    env: PlexosEnv | None,
    sample: str = "mean",
    umbral: float = 0.3,
    demo: bool = False,
    diccionario_lineas: str | None = None,
    sheet_lineas: str = "Cap_Tx",
    category_filter: str | None = None,
    floor_interregional: int = 2032,
    floor_zonal: int = 2030,
    parent_object: str = "SING-SIC",
    plexos_category: str = "Transmission Expansion",
    scenario_prefix: str = "9_PlanOptimoTx",
    progress_callback: ProgressCallback | None = None,
    years: list[int] | None = None,
    phase: str = "LTPlan",
    period_yearly: str = "FiscalYear",
    series_type: str = "Values",
) -> tuple[dict[str, pd.DataFrame], pd.DataFrame, dict[str, pd.DataFrame], pd.DataFrame]:
    all_models: dict[str, pd.DataFrame] = {}
    total = max(len(files), 1)
    done = 0
    for f in files:
        model_key = f.scenario  # se mantiene como notebook: una hoja/columna por escenario/modelo.
        if progress_callback:
            progress_callback(done, total, f"Tx Units - {f.scenario} / {f.case}")
        if demo:
            df = demo_tx_plan(f.scenario, f.case)
        else:
            if env is None:
                raise ValueError("Se requiere PlexosEnv para ejecutar en modo REAL.")
            df = process_line_units(f.path, f.scenario, env, sample=sample, phase=phase, period_yearly=period_yearly, series_type=series_type, years=years)
        df = merge_line_dictionary(df, diccionario_lineas, sheet_lineas)
        if years and "Fiscal Year" in df.columns:
            df = df[df["Fiscal Year"].isin(years)].reset_index(drop=True)
        all_models[model_key] = pd.concat([all_models.get(model_key, pd.DataFrame()), df], ignore_index=True)
        done += 1
        if progress_callback:
            progress_callback(done, total, f"Listo: {f.scenario} / {f.case}")

    df_plan = read_line_dictionary(diccionario_lineas, sheet_lineas)
    units_pivot_sheets = pivot_units_by_model(all_models, category_filter)
    resumen = build_resumen_original(
        all_models,
        df_plan,
        category_filter=category_filter,
        threshold=umbral,
        floor_interregional=floor_interregional,
        floor_zonal=floor_zonal,
    )
    planilla_sheets = build_plexos_plan_from_summary(resumen, parent_object=parent_object, category=plexos_category, scenario_prefix=scenario_prefix)
    # df_units concatenado se deja para vista previa/CSV, pero no reemplaza las hojas originales.
    df_units = pd.concat(all_models.values(), ignore_index=True) if all_models else pd.DataFrame()
    return units_pivot_sheets, resumen, planilla_sheets, df_units

from __future__ import annotations

from pathlib import Path
from typing import Callable
import pandas as pd

from .plexos_api import PlexosEnv, query_result_to_df, get_sample_id_list, as_number, parse_plexos_date, is_plexos_day_id_error
from .discovery import SolutionFile
from .demo_data import demo_capacity


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

DICT_COLS_ORIGINAL = [
    "Central", "Tipo", "Estado", "Barra", "Potencia Máxima (MW)", "Energía Almacenada (MWh)",
    "Nodo Opt", "Tipo 2", "Costo Inv", "Max Units Built",
]

CAPACITY_OUTPUT_ORDER = [
    "Collection",
    "Central",
    "Category",
    "Fiscal Year",
    "Installed Capacity",
    "Units",
    "Tipo",
    "Estado",
    "Barra",
    "Potencia Máxima (MW)",
    "Energía Almacenada (MWh)",
    "Nodo Opt",
    "Tipo 2",
    "Costo Inv",
    "Max Units Built",
    "Escenario",
    "Caso",
    "Capacidad (c/ batería)",
]


def enforce_capacity_column_order(df: pd.DataFrame) -> pd.DataFrame:
    """Ordena la salida de capacidad instalada según el contrato Power BI acordado.

    Se agregan columnas faltantes como NA para mantener una estructura estable.
    Las columnas adicionales, si existieran, quedan al final para no perder información.
    """
    if df is None or df.empty:
        return df
    out = df.copy()
    for col in CAPACITY_OUTPUT_ORDER:
        if col not in out.columns:
            out[col] = pd.NA
    extras = [c for c in out.columns if c not in CAPACITY_OUTPUT_ORDER]
    return out[CAPACITY_OUTPUT_ORDER + extras]


def pivot_capacity_data(df: pd.DataFrame, rename_map: dict, index_cols: list[str], pivot_col: str = "property_name", value_col: str = "value") -> pd.DataFrame:
    df_pivot = df.pivot_table(index=index_cols, columns=pivot_col, values=value_col, aggfunc="first").reset_index()
    df_pivot.rename(columns=rename_map, inplace=True)
    return df_pivot



def _query(sol, env, coll, collection_key, property_tuple, property_name, id_list, phase: str = "LTPlan", period_yearly: str = "FiscalYear", series_type: str = "Values", years: list[int] | None = None):
    prop_id = sol.PropertyName2EnumId(*property_tuple)
    cols = ["collection_name", "child_name", "category_name", "property_name", "_date", "value"]
    years_int = sorted({int(y) for y in years or []})
    blocks = _contiguous_year_blocks(years_int)

    def _query_inner(start_date, end_date):
        return sol.QueryToList(
            env.phase(phase),
            coll[collection_key],
            "",
            "",
            env.period(period_yearly),
            env.series_type(series_type),
            str(prop_id),
            start_date,
            end_date,
            "0",
            id_list,
        )

    frames: list[pd.DataFrame] = []
    fallback_all = False

    if blocks:
        for y0, y1 in blocks:
            attempts = [(f"01-01-{y0}", f"31-12-{y1}")]
            attempts += [(f"01-01-{y0}", f"{d:02d}-12-{y1}") for d in (30, 29, 28)]
            block_ok = False
            for start_date, end_date in attempts:
                try:
                    res = _query_inner(start_date, end_date)
                    df_block = query_result_to_df(res, cols, env.String) if res else pd.DataFrame(columns=cols)
                    if df_block.empty:
                        fallback_all = True
                        block_ok = False
                        break
                    frames.append(df_block)
                    block_ok = True
                    break
                except Exception as exc:
                    if is_plexos_day_id_error(exc):
                        continue
                    fallback_all = True
                    block_ok = False
                    break
            if fallback_all or not block_ok:
                fallback_all = True
                break

    if not blocks or fallback_all:
        res = _query_inner(None, None)
        df = query_result_to_df(res, cols, env.String) if res else pd.DataFrame(columns=cols)
    else:
        df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=cols)

    df = _filter_df_years(df, years_int)
    if not df.empty:
        df["property_name"] = property_name
    return df


def process_solution_file(sol_file: str | Path, escenario: str, caso: str, env: PlexosEnv, sample: str = "mean", phase: str = "LTPlan", period_yearly: str = "FiscalYear", series_type: str = "Values", years: list[int] | None = None) -> pd.DataFrame:
    """Replica la estructura de salida del notebook de capacidad instalada."""
    sol = env.open_solution(sol_file)
    try:
        coll = sol.FetchAllCollectionIds()
        id_list = get_sample_id_list(sol, [sample])
        gen_units = _query(sol, env, coll, "SystemGenerators", ("System", "Generator", "Generators", "Units"), "Units", id_list, phase, period_yearly, series_type, years=years)
        installed_capacity = _query(sol, env, coll, "SystemGenerators", ("System", "Generator", "Generators", "Installed Capacity"), "Installed Capacity", id_list, phase, period_yearly, series_type, years=years)
        battery_units = _query(sol, env, coll, "SystemBatteries", ("System", "Battery", "Batteries", "Units"), "Units", id_list, phase, period_yearly, series_type, years=years)
        battery_capacity = _query(sol, env, coll, "SystemBatteries", ("System", "Battery", "Batteries", "Installed Capacity"), "Installed Capacity", id_list, phase, period_yearly, series_type, years=years)
    finally:
        sol.Close()

    rename_map_gen = {
        "collection_name": "Collection",
        "child_name": "Central",
        "category_name": "Category",
        "_date": "Fiscal Year",
        "Units": "Units",
        "Installed Capacity": "Installed Capacity",
    }
    index_cols = ["collection_name", "child_name", "category_name", "_date"]

    df_gen = pd.concat([gen_units, installed_capacity], ignore_index=True)
    df_gen["value"] = as_number(df_gen["value"]).astype(float).round(2)
    df_gen["_date"] = parse_plexos_date(df_gen["_date"]).dt.year
    df_cap_inst_gen = pivot_capacity_data(df_gen, rename_map_gen, index_cols)

    df_batt = pd.concat([battery_capacity, battery_units], ignore_index=True)
    df_batt["value"] = as_number(df_batt["value"]).astype(float).round(2)
    df_batt["_date"] = parse_plexos_date(df_batt["_date"]).dt.year
    df_cap_inst_batt = pivot_capacity_data(df_batt, rename_map_gen, index_cols)

    df_cap_inst = pd.concat([df_cap_inst_gen, df_cap_inst_batt], ignore_index=True)
    df_cap_inst["Escenario"] = escenario
    df_cap_inst["Caso"] = caso
    return df_cap_inst


def read_centrales_dictionary(path_diccionario: str | Path | None, sheet_name: str = "Centrales_Bdatos_2026") -> pd.DataFrame:
    if not path_diccionario:
        return pd.DataFrame()
    path = Path(path_diccionario)
    if not path.exists():
        return pd.DataFrame()
    dic = pd.read_excel(path, sheet_name=sheet_name, engine="openpyxl")
    keep = [c for c in DICT_COLS_ORIGINAL if c in dic.columns]
    if "Central" not in keep:
        return pd.DataFrame()
    return dic[keep].drop_duplicates("Central")


def merge_centrales(df: pd.DataFrame, path_diccionario: str | Path | None, sheet_name: str = "Centrales_Bdatos_2026") -> pd.DataFrame:
    dic = read_centrales_dictionary(path_diccionario, sheet_name)
    if df.empty or dic.empty:
        return df
    out = df.merge(dic, on="Central", how="inner")

    if "Potencia Máxima (MW)" in out.columns:
        out["Capacidad (c/ batería)"] = out.apply(
            lambda row: row.get("Units", 0) * row.get("Potencia Máxima (MW)", 0)
            if row.get("Collection") == "Battery" else row.get("Installed Capacity", 0),
            axis=1,
        )

    # Ajustes especiales de reconversión, idénticos al notebook.
    if {"Central", "Fiscal Year", "Tipo", "Tipo 2"}.issubset(out.columns):
        mask_mejillones = (out["Central"] == "Infraestructura Energetica Mejillones") & (out["Fiscal Year"] >= 2026)
        out.loc[mask_mejillones, ["Tipo", "Tipo 2"]] = ["GNL", "GNL"]
        mask_ct = out["Central"].isin(["CTA", "CTH"]) & (out["Fiscal Year"] >= 2028)
        out.loc[mask_ct, ["Tipo", "Tipo 2"]] = ["GNL", "GNL"]
    return out


def run(
    files: list[SolutionFile],
    env: PlexosEnv | None,
    sample: str = "mean",
    demo: bool = False,
    diccionario: str | None = None,
    sheet_name: str = "Centrales_Bdatos_2026",
    progress_callback: ProgressCallback | None = None,
    phase: str = "LTPlan",
    period_yearly: str = "FiscalYear",
    series_type: str = "Values",
    years: list[int] | None = None,
) -> pd.DataFrame:
    frames = []
    total = max(len(files), 1)
    done = 0
    for f in files:
        if progress_callback:
            progress_callback(done, total, f"Capacidad instalada - {f.scenario} / {f.case}")
        if demo:
            df = demo_capacity(f.scenario, f.case)
        else:
            if env is None:
                raise ValueError("Se requiere PlexosEnv para ejecutar en modo REAL.")
            df = process_solution_file(f.path, f.scenario, f.case, env, sample=sample, phase=phase, period_yearly=period_yearly, series_type=series_type, years=years)
        frames.append(df)
        done += 1
        if progress_callback:
            progress_callback(done, total, f"Listo: {f.scenario} / {f.case}")

    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    return enforce_capacity_column_order(merge_centrales(df, diccionario, sheet_name))

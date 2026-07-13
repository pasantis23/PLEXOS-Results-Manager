from __future__ import annotations

from pathlib import Path
from functools import reduce
from typing import Callable
import pandas as pd

from .plexos_api import PlexosEnv, query_result_to_df, get_sample_id_list, as_number, parse_plexos_date, query_to_list_with_date_retry, is_plexos_day_id_error
from .discovery import SolutionFile
from .demo_data import demo_generation_annual, demo_generation_hourly


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


def filter_years(df: pd.DataFrame, years: list[int] | None, year_col: str = "Fiscal Year") -> pd.DataFrame:
    if not years or df.empty or year_col not in df.columns:
        return df
    return df[df[year_col].isin(years)].reset_index(drop=True)



def _query_annual_by_blocks(sol, env: PlexosEnv, collection_id, prop_id, id_list: str, phase: str, period_yearly: str, series_type: str, years: list[int] | None) -> pd.DataFrame:
    cols = ["child_name", "_date", "value"]
    years_int = sorted({int(y) for y in years or []})
    blocks = _contiguous_year_blocks(years_int)

    def _query(start_date, end_date):
        return sol.QueryToList(
            env.phase(phase),
            collection_id,
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
        res = _query(None, None)
        df = query_result_to_df(res, cols, env.String) if res else pd.DataFrame(columns=cols)
    else:
        df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=cols)
    return _filter_df_years(df, years_int)


def process_generation_annual(sol_file: str | Path, escenario: str, caso: str, env: PlexosEnv, sample: str = "mean", phase: str = "LTPlan", period_yearly: str = "FiscalYear", series_type: str = "Values", years: list[int] | None = None) -> pd.DataFrame:
    """Replica process_generation_annual del notebook: Central, Fiscal Year, Generation [MWh], Escenario."""
    sol = env.open_solution(sol_file)
    try:
        coll = sol.FetchAllCollectionIds()
        id_list = get_sample_id_list(sol, [sample])
        prop_gen = sol.PropertyName2EnumId("System", "Generator", "Generators", "Generation")
        prop_batt = sol.PropertyName2EnumId("System", "Battery", "Batteries", "Generation")
        df_g = _query_annual_by_blocks(sol, env, coll["SystemGenerators"], prop_gen, id_list, phase, period_yearly, series_type, years)
        df_b = _query_annual_by_blocks(sol, env, coll["SystemBatteries"], prop_batt, id_list, phase, period_yearly, series_type, years)
    finally:
        sol.Close()

    # df_g y df_b ya vienen filtrados por los años seleccionados.

    def clean(df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return pd.DataFrame(columns=["Central", "Fiscal Year", "Generation [MWh]"])
        df = df.copy()
        df["value"] = as_number(df["value"]).round(2)
        df["_datetime"] = parse_plexos_date(df["_date"])
        df["Fiscal Year"] = df["_datetime"].dt.year
        df.rename(columns={"child_name": "Central", "value": "Generation [MWh]"}, inplace=True)
        return df

    df_all = pd.concat([clean(df_g), clean(df_b)], ignore_index=True)
    df_all["Escenario"] = escenario
    return df_all[["Central", "Fiscal Year", "Generation [MWh]", "Escenario"]]


def process_curtailed_annual(sol_file: str | Path, escenario: str, caso: str, env: PlexosEnv, sample: str = "mean", phase: str = "LTPlan", period_yearly: str = "FiscalYear", series_type: str = "Values", years: list[int] | None = None) -> pd.DataFrame:
    sol = env.open_solution(sol_file)
    try:
        coll = sol.FetchAllCollectionIds()
        id_list = get_sample_id_list(sol, [sample])
        prop = sol.PropertyName2EnumId("System", "Generator", "Generators", "Capacity Curtailed")
        df = _query_annual_by_blocks(sol, env, coll["SystemGenerators"], prop, id_list, phase, period_yearly, series_type, years)
    finally:
        sol.Close()

    if df.empty:
        return pd.DataFrame(columns=["Central", "Fiscal Year", "Energy Curtailed [MWh]", "Escenario"])
    df["value"] = as_number(df["value"]).round(2)
    df["_datetime"] = parse_plexos_date(df["_date"])
    df["Fiscal Year"] = df["_datetime"].dt.year
    df.rename(columns={"child_name": "Central", "value": "Energy Curtailed [MWh]"}, inplace=True)
    df["Escenario"] = escenario
    return df[["Central", "Fiscal Year", "Energy Curtailed [MWh]", "Escenario"]]


def process_generation_hourly_one_year(sol_file: str | Path, scenario: str, case: str, env: PlexosEnv, year: int, samples: list[str], phase: str = "LTPlan", period_hourly: str = "Interval", series_type: str = "Values") -> pd.DataFrame:
    """Replica el notebook horario: Central, Fecha, Generation_<sample>, Escenario, Case."""
    sol = env.open_solution(sol_file)
    try:
        coll = sol.FetchAllCollectionIds()
        prop_gen = sol.PropertyName2EnumId("System", "Generator", "Generators", "Generation")
        prop_batt = sol.PropertyName2EnumId("System", "Battery", "Batteries", "Net Generation")
        start, end = f"01-01-{year}", f"31-12-{year}"
        dfs = []
        for sample in samples:
            sid = get_sample_id_list(sol, [sample])
            res_g = query_to_list_with_date_retry(sol, env, phase, coll["SystemGenerators"], "", "", period_hourly, series_type, str(prop_gen), start, end, "0", sid)
            res_b = query_to_list_with_date_retry(sol, env, phase, coll["SystemBatteries"], "", "", period_hourly, series_type, str(prop_batt), start, end, "0", sid)
            df = pd.concat([
                query_result_to_df(res_g, ["child_name", "_date", "value"], env.String),
                query_result_to_df(res_b, ["child_name", "_date", "value"], env.String),
            ], ignore_index=True)
            df["value"] = as_number(df["value"]).round(2)
            df["_datetime_filter"] = parse_plexos_date(df["_date"])
            if year is not None and "_datetime_filter" in df.columns:
                df = df[df["_datetime_filter"].dt.year == int(year)].reset_index(drop=True)
            df.rename(columns={"child_name": "Central", "_date": "Fecha", "value": f"Generation_{sample}"}, inplace=True)
            dfs.append(df[["Central", "Fecha", f"Generation_{sample}"]])
    finally:
        sol.Close()

    if not dfs:
        return pd.DataFrame(columns=["Central", "Fecha", "Escenario", "Case"])
    df_merge = reduce(lambda left, right: pd.merge(left, right, on=["Central", "Fecha"], how="outer"), dfs)
    df_merge["Escenario"] = scenario
    df_merge["Case"] = case
    return df_merge


def process_generation_hourly(sol_file: str | Path, scenario: str, case: str, env: PlexosEnv, years: list[int], samples: list[str], phase: str = "LTPlan", period_hourly: str = "Interval", series_type: str = "Values") -> pd.DataFrame:
    frames = [process_generation_hourly_one_year(sol_file, scenario, case, env, year=y, samples=samples, phase=phase, period_hourly=period_hourly, series_type=series_type) for y in years]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def run(
    files: list[SolutionFile],
    env: PlexosEnv | None,
    sample: str = "mean",
    years: list[int] | None = None,
    samples: list[str] | None = None,
    mode: str = "Anual",
    demo: bool = False,
    progress_callback: ProgressCallback | None = None,
    phase: str = "LTPlan",
    period_yearly: str = "FiscalYear",
    period_hourly: str = "Interval",
    series_type: str = "Values",
) -> pd.DataFrame:
    years = years or [2030]
    samples = samples or [sample]
    frames = []
    total = max(len(files), 1)
    done = 0
    for f in files:
        msg = f"Generación - {f.scenario} / {f.case}"
        if progress_callback:
            progress_callback(done, total, msg)
        if demo:
            if mode == "Anual":
                df = demo_generation_annual(f.scenario, f.case, years=years, value_col="Generation [MWh]")
            elif mode == "Curtailment anual":
                df = demo_generation_annual(f.scenario, f.case, years=years, value_col="Energy Curtailed [MWh]")
            else:
                df = demo_generation_hourly(f.scenario, f.case, years=years, samples=samples)
        else:
            if env is None:
                raise ValueError("Se requiere PlexosEnv para ejecutar en modo REAL.")
            if mode == "Anual":
                df = filter_years(process_generation_annual(f.path, f.scenario, f.case, env, sample=sample, phase=phase, period_yearly=period_yearly, series_type=series_type), years)
            elif mode == "Curtailment anual":
                df = filter_years(process_curtailed_annual(f.path, f.scenario, f.case, env, sample=sample, phase=phase, period_yearly=period_yearly, series_type=series_type), years)
            else:
                df = process_generation_hourly(f.path, f.scenario, f.case, env, years=years, samples=samples, phase=phase, period_hourly=period_hourly, series_type=series_type)
        if mode in {"Anual", "Curtailment anual"}:
            df["caso"] = f.case
        frames.append(df)
        done += 1
        if progress_callback:
            progress_callback(done, total, f"Listo: {f.scenario} / {f.case}")
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

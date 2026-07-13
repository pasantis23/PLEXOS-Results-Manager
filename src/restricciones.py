from __future__ import annotations

from pathlib import Path
from functools import reduce
from typing import Callable
import pandas as pd

from .plexos_api import PlexosEnv, query_result_to_df, get_sample_id_list, as_number, parse_plexos_date, query_to_list_with_date_retry
from .discovery import SolutionFile
from .demo_data import demo_restrictions

ProgressCallback = Callable[[int, int, str], None]


def process_restrictions_one_year(sol_file: str | Path, scenario: str, case: str, env: PlexosEnv, year: int, samples: list[str], phase: str = "LTPlan", period_hourly: str = "Interval", series_type: str = "Values") -> pd.DataFrame:
    """Replica el notebook: child_name, category_name, Fecha, Value_<sample>, Escenario, Case."""
    sol = env.open_solution(sol_file)
    try:
        coll = sol.FetchAllCollectionIds()
        prop_units_generating = sol.PropertyName2EnumId("System", "Generator", "Generators", "Units Generating")
        prop_decision_variable = sol.PropertyName2EnumId("System", "Decision Variable", "Decision Variables", "Value")
        start, end = f"01-01-{year}", f"31-12-{year}"
        dfs = []
        for sample in samples:
            sid = get_sample_id_list(sol, [sample])
            res_units = query_to_list_with_date_retry(sol, env, phase, coll["SystemGenerators"], "", "", period_hourly, series_type, str(prop_units_generating), start, end, "0", sid)
            df_units = query_result_to_df(res_units, ["child_name", "_date", "value"], env.String) if res_units else pd.DataFrame(columns=["child_name", "_date", "value"])
            df_units["category_name"] = "Units Generating"

            res_decision = query_to_list_with_date_retry(sol, env, phase, coll["SystemDecisionVariables"], "", "", period_hourly, series_type, str(prop_decision_variable), start, end, "0", sid)
            df_decision = query_result_to_df(res_decision, ["child_name", "category_name", "_date", "value"], env.String) if res_decision else pd.DataFrame(columns=["child_name", "category_name", "_date", "value"])

            df = pd.concat([df_units, df_decision], ignore_index=True)
            df["value"] = as_number(df["value"]).round(2)
            df["_datetime_filter"] = parse_plexos_date(df["_date"])
            if year is not None:
                df = df[df["_datetime_filter"].dt.year == int(year)].reset_index(drop=True)
            df.rename(columns={"_date": "Fecha", "value": f"Value_{sample}"}, inplace=True)
            # Normaliza System.DateTime de .NET a datetime pandas antes de exportar/cachear.
            try:
                df["Fecha"] = pd.to_datetime(df["Fecha"].map(lambda x: x.ToString() if hasattr(x, "ToString") else x), errors="coerce")
            except Exception:
                df["Fecha"] = df["Fecha"].astype(str)
            df = df[["child_name", "category_name", "Fecha", f"Value_{sample}"]].drop_duplicates()
            key_cols = ["child_name", "category_name", "Fecha"]
            if not df.empty and df.duplicated(key_cols).any():
                df = df.groupby(key_cols, as_index=False, sort=False)[f"Value_{sample}"].mean()
            dfs.append(df)
    finally:
        sol.Close()

    if not dfs:
        return pd.DataFrame(columns=["child_name", "category_name", "Fecha", "Escenario", "Case"])
    df_merge = reduce(lambda l, r: pd.merge(l, r, on=["child_name", "category_name", "Fecha"], how="outer", validate="one_to_one"), dfs)
    df_merge["Escenario"] = scenario
    df_merge["Case"] = case
    return df_merge


def process_restrictions(
    sol_file: str | Path,
    scenario: str,
    case: str,
    env: PlexosEnv,
    years: list[int],
    samples: list[str],
    phase: str = "LTPlan",
    period_hourly: str = "Interval",
    series_type: str = "Values",
) -> pd.DataFrame:
    frames = [
        process_restrictions_one_year(
            sol_file,
            scenario,
            case,
            env,
            year=y,
            samples=samples,
            phase=phase,
            period_hourly=period_hourly,
            series_type=series_type,
        )
        for y in years
    ]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def run(
    files: list[SolutionFile],
    env: PlexosEnv | None,
    years: list[int],
    samples: list[str],
    demo: bool = False,
    variables: list[str] | None = None,
    progress_callback: ProgressCallback | None = None,
    phase: str = "LTPlan",
    period_hourly: str = "Interval",
    series_type: str = "Values",
) -> pd.DataFrame:
    frames = []
    total = max(len(files), 1)
    done = 0
    for f in files:
        if progress_callback:
            progress_callback(done, total, f"Restricciones - {f.scenario} / {f.case}")
        if demo:
            df = demo_restrictions(f.scenario, f.case, years=years, samples=samples)
        else:
            if env is None:
                raise ValueError("Se requiere PlexosEnv para ejecutar en modo REAL.")
            df = process_restrictions(f.path, f.scenario, f.case, env, years=years, samples=samples, phase=phase, period_hourly=period_hourly, series_type=series_type)
        frames.append(df)
        done += 1
        if progress_callback:
            progress_callback(done, total, f"Listo: {f.scenario} / {f.case}")
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

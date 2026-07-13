from __future__ import annotations

from pathlib import Path
from functools import reduce
from typing import Callable
import pandas as pd

from .plexos_api import PlexosEnv, query_result_to_df, get_sample_id_list, as_number, parse_plexos_date, is_plexos_day_id_error
from .discovery import SolutionFile
from .demo_data import demo_load

ProgressCallback = Callable[[int, int, str], None]



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


def process_load_hourly_range(sol_file: str | Path, scenario: str, case: str, env: PlexosEnv, years: list[int] | None, samples: list[str], phase: str = "LTPlan", period_hourly: str = "Interval", series_type: str = "Values") -> pd.DataFrame:
    """Replica el notebook de Load consultando por bloques continuos de años.

    v6.0.19:
    - Si years=[2026, 2040, ..., 2046], consulta dos bloques:
      01-01-2026 a 31-12-2026 y 01-01-2040 a 31-12-2046.
    - No consulta el rango envolvente 2026-2046 si los años son discontinuos.
    - Luego se filtran en pandas los años seleccionados.
    """
    years_int = sorted({int(y) for y in years}) if years else []
    blocks = _contiguous_year_blocks(years_int)

    sol = env.open_solution(sol_file)
    try:
        coll = sol.FetchAllCollectionIds()
        prop_load = sol.PropertyName2EnumId("System", "Node", "Nodes", "Load")
        prop_battery_load = sol.PropertyName2EnumId("System", "Node", "Nodes", "Battery Load")
        dfs = []

        def _clean(df: pd.DataFrame, value_col: str) -> pd.DataFrame:
            if df.empty:
                return df
            df["value"] = as_number(df["value"]).round(2)
            parsed = parse_plexos_date(df["_date"])
            if not parsed.isna().all():
                df["_date"] = parsed
            if years_int:
                parsed_filter = parsed if not parsed.isna().all() else parse_plexos_date(df["_date"])
                df = df[parsed_filter.dt.year.isin(years_int)].copy()
            df.rename(columns={"child_name": "Barra", "_date": "Fecha", "value": value_col}, inplace=True)
            df = df[["Barra", "Fecha", value_col]].drop_duplicates()
            if not df.empty and df.duplicated(["Barra", "Fecha"]).any():
                df = df.groupby(["Barra", "Fecha"], as_index=False, sort=False)[value_col].mean()
            return df

        def _query_property(prop_id, sid) -> pd.DataFrame:
            cols = ["child_name", "_date", "value"]

            def _query(start_date, end_date):
                return sol.QueryToList(
                    env.phase(phase),
                    coll["SystemNodes"],
                    "",
                    "",
                    env.period(period_hourly),
                    env.series_type(series_type),
                    str(prop_id),
                    start_date,
                    end_date,
                    "0",
                    sid,
                )

            if not blocks:
                res = _query(None, None)
                return query_result_to_df(res, cols, env.String) if res else pd.DataFrame(columns=cols)

            frames: list[pd.DataFrame] = []
            fallback_all = False

            for y0, y1 in blocks:
                attempts = [(f"01-01-{y0}", f"31-12-{y1}")]
                attempts += [(f"01-01-{y0}", f"{d:02d}-12-{y1}") for d in (30, 29, 28)]
                block_ok = False

                for start_date, end_date in attempts:
                    try:
                        res = _query(start_date, end_date)
                        df_block = query_result_to_df(res, cols, env.String) if res else pd.DataFrame(columns=cols)
                        if df_block.empty:
                            # Si la solución tiene un horizonte menor que el rango
                            # pedido, PLEXOS puede devolver vacío en vez de parcial.
                            # Se activa fallback completo y luego se filtra por año.
                            fallback_all = True
                            block_ok = False
                            break
                        frames.append(df_block)
                        block_ok = True
                        break
                    except Exception as exc:
                        if is_plexos_day_id_error(exc):
                            continue
                        raise

                if not block_ok:
                    fallback_all = True
                    break

            if fallback_all:
                res = _query(None, None)
                return query_result_to_df(res, cols, env.String) if res else pd.DataFrame(columns=cols)

            return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=cols)

        for sample in samples:
            sid = get_sample_id_list(sol, [sample])

            df_load = _query_property(prop_load, sid)
            df_load = _clean(df_load, f"Load_{sample}")

            df_batt = _query_property(prop_battery_load, sid)
            df_batt = _clean(df_batt, f"Battery Load_{sample}")

            dfs.append(df_load.merge(df_batt, on=["Barra", "Fecha"], how="outer", validate="one_to_one"))
    finally:
        sol.Close()

    if not dfs:
        return pd.DataFrame(columns=["Barra", "Fecha", "Escenario", "Case"])
    df_merge = reduce(lambda l, r: pd.merge(l, r, on=["Barra", "Fecha"], how="outer", validate="one_to_one"), dfs)
    df_merge["Escenario"] = scenario
    df_merge["Case"] = case
    return df_merge


def process_load_hourly_one_year(sol_file: str | Path, scenario: str, case: str, env: PlexosEnv, year: int | None, samples: list[str], phase: str = "LTPlan", period_hourly: str = "Interval", series_type: str = "Values") -> pd.DataFrame:
    """Compatibilidad: usa el motor por rango con un solo año."""
    years = [int(year)] if year is not None else None
    return process_load_hourly_range(sol_file, scenario, case, env, years=years, samples=samples, phase=phase, period_hourly=period_hourly, series_type=series_type)


def process_load_hourly(sol_file: str | Path, scenario: str, case: str, env: PlexosEnv, years: list[int] | None, samples: list[str], phase: str = "LTPlan", period_hourly: str = "Interval", series_type: str = "Values") -> pd.DataFrame:
    return process_load_hourly_range(sol_file, scenario, case, env, years=years, samples=samples, phase=phase, period_hourly=period_hourly, series_type=series_type)

def run(files: list[SolutionFile], env: PlexosEnv | None, years: list[int] | None, samples: list[str], demo: bool = False, progress_callback: ProgressCallback | None = None, phase: str = "LTPlan", period_hourly: str = "Interval", series_type: str = "Values") -> pd.DataFrame:
    frames = []
    total = max(len(files), 1)
    done = 0
    for f in files:
        if progress_callback:
            progress_callback(done, total, f"Demanda Load - {f.scenario} / {f.case}")
        if demo:
            df = demo_load(f.scenario, f.case, years=years or [2030], samples=samples)
        else:
            if env is None:
                raise ValueError("Se requiere PlexosEnv para ejecutar en modo REAL.")
            df = process_load_hourly(f.path, f.scenario, f.case, env, years=years, samples=samples, phase=phase, period_hourly=period_hourly, series_type=series_type)
        frames.append(df)
        done += 1
        if progress_callback:
            progress_callback(done, total, f"Listo: {f.scenario} / {f.case}")
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

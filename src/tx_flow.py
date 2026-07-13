from __future__ import annotations

from pathlib import Path
from functools import reduce
from typing import Callable
import pandas as pd

from .plexos_api import PlexosEnv, query_result_to_df, get_sample_id_list, as_number, parse_plexos_date, is_plexos_day_id_error
from .discovery import SolutionFile
from .demo_data import demo_tx_flow

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



def _query_property_frame(
    sol,
    env: PlexosEnv,
    collection_id,
    prop_id,
    sample_id: str,
    years: list[int] | None,
    phase: str,
    period_hourly: str,
    series_type: str,
) -> pd.DataFrame:
    """Consulta una propiedad horaria de líneas por bloques continuos de años.

    v6.0.19:
    - Si years=[2026, 2040, ..., 2046], consulta dos bloques:
      01-01-2026 a 31-12-2026 y 01-01-2040 a 31-12-2046.
    - No consulta el rango envolvente 2026-2046 si los años son discontinuos.
    - Si un bloque no devuelve datos, o si PLEXOS falla por Day Id al cierre de diciembre, se prueban
      cierres 30/29/28 de diciembre de ese mismo bloque.
    - Como último recurso, se consulta sin filtro de fecha una sola vez y luego
      se filtra en pandas.
    """
    cols = ["child_name", "_date", "value"]
    years_int = sorted({int(y) for y in years}) if years else []
    blocks = _contiguous_year_blocks(years_int)

    def _to_df(result) -> pd.DataFrame:
        df = query_result_to_df(result, cols, env.String) if result else pd.DataFrame(columns=cols)
        if df.empty:
            return df
        df["value"] = as_number(df["value"]).round(2)

        parsed = parse_plexos_date(df["_date"])
        if not parsed.isna().all():
            df["_date"] = parsed

        if years_int:
            parsed_filter = parsed if not parsed.isna().all() else parse_plexos_date(df["_date"])
            df = df[parsed_filter.dt.year.isin(years_int)].copy()

        df = df[["child_name", "_date", "value"]]

        # Evita merges many-to-many por claves repetidas línea-fecha.
        df = df.drop_duplicates()
        key = ["child_name", "_date"]
        if not df.empty and df.duplicated(key).any():
            df = df.groupby(key, as_index=False, sort=False)["value"].mean()
        return df

    def _query(start_date, end_date):
        return sol.QueryToList(
            env.phase(phase),
            collection_id,
            "",
            "",
            env.period(period_hourly),
            env.series_type(series_type),
            str(prop_id),
            start_date,
            end_date,
            "0",
            sample_id,
        )

    if not blocks:
        return _to_df(_query(None, None))

    frames: list[pd.DataFrame] = []
    fallback_all = False
    last_exc = None

    for y0, y1 in blocks:
        attempts = [(f"01-01-{y0}", f"31-12-{y1}")]
        attempts += [(f"01-01-{y0}", f"{d:02d}-12-{y1}") for d in (30, 29, 28)]
        block_ok = False

        for start_date, end_date in attempts:
            try:
                df_block = _to_df(_query(start_date, end_date))
                if df_block.empty:
                    # Algunas soluciones con menos años no devuelven parcial para un
                    # rango que excede su horizonte. Se detecta y se cae a consulta
                    # completa una sola vez, filtrando después por los años pedidos.
                    fallback_all = True
                    block_ok = False
                    break
                frames.append(df_block)
                block_ok = True
                break
            except Exception as exc:
                last_exc = exc
                if is_plexos_day_id_error(exc):
                    continue
                raise

        if not block_ok:
            fallback_all = True
            break

    if fallback_all:
        try:
            return _to_df(_query(None, None))
        except Exception as exc:
            raise RuntimeError(
                "PLEXOS no pudo responder la consulta de Flujos Tx. "
                "Se intentó por bloques continuos, cierres alternativos de diciembre y sin filtro de fecha."
            ) from exc

    if not frames:
        return pd.DataFrame(columns=cols)

    df_all = pd.concat(frames, ignore_index=True)
    df_all = df_all.drop_duplicates()
    key = ["child_name", "_date"]
    if not df_all.empty and df_all.duplicated(key).any():
        df_all = df_all.groupby(key, as_index=False, sort=False)["value"].mean()
    return df_all

def process_line_units(sol_file: str | Path, model_name: str, caso: str, env: PlexosEnv, years: list[int] | None = None, samples: list[str] | None = None, phase: str = "LTPlan", period_hourly: str = "Interval", series_type: str = "Values") -> pd.DataFrame:
    """Replica el notebook de flujos: Flow_mean/sample 1/sample 2 + límites, LineName, Fecha, Escenario, caso.

    Ajuste v6.0.15:
    - consulta años seleccionados en bloques continuos por propiedad/sample;
    - evita merges many-to-many si PLEXOS devuelve claves repetidas;
    - conserva columnas finales compatibles con Power BI.
    """
    samples = samples or ["mean", "sample 1", "sample 2"]
    sol = env.open_solution(sol_file)
    try:
        collections = sol.FetchAllCollectionIds()
        collection_id = collections["SystemLines"]
        prop_flow_id = sol.PropertyName2EnumId("System", "Line", "Lines", "Flow")
        other_props = {
            "Import Limit [MW]": sol.PropertyName2EnumId("System", "Line", "Lines", "Import Limit"),
            "Export Limit [MW]": sol.PropertyName2EnumId("System", "Line", "Lines", "Export Limit"),
        }

        dfs = []
        for sample_name in samples:
            s_id = get_sample_id_list(sol, [sample_name])
            df_flow = _query_property_frame(
                sol, env, collection_id, prop_flow_id, s_id, years,
                phase, period_hourly, series_type,
            )
            df_flow.rename(columns={"value": f"Flow_{sample_name}"}, inplace=True)
            dfs.append(df_flow)

        if dfs:
            df_summary = dfs[0]
            for df_part in dfs[1:]:
                df_summary = df_summary.merge(df_part, on=["child_name", "_date"], how="outer", validate="one_to_one")
        else:
            df_summary = pd.DataFrame(columns=["child_name", "_date"])

        for name, prop_id in other_props.items():
            sample_id = get_sample_id_list(sol, ["mean"])
            df_prop = _query_property_frame(
                sol, env, collection_id, prop_id, sample_id, years,
                phase, period_hourly, series_type,
            )
            df_prop.rename(columns={"value": name}, inplace=True)
            df_summary = df_summary.merge(df_prop, on=["child_name", "_date"], how="outer", validate="one_to_one")
    finally:
        sol.Close()

    df_summary.rename(columns={"child_name": "LineName", "_date": "Fecha"}, inplace=True)
    df_summary["Escenario"] = model_name
    df_summary["caso"] = caso
    return df_summary


def run(files: list[SolutionFile], env: PlexosEnv | None, years: list[int] | None, samples: list[str] | None = None, sample: str = "mean", demo: bool = False, progress_callback: ProgressCallback | None = None, phase: str = "LTPlan", period_hourly: str = "Interval", series_type: str = "Values") -> pd.DataFrame:
    samples = samples or ["mean", "sample 1", "sample 2"]
    frames = []
    total = max(len(files), 1)
    done = 0
    for f in files:
        if progress_callback:
            progress_callback(done, total, f"Flujos Tx - {f.scenario} / {f.case}")
        if demo:
            df = demo_tx_flow(f.scenario, f.case, years=years or [2030], samples=samples)
        else:
            if env is None:
                raise ValueError("Se requiere PlexosEnv para ejecutar en modo REAL.")
            df = process_line_units(f.path, f.scenario, f.case, env, years=years, samples=samples, phase=phase, period_hourly=period_hourly, series_type=series_type)
        frames.append(df)
        done += 1
        if progress_callback:
            progress_callback(done, total, f"Listo: {f.scenario} / {f.case}")
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

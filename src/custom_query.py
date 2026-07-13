from __future__ import annotations

from pathlib import Path
from functools import reduce
from typing import Callable
import pandas as pd

from .plexos_api import PlexosEnv, query_result_to_df, get_sample_id_list, as_number, parse_plexos_date, is_plexos_day_id_error
from .discovery import SolutionFile

ProgressCallback = Callable[[int, int, str], None]

PRESETS: dict[str, dict[str, str]] = {
    "Generación por central": {
        "parent_class": "System",
        "child_class": "Generator",
        "collection_name": "Generators",
        "collection_key": "SystemGenerators",
        "property_name": "Generation",
        "period": "FiscalYear",
    },
    "Generación horaria por central": {
        "parent_class": "System",
        "child_class": "Generator",
        "collection_name": "Generators",
        "collection_key": "SystemGenerators",
        "property_name": "Generation",
        "period": "Interval",
    },
    "Generación neta de baterías": {
        "parent_class": "System",
        "child_class": "Battery",
        "collection_name": "Batteries",
        "collection_key": "SystemBatteries",
        "property_name": "Net Generation",
        "period": "Interval",
    },
    "Demanda por barra": {
        "parent_class": "System",
        "child_class": "Node",
        "collection_name": "Nodes",
        "collection_key": "SystemNodes",
        "property_name": "Load",
        "period": "Interval",
    },
    "Battery Load por barra": {
        "parent_class": "System",
        "child_class": "Node",
        "collection_name": "Nodes",
        "collection_key": "SystemNodes",
        "property_name": "Battery Load",
        "period": "Interval",
    },
    "Flow por línea": {
        "parent_class": "System",
        "child_class": "Line",
        "collection_name": "Lines",
        "collection_key": "SystemLines",
        "property_name": "Flow",
        "period": "Interval",
    },
    "Units línea": {
        "parent_class": "System",
        "child_class": "Line",
        "collection_name": "Lines",
        "collection_key": "SystemLines",
        "property_name": "Units",
        "period": "FiscalYear",
    },
    "Capacidad instalada generador": {
        "parent_class": "System",
        "child_class": "Generator",
        "collection_name": "Generators",
        "collection_key": "SystemGenerators",
        "property_name": "Installed Capacity",
        "period": "FiscalYear",
    },
    "Capacidad instalada batería": {
        "parent_class": "System",
        "child_class": "Battery",
        "collection_name": "Batteries",
        "collection_key": "SystemBatteries",
        "property_name": "Installed Capacity",
        "period": "FiscalYear",
    },
    "Decision Variables": {
        "parent_class": "System",
        "child_class": "Decision Variable",
        "collection_name": "Decision Variables",
        "collection_key": "SystemDecisionVariables",
        "property_name": "Value",
        "period": "Interval",
    },
}



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


def _date_windows(years: list[int] | None, period: str) -> list[tuple[str | None, str | None]]:
    """Ventanas temporales por bloques continuos.

    FiscalYear e Interval usan la misma lógica de años seleccionados:
    `2026, 2040-2046` -> ventanas 2026 y 2040-2046.
    Si no hay años, se consulta sin filtro.
    """
    blocks = _contiguous_year_blocks(years)
    if not blocks:
        return [(None, None)]
    return [(f"01-01-{a}", f"31-12-{b}") for a, b in blocks]


def _clean_year_filter(df: pd.DataFrame, years: list[int] | None, period: str) -> pd.DataFrame:
    if not years or df.empty or "_date" not in df.columns:
        return df
    aux = df.copy()
    aux["Fiscal Year"] = parse_plexos_date(aux["_date"]).dt.year
    return aux[aux["Fiscal Year"].isin([int(y) for y in years])].reset_index(drop=True)



def validate_property(sol_file: str | Path, env: PlexosEnv, parent_class: str, child_class: str, collection_name: str, property_name: str) -> int:
    sol = env.open_solution(sol_file)
    try:
        return int(sol.PropertyName2EnumId(parent_class, child_class, collection_name, property_name))
    finally:
        sol.Close()


def process_custom_file(
    sol_file: str | Path,
    scenario: str,
    case: str,
    env: PlexosEnv,
    parent_class: str,
    child_class: str,
    collection_name: str,
    collection_key: str,
    property_name: str,
    years: list[int] | None,
    samples: list[str],
    phase: str = "LTPlan",
    period: str = "Interval",
    series_type: str = "Values",
    output_mode: str = "Columnas por sample",
) -> pd.DataFrame:
    sol = env.open_solution(sol_file)
    try:
        collections = sol.FetchAllCollectionIds()
        if collection_key not in collections:
            raise KeyError(f"No existe collection_key '{collection_key}' en la solución. Revisa el objeto/colección.")
        prop_id = sol.PropertyName2EnumId(parent_class, child_class, collection_name, property_name)
        cols = ["collection_name", "child_name", "category_name", "property_name", "_date", "value"]
        frames: list[pd.DataFrame] = []
        for sample in samples:
            sample_frames = []
            sid = get_sample_id_list(sol, [sample])
            for start, end in _date_windows(years, period):
                attempts = [(start, end)]
                if start is not None and end is not None:
                    try:
                        end_year = int(str(end).split("-")[-1])
                        attempts += [(start, f"{d:02d}-12-{end_year}") for d in (30, 29, 28)]
                    except Exception:
                        pass
                block_ok = False
                for start_i, end_i in attempts:
                    try:
                        result = sol.QueryToList(
                            env.phase(phase),
                            collections[collection_key],
                            "",
                            "",
                            env.period(period),
                            env.series_type(series_type),
                            str(prop_id),
                            start_i,
                            end_i,
                            "0",
                            sid,
                        )
                        df = query_result_to_df(result, cols, env.String) if result else pd.DataFrame(columns=cols)
                        if df.empty:
                            block_ok = False
                            break
                        sample_frames.append(df)
                        block_ok = True
                        break
                    except Exception as exc:
                        if is_plexos_day_id_error(exc):
                            continue
                        raise
                if not block_ok:
                    # Fallback único para ese sample sin filtro; luego se filtra por año.
                    result = sol.QueryToList(
                        env.phase(phase),
                        collections[collection_key],
                        "",
                        "",
                        env.period(period),
                        env.series_type(series_type),
                        str(prop_id),
                        None,
                        None,
                        "0",
                        sid,
                    )
                    df = query_result_to_df(result, cols, env.String) if result else pd.DataFrame(columns=cols)
                    sample_frames.append(df)
                    break
            df_sample = pd.concat(sample_frames, ignore_index=True) if sample_frames else pd.DataFrame(columns=cols)
            df_sample = _clean_year_filter(df_sample, years, period)
            if not df_sample.empty:
                df_sample["value"] = as_number(df_sample["value"])
                df_sample = df_sample.drop_duplicates()
                key_cols = ["collection_name", "child_name", "category_name", "property_name", "_date"]
                if df_sample.duplicated(key_cols).any():
                    df_sample = df_sample.groupby(key_cols, as_index=False, sort=False)["value"].mean()
            if output_mode == "Formato largo":
                df_sample["Sample"] = sample
                frames.append(df_sample)
            else:
                value_col = f"{property_name}_{sample}"
                df_sample = df_sample.rename(columns={"value": value_col})
                frames.append(df_sample[["collection_name", "child_name", "category_name", "property_name", "_date", value_col]])
    finally:
        sol.Close()

    if not frames:
        out = pd.DataFrame()
    elif output_mode == "Formato largo":
        out = pd.concat(frames, ignore_index=True)
    else:
        out = reduce(
            lambda left, right: pd.merge(left, right, on=["collection_name", "child_name", "category_name", "property_name", "_date"], how="outer", validate="one_to_one"),
            frames,
        )

    if out.empty:
        out = pd.DataFrame(columns=["collection_name", "child_name", "category_name", "property_name", "_date"])
    out["Escenario"] = scenario
    out["Caso"] = case
    return out


def demo_custom(scenario: str, case: str, years: list[int] | None, samples: list[str], property_name: str, period: str, output_mode: str) -> pd.DataFrame:
    years = years or [2030]
    rows = []
    if str(period).lower() == "fiscalyear":
        for y in years:
            base = {"collection_name": "SystemGenerators", "child_name": "Demo object", "category_name": "Demo", "property_name": property_name, "_date": f"31-12-{y} 00:00:00"}
            if output_mode == "Formato largo":
                for sample in samples:
                    rows.append({**base, "Sample": sample, "value": float(y)})
            else:
                row = base.copy()
                for i, sample in enumerate(samples):
                    row[f"{property_name}_{sample}"] = float(y) + i
                rows.append(row)
    else:
        for y in years:
            for h in range(24):
                base = {"collection_name": "SystemNodes", "child_name": "Demo object", "category_name": "Demo", "property_name": property_name, "_date": f"01-01-{y} {h:02d}:00:00"}
                if output_mode == "Formato largo":
                    for sample in samples:
                        rows.append({**base, "Sample": sample, "value": float(h)})
                else:
                    row = base.copy()
                    for i, sample in enumerate(samples):
                        row[f"{property_name}_{sample}"] = float(h) + i
                    rows.append(row)
    df = pd.DataFrame(rows)
    df["Escenario"] = scenario
    df["Caso"] = case
    return df


def run(
    files: list[SolutionFile],
    env: PlexosEnv | None,
    parent_class: str,
    child_class: str,
    collection_name: str,
    collection_key: str,
    property_name: str,
    years: list[int] | None,
    samples: list[str],
    phase: str = "LTPlan",
    period: str = "Interval",
    series_type: str = "Values",
    output_mode: str = "Columnas por sample",
    demo: bool = False,
    progress_callback: ProgressCallback | None = None,
) -> pd.DataFrame:
    frames = []
    total = max(len(files), 1)
    done = 0
    for f in files:
        if progress_callback:
            progress_callback(done, total, f"Consulta personalizada - {f.scenario} / {f.case}")
        if demo:
            df = demo_custom(f.scenario, f.case, years, samples, property_name, period, output_mode)
        else:
            if env is None:
                raise ValueError("Se requiere PlexosEnv para ejecutar en modo REAL.")
            df = process_custom_file(
                f.path,
                f.scenario,
                f.case,
                env,
                parent_class,
                child_class,
                collection_name,
                collection_key,
                property_name,
                years,
                samples,
                phase=phase,
                period=period,
                series_type=series_type,
                output_mode=output_mode,
            )
        frames.append(df)
        done += 1
        if progress_callback:
            progress_callback(done, total, f"Listo: {f.scenario} / {f.case}")
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

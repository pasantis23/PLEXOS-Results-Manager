from __future__ import annotations

from pathlib import Path
import pandas as pd

from .plexos_api import PlexosEnv, query_result_to_df, get_sample_id_list, as_number, parse_plexos_date, is_plexos_day_id_error
from .discovery import SolutionFile
from .demo_data import demo_costs



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



COST_COLUMNS = [
    "Fiscal Year",
    "Cost of Unserved Energy",
    "Total Generation Cost",
    "Annualized Build Cost Gen",
    "Annualized Build Cost Batt",
    "Escenario",
    "Caso",
]



def _query_property(
    sol,
    env: PlexosEnv,
    collections: dict,
    collection_key: str,
    property_tuple: tuple[str, str, str, str],
    value_name: str,
    id_list: str,
    phase: str = "LTPlan",
    period_yearly: str = "FiscalYear",
    series_type: str = "Values",
    years: list[int] | None = None,
) -> pd.DataFrame:
    """Consulta una propiedad anual por bloques continuos de años.

    Si los años son discontinuos, por ejemplo 2026 y 2040-2046, se consulta
    2026 y 2040-2046 por separado. Si PLEXOS no acepta o no encuentra el Day Id
    para un bloque, se usa un único fallback sin filtro y se filtra en pandas.
    """
    prop_id = sol.PropertyName2EnumId(*property_tuple)
    cols = ["collection_name", "child_name", "category_name", "property_name", "_date", "value"]
    years_int = sorted({int(y) for y in years or []})
    blocks = _contiguous_year_blocks(years_int)

    def _query(start_date, end_date):
        return sol.QueryToList(
            env.phase(phase),
            collections[collection_key],
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
                    res = _query(start_date, end_date)
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
                    # Algunas soluciones no aceptan filtro de fecha en FiscalYear.
                    # En ese caso no se cae: se consulta completo una sola vez y se filtra.
                    fallback_all = True
                    block_ok = False
                    break
            if fallback_all or not block_ok:
                fallback_all = True
                break

    if not blocks or fallback_all:
        res = _query(None, None)
        df = query_result_to_df(res, cols, env.String) if res else pd.DataFrame(columns=cols)
    else:
        df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=cols)

    df = _filter_df_years(df, years_int)
    if df.empty:
        return pd.DataFrame(columns=["Region", "Fiscal Year", value_name])

    df[value_name] = as_number(df["value"])
    df["Fiscal Year"] = parse_plexos_date(df["_date"]).dt.year
    df = df[["child_name", "Fiscal Year", value_name]].rename(columns={"child_name": "Region"})
    return df


def process_costs_file(sol_file: str | Path, scenario: str, case: str, env: PlexosEnv, sample: str = "mean", phase: str = "LTPlan", period_yearly: str = "FiscalYear", series_type: str = "Values", years: list[int] | None = None) -> pd.DataFrame:
    """
    Replica la lógica del notebook `Procesamiento_Costos_Sistema v2.ipynb`.

    Importante: la salida se mantiene en formato ancho, no en formato largo.
    Una fila corresponde a un año fiscal, escenario y caso, con las columnas de costos separadas.
    """
    sol = env.open_solution(sol_file)
    try:
        collections = sol.FetchAllCollectionIds()
        id_list = get_sample_id_list(sol, [sample])

        df_cost = _query_property(
            sol,
            env,
            collections,
            "SystemRegions",
            ("System", "Region", "Regions", "Cost of Unserved Energy"),
            "Cost of Unserved Energy",
            id_list,
            phase=phase,
            period_yearly=period_yearly,
            series_type=series_type,
            years=years,
        )
        df_total = _query_property(
            sol,
            env,
            collections,
            "SystemRegions",
            ("System", "Region", "Regions", "Total Generation Cost"),
            "Total Generation Cost",
            id_list,
            phase=phase,
            period_yearly=period_yearly,
            series_type=series_type,
            years=years,
        )
        df_gen = _query_property(
            sol,
            env,
            collections,
            "SystemGenerators",
            ("System", "Generator", "Generators", "Annualized Build Cost"),
            "Annualized Build Cost Gen",
            id_list,
            phase=phase,
            period_yearly=period_yearly,
            series_type=series_type,
            years=years,
        )
        df_bat = _query_property(
            sol,
            env,
            collections,
            "SystemBatteries",
            ("System", "Battery", "Batteries", "Annualized Build Cost"),
            "Annualized Build Cost Batt",
            id_list,
            phase=phase,
            period_yearly=period_yearly,
            series_type=series_type,
            years=years,
        )
    finally:
        sol.Close()

    # Mismo criterio del notebook: sumar por año fiscal y luego mergear columnas.
    df_cost_agg = df_cost.groupby("Fiscal Year", as_index=False)["Cost of Unserved Energy"].sum()
    df_total_agg = df_total.groupby("Fiscal Year", as_index=False)["Total Generation Cost"].sum()
    df_gen_agg = df_gen.groupby("Fiscal Year", as_index=False)["Annualized Build Cost Gen"].sum()
    df_bat_agg = df_bat.groupby("Fiscal Year", as_index=False)["Annualized Build Cost Batt"].sum()

    df_summary = (
        df_cost_agg
        .merge(df_total_agg, on="Fiscal Year", how="outer")
        .merge(df_gen_agg, on="Fiscal Year", how="outer")
        .merge(df_bat_agg, on="Fiscal Year", how="outer")
    )
    df_summary["Escenario"] = scenario
    df_summary["Caso"] = case

    for col in COST_COLUMNS:
        if col not in df_summary.columns:
            df_summary[col] = 0 if col not in {"Escenario", "Caso"} else ""

    return df_summary[COST_COLUMNS].sort_values(["Escenario", "Caso", "Fiscal Year"]).reset_index(drop=True)


def run(files: list[SolutionFile], env: PlexosEnv | None, sample: str = "mean", demo: bool = False, progress_callback=None, phase: str = "LTPlan", period_yearly: str = "FiscalYear", series_type: str = "Values", years: list[int] | None = None) -> pd.DataFrame:
    frames = []
    total = max(len(files), 1)
    done = 0
    for f in files:
        if progress_callback:
            progress_callback(done, total, f"Costos - {f.scenario} / {f.case}")
        if demo:
            frames.append(demo_costs(f.scenario, f.case))
        else:
            if env is None:
                raise ValueError("Se requiere PlexosEnv para ejecutar en modo REAL.")
            frames.append(process_costs_file(f.path, f.scenario, f.case, env, sample=sample, phase=phase, period_yearly=period_yearly, series_type=series_type, years=years))
        done += 1
        if progress_callback:
            progress_callback(done, total, f"Listo: {f.scenario} / {f.case}")
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=COST_COLUMNS)


def _cost_component_columns(df: pd.DataFrame) -> list[str]:
    candidates = [
        "Cost of Unserved Energy",
        "Total Generation Cost",
        "Annualized Build Cost Gen",
        "Annualized Build Cost Batt",
        # Compatibilidad por si un archivo previo usa nombres alternativos.
        "Annualized Build Cost Gx",
        "Annualized Build Cost Sx",
    ]
    return [c for c in candidates if c in df.columns]


def build_cost_summary_rows(df: pd.DataFrame) -> pd.DataFrame:
    """Construye la base de resumen del notebook: costo total anual MM por escenario/caso."""
    if df.empty:
        return pd.DataFrame(columns=["Fiscal Year", "Caso", "Escenario", "Total Cost (MM)"])

    comp_cols = _cost_component_columns(df)
    df_work = df.copy()
    for col in comp_cols:
        df_work[col] = pd.to_numeric(df_work[col], errors="coerce").fillna(0)

    df_sum = df_work.groupby(["Fiscal Year", "Caso", "Escenario"], as_index=False)[comp_cols].sum()
    df_sum["Total Cost (MM)"] = df_sum[comp_cols].sum(axis=1) / 1000.0
    return df_sum[["Fiscal Year", "Caso", "Escenario", "Total Cost (MM)"]]


def build_cost_summary_tables(
    df: pd.DataFrame,
    discount_rate: float = 0.06,
    vp_horizons: list[int] | None = None,
    include_full_vp: bool = True,
) -> dict[str, pd.DataFrame]:
    """
    Construye las hojas de resumen de costos.

    RESUMEN:
      - Costo total anual en MM por Fiscal Year, Escenario y Caso.

    Hojas VP opcionales:
      - RESUMEN_VP: suma descontada del horizonte completo disponible.
      - Resumen_VP_N: suma descontada de los primeros N años disponibles por Escenario/Caso.

    VP significa Valor Presente. Permite comparar costos ubicados en distintos años
    trayéndolos a una misma base temporal mediante una tasa de descuento anual.
    """
    vp_horizons = sorted({int(h) for h in (vp_horizons or []) if int(h) > 0})

    df_res = build_cost_summary_rows(df)
    if df_res.empty:
        tables = {"RESUMEN": pd.DataFrame()}
        if include_full_vp:
            tables["RESUMEN_VP"] = pd.DataFrame()
        for horizon in vp_horizons:
            tables[f"Resumen_VP_{horizon}"] = pd.DataFrame()
        return tables

    df_res_piv = df_res.pivot_table(
        index=["Fiscal Year", "Escenario"],
        columns="Caso",
        values="Total Cost (MM)",
        aggfunc="first",
    ).reset_index()
    df_res_piv = df_res_piv.sort_values(["Escenario", "Fiscal Year"]).reset_index(drop=True)
    df_res_piv.columns.name = None

    tables: dict[str, pd.DataFrame] = {"RESUMEN": df_res_piv}

    if include_full_vp or vp_horizons:
        rate = float(discount_rate)
        df_vp = df_res.sort_values(["Escenario", "Caso", "Fiscal Year"]).copy()
        df_vp["t"] = df_vp.groupby(["Escenario", "Caso"]).cumcount() + 1
        df_vp["Discount Rate"] = rate
        df_vp["PV Cost (MM)"] = df_vp["Total Cost (MM)"] / ((1 + rate) ** df_vp["t"])

        def vp_pivot(limit_years: int | None = None) -> pd.DataFrame:
            aux = df_vp if limit_years is None else df_vp[df_vp["t"] <= int(limit_years)]
            if aux.empty:
                return pd.DataFrame()
            out = (
                aux.groupby(["Escenario", "Caso"], as_index=False)["PV Cost (MM)"]
                .sum()
                .pivot_table(index="Escenario", columns="Caso", values="PV Cost (MM)", aggfunc="first", fill_value=0)
                .reset_index()
            )
            out.columns.name = None
            return out

        if include_full_vp:
            tables["RESUMEN_VP"] = vp_pivot(None)
        for horizon in vp_horizons:
            tables[f"Resumen_VP_{horizon}"] = vp_pivot(horizon)

    return tables


def resumen_costos(df: pd.DataFrame) -> pd.DataFrame:
    """Compatibilidad: retorna la hoja RESUMEN del procesamiento original."""
    return build_cost_summary_tables(df).get("RESUMEN", pd.DataFrame())

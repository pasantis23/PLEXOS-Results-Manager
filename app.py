from __future__ import annotations

from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
import ast
import sys
import time
import tempfile
from concurrent.futures import ThreadPoolExecutor

ROOT_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import pandas as pd
import streamlit as st

from src.config import load_config, ensure_output_dir
from src.discovery import discover_solution_files, filter_solution_files, solution_files_to_df, SolutionFile
from src.plexos_api import PlexosEnv, PlexosImportError, diagnose_plexos_api, format_diagnostics_text
from src.exporter import safe_sheet_name, safe_filename, choose_export_format
from src import costos, generacion, capacidad_instalada, load, tx_plan, tx_flow, restricciones, custom_query, visualizations, comparator, project_manager, execution_log, queue_manager, cache_manager, report_manager, catalog_manager, advanced_comparator

st.set_page_config(page_title="PLEXOS Resultados", layout="wide")

APP_VERSION = "v6.0.21"
PREVIEW_ROWS = 1000
MAX_ROWS_SPLIT_BY_SCENARIO = 2_000_000
CONFIG = load_config(ROOT_DIR / "settings" / "parametros.yaml")

EXPORT_LAYOUT_RECOMMENDED = "Recomendado por consulta"
EXPORT_LAYOUT_XLSX_ONE = "Excel único: una hoja por escenario"
EXPORT_LAYOUT_XLSX_PER_SCENARIO = "Excel por escenario"
EXPORT_LAYOUT_CSV_PER_SCENARIO = "CSV ZIP: un CSV por escenario"
EXPORT_LAYOUT_OPTIONS = [
    EXPORT_LAYOUT_RECOMMENDED,
    EXPORT_LAYOUT_XLSX_ONE,
    EXPORT_LAYOUT_XLSX_PER_SCENARIO,
    EXPORT_LAYOUT_CSV_PER_SCENARIO,
]


# Streamlit/Arrow es estricto con columnas object que mezclan texto, enteros, fechas u otros objetos.
# Esto solo afecta la visualización en pantalla. No modifica los DataFrames que se exportan a CSV/XLSX/ZIP.
def _display_value_for_arrow(value):
    try:
        if value is None:
            return ""
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value)


def make_arrow_safe_for_display(data):
    if not isinstance(data, pd.DataFrame):
        return data
    df = data.copy()
    df.columns = [str(c) for c in df.columns]
    for col in df.columns:
        series = df[col]
        if pd.api.types.is_object_dtype(series) or pd.api.types.is_string_dtype(series):
            df[col] = series.map(_display_value_for_arrow).astype("string")
    return df


_ORIGINAL_ST_DATAFRAME = st.dataframe


def _arrow_safe_st_dataframe(*args, **kwargs):
    if args:
        args = list(args)
        args[0] = make_arrow_safe_for_display(args[0])
    elif "data" in kwargs:
        kwargs["data"] = make_arrow_safe_for_display(kwargs["data"])
    return _ORIGINAL_ST_DATAFRAME(*args, **kwargs)


st.dataframe = _arrow_safe_st_dataframe


class UserCancelled(Exception):
    """Excepción controlada para detener una ejecución entre particiones."""


def check_cancel(job: dict | None) -> None:
    if job and job.get("cancel_requested"):
        raise UserCancelled("Ejecución detenida por el usuario.")


QUERY_DESCRIPTIONS = {
    "Costos del sistema": (
        "Extrae costos anuales del sistema y respeta la estructura del notebook: Fiscal Year, Cost of Unserved Energy, "
        "Total Generation Cost, Annualized Build Cost Gen, Annualized Build Cost Batt, Escenario y Caso. "
        "Permite activar de forma opcional las hojas de Valor Presente: RESUMEN_VP, Resumen_VP_10, Resumen_VP_15 y Resumen_VP_20."
    ),
    "Generación de energía": (
        "Extrae generación anual, generación horaria o energía vertida/curtailment. La salida conserva los nombres de columnas del notebook: "
        "en anual usa Central, Fiscal Year, Generation [MWh], Escenario y caso; en horario usa Central, Fecha, Generation_<sample>, Escenario y Case."
    ),
    "Capacidad instalada": (
        "Extrae Units e Installed Capacity de generadores y baterías por año fiscal. Cruza con el diccionario de centrales cuando se entrega, "
        "manteniendo los atributos originales usados en el notebook y la columna Capacidad (c/ batería)."
    ),
    "Demanda de energía (Load)": (
        "Extrae Load y Battery Load horario por barra/nodo. La estructura se mantiene como en el notebook: Barra, Fecha, Load_<sample>, "
        "Battery Load_<sample>, Escenario y Case."
    ),
    "Flujos de transmisión": (
        "Extrae Flow horario para samples mean, sample 1 y sample 2, además de Import Limit [MW] y Export Limit [MW]. "
        "La estructura se mantiene como en el notebook: LineName, Fecha, Flow_<sample>, límites, Escenario y caso."
    ),
    "Restricciones": (
        "Extrae en una misma salida Units Generating y Decision Variables. Mantiene la columna category_name para revisar inercia, reservas, CPF, CSF, rampas u otras variables sin separarlas desde la app."
    ),
    "Optimización de transmisión (Plan de Transmisión - Units)": (
        "Extrae Units de líneas y replica las dos etapas del notebook: primero hojas por escenario con Units pivoteado y hoja RESUMEN; luego planillas por escenario para fijar Units en PLEXOS."
    ),
    "Consulta personalizada PLEXOS": (
        "Permite consultar una propiedad PLEXOS definida por el usuario mediante predefinidos o parámetros avanzados. No reemplaza las salidas compatibles con Power BI; exporta una salida independiente para análisis exploratorio."
    ),
}
QUERY_OPTIONS = list(QUERY_DESCRIPTIONS.keys())

OUTPUT_CONTRACTS = {
    "Costos del sistema": {"default": "XLSX", "estructura": "RESUMEN + VP opcional + hojas por escenario", "particion": "No"},
    "Generación de energía": {"default": "XLSX anual / CSV ZIP particionado si horaria", "estructura": "Central, año/fecha, Generation_<sample>, Escenario, Case/caso", "particion": "Solo modo Horaria"},
    "Capacidad instalada": {"default": "XLSX", "estructura": "Hojas por escenario con cruce de diccionario", "particion": "No"},
    "Demanda de energía (Load)": {"default": "XLSX por escenario", "estructura": "Barra, Fecha, Load_<sample>, Battery Load_<sample>, Escenario, Case", "particion": "No; escritura incremental por hoja"},
    "Flujos de transmisión": {"default": "XLSX por escenario", "estructura": "LineName, Fecha, Flow_<sample>, límites, Escenario, caso", "particion": "No; escritura incremental por hoja"},
    "Restricciones": {"default": "CSV ZIP particionado", "estructura": "child_name, category_name, Fecha, Value_<sample>, Escenario, Case", "particion": "Sí, por escenario/caso/año"},
    "Optimización de transmisión (Plan de Transmisión - Units)": {"default": "XLSX/ZIP según etapa", "estructura": "RESUMEN + planillas para PLEXOS", "particion": "No"},
    "Consulta personalizada PLEXOS": {"default": "CSV ZIP si grande", "estructura": "Salida independiente exploratoria", "particion": "Opcional avanzado"},
}



def parse_years_text(text: str, min_year: int = 2020, max_year: int = 2050) -> list[int]:
    years: set[int] = set()
    if not text or not text.strip():
        return []
    tokens = [t.strip() for t in text.replace(";", ",").split(",") if t.strip()]
    for token in tokens:
        if "-" in token:
            a, b = [x.strip() for x in token.split("-", 1)]
            start, end = int(a), int(b)
            if end < start:
                start, end = end, start
            years.update(range(start, end + 1))
        else:
            years.add(int(token))
    clean = sorted(y for y in years if min_year <= y <= max_year)
    if len(clean) != len(years):
        st.warning(f"Algunos años quedaron fuera del rango permitido {min_year}-{max_year}.")
    return clean


def parse_samples_text(text: str) -> list[str]:
    if not text or not text.strip():
        return []
    raw = text.strip()
    if raw.startswith("[") and raw.endswith("]"):
        try:
            parsed = ast.literal_eval(raw)
            if isinstance(parsed, (list, tuple)):
                return [str(x).strip() for x in parsed if str(x).strip()]
        except Exception:
            pass
    return [s.strip().strip('"').strip("'") for s in raw.replace(";", ",").split(",") if s.strip().strip('"').strip("'")]


def year_label(years: list[int]) -> str:
    if not years:
        return "todos"
    if len(years) == 1:
        return str(years[0])
    if years == list(range(min(years), max(years) + 1)):
        return f"{min(years)}_{max(years)}"
    return "multi_anios"


def default_years_text() -> str:
    years = CONFIG.get("procesamiento", {}).get("anios_default", [2030])
    return ",".join(str(y) for y in years)


def default_samples_text() -> str:
    samples = CONFIG.get("procesamiento", {}).get("samples_default", ["mean", "sample 1", "sample 2"])
    return repr(samples)


def strip_known_extension(name: str) -> str:
    path = Path(str(name))
    if path.suffix.lower() in {".xlsx", ".csv", ".zip"}:
        return path.with_suffix("").name
    return path.name


def _project_value(key: str, default):
    project = st.session_state.get("active_project", {}) or {}
    return project.get(key, default)


def sidebar_params():
    st.sidebar.title("PLEXOS Results Manager")

    modo = "Real PLEXOS"
    st.sidebar.caption("Modo: Procesamiento resultados PLEXOS")
    base_dir = st.sidebar.text_input(
        "Carpeta base de resultados",
        value=_project_value("base_dir", CONFIG.get("rutas", {}).get("base_dir", "")),
        help="Carpeta raíz donde la app busca archivos Solution.zip.",
    )
    output_dir = st.sidebar.text_input(
        "Carpeta de salida",
        value=_project_value("output_dir", CONFIG.get("rutas", {}).get("output_dir", "outputs")),
        help="Carpeta donde se guardan archivos procesados, logs y catálogo.",
    )
    sample = st.sidebar.text_input(
        "Sample base",
        value=_project_value("sample", CONFIG.get("plexos", {}).get("sample_default", "mean")),
        help="Normalmente mean. Para consultas horarias se usa el campo Samples de la consulta.",
    )

    with st.sidebar.expander("Avanzado: entorno PLEXOS", expanded=(modo == "Procesamiento resultados PLEXOS")):
        api_path = st.text_input(
            "Ruta API PLEXOS",
            value=_project_value("api_path", CONFIG.get("plexos", {}).get("api_path", "C:/Program Files/Energy Exemplar/PLEXOS 11.0 API/")),
            help="Ruta donde están las DLL de PLEXOS.",
        )
        phase = st.text_input("Fase de simulación", value=_project_value("phase", CONFIG.get("plexos", {}).get("simulation_phase", "LTPlan")))
        period_yearly = st.selectbox(
            "Período anual",
            ["FiscalYear", "Interval", "Block"],
            index=["FiscalYear", "Interval", "Block"].index(_project_value("period_yearly", CONFIG.get("plexos", {}).get("period_yearly", "FiscalYear"))) if _project_value("period_yearly", CONFIG.get("plexos", {}).get("period_yearly", "FiscalYear")) in ["FiscalYear", "Interval", "Block"] else 0,
        )
        period_hourly = st.selectbox(
            "Período horario",
            ["Interval", "Block", "FiscalYear"],
            index=["Interval", "Block", "FiscalYear"].index(_project_value("period_hourly", CONFIG.get("plexos", {}).get("period_hourly", "Interval"))) if _project_value("period_hourly", CONFIG.get("plexos", {}).get("period_hourly", "Interval")) in ["Interval", "Block", "FiscalYear"] else 0,
        )
        series_type = st.selectbox("Tipo de serie", ["Values", "Samples"], index=0)

    with st.sidebar.expander("Avanzado: exportación y cache", expanded=False):
        output_mode = st.selectbox(
            "Modo de salida",
            ["Compatible Power BI", "Analítico / personalizado"],
            help="Mantén Compatible Power BI para respetar las salidas de notebooks.",
        )
        use_cache = st.checkbox("Usar cache por parámetros", value=True)
        export_pref = st.selectbox("Formato de exportación", ["Automático", "XLSX", "CSV"])
        output_layout = st.selectbox(
            "Estructura de archivo de salida",
            EXPORT_LAYOUT_OPTIONS,
            index=0,
            help=(
                "Recomendado mantiene el comportamiento definido para cada consulta. "
                "Las otras opciones fuerzan cómo se empaquetan los resultados por escenario."
            ),
        )
        row_threshold = st.number_input(
            "Umbral automático CSV (filas)",
            min_value=10_000,
            max_value=5_000_000,
            value=int(_project_value("row_threshold", CONFIG.get("procesamiento", {}).get("umbral_csv_filas", 500_000))),
            step=50_000,
        )
        partition_mode = st.selectbox(
            "Exportación particionada",
            [
                "Restricciones y Generación horaria (recomendado)",
                "Desactivada",
                "Forzar en salidas horarias/grandes",
            ],
            index=0,
            help="Por defecto particiona Restricciones y Generación horaria. Flujos Tx y Load se escriben como XLSX incremental por escenario.",
        )

    with st.sidebar.expander("Avanzado: proyectos", expanded=False):
        projects = project_manager.list_projects(ROOT_DIR)
        if projects:
            labels = [p.stem for p in projects]
            selected = st.selectbox("Proyecto guardado", labels, key="sidebar_project_select")
            selected_path = projects[labels.index(selected)]
            c_load, c_clear = st.columns(2)
            if c_load.button("Cargar", key="load_project_sidebar"):
                st.session_state["active_project"] = project_manager.load_project(selected_path)
                st.rerun()
            if c_clear.button("Limpiar", key="clear_project_sidebar"):
                st.session_state.pop("active_project", None)
                st.rerun()
        else:
            st.caption("Aún no hay proyectos guardados.")

    return modo, base_dir, output_dir, api_path, sample, phase, period_yearly, period_hourly, series_type, output_mode, use_cache, export_pref, output_layout, int(row_threshold), partition_mode

def get_files(modo: str, base_dir: str) -> list[SolutionFile]:
    return discover_solution_files(base_dir)

def init_env(modo: str, api_path: str) -> PlexosEnv | None:
    return PlexosEnv(api_path)

def year_selector(label: str, default: str | None = None, allow_empty: bool = False) -> list[int]:
    default = default if default is not None else default_years_text()
    years_text = st.text_input(label, value=default, help="Ejemplos: 2030 | 2030,2035,2040 | 2030-2035 | 2030,2035-2037.")
    years = parse_years_text(years_text)
    if not years and not allow_empty:
        st.error("Debes ingresar al menos un año válido.")
        st.stop()
    return years


def samples_input(label: str = "Samples", default: str | None = None) -> list[str]:
    text = st.text_input(
        label,
        value=default if default is not None else default_samples_text(),
        help='Formato recomendado: ["mean", "sample 1", "sample 2"]. Los nombres deben coincidir exactamente con PLEXOS.',
    )
    samples = parse_samples_text(text)
    if not samples:
        st.error("Debes ingresar al menos un sample válido.")
        st.stop()
    return samples


def sheets_by_scenario(df: pd.DataFrame, scenario_col: str = "Escenario", max_split_rows: int = MAX_ROWS_SPLIT_BY_SCENARIO) -> dict[str, pd.DataFrame]:
    """Divide por escenario solo cuando el DataFrame es razonable en memoria.

    Para tablas horarias muy grandes, evitar groupby porque pandas puede crear
    arreglos auxiliares de varios GB. En esos casos se exporta consolidado con
    la columna Escenario, que es más seguro para CSV/Power BI.
    """
    if df is None or df.empty or scenario_col not in df.columns:
        return {"Resultados": df if df is not None else pd.DataFrame()}
    if len(df) > int(max_split_rows):
        return {"Resultados": df}
    return {str(k): v for k, v in df.groupby(scenario_col, sort=False)}


def export_excel_sheets(sheets: dict[str, pd.DataFrame], output_path: str | Path) -> Path:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    used: set[str] = set()
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        for name, df in sheets.items():
            sheet = safe_sheet_name(name)
            base = sheet
            i = 1
            while sheet in used:
                suffix = f"_{i}"
                sheet = safe_sheet_name(base[:31 - len(suffix)] + suffix)
                i += 1
            used.add(sheet)
            (df if df is not None else pd.DataFrame()).to_excel(writer, sheet_name=sheet, index=False)
    return output_path


def export_csv_zip_sheets(sheets: dict[str, pd.DataFrame], output_path: str | Path) -> Path:
    """Exporta CSV ZIP escribiendo a disco temporal.

    Evita construir strings/bytes gigantes en memoria con df.to_csv().encode(),
    lo que es crítico para salidas horarias grandes.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        with ZipFile(output_path, mode="w", compression=ZIP_DEFLATED) as zf:
            used: set[str] = set()
            for name, df in sheets.items():
                data = df if df is not None else pd.DataFrame()
                csv_name = safe_filename(f"{name}.csv")
                base = csv_name
                i = 1
                while csv_name in used:
                    stem = Path(base).stem
                    csv_name = safe_filename(f"{stem}_{i}.csv")
                    i += 1
                used.add(csv_name)
                tmp_csv = tmp_dir / csv_name
                data.to_csv(tmp_csv, index=False, encoding="utf-8-sig")
                zf.write(tmp_csv, arcname=csv_name)
    return output_path


def zip_files(files: list[Path], output_path: str | Path) -> Path:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output_path, mode="w", compression=ZIP_DEFLATED) as zf:
        for f in files:
            zf.write(f, arcname=f.name)
    return output_path


def export_sheets_auto(sheets: dict[str, pd.DataFrame], output_base: Path, export_pref: str, row_threshold: int, force_csv: bool = False) -> tuple[Path, str]:
    total_rows = sum(len(df) for df in sheets.values() if df is not None)
    max_sheet_rows = max([len(df) for df in sheets.values() if df is not None] or [0])
    pref = str(export_pref).strip().lower()
    if force_csv or pref == "csv":
        fmt = "csv_zip"
    elif pref == "xlsx" and max_sheet_rows < 1_048_576:
        fmt = "xlsx"
    elif total_rows >= int(row_threshold) or max_sheet_rows >= 1_048_576:
        fmt = "csv_zip"
    else:
        fmt = "xlsx"
    if fmt == "xlsx":
        return export_excel_sheets(sheets, output_base.with_suffix(".xlsx")), "XLSX"
    return export_csv_zip_sheets(sheets, output_base.with_suffix(".zip")), "CSV ZIP"




def export_excel_files_by_sheet_zip(sheets: dict[str, pd.DataFrame], output_path: str | Path) -> Path:
    """Exporta un ZIP con un archivo XLSX por hoja/escenario.

    Para consultas con hojas auxiliares (RESUMEN, diccionarios), esas hojas se exportan
    como archivos separados dentro del ZIP. Esto mantiene la trazabilidad y evita mezclar
    hojas auxiliares con escenarios específicos.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        files: list[Path] = []
        for name, df in sheets.items():
            xlsx_path = tmp_dir / safe_filename(f"{name}.xlsx")
            export_excel_sheets({name: df if df is not None else pd.DataFrame()}, xlsx_path)
            files.append(xlsx_path)
        with ZipFile(output_path, mode="w", compression=ZIP_DEFLATED) as zf:
            for f in files:
                zf.write(f, arcname=f.name)
    return output_path


def export_sheets_by_layout(
    sheets: dict[str, pd.DataFrame],
    output_base: Path,
    output_layout: str,
    export_pref: str,
    row_threshold: int,
    force_csv: bool = False,
) -> tuple[Path, str]:
    """Exporta tablas ya calculadas según la estructura elegida por el usuario."""
    layout = str(output_layout or EXPORT_LAYOUT_RECOMMENDED).strip()

    if layout == EXPORT_LAYOUT_XLSX_ONE:
        return export_excel_sheets(sheets, output_base.with_suffix(".xlsx")), "XLSX · hoja por escenario"

    if layout == EXPORT_LAYOUT_XLSX_PER_SCENARIO:
        return export_excel_files_by_sheet_zip(sheets, output_base.with_suffix(".zip")), "ZIP · un XLSX por escenario/hoja"

    if layout == EXPORT_LAYOUT_CSV_PER_SCENARIO:
        return export_csv_zip_sheets(sheets, output_base.with_suffix(".zip")), "CSV ZIP · un CSV por escenario/hoja"

    return export_sheets_by_layout(sheets, output_base, output_layout, export_pref, row_threshold, force_csv=force_csv)


def partitioned_output_name(f: SolutionFile, year: int | None = None) -> str:
    parts = [str(getattr(f, "scenario", "Escenario") or "Escenario"), str(getattr(f, "case", "Caso") or "Caso")]
    if year is not None:
        parts.append(str(year))
    return safe_filename("__".join(parts) + ".csv")


def should_use_partitioned_export(consulta: str, params: dict, partition_mode: str, output_layout: str = EXPORT_LAYOUT_RECOMMENDED) -> bool:
    """Define cuándo usar exportación incremental por escenario/caso/año.

    Por defecto se mantiene la estructura de columnas de los notebooks. Las salidas
    más críticas se tratan de forma segura: Restricciones y Generación horaria se
    particionan a CSV ZIP; Flujos Tx y Load se consultan por rango completo y se escriben incrementalmente a XLSX
    por escenario para replicar el notebook sin consolidar todo en memoria.
    """
    if str(output_layout or EXPORT_LAYOUT_RECOMMENDED).strip() != EXPORT_LAYOUT_RECOMMENDED:
        # Si el usuario fuerza una estructura de archivo, se respeta esa decisión.
        # Advertencia: para salidas muy grandes, XLSX puede ser más lento o exceder límites de Excel.
        return False

    mode = str(partition_mode or "").strip().lower()

    if mode.startswith("desactivada"):
        return False

    if mode.startswith("forzar"):
        if consulta in {"Demanda de energía (Load)", "Flujos de transmisión", "Restricciones", "Consulta personalizada PLEXOS"}:
            return True
        return consulta == "Generación de energía" and params.get("modo_generacion") == "Horaria"

    # Recomendado por defecto: solo salidas más críticas en memoria.
    if consulta == "Restricciones":
        return True
    if consulta == "Generación de energía" and params.get("modo_generacion") == "Horaria":
        return True
    return False


def run_partition_df(
    consulta: str,
    f: SolutionFile,
    year: int,
    env: PlexosEnv | None,
    demo: bool,
    params: dict,
    sample: str,
    phase: str,
    period_yearly: str,
    period_hourly: str,
    series_type: str,
) -> pd.DataFrame:
    if consulta == "Generación de energía":
        return generacion.run(
            [f], env, sample=sample, years=[year], samples=params.get("samples", [sample]),
            mode=params.get("modo_generacion", "Horaria"), demo=demo, progress_callback=None,
            phase=phase, period_yearly=period_yearly, period_hourly=period_hourly, series_type=series_type,
        )
    if consulta == "Demanda de energía (Load)":
        return load.run(
            [f], env, years=[year], samples=params.get("samples", [sample]), demo=demo,
            progress_callback=None, phase=phase, period_hourly=period_hourly, series_type=series_type,
        )
    if consulta == "Flujos de transmisión":
        return tx_flow.run(
            [f], env, years=[year], samples=params.get("samples", ["mean", "sample 1", "sample 2"]), demo=demo,
            progress_callback=None, phase=phase, period_hourly=period_hourly, series_type=series_type,
        )
    if consulta == "Restricciones":
        return restricciones.run(
            [f], env, years=[year], samples=params.get("samples", [sample]), demo=demo,
            progress_callback=None, phase=phase, period_hourly=period_hourly, series_type=series_type,
        )
    if consulta == "Consulta personalizada PLEXOS":
        return custom_query.run(
            [f], env, parent_class=params.get("parent_class", "System"), child_class=params.get("child_class", "Node"),
            collection_name=params.get("collection_name", "Nodes"), collection_key=params.get("collection_key", "SystemNodes"),
            property_name=params.get("property_name", "Battery Load"), years=[year], samples=params.get("samples", [sample]),
            phase=phase, period=params.get("custom_period", period_hourly), series_type=series_type,
            output_mode=params.get("custom_output_mode", "Columnas por sample"), demo=demo, progress_callback=None,
        )
    raise ValueError(f"Consulta no soportada para exportación particionada: {consulta}")


def export_partitioned_csv_zip(
    *,
    consulta: str,
    selected_files: list[SolutionFile],
    env: PlexosEnv | None,
    demo: bool,
    params: dict,
    sample: str,
    phase: str,
    period_yearly: str,
    period_hourly: str,
    series_type: str,
    output_base: Path,
    job: dict,
    step,
    extra_csvs: dict[str, pd.DataFrame] | None = None,
) -> tuple[Path, pd.DataFrame]:
    """Procesa y exporta por escenario/caso/año sin concatenar todo en memoria."""
    years = params.get("years") or [2030]
    years = [int(y) for y in years]
    output_path = output_base.with_suffix(".zip")
    partial_path = output_base.with_suffix(".partial.zip")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    partial_path.unlink(missing_ok=True)
    total = max(1, len(selected_files) * len(years))
    done = 0
    preview = pd.DataFrame()

    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            with ZipFile(partial_path, mode="w", compression=ZIP_DEFLATED) as zf:
                # Archivos auxiliares chicos, por ejemplo diccionarios. Se escriben una sola vez.
                for name, df_extra in (extra_csvs or {}).items():
                    if isinstance(df_extra, pd.DataFrame) and not df_extra.empty:
                        tmp_csv = tmp_dir / safe_filename(f"{name}.csv")
                        df_extra.to_csv(tmp_csv, index=False, encoding="utf-8-sig")
                        zf.write(tmp_csv, arcname=tmp_csv.name)

                for f in selected_files:
                    for year in years:
                        check_cancel(job)
                        done += 1
                        msg = f"{consulta} · {getattr(f, 'scenario', '')} / {getattr(f, 'case', '')} · {year}"
                        step(12 + int(74 * (done - 1) / total), msg)
                        df_part = run_partition_df(
                            consulta, f, year, env, demo, params, sample, phase, period_yearly, period_hourly, series_type
                        )
                        if preview.empty and isinstance(df_part, pd.DataFrame) and not df_part.empty:
                            preview = make_preview_df(df_part)
                        csv_name = partitioned_output_name(f, year)
                        tmp_csv = tmp_dir / csv_name
                        (df_part if isinstance(df_part, pd.DataFrame) else pd.DataFrame()).to_csv(tmp_csv, index=False, encoding="utf-8-sig")
                        zf.write(tmp_csv, arcname=csv_name)
                        try:
                            del df_part
                        except Exception:
                            pass
                        step(12 + int(74 * done / total), f"Exportado: {csv_name}")
                        check_cancel(job)
        partial_path.replace(output_path)
    except Exception:
        partial_path.unlink(missing_ok=True)
        raise

    return output_path, preview

def read_generation_dictionary() -> pd.DataFrame:
    path = CONFIG.get("rutas", {}).get("diccionario_centrales", "")
    sheet = CONFIG.get("rutas", {}).get("hoja_diccionario_centrales", "Centrales_Bdatos_2026")
    if not path or not Path(path).exists():
        return pd.DataFrame()
    try:
        df = pd.read_excel(path, sheet_name=sheet, engine="openpyxl")
        attrs = ["Central", "Tipo", "Estado", "Nodo Opt", "Tipo 2"]
        keep = [c for c in attrs if c in df.columns]
        return df[keep].drop_duplicates("Central") if "Central" in keep else pd.DataFrame()
    except Exception:
        return pd.DataFrame()


def read_restrictions_dictionary() -> pd.DataFrame:
    path = CONFIG.get("rutas", {}).get("diccionario_centrales", "")
    sheet = CONFIG.get("rutas", {}).get("hoja_diccionario_centrales", "Centrales_Bdatos_2026")
    if not path or not Path(path).exists():
        return pd.DataFrame()
    try:
        df = pd.read_excel(path, sheet_name=sheet, engine="openpyxl")
        attrs = ["Central", "Tipo", "Estado", "Nodo Opt", "Tipo 2", "Inercia ui*Hi*Si"]
        keep = [c for c in attrs if c in df.columns]
        return df[keep].drop_duplicates("Central") if "Central" in keep else pd.DataFrame()
    except Exception:
        return pd.DataFrame()



def render_results_catalog(output_dir: str) -> None:
    """Vista simple de resultados generados."""
    st.subheader("Resultados generados")
    st.caption("Catálogo de archivos exportados por la app. Sirve para reutilizar resultados sin reprocesar.")

    paths = [
        ROOT_DIR / "workspace" / "catalog" / "results_index.csv",
        Path(output_dir) / "results_index.csv",
    ]
    paths += list((ROOT_DIR / "workspace").glob("**/results_index.csv")) if (ROOT_DIR / "workspace").exists() else []
    existing = next((p for p in paths if p.exists()), None)

    if not existing:
        st.info("Aún no hay catálogo de resultados. Ejecuta una consulta para generarlo.")
        return

    try:
        df = pd.read_csv(existing)
    except Exception as exc:
        st.error(f"No se pudo leer el catálogo: {exc}")
        return

    if df.empty:
        st.info("El catálogo existe, pero está vacío.")
        return

    st.caption(f"Catálogo: `{existing}`")
    st.dataframe(df, width="stretch")


def render_technical_analysis_tab() -> None:
    """Comparador técnico avanzado, liviano y basado en outputs ya exportados."""
    st.subheader("Análisis técnico")
    st.caption(
        "Comparador orientado a decisión. Trabaja sobre archivos ya exportados para no volver a consultar PLEXOS."
    )

    try:
        from src import advanced_comparator
    except Exception:
        advanced_comparator = None

    if advanced_comparator is not None and hasattr(advanced_comparator, "render"):
        advanced_comparator.render()
        return

    st.info("Módulo avanzado no disponible. Usa el comparador estándar mientras se configura el módulo.")
    try:
        comparator.render_comparator_tab()
    except Exception as exc:
        st.error(f"No se pudo abrir el comparador: {exc}")



def validate_run_config(
    *,
    modo: str,
    base_dir: str,
    api_path: str,
    selected_files: list[SolutionFile],
    consulta: str,
    params: dict,
    partition_mode: str,
) -> pd.DataFrame:
    """Valida configuración antes de ejecutar y devuelve una tabla para Streamlit.

    Estado: OK, Advertencia o Error. La tabla se usa para bloquear ejecución si hay errores.
    """
    rows: list[dict[str, str]] = []

    def add(item: str, estado: str, detalle: str) -> None:
        rows.append({"Ítem": item, "Estado": estado, "Detalle": detalle})

    # Modo productivo
    add("Modo", "OK", "Procesamiento resultados PLEXOS")

    # Carpeta base
    if not base_dir:
        add("Carpeta base", "Error", "No se indicó carpeta base de resultados.")
    else:
        p = Path(base_dir)
        if p.exists() and p.is_dir():
            add("Carpeta base", "OK", str(p))
        else:
            add("Carpeta base", "Error", f"La carpeta no existe: {base_dir}")

    tx_from_edited_summary = (
        consulta == "Optimización de transmisión (Plan de Transmisión - Units)"
        and params.get("etapa_tx") == "Procesado desde RESUMEN editado"
    )

    # API PLEXOS. No se requiere si solo se transforma un RESUMEN editado a planilla PLEXOS.
    if tx_from_edited_summary:
        add("API PLEXOS", "OK", "No requerida para procesar desde RESUMEN editado.")
    elif not api_path:
        add("API PLEXOS", "Error", "No se indicó ruta API PLEXOS.")
    else:
        api = Path(api_path)
        expected = [
            api / "PLEXOS_NET.Core.dll",
            api / "EEUTILITY.dll",
            api / "EnergyExemplar.PLEXOS.Utility.dll",
        ]
        found = [p.name for p in expected if p.exists()]
        if len(found) >= 2:
            add("API PLEXOS", "OK", f"DLL detectadas: {', '.join(found)}")
        elif api.exists():
            add("API PLEXOS", "Advertencia", "La ruta existe, pero no se detectaron todas las DLL esperadas.")
        else:
            add("API PLEXOS", "Error", f"La ruta API no existe: {api_path}")

    # Archivos seleccionados. No se requieren si solo se transforma un RESUMEN editado.
    n_files = len(selected_files or [])
    if tx_from_edited_summary:
        resumen_path = str(params.get("resumen_editado_path", "")).strip()
        if resumen_path and Path(resumen_path).exists():
            add("RESUMEN editado", "OK", resumen_path)
        elif resumen_path:
            add("RESUMEN editado", "Error", f"No existe el archivo: {resumen_path}")
        else:
            add("RESUMEN editado", "Error", "Debes indicar la ruta del Excel con hoja RESUMEN editada.")
    elif n_files > 0:
        add("Solution.zip seleccionados", "OK", f"{n_files:,} archivo(s)")
    else:
        add("Solution.zip seleccionados", "Error", "No hay archivos seleccionados para procesar.")

    # Años
    years = params.get("years") or []
    if years:
        try:
            years_int = [int(y) for y in years]
            add("Años", "OK", f"{min(years_int)}–{max(years_int)} ({len(years_int)} año(s))")
        except Exception:
            add("Años", "Advertencia", f"Años informados, pero no se pudieron interpretar completamente: {years}")
    else:
        add("Años", "Advertencia", "No se indicaron años; la consulta usará su valor por defecto.")

    # Samples
    samples = params.get("samples") or []
    if consulta in {"Generación de energía", "Demanda de energía (Load)", "Flujos de transmisión", "Restricciones", "Consulta personalizada PLEXOS"}:
        if samples:
            add("Samples", "OK", ", ".join(map(str, samples[:8])) + ("..." if len(samples) > 8 else ""))
        else:
            add("Samples", "Advertencia", "No se indicaron samples; se usará el sample base.")

    # Contrato de salida
    contract = OUTPUT_CONTRACTS.get(consulta, {})
    if contract:
        add(
            "Formato de salida",
            "OK",
            f"{contract.get('default', 'no definido')} · {contract.get('estructura', 'estructura no definida')}",
        )
    else:
        add("Formato de salida", "Advertencia", "No hay contrato de salida definido para esta consulta.")

    # Particionado
    if consulta in {"Demanda de energía (Load)", "Flujos de transmisión"}:
        add("Exportación particionada", "OK", "No aplica. Se exporta como XLSX incremental por escenario; la consulta usa el rango completo de años seleccionado.")
    elif consulta in {"Restricciones"} or (consulta == "Generación de energía" and params.get("modo_generacion") == "Horaria"):
        add("Exportación particionada", "OK", f"Activa por defecto/recomendación: {partition_mode}")
    elif str(partition_mode).lower().startswith("forzar"):
        add("Exportación particionada", "Advertencia", "Particionado forzado para salidas horarias/grandes.")
    else:
        add("Exportación particionada", "OK", f"{partition_mode}")

    # Riesgo de memoria
    estimated_parts = max(1, n_files) * max(1, len(years) if years else 1)
    if consulta in {"Demanda de energía (Load)", "Flujos de transmisión"}:
        add("Riesgo de memoria", "Advertencia" if estimated_parts > 20 else "OK", f"{estimated_parts} bloque(s) escenario/caso/año estimados. Se consulta el rango completo una vez por Solution.zip y se escribe directamente a Excel sin consolidar todo en memoria.")
    elif consulta in {"Restricciones"} or (consulta == "Generación de energía" and params.get("modo_generacion") == "Horaria"):
        add("Riesgo de memoria", "Advertencia" if estimated_parts > 20 else "OK", f"{estimated_parts} partición(es) estimadas. Se evita consolidar todo en memoria.")
    else:
        add("Riesgo de memoria", "OK", "Sin alerta relevante con la configuración actual.")

    return pd.DataFrame(rows, columns=["Ítem", "Estado", "Detalle"])



def render_run_validation(
    selected_files: list[SolutionFile],
    params: dict,
    consulta: str,
    api_path: str,
    base_dir: str,
) -> bool:
    """Checklist mínimo previo a ejecución. Retorna True si puede ejecutar."""
    st.markdown("**Validación previa**")
    issues = []
    warnings = []

    if not api_path:
        issues.append("Ruta API PLEXOS no informada.")
    if not base_dir or not Path(base_dir).exists():
        issues.append("Carpeta base no válida.")
    if not selected_files:
        issues.append("No hay archivos `Solution.zip` seleccionados.")
    if not params.get("years"):
        warnings.append("No se especificaron años; se usará el valor por defecto de la consulta.")
    if not params.get("samples") and consulta in {"Generación de energía", "Demanda de energía (Load)", "Flujos de transmisión", "Restricciones"}:
        warnings.append("No se especificaron samples; se usará sample base.")

    if issues:
        for i in issues:
            st.error(i)
    else:
        st.success("Configuración mínima lista para ejecutar.")

    for w in warnings:
        st.warning(w)

    return not issues




def read_tx_summary_excel(path_str: str, sheet_name: str = "RESUMEN") -> pd.DataFrame:
    """Lee una hoja RESUMEN editada por el usuario para generar planilla PLEXOS.

    Esta vía evita volver a consultar PLEXOS y permite ajustar manualmente años de activación
    antes de construir la planilla procesada.
    """
    path = Path(str(path_str or "").strip().strip('"'))
    if not path.exists():
        raise FileNotFoundError(f"No existe el archivo RESUMEN editado: {path}")
    return pd.read_excel(path, sheet_name=sheet_name, engine="openpyxl")


def warn_missing_central_dictionary(consulta: str, params: dict) -> None:
    """Advierte cuando no se carga diccionario de centrales.

    La extracción PLEXOS no se bloquea. Solo se informa que las columnas enriquecidas
    pueden quedar vacías o incompletas.
    """
    consultas_con_diccionario = {
        "Capacidad instalada",
        "Generación de energía",
        "Restricciones",
    }
    if consulta not in consultas_con_diccionario:
        return

    path = str(params.get("diccionario") or CONFIG.get("rutas", {}).get("diccionario_centrales", "") or "").strip()
    if path and Path(path).exists():
        return

    if consulta == "Capacidad instalada":
        st.warning(
            "No se cargó diccionario de centrales. La extracción continuará, pero no se completarán atributos "
            "como Tipo, Estado, Barra, Nodo Opt, Tipo 2, Costo Inv, Max Units Built ni Capacidad (c/ batería)."
        )
    elif consulta == "Generación de energía":
        st.info(
            "No se cargó diccionario de centrales. La generación se extraerá igual, pero no se agregarán atributos "
            "auxiliares para análisis por tecnología, estado, nodo o tipo de central."
        )
    elif consulta == "Restricciones":
        st.info(
            "No se cargó diccionario de centrales. Las restricciones se extraerán igual, pero la salida no tendrá "
            "clasificación auxiliar de centrales para análisis posterior."
        )


def build_current_job(
    *,
    consulta: str,
    selected_files: list[SolutionFile],
    selected_scenarios: list[str],
    selected_cases: list[str],
    params: dict,
    modo: str,
    base_dir: str,
    output_dir: str,
    api_path: str,
    sample: str,
    phase: str,
    period_yearly: str,
    period_hourly: str,
    series_type: str,
    output_mode: str,
    use_cache: bool,
    export_pref: str,
    output_layout: str,
    row_threshold: int,
    partition_mode: str,
) -> dict:
    return {
        "consulta": consulta,
        "selected_files": selected_files,
        "selected_scenarios": selected_scenarios,
        "selected_cases": selected_cases,
        "params": dict(params),
        "modo": "Real PLEXOS",
        "base_dir": base_dir,
        "output_dir": output_dir,
        "api_path": api_path,
        "sample": sample,
        "phase": phase,
        "period_yearly": period_yearly,
        "period_hourly": period_hourly,
        "series_type": series_type,
        "output_mode": output_mode,
        "use_cache": use_cache,
        "export_pref": export_pref,
        "output_layout": output_layout,
        "row_threshold": row_threshold,
        "partition_mode": partition_mode,
    }


def make_preview_df(df: pd.DataFrame | None, rows: int = PREVIEW_ROWS) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()
    return df.head(rows).copy()


def contiguous_year_blocks(years: list[int] | None) -> list[tuple[int, int]]:
    """Agrupa años seleccionados en bloques continuos.

    Ejemplo:
    [2026, 2040, 2041, 2042, 2043, 2044, 2045, 2046]
    -> [(2026, 2026), (2040, 2046)]
    """
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


def format_year_blocks(years: list[int] | None) -> str:
    blocks = contiguous_year_blocks(years)
    if not blocks:
        return "todo el período"
    labels = [str(a) if a == b else f"{a}–{b}" for a, b in blocks]
    return ", ".join(labels)



EXCEL_MAX_ROWS_APP = 1_048_576



def export_hourly_excel_by_scenario(
    *,
    consulta: str,
    selected_files: list[SolutionFile],
    env: PlexosEnv | None,
    demo: bool,
    params: dict,
    sample: str,
    phase: str,
    period_yearly: str,
    period_hourly: str,
    series_type: str,
    output_base: Path,
    job: dict,
    step,
    output_layout: str = EXPORT_LAYOUT_RECOMMENDED,
    extra_sheets: dict[str, pd.DataFrame] | None = None,
) -> tuple[Path, pd.DataFrame, str]:
    """Exporta Load y Flujos Tx con estrategia elegida por el usuario.

    Todas las estrategias consultan los años seleccionados en bloques continuos por Solution.zip.
    La diferencia está solo en cómo se empaqueta la salida:
    - Recomendado / Excel único: un XLSX con una hoja por escenario.
    - Excel por escenario: ZIP con un XLSX por escenario.
    - CSV por escenario: ZIP con un CSV por escenario.
    """
    if consulta not in {"Demanda de energía (Load)", "Flujos de transmisión"}:
        raise ValueError(f"Consulta no soportada para exportación incremental horaria: {consulta}")

    years = params.get("years") or [2030]
    years = sorted({int(y) for y in years})
    years_label = format_year_blocks(years)
    layout = str(output_layout or EXPORT_LAYOUT_RECOMMENDED).strip()
    if layout == EXPORT_LAYOUT_RECOMMENDED:
        layout = EXPORT_LAYOUT_XLSX_ONE

    step(11, f"Años de consulta agrupados en bloque(s): {years_label}")
    output_base.parent.mkdir(parents=True, exist_ok=True)
    total = max(1, len(selected_files))
    preview = pd.DataFrame()

    def _run_full_range(f: SolutionFile) -> pd.DataFrame:
        if consulta == "Demanda de energía (Load)":
            return load.run(
                [f], env, years=years, samples=params.get("samples", [sample]), demo=demo,
                progress_callback=None, phase=phase, period_hourly=period_hourly, series_type=series_type,
            )
        if consulta == "Flujos de transmisión":
            return tx_flow.run(
                [f], env, years=years, samples=params.get("samples", ["mean", "sample 1", "sample 2"]), demo=demo,
                progress_callback=None, phase=phase, period_hourly=period_hourly, series_type=series_type,
            )
        return pd.DataFrame()

    def _sheet_name_for(scenario: str, part: int) -> str:
        base = safe_sheet_name(scenario or "Escenario")
        if part <= 1:
            return base
        suffix = f"_{part}"
        return safe_sheet_name(base[: 31 - len(suffix)] + suffix)

    def _write_df_to_excel_sheet(writer, sheet_state: dict[str, dict[str, int]], scenario: str, df: pd.DataFrame) -> None:
        if df is None or df.empty:
            return
        state = sheet_state.setdefault(str(scenario or "Escenario"), {"part": 1, "row": 0})
        start_idx = 0
        n = len(df)

        while start_idx < n:
            row = int(state["row"])
            part = int(state["part"])
            header = row == 0
            header_rows = 1 if header else 0
            available = EXCEL_MAX_ROWS_APP - row - header_rows
            if available <= 0:
                state["part"] = part + 1
                state["row"] = 0
                continue
            take = min(available, n - start_idx)
            chunk = df.iloc[start_idx : start_idx + take]
            sheet_name = _sheet_name_for(scenario, int(state["part"]))
            chunk.to_excel(writer, sheet_name=sheet_name, startrow=row, index=False, header=header)
            state["row"] = row + header_rows + take
            start_idx += take

    def _update_preview(df: pd.DataFrame) -> None:
        nonlocal preview
        if preview.empty and isinstance(df, pd.DataFrame) and not df.empty:
            preview = make_preview_df(df)

    def _selected_by_scenario() -> dict[str, list[SolutionFile]]:
        grouped: dict[str, list[SolutionFile]] = {}
        for f in selected_files:
            scenario = str(getattr(f, "scenario", "Escenario") or "Escenario")
            grouped.setdefault(scenario, []).append(f)
        return grouped

    if layout == EXPORT_LAYOUT_CSV_PER_SCENARIO:
        output_path = output_base.with_suffix(".zip")
        partial_path = output_base.with_suffix(".partial.zip")
        partial_path.unlink(missing_ok=True)
        done = 0
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tmp_dir = Path(tmp)
                csv_paths: dict[str, Path] = {}
                wrote_header: dict[str, bool] = {}

                # Extra sheets como CSV auxiliares
                for name, df_extra in (extra_sheets or {}).items():
                    if isinstance(df_extra, pd.DataFrame) and not df_extra.empty:
                        p_extra = tmp_dir / safe_filename(f"{name}.csv")
                        df_extra.to_csv(p_extra, index=False, encoding="utf-8-sig")
                        csv_paths[name] = p_extra
                        wrote_header[name] = True

                for f in selected_files:
                    check_cancel(job)
                    done += 1
                    scenario = str(getattr(f, "scenario", "Escenario") or "Escenario")
                    case = str(getattr(f, "case", "") or "")
                    step(12 + int(74 * (done - 1) / total), f"{consulta} · {scenario} / {case} · rango {years_label}")
                    df_part = _run_full_range(f)
                    _update_preview(df_part)

                    csv_path = csv_paths.setdefault(scenario, tmp_dir / safe_filename(f"{scenario}.csv"))
                    header = not wrote_header.get(scenario, False)
                    (df_part if isinstance(df_part, pd.DataFrame) else pd.DataFrame()).to_csv(
                        csv_path, mode="a", index=False, header=header, encoding="utf-8-sig"
                    )
                    wrote_header[scenario] = True
                    try:
                        del df_part
                    except Exception:
                        pass
                    step(12 + int(74 * done / total), f"CSV actualizado: {scenario}")
                    check_cancel(job)

                with ZipFile(partial_path, mode="w", compression=ZIP_DEFLATED) as zf:
                    for scenario, csv_path in csv_paths.items():
                        zf.write(csv_path, arcname=csv_path.name)
            partial_path.replace(output_path)
        except Exception:
            partial_path.unlink(missing_ok=True)
            raise
        return output_path, preview, "CSV ZIP · un CSV por escenario"

    if layout == EXPORT_LAYOUT_XLSX_PER_SCENARIO:
        output_path = output_base.with_suffix(".zip")
        partial_path = output_base.with_suffix(".partial.zip")
        partial_path.unlink(missing_ok=True)
        grouped = _selected_by_scenario()
        done = 0
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tmp_dir = Path(tmp)
                xlsx_files: list[Path] = []
                for scenario, files in grouped.items():
                    xlsx_path = tmp_dir / safe_filename(f"{scenario}.xlsx")
                    sheet_state: dict[str, dict[str, int]] = {}
                    with pd.ExcelWriter(
                        xlsx_path,
                        engine="xlsxwriter",
                        datetime_format="yyyy-mm-dd hh:mm:ss",
                        date_format="yyyy-mm-dd",
                    ) as writer:
                        for sheet, df_extra in (extra_sheets or {}).items():
                            if isinstance(df_extra, pd.DataFrame) and not df_extra.empty:
                                df_extra.to_excel(writer, sheet_name=safe_sheet_name(sheet), index=False)

                        for f in files:
                            check_cancel(job)
                            done += 1
                            case = str(getattr(f, "case", "") or "")
                            step(12 + int(74 * (done - 1) / total), f"{consulta} · {scenario} / {case} · rango {years_label}")
                            df_part = _run_full_range(f)
                            _update_preview(df_part)
                            _write_df_to_excel_sheet(writer, sheet_state, scenario, df_part if isinstance(df_part, pd.DataFrame) else pd.DataFrame())
                            try:
                                del df_part
                            except Exception:
                                pass
                            step(12 + int(74 * done / total), f"Escrito en Excel: {scenario}")
                            check_cancel(job)
                        if not sheet_state:
                            pd.DataFrame({"Mensaje": ["Sin datos para exportar"]}).to_excel(writer, sheet_name="Resultados", index=False)
                    xlsx_files.append(xlsx_path)

                with ZipFile(partial_path, mode="w", compression=ZIP_DEFLATED) as zf:
                    for f in xlsx_files:
                        zf.write(f, arcname=f.name)
            partial_path.replace(output_path)
        except Exception:
            partial_path.unlink(missing_ok=True)
            raise
        return output_path, preview, "ZIP · un XLSX por escenario"

    # Default: Excel único con hojas por escenario.
    output_path = output_base.with_suffix(".xlsx")
    partial_path = output_base.with_suffix(".partial.xlsx")
    partial_path.unlink(missing_ok=True)
    sheet_state: dict[str, dict[str, int]] = {}
    done = 0
    try:
        with pd.ExcelWriter(
            partial_path,
            engine="xlsxwriter",
            datetime_format="yyyy-mm-dd hh:mm:ss",
            date_format="yyyy-mm-dd",
        ) as writer:
            for sheet, df_extra in (extra_sheets or {}).items():
                if isinstance(df_extra, pd.DataFrame) and not df_extra.empty:
                    df_extra.to_excel(writer, sheet_name=safe_sheet_name(sheet), index=False)

            for f in selected_files:
                check_cancel(job)
                done += 1
                scenario = str(getattr(f, "scenario", "Escenario") or "Escenario")
                case = str(getattr(f, "case", "") or "")
                step(12 + int(74 * (done - 1) / total), f"{consulta} · {scenario} / {case} · rango {years_label}")
                df_part = _run_full_range(f)
                _update_preview(df_part)
                _write_df_to_excel_sheet(writer, sheet_state, scenario, df_part if isinstance(df_part, pd.DataFrame) else pd.DataFrame())
                try:
                    del df_part
                except Exception:
                    pass
                step(12 + int(74 * done / total), f"Escrito en Excel: {scenario} / {case} · rango {years_label}")
                check_cancel(job)

            if not sheet_state:
                pd.DataFrame({"Mensaje": ["Sin datos para exportar"]}).to_excel(writer, sheet_name="Resultados", index=False)
        partial_path.replace(output_path)
    except Exception:
        partial_path.unlink(missing_ok=True)
        raise

    return output_path, preview, "XLSX · hoja por escenario"

def execute_processing_job(job: dict, step_callback=None) -> dict:
    t_total = time.perf_counter()
    timings: dict[str, float] = {}

    def mark(label: str, started_at: float) -> None:
        timings[label] = round(time.perf_counter() - started_at, 3)

    def step(percent: int, message: str):
        check_cancel(job)
        if step_callback:
            step_callback(percent, message)

    cache_lookup_t = time.perf_counter()
    cache_key = cache_manager.job_cache_key(job)
    if job.get("use_cache", True):
        cached = cache_manager.load_result(ROOT_DIR, cache_key)
        mark("cache_lookup_s", cache_lookup_t)
        if cached is not None:
            timings.update(cached.get("timings") or {})
            timings["cache_hit_s"] = round(time.perf_counter() - t_total, 3)
            cached["timings"] = timings
            step(100, f"Resultado recuperado desde cache ({cache_key})")
            return cached
    else:
        mark("cache_lookup_s", cache_lookup_t)

    consulta = job["consulta"]
    selected_files = job["selected_files"]
    params = job.get("params", {})
    modo = "Real PLEXOS"
    api_path = job.get("api_path", "")
    sample = job.get("sample", "mean")
    phase = job.get("phase", "LTPlan")
    period_yearly = job.get("period_yearly", "FiscalYear")
    period_hourly = job.get("period_hourly", "Interval")
    series_type = job.get("series_type", "Values")
    export_pref = job.get("export_pref", "Automático")
    output_layout = job.get("output_layout", EXPORT_LAYOUT_RECOMMENDED)
    row_threshold = int(job.get("row_threshold", 500_000))
    partition_mode = job.get("partition_mode", "Restricciones y Generación horaria (recomendado)")
    out_dir_job = ensure_output_dir(job.get("output_dir", "outputs"))

    step(6, "Inicializando entorno de ejecución")
    env_t = time.perf_counter()
    env = init_env(modo, api_path)
    if env is not None:
        env.stage_solution_zip = True
        # No forzar staging dentro de la app. plexos_api.py usa LOCALAPPDATA/TEMP
        # para evitar rutas largas o carpetas sincronizadas en OneDrive/SharePoint.
        env.staging_dir = None
    mark("inicializacion_entorno_s", env_t)
    demo = False
    consulta_t = time.perf_counter()
    callback = None
    if step_callback:
        def callback(done: int, total: int, message: str):
            total = max(total, 1)
            step(12 + int(72 * done / total), message)

    resumen = None
    extra_tables: dict[str, pd.DataFrame] = {}
    preview_df = pd.DataFrame()
    output_file: Path | None = None
    export_fmt = ""

    if consulta in {"Demanda de energía (Load)", "Flujos de transmisión"}:
        label = year_label(params.get("years", []))
        if consulta == "Demanda de energía (Load)":
            output_name = CONFIG.get("nombres", {}).get("archivo_load", "Load_PO_2026_{years}").format(years=label, year=label)
            extra_sheets = {}
            step(10, "Exportación XLSX incremental activada: Load por hojas de escenario")
        else:
            output_name = CONFIG.get("nombres", {}).get("archivo_tx_flow", "Tx_Flow_{years}").format(years=label, year=label)
            extra_sheets = {}
            step(10, "Exportación XLSX incremental activada: Flujos Tx por hojas de escenario")

        output_base = out_dir_job / strip_known_extension(output_name)
        output_file, preview_df, export_fmt = export_hourly_excel_by_scenario(
            consulta=consulta, selected_files=selected_files, env=env, demo=demo, params=params,
            sample=sample, phase=phase, period_yearly=period_yearly, period_hourly=period_hourly,
            series_type=series_type, output_base=output_base, job=job, step=step,
            output_layout=output_layout, extra_sheets=extra_sheets,
        )

    elif should_use_partitioned_export(consulta, params, partition_mode, output_layout):
        label = year_label(params.get("years", []))
        if consulta == "Generación de energía":
            output_name = CONFIG.get("nombres", {}).get("archivo_generacion_horaria", "Generacion_Horaria_{years}").format(years=label, year=label)
            extra_csvs = {}
            dic = read_generation_dictionary()
            if not dic.empty:
                extra_csvs["Diccionario_centrales"] = dic
        elif consulta == "Demanda de energía (Load)":
            output_name = CONFIG.get("nombres", {}).get("archivo_load", "Load_PO_2026_{years}").format(years=label, year=label)
            extra_csvs = {}
        elif consulta == "Flujos de transmisión":
            output_name = CONFIG.get("nombres", {}).get("archivo_tx_flow", "Tx_Flow_{years}").format(years=label, year=label)
            extra_csvs = {}
        elif consulta == "Restricciones":
            output_name = CONFIG.get("nombres", {}).get("archivo_restricciones", "Restricciones_{years}").format(years=label, year=label)
            extra_csvs = {}
            dic = read_restrictions_dictionary()
            if not dic.empty:
                extra_csvs["Diccionario_centrales"] = dic
        elif consulta == "Consulta personalizada PLEXOS":
            prop_label = safe_filename(params.get("property_name", "Custom"))
            output_name = f"custom_queries/Consulta_PLEXOS_{prop_label}_{label}"
            extra_csvs = {}
        else:
            output_name = f"Resultados_{label}"
            extra_csvs = {}

        output_base = out_dir_job / strip_known_extension(output_name)
        step(10, f"Exportación particionada activada ({partition_mode}): escenario/caso/año")
        output_file, preview_df = export_partitioned_csv_zip(
            consulta=consulta, selected_files=selected_files, env=env, demo=demo, params=params, sample=sample,
            phase=phase, period_yearly=period_yearly, period_hourly=period_hourly, series_type=series_type,
            output_base=output_base, job=job, step=step, extra_csvs=extra_csvs,
        )
        export_fmt = "CSV ZIP particionado"

    elif consulta == "Costos del sistema":
        df = costos.run(selected_files, env, sample=sample, demo=demo, progress_callback=callback, phase=phase, period_yearly=period_yearly, series_type=series_type, years=params.get("years") or None)
        if params.get("years") and "Fiscal Year" in df.columns:
            df = df[df["Fiscal Year"].isin(params["years"])].reset_index(drop=True)
        discount_rate = float(params.get("vp_discount_rate", 0.06))
        include_vp_sheets = bool(params.get("include_vp_sheets", True))
        include_full_vp = bool(params.get("include_full_vp", True))
        vp_horizons = [int(x) for x in params.get("vp_horizons", [10, 15, 20])]

        cost_tables = costos.build_cost_summary_tables(
            df,
            discount_rate=discount_rate,
            vp_horizons=vp_horizons,
            include_full_vp=include_full_vp,
        )

        sheets = {"RESUMEN": cost_tables["RESUMEN"]}
        if include_vp_sheets:
            for sheet_name, table in cost_tables.items():
                if sheet_name != "RESUMEN":
                    sheets[sheet_name] = table
        sheets.update(sheets_by_scenario(df))
        output_base = out_dir_job / strip_known_extension(CONFIG.get("nombres", {}).get("archivo_costos", "Analisis_costos"))
        step(88, "Exportando costos con estructura original")
        output_file, export_fmt = export_sheets_by_layout(sheets, output_base, output_layout, export_pref, row_threshold)
        resumen = cost_tables["RESUMEN"]
        preview_df = df

    elif consulta == "Generación de energía":
        mode_gen = params.get("modo_generacion", "Anual")
        df = generacion.run(selected_files, env, sample=sample, years=params.get("years", [2030]), samples=params.get("samples", [sample]), mode=mode_gen, demo=demo, progress_callback=callback, phase=phase, period_yearly=period_yearly, period_hourly=period_hourly, series_type=series_type)
        label = year_label(params.get("years", []))
        if mode_gen == "Horaria":
            output_name = CONFIG.get("nombres", {}).get("archivo_generacion_horaria", "Generacion_Horaria_{years}").format(years=label, year=label)
            force_csv = len(df) >= row_threshold or export_pref == "CSV"
        elif mode_gen == "Curtailment anual":
            output_name = CONFIG.get("nombres", {}).get("archivo_curtailment_anual", "Curtailed_Energy_Anual_{years}").format(years=label, year=label)
            force_csv = export_pref == "CSV"
        else:
            output_name = CONFIG.get("nombres", {}).get("archivo_generacion_anual", "Generacion_Anual_{years}").format(years=label, year=label)
            force_csv = export_pref == "CSV"
        sheets = {}
        if mode_gen in {"Anual", "Horaria"}:
            dic = read_generation_dictionary()
            if not dic.empty:
                sheets["Diccionario_centrales"] = dic
        sheets.update(sheets_by_scenario(df))
        output_base = out_dir_job / strip_known_extension(output_name)
        step(88, "Exportando generación con estructura original")
        output_file, export_fmt = export_sheets_by_layout(sheets, output_base, output_layout, export_pref, row_threshold, force_csv=force_csv)
        preview_df = df

    elif consulta == "Capacidad instalada":
        df = capacidad_instalada.run(selected_files, env, sample=sample, demo=demo, diccionario=params.get("diccionario"), sheet_name=params.get("sheet_diccionario", "Centrales_Bdatos_2026"), progress_callback=callback, phase=phase, period_yearly=period_yearly, series_type=series_type, years=params.get("years") or None)
        if params.get("years") and "Fiscal Year" in df.columns:
            df = df[df["Fiscal Year"].isin(params["years"])].reset_index(drop=True)
        label = year_label(params.get("years", []))
        output_name = CONFIG.get("nombres", {}).get("archivo_capacidad", "Cap_Inst_caso_{years}").format(years=label, year=label)
        output_base = out_dir_job / strip_known_extension(output_name)
        step(88, "Exportando capacidad instalada con hojas por escenario")
        output_file, export_fmt = export_sheets_by_layout(sheets_by_scenario(df), output_base, output_layout, export_pref, row_threshold)
        preview_df = df

    elif consulta == "Demanda de energía (Load)":
        df = load.run(selected_files, env, years=params.get("years", [2030]), samples=params.get("samples", [sample]), demo=demo, progress_callback=callback, phase=phase, period_hourly=period_hourly, series_type=series_type)
        label = year_label(params.get("years", []))
        output_name = CONFIG.get("nombres", {}).get("archivo_load", "Load_PO_2026_{years}").format(years=label, year=label)
        output_base = out_dir_job / strip_known_extension(output_name)
        step(88, "Exportando demanda Load con estructura original")
        output_file, export_fmt = export_sheets_by_layout(sheets_by_scenario(df), output_base, output_layout, export_pref, row_threshold, force_csv=(export_pref == "CSV" or len(df) >= row_threshold))
        preview_df = df

    elif consulta == "Flujos de transmisión":
        df = tx_flow.run(selected_files, env, years=params.get("years", [2030]), samples=params.get("samples", ["mean", "sample 1", "sample 2"]), demo=demo, progress_callback=callback, phase=phase, period_hourly=period_hourly, series_type=series_type)
        label = year_label(params.get("years", []))
        output_name = CONFIG.get("nombres", {}).get("archivo_tx_flow", "Tx_Flow_{years}").format(years=label, year=label)
        output_base = out_dir_job / strip_known_extension(output_name)
        step(88, "Exportando flujos Tx con estructura original")
        output_file, export_fmt = export_sheets_by_layout(sheets_by_scenario(df), output_base, output_layout, export_pref, row_threshold, force_csv=(export_pref == "CSV" or len(df) >= row_threshold))
        preview_df = df

    elif consulta == "Restricciones":
        df = restricciones.run(selected_files, env, years=params.get("years", [2030]), samples=params.get("samples", [sample]), demo=demo, progress_callback=callback, phase=phase, period_hourly=period_hourly, series_type=series_type)
        label = year_label(params.get("years", []))
        output_name = CONFIG.get("nombres", {}).get("archivo_restricciones", "Restricciones_{years}").format(years=label, year=label)
        output_base = out_dir_job / strip_known_extension(output_name)
        sheets = {}
        dic = read_restrictions_dictionary()
        if not dic.empty:
            sheets["Diccionario_centrales"] = dic
        sheets.update(sheets_by_scenario(df))
        step(88, "Exportando restricciones consolidadas")
        output_file, export_fmt = export_sheets_by_layout(sheets, output_base, output_layout, "CSV" if export_pref == "Automático" else export_pref, row_threshold, force_csv=(export_pref != "XLSX"))
        preview_df = df

    elif consulta == "Optimización de transmisión (Plan de Transmisión - Units)":
        etapa = params.get("etapa_tx")
        label = year_label(params.get("years", []))
        base_name = strip_known_extension(CONFIG.get("nombres", {}).get("archivo_tx_plan", "Plan_Tx_caso_A_2026_{years}").format(years=label, year=label))
        step(88, "Exportando optimización Tx por etapas")

        if etapa == "Procesado desde RESUMEN editado":
            resumen_tx = read_tx_summary_excel(
                params.get("resumen_editado_path", ""),
                params.get("resumen_editado_sheet", "RESUMEN"),
            )
            planilla_sheets = tx_plan.build_plexos_plan_from_summary(
                resumen_tx,
                parent_object=params.get("parent_object", "SING-SIC"),
                category=params.get("plexos_category", "Transmission Expansion"),
                scenario_prefix=params.get("scenario_prefix", "9_PlanOptimoTx"),
            )
            output_base = out_dir_job / f"{base_name}_Procesado_desde_RESUMEN"
            output_file, export_fmt = export_sheets_auto(planilla_sheets or {"Sin_activaciones": pd.DataFrame()}, output_base, "XLSX" if export_pref != "CSV" else "CSV", row_threshold)
            resumen = resumen_tx
            extra_tables = {"RESUMEN_EDITADO": resumen_tx}
            preview_df = pd.concat(planilla_sheets.values(), ignore_index=True) if planilla_sheets else pd.DataFrame()
        else:
            units_sheets, resumen_tx, planilla_sheets, df_units = tx_plan.run(
                selected_files, env, sample=sample, umbral=float(params.get("umbral_tx", 0.3)), demo=demo,
                diccionario_lineas=params.get("diccionario_lineas"), sheet_lineas=params.get("sheet_lineas", "Cap_Tx"),
                category_filter=params.get("category_filter") or None, floor_interregional=int(params.get("floor_interregional", 2032)),
                floor_zonal=int(params.get("floor_zonal", 2030)), parent_object=params.get("parent_object", "SING-SIC"),
                plexos_category=params.get("plexos_category", "Transmission Expansion"), scenario_prefix=params.get("scenario_prefix", "9_PlanOptimoTx"),
                progress_callback=callback, years=params.get("years") or None, phase=phase, period_yearly=period_yearly, series_type=series_type,
            )
            generated_files: list[Path] = []
            if etapa in {"1. Resumen de activación", "Ambas"}:
                sheets_stage1 = dict(units_sheets)
                sheets_stage1["RESUMEN"] = resumen_tx
                sheets_stage1["RESUMEN_ORIG"] = resumen_tx
                f1, _ = export_sheets_auto(sheets_stage1, out_dir_job / f"{base_name}_Etapa_1", "XLSX" if export_pref != "CSV" else "CSV", row_threshold)
                generated_files.append(f1)
            if etapa in {"2. Procesado para PLEXOS", "Ambas"}:
                f2, _ = export_sheets_auto(planilla_sheets or {"Sin_activaciones": pd.DataFrame()}, out_dir_job / f"{base_name}_Etapa_A", "XLSX" if export_pref != "CSV" else "CSV", row_threshold)
                generated_files.append(f2)
            if len(generated_files) == 1:
                output_file = generated_files[0]
                export_fmt = "XLSX" if output_file.suffix.lower() == ".xlsx" else "CSV ZIP"
            else:
                output_file = zip_files(generated_files, out_dir_job / f"{base_name}_Ambas.zip")
                export_fmt = "ZIP"
            resumen = resumen_tx
            extra_tables = {"UNITS_PROCESADOS": df_units}
            preview_df = resumen_tx if etapa == "1. Resumen de activación" else (pd.concat(planilla_sheets.values(), ignore_index=True) if planilla_sheets else pd.DataFrame())

    elif consulta == "Consulta personalizada PLEXOS":
        df = custom_query.run(
            selected_files, env, parent_class=params.get("parent_class", "System"), child_class=params.get("child_class", "Node"),
            collection_name=params.get("collection_name", "Nodes"), collection_key=params.get("collection_key", "SystemNodes"),
            property_name=params.get("property_name", "Battery Load"), years=params.get("years", [2030]), samples=params.get("samples", [sample]),
            phase=phase, period=params.get("custom_period", period_hourly), series_type=series_type,
            output_mode=params.get("custom_output_mode", "Columnas por sample"), demo=demo, progress_callback=callback,
        )
        label = year_label(params.get("years", []))
        prop_label = safe_filename(params.get("property_name", "Custom"))
        output_base = out_dir_job / "custom_queries" / f"Consulta_PLEXOS_{prop_label}_{label}"
        step(88, "Exportando consulta personalizada")
        output_file, export_fmt = export_sheets_by_layout(sheets_by_scenario(df), output_base, output_layout, export_pref, row_threshold, force_csv=(export_pref == "CSV" or len(df) >= row_threshold))
        preview_df = df
    else:
        raise ValueError(f"Consulta no reconocida: {consulta}")

    if output_file is None:
        raise RuntimeError("No se generó archivo de salida.")
    mark("consulta_y_exportacion_s", consulta_t)
    timings["total_s"] = round(time.perf_counter() - t_total, 3)
    step(96, "Guardando resultado en cache")
    preview_df = make_preview_df(preview_df)
    result_payload = {"preview_df": preview_df, "resumen": resumen, "extra_tables": extra_tables, "output_file": output_file, "export_fmt": export_fmt, "timings": timings, "cache_hit": False, "cache_key": cache_key}
    try:
        catalog_manager.record_result(ROOT_DIR, job, result_payload)
    except Exception:
        pass
    if job.get("use_cache", True):
        cache_manager.save_result(ROOT_DIR, cache_key, result_payload, job=job)
    step(100, "Procesamiento terminado")
    return result_payload


def get_background_executor() -> ThreadPoolExecutor:
    if "background_executor" not in st.session_state:
        st.session_state["background_executor"] = ThreadPoolExecutor(max_workers=1)
    return st.session_state["background_executor"]


def _background_worker(job: dict) -> dict:
    job["status"] = "Procesando"
    job["progress"] = 0
    job["current_step"] = "Inicializando"
    job.setdefault("steps", [])
    job["started_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

    def bg_step(percent: int, message: str):
        pct = max(0, min(100, int(percent)))
        job["progress"] = pct
        job["current_step"] = message
        job.setdefault("steps", []).append(f"{pct}% · {message}")

    try:
        check_cancel(job)
        result = execute_processing_job(job, step_callback=bg_step)
        output_file = result["output_file"]
        preview_df = result.get("preview_df", pd.DataFrame())
        payload = execution_log.build_log_payload(
            app_version=APP_VERSION,
            job=job,
            steps=job.get("steps", []),
            status="Terminado",
            output_file=str(output_file),
            export_fmt=result.get("export_fmt"),
            preview_df=preview_df,
            timings=result.get("timings", {}),
        )
        log_json, log_txt = execution_log.write_execution_log(job.get("output_dir", "workspace/outputs"), payload)
        job["status"] = "Terminado"
        job["progress"] = 100
        job["current_step"] = "Terminado"
        job["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        job["output_file"] = str(output_file)
        job["export_fmt"] = result.get("export_fmt")
        job["log_json"] = str(log_json)
        job["log_txt"] = str(log_txt)
        job["timings"] = result.get("timings", {})
        job["cache_hit"] = result.get("cache_hit", False)
        job["result_payload"] = {
            "consulta": job.get("consulta"),
            "df": preview_df.copy() if isinstance(preview_df, pd.DataFrame) else pd.DataFrame(),
            "resumen": result.get("resumen"),
            "extra_tables": result.get("extra_tables", {}),
            "output_file": str(output_file),
            "export_fmt": result.get("export_fmt"),
        }
        return result
    except UserCancelled as exc:
        job["status"] = "Cancelado"
        job["error"] = str(exc)
        job["progress"] = int(job.get("progress", 0) or 0)
        job["current_step"] = "Cancelado por usuario"
        job["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        payload = execution_log.build_log_payload(app_version=APP_VERSION, job=job, steps=job.get("steps", []), status="Cancelado", error=exc)
        log_json, log_txt = execution_log.write_execution_log(job.get("output_dir", "workspace/outputs"), payload)
        job["log_json"] = str(log_json)
        job["log_txt"] = str(log_txt)
        return {"preview_df": pd.DataFrame(), "resumen": None, "extra_tables": {}, "output_file": None, "export_fmt": "Cancelado", "timings": {}}
    except Exception as exc:
        job["status"] = "Fallido"
        job["error"] = str(exc)
        job["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        payload = execution_log.build_log_payload(app_version=APP_VERSION, job=job, steps=job.get("steps", []), status="Fallido", error=exc)
        log_json, log_txt = execution_log.write_execution_log(job.get("output_dir", "workspace/outputs"), payload)
        job["log_json"] = str(log_json)
        job["log_txt"] = str(log_txt)
        raise


def start_background_job(job_config: dict) -> dict:
    job = queue_manager.create_job(job_config)
    st.session_state.setdefault("processing_queue", [])
    st.session_state["processing_queue"].append(job)
    future = get_background_executor().submit(_background_worker, job)
    job["_future"] = future
    return job


modo, base_dir, output_dir, api_path, sample, phase, period_yearly, period_hourly, series_type, output_mode, use_cache, export_pref, output_layout, row_threshold, partition_mode = sidebar_params()
out_dir = ensure_output_dir(output_dir)

st.title("PLEXOS Results Manager v6.0.21")
st.caption("Procesamiento de resultados PLEXOS para generar salidas compatibles y analizar diferencias técnicas entre escenarios y casos.")

with st.expander("Instrucciones básicas", expanded=False):
    st.markdown(
        """
        **Carpeta base de resultados:** ruta raíz donde están los escenarios/casos. La app busca automáticamente archivos `Solution.zip` dentro de esa carpeta y sus subcarpetas. Para evitar errores de lectura de PLEXOS, la app copia cada `Solution.zip` a un cache local antes de consultarlo; aun así, se recomienda trabajar fuera de OneDrive o mantener las carpetas siempre disponibles en este dispositivo.

        **Carpeta de salida:** carpeta donde se guardan los archivos procesados. Si escribes `outputs`, se crea una carpeta `outputs` junto a la app. También puedes usar una ruta completa, por ejemplo `C:/PLEXOS/salidas_app`.

        **Sample base:** nombre del sample usado por consultas con una sola serie. Normalmente `mean`.

        **Samples:** para consultas horarias usa el formato recomendado `['mean', 'sample 1', 'sample 2']`. Los nombres deben estar escritos exactamente igual que en PLEXOS.

        **Años de análisis:** puedes escribir un año, varios años o un rango. Ejemplos: `2030`, `2030,2035,2040`, `2030-2035`, `2030,2035-2037`.

        **Formato de exportación:** `XLSX` genera Excel con hojas equivalentes al notebook. `CSV` genera un ZIP con los CSV equivalentes. `Automático` usa CSV ZIP cuando el resultado es grande.
        """
    )

with st.expander("Archivos, rutas y campos mínimos", expanded=False):
    st.markdown(
        """
        **Carpeta base de resultados**  
        Debes ingresar una **ruta de carpeta**, no un archivo. La app busca automáticamente `Solution.zip` dentro de esa carpeta y subcarpetas.  
        Ejemplo: `C:/PLEXOS/Salidas/PO_2026`.

        **Carpeta de salida**  
        Debes ingresar una **ruta de carpeta** o un nombre relativo. Si escribes `outputs`, la app crea esa carpeta junto a `app.py`.  
        Esta carpeta contendrá los `.xlsx`, `.csv` o `.zip` generados.

        **Ruta API PLEXOS**  
        Debes ingresar una **ruta de carpeta** donde estén las DLL de la API. Campos/archivos mínimos esperados dentro de esa carpeta:  
        `PLEXOS_NET.Core.dll`, `EEUTILITY.dll`, `EnergyExemplar.PLEXOS.Utility.dll`.

        **Diccionario de centrales**  
        Debes ingresar la **ruta completa del archivo Excel** cuando proceses capacidad instalada o cuando quieras agregar atributos a generación/restricciones.  
        Campo mínimo obligatorio: `Central`. Campos recomendados: `Tipo`, `Estado`, `Barra`, `Potencia Máxima (MW)`, `Energía Almacenada (MWh)`, `Nodo Opt`, `Tipo 2`, `Costo Inv`, `Max Units Built`, `Inercia ui*Hi*Si`.

        **Hoja diccionario de centrales**  
        Debes ingresar el **nombre exacto de la hoja** dentro del Excel anterior. No es ruta ni archivo completo. Ejemplo: `Centrales_Bdatos_2026`.

        **Diccionario de líneas**  
        Debes ingresar la **ruta completa del archivo Excel** para optimización de transmisión. Campo mínimo: `Línea` o `LineName`. Campos recomendados: `Tipo`, `Zona`, `Regional`, `VI (MMUSD)`, `Max Flow`, `Desde`, `Hasta`.

        **Hoja diccionario de líneas**  
        Debes ingresar el **nombre exacto de la hoja** dentro del Excel anterior. Ejemplo: `Cap_Tx`.

        **Samples**  
        Debes ingresar texto, no archivo. Formato recomendado: `["mean", "sample 1", "sample 2"]`. Los nombres deben coincidir exactamente con PLEXOS.
        """
    )

all_files = get_files(modo, base_dir)
files_df = solution_files_to_df(all_files)
if not all_files:
    st.warning("No se encontraron archivos *Solution.zip. Ingresa una carpeta base válida o usa modo de prueba interno.")
    st.stop()

with st.expander("Archivos Solution.zip detectados", expanded=False):
    st.dataframe(files_df, width="stretch")

scenarios = sorted(files_df["Escenario"].dropna().unique().tolist()) if not files_df.empty else []
cases = sorted(files_df["Caso"].dropna().unique().tolist()) if not files_df.empty else []

c1, c2, c3 = st.columns([1.35, 1.35, 1.45])
with c1:
    selected_scenarios = st.multiselect("Escenarios", scenarios, default=[x for x in _project_value("selected_scenarios", scenarios) if x in scenarios] or scenarios)
with c2:
    selected_cases = st.multiselect("Casos", cases, default=[x for x in _project_value("selected_cases", cases) if x in cases] or cases)
with c3:
    consulta_default = _project_value("consulta", QUERY_OPTIONS[0])
    consulta_index = QUERY_OPTIONS.index(consulta_default) if consulta_default in QUERY_OPTIONS else 0
    consulta = st.selectbox("Consulta", QUERY_OPTIONS, index=consulta_index)

st.info(f"**Qué hace esta consulta:** {QUERY_DESCRIPTIONS[consulta]}")
selected_files = filter_solution_files(all_files, selected_scenarios, selected_cases)
st.info(f"Se procesarán {len(selected_files)} archivo(s) Solution.zip.")
if output_mode == "Compatible Power BI":
    st.caption("Modo Compatible Power BI activo: las consultas estándar mantienen la estructura de columnas/hojas de los notebooks originales.")

tab_proc, tab_results, tab_analysis, tab_vis, tab_report, tab_projects, tab_queue = st.tabs(["Procesamiento", "Resultados generados", "Análisis técnico", "Visualización", "Reporte", "Proyectos", "Cola"])

with tab_proc:
    params: dict = {}
    with st.container(border=True):
        st.subheader("Parámetros de la consulta")
        if consulta == "Generación de energía":
            params["modo_generacion"] = st.radio("Nivel de generación", ["Anual", "Horaria", "Curtailment anual"], horizontal=True)
            params["years"] = year_selector("Años de análisis")
            if params["modo_generacion"] == "Horaria":
                params["samples"] = samples_input("Samples para generación horaria")
            else:
                params["samples"] = [sample]

        elif consulta == "Demanda de energía (Load)":
            params["years"] = year_selector("Años de análisis")
            params["samples"] = samples_input("Samples para demanda", default=repr(["mean"]))

        elif consulta == "Flujos de transmisión":
            params["years"] = year_selector("Años de análisis")
            params["samples"] = samples_input("Samples para flujos", default=repr(["mean", "sample 1", "sample 2"]))

        elif consulta == "Restricciones":
            params["years"] = year_selector("Años de análisis")
            params["samples"] = samples_input("Samples para restricciones", default=repr(["sample 1", "sample 2"]))
            st.caption("La salida no separa variables; conserva category_name para filtrar después en Power BI.")

        elif consulta == "Optimización de transmisión (Plan de Transmisión - Units)":
            params["etapa_tx"] = st.radio(
                "Etapa de procesamiento",
                ["1. Resumen de activación", "2. Procesado para PLEXOS", "Procesado desde RESUMEN editado", "Ambas"],
                horizontal=True,
            )
            if params["etapa_tx"] == "Procesado desde RESUMEN editado":
                st.info(
                    "Modo recomendado para ajustes manuales: usa un Excel generado previamente en Etapa 1, "
                    "edita la hoja RESUMEN y la app construye la planilla procesada para PLEXOS sin volver a consultar Solution.zip."
                )
                col_rs1, col_rs2 = st.columns([1.6, 0.8])
                with col_rs1:
                    params["resumen_editado_path"] = st.text_input("Archivo Excel con RESUMEN editado", value="", placeholder=r"C:\ruta\Plan_Tx_caso_A_2026_2030_Etapa_1.xlsx")
                with col_rs2:
                    params["resumen_editado_sheet"] = st.text_input("Hoja RESUMEN", value="RESUMEN")
                params["years"] = []
                params["umbral_tx"] = float(CONFIG.get("procesamiento", {}).get("umbral_tx_units", 0.3))
                params["diccionario_lineas"] = CONFIG.get("rutas", {}).get("diccionario_lineas", "")
                params["sheet_lineas"] = CONFIG.get("rutas", {}).get("hoja_diccionario_lineas", "Cap_Tx")
                params["category_filter"] = CONFIG.get("procesamiento", {}).get("categoria_tx_units", "Sistema-Tx-Evaluacion_Opt")
                params["floor_zonal"] = int(CONFIG.get("procesamiento", {}).get("piso_tx_zonal", 2030))
                params["floor_interregional"] = int(CONFIG.get("procesamiento", {}).get("piso_tx_interregional", 2032))
            else:
                params["years"] = year_selector("Años de análisis, opcional para filtrar", default="", allow_empty=True)
                params["umbral_tx"] = st.number_input("Umbral Units para considerar línea activa", min_value=0.0, max_value=10.0, value=float(CONFIG.get("procesamiento", {}).get("umbral_tx_units", 0.3)), step=0.1)
                col_tx1, col_tx2 = st.columns(2)
                with col_tx1:
                    params["diccionario_lineas"] = st.text_input("Diccionario de líneas", value=CONFIG.get("rutas", {}).get("diccionario_lineas", ""))
                with col_tx2:
                    params["sheet_lineas"] = st.text_input("Hoja diccionario de líneas", value=CONFIG.get("rutas", {}).get("hoja_diccionario_lineas", "Cap_Tx"))
                col_tx3, col_tx4, col_tx5 = st.columns(3)
                with col_tx3:
                    params["category_filter"] = st.text_input("Categoría a filtrar en Units", value=CONFIG.get("procesamiento", {}).get("categoria_tx_units", "Sistema-Tx-Evaluacion_Opt"))
                with col_tx4:
                    params["floor_zonal"] = st.number_input("Piso año regional/zonal", value=int(CONFIG.get("procesamiento", {}).get("piso_tx_zonal", 2030)), step=1)
                with col_tx5:
                    params["floor_interregional"] = st.number_input("Piso año interregional", value=int(CONFIG.get("procesamiento", {}).get("piso_tx_interregional", 2032)), step=1)
            with st.expander("Parámetros para planilla procesada PLEXOS"):
                params["parent_object"] = st.text_input("Parent Object", value=CONFIG.get("procesamiento", {}).get("tx_parent_object", "SING-SIC"))
                params["plexos_category"] = st.text_input("Category PLEXOS", value=CONFIG.get("procesamiento", {}).get("tx_plexos_category", "Transmission Expansion"))
                params["scenario_prefix"] = st.text_input("Prefijo Scenario", value=CONFIG.get("procesamiento", {}).get("tx_scenario_prefix", "9_PlanOptimoTx"))

        elif consulta == "Capacidad instalada":
            params["years"] = year_selector("Años de análisis, opcional para filtrar", default="", allow_empty=True)
            st.markdown(
                """
                **Diccionario de centrales:** Excel que permite replicar el cruce del notebook. Debe contener la columna `Central` y, si están disponibles, `Tipo`, `Estado`, `Barra`, `Potencia Máxima (MW)`, `Energía Almacenada (MWh)`, `Nodo Opt`, `Tipo 2`, `Costo Inv` y `Max Units Built`.

                **Hoja diccionario:** nombre exacto de la hoja dentro del Excel anterior.
                """
            )
            col_cap1, col_cap2 = st.columns([1.4, 1])
            with col_cap1:
                params["diccionario"] = st.text_input("Diccionario de centrales", value=CONFIG.get("rutas", {}).get("diccionario_centrales", ""))
            with col_cap2:
                params["sheet_diccionario"] = st.text_input("Hoja diccionario", value=CONFIG.get("rutas", {}).get("hoja_diccionario_centrales", "Centrales_Bdatos_2026"))

        elif consulta == "Costos del sistema":
            params["years"] = year_selector("Años de análisis, opcional para filtrar", default="", allow_empty=True)
            st.info(
                "VP significa Valor Presente: convierte costos futuros a un valor comparable en el año base usando una tasa de descuento. "
                "Mientras más lejos está un costo en el tiempo, menor peso tiene en VP. En esta app se usa para comparar casos con distinta distribución temporal de costos."
            )
            col_vp1, col_vp2 = st.columns([1, 1])
            with col_vp1:
                params["include_vp_sheets"] = st.checkbox("Incluir hojas de Valor Presente (VP)", value=True)
            with col_vp2:
                params["vp_discount_rate"] = st.number_input(
                    "Tasa de descuento VP (%)",
                    min_value=0.0,
                    max_value=30.0,
                    value=6.0,
                    step=0.5,
                    help="Se aplica como tasa anual para descontar los costos futuros. 6% equivale a 0,06.",
                ) / 100.0

            if params["include_vp_sheets"]:
                col_vp3, col_vp4 = st.columns([1, 1])
                with col_vp3:
                    params["include_full_vp"] = st.checkbox(
                        "Incluir RESUMEN_VP del horizonte completo",
                        value=True,
                        help="Suma descontada de todos los años disponibles en la salida filtrada.",
                    )
                with col_vp4:
                    params["vp_horizons"] = st.multiselect(
                        "Hojas VP por horizonte",
                        options=[10, 15, 20],
                        default=[10, 15, 20],
                        help="Crea hojas Resumen_VP_10, Resumen_VP_15 y/o Resumen_VP_20 usando los primeros N años disponibles por escenario/caso.",
                    )
            else:
                params["include_full_vp"] = False
                params["vp_horizons"] = []

        elif consulta == "Consulta personalizada PLEXOS":
            st.warning("Esta consulta genera una salida exploratoria separada. No reemplaza ni altera los archivos estándar compatibles con Power BI.")
            params["custom_mode"] = st.radio("Modo de definición", ["Predefinido", "Avanzado"], horizontal=True)
            preset_names = list(custom_query.PRESETS.keys())
            params["preset_name"] = st.selectbox("Consulta predefinida", preset_names, disabled=params["custom_mode"] == "Avanzado")
            preset = custom_query.PRESETS[params["preset_name"]]
            col_c1, col_c2, col_c3 = st.columns(3)
            with col_c1:
                params["parent_class"] = st.text_input("Parent class", value=preset["parent_class"] if params["custom_mode"] == "Predefinido" else "System")
                params["collection_name"] = st.text_input("Collection name", value=preset["collection_name"] if params["custom_mode"] == "Predefinido" else "Nodes")
            with col_c2:
                params["child_class"] = st.text_input("Child class", value=preset["child_class"] if params["custom_mode"] == "Predefinido" else "Node")
                params["collection_key"] = st.text_input("Collection key", value=preset["collection_key"] if params["custom_mode"] == "Predefinido" else "SystemNodes")
            with col_c3:
                params["property_name"] = st.text_input("Property name", value=preset["property_name"] if params["custom_mode"] == "Predefinido" else "Battery Load")
                default_period = preset.get("period", period_hourly if params["custom_mode"] == "Predefinido" else "Interval")
                params["custom_period"] = st.selectbox("Período de consulta", ["Interval", "FiscalYear", "Block"], index=["Interval", "FiscalYear", "Block"].index(default_period) if default_period in ["Interval", "FiscalYear", "Block"] else 0)
            params["years"] = year_selector("Años de análisis", default=default_years_text())
            params["samples"] = samples_input("Samples para consulta personalizada", default=default_samples_text())
            params["custom_output_mode"] = st.radio(
                "Estructura de salida",
                ["Columnas por sample", "Formato largo"],
                horizontal=True,
                help="Columnas por sample: una columna por cada sample. Formato largo: una fila por sample; útil para Power BI o muchos samples.",
            )
            st.caption("Ejemplo avanzado para Battery Load: Parent class = System, Child class = Node, Collection name = Nodes, Collection key = SystemNodes, Property name = Battery Load.")
            if st.button("Probar propiedad personalizada"):
                if not selected_files:
                    st.error("Selecciona al menos un Solution.zip para validar la propiedad.")
                else:
                    try:
                        env_test = init_env(modo, api_path)
                        prop_id = custom_query.validate_property(
                            selected_files[0].path,
                            env_test,
                            params["parent_class"],
                            params["child_class"],
                            params["collection_name"],
                            params["property_name"],
                        )
                        st.success(f"Propiedad válida. PropertyId: {prop_id}")
                    except Exception as exc:
                        st.error(f"No se pudo validar la propiedad: {exc}")

    with st.expander("Diagnóstico API PLEXOS", expanded=False):
        st.caption("Usa este bloque cuando el PLEXOS no logre cargar los assemblies. No procesa soluciones; solo revisa ruta, DLL y entorno Python.")
        if st.button("Ejecutar diagnóstico PLEXOS API"):
            diag = diagnose_plexos_api(api_path)
            st.code(format_diagnostics_text(diag), language="text")
            if diag.get("error"):
                st.error("La API de PLEXOS todavía presenta errores. Revisa el diagnóstico técnico.")
            else:
                st.success("La API de PLEXOS cargó correctamente.")

    warn_missing_central_dictionary(consulta, params)

    st.subheader("Validación")
    validation_df_full = validate_run_config(modo=modo, base_dir=base_dir, api_path=api_path, selected_files=selected_files, consulta=consulta, params=params, partition_mode=partition_mode)
    validation_items_to_show = ["Años", "Samples", "Formato de salida", "Riesgo de memoria"]
    validation_df = validation_df_full[validation_df_full["Ítem"].isin(validation_items_to_show)].reset_index(drop=True)
    st.dataframe(validation_df, width="stretch")
    has_blocking_error = validation_df_full["Estado"].astype(str).str.lower().isin(["error"]).any()
    hidden_errors = validation_df_full[
        validation_df_full["Estado"].astype(str).str.lower().isin(["error"])
        & ~validation_df_full["Ítem"].isin(validation_items_to_show)
    ]
    if not hidden_errors.empty:
        st.error("Hay errores de configuración en carpeta base, API PLEXOS o archivos seleccionados. Revisa los parámetros antes de ejecutar.")
    elif has_blocking_error:
        st.warning("Hay errores de configuración. Puedes ajustar parámetros antes de ejecutar.")

    st.markdown(
        """
        <style>
        button[data-testid="stBaseButton-primary"] {
            background-color: #F59E0B !important;
            border-color: #F59E0B !important;
            color: #111827 !important;
            font-weight: 700 !important;
        }
        button[data-testid="stBaseButton-primary"]:hover {
            background-color: #D97706 !important;
            border-color: #D97706 !important;
            color: #111827 !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    col_run, col_queue, col_sync = st.columns([1, 1, 1])
    run_button = col_run.button("Ejecutar en segundo plano", disabled=has_blocking_error, help="Inicia el procesamiento en la cola. Puedes cambiar de pestaña mientras continúa ejecutándose.")
    queue_button = col_queue.button("Agregar a cola", disabled=has_blocking_error)
    sync_button = col_sync.button("Ejecutar y esperar", type="primary", disabled=has_blocking_error, help="Modo clásico: mantiene la página esperando hasta terminar.")

    current_job = build_current_job(
        consulta=consulta,
        selected_files=selected_files,
        selected_scenarios=selected_scenarios,
        selected_cases=selected_cases,
        params=params,
        modo=modo,
        base_dir=base_dir,
        output_dir=output_dir,
        api_path=api_path,
        sample=sample,
        phase=phase,
        period_yearly=period_yearly,
        period_hourly=period_hourly,
        series_type=series_type,
        output_mode=output_mode,
        use_cache=use_cache,
        export_pref=export_pref,
        output_layout=output_layout,
        row_threshold=row_threshold,
        partition_mode=partition_mode,
    )

    if queue_button:
        st.session_state.setdefault("processing_queue", [])
        st.session_state["processing_queue"].append(queue_manager.create_job(current_job))
        st.success("Trabajo agregado a la cola de procesamiento.")

    if run_button:
        if not selected_files:
            st.error("No hay archivos seleccionados para procesar.")
        else:
            job = start_background_job(current_job)
            st.success(f"Trabajo iniciado en segundo plano: {job.get('id')}. Puedes cambiar de pestaña y revisar el estado en Cola de procesamiento.")
            st.info("La ejecución continúa mientras la sesión de Streamlit siga activa.")

    if sync_button:
        progress_bar = st.progress(0)
        status = st.empty()
        log_box = st.empty()
        steps: list[str] = []

        def set_step(percent: int, message: str):
            pct = max(0, min(100, int(percent)))
            progress_bar.progress(pct)
            status.info(f"{pct}% · {message}")
            steps.append(f"{pct}% · {message}")
            log_box.code("\n".join(steps[-12:]), language="text")

        try:
            set_step(2, "Validando parámetros y archivos seleccionados")
            if not selected_files:
                st.error("No hay archivos seleccionados para procesar.")
                st.stop()

            result = execute_processing_job(current_job, step_callback=set_step)
            preview_df = result["preview_df"]
            resumen = result["resumen"]
            extra_tables = result["extra_tables"]
            output_file = result["output_file"]
            export_fmt = result["export_fmt"]

            payload = execution_log.build_log_payload(
                app_version=APP_VERSION,
                job=current_job,
                steps=steps,
                status="Terminado",
                output_file=str(output_file),
                export_fmt=export_fmt,
                preview_df=preview_df,
                timings=result.get("timings", {}),
            )
            log_json, log_txt = execution_log.write_execution_log(out_dir, payload)

            st.session_state["last_visual_result"] = {
                "consulta": consulta,
                "df": preview_df.copy() if isinstance(preview_df, pd.DataFrame) else pd.DataFrame(),
                "resumen": resumen.copy() if isinstance(resumen, pd.DataFrame) else None,
                "extra_tables": extra_tables,
                "output_file": str(output_file),
                "export_fmt": export_fmt,
            }
            st.success(f"Procesamiento terminado. Archivo generado: {Path(output_file).name} ({export_fmt}).")
            if result.get("cache_hit"):
                st.info(f"Resultado recuperado desde cache: {result.get('cache_key')}")
            st.caption(f"Log generado: {log_json.name} / {log_txt.name}")
            if result.get("timings"):
                with st.expander("Perfilador de tiempo", expanded=False):
                    st.dataframe(pd.DataFrame([{"Etapa": k, "Segundos": v} for k, v in result.get("timings", {}).items()]), width="stretch")

            if resumen is not None and not resumen.empty:
                st.subheader("Resumen")
                st.dataframe(resumen, width="stretch")
            if extra_tables:
                with st.expander("Tablas adicionales generadas", expanded=False):
                    for name, data in extra_tables.items():
                        st.markdown(f"**{name}**")
                        st.dataframe(data.head(PREVIEW_ROWS), width="stretch")
            st.subheader("Vista previa")
            st.caption(f"Filas: {len(preview_df):,} | Columnas: {len(preview_df.columns):,}")
            st.dataframe(preview_df.head(PREVIEW_ROWS), width="stretch")

            mime = "application/zip" if Path(output_file).suffix.lower() == ".zip" else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            st.download_button(f"Descargar resultado ({export_fmt})", data=Path(output_file).read_bytes(), file_name=Path(output_file).name, mime=mime)
            st.download_button("Descargar log JSON", data=log_json.read_bytes(), file_name=log_json.name, mime="application/json")
            st.download_button("Descargar log TXT", data=log_txt.read_bytes(), file_name=log_txt.name, mime="text/plain")
        except UserCancelled as exc:
            st.warning(str(exc))
            payload = execution_log.build_log_payload(app_version=APP_VERSION, job=current_job, steps=steps, status="Cancelado", error=exc)
            log_json, log_txt = execution_log.write_execution_log(out_dir, payload)
            st.caption(f"Log de cancelación generado: {log_json.name} / {log_txt.name}")
        except PlexosImportError as exc:
            st.error(str(exc))
            st.info("El modo de prueba interno puede funcionar aunque Procesamiento resultados PLEXOS falle. Para Procesamiento resultados PLEXOS, revisa la ruta exacta de la API, que existan las DLL y que estés usando el entorno Python correcto.")
            payload = execution_log.build_log_payload(app_version=APP_VERSION, job=current_job, steps=steps, status="Fallido", error=exc)
            log_json, log_txt = execution_log.write_execution_log(out_dir, payload)
            st.caption(f"Log de error generado: {log_json.name} / {log_txt.name}")
            diag = getattr(exc, "diagnostics", None)
            if diag:
                with st.expander("Ver diagnóstico técnico", expanded=True):
                    st.code(format_diagnostics_text(diag), language="text")
        except Exception as exc:
            payload = execution_log.build_log_payload(app_version=APP_VERSION, job=current_job, steps=steps, status="Fallido", error=exc)
            log_json, log_txt = execution_log.write_execution_log(out_dir, payload)
            st.caption(f"Log de error generado: {log_json.name} / {log_txt.name}")
            st.exception(exc)


with tab_results:
    render_results_catalog(output_dir)

with tab_analysis:
    advanced_comparator.render_decision_comparator_tab()
    with st.expander("Comparador genérico anterior", expanded=False):
        comparator.render_comparator_tab(st.session_state.get("last_visual_result"))

with tab_vis:
    visualizations.render_visualization_tab(st.session_state.get("last_visual_result"))

with tab_report:
    st.header("Reporte automático de hallazgos")
    st.caption("Genera un resumen técnico automático sobre la tabla procesada o sobre un archivo exportado. No modifica las salidas Power BI.")
    source = st.radio("Fuente de datos del reporte", ["Último procesamiento", "Cargar archivo exportado"], horizontal=True, key="report_source")
    rep_df = pd.DataFrame()
    rep_consulta = None
    if source == "Último procesamiento":
        last = st.session_state.get("last_visual_result")
        if last and isinstance(last.get("df"), pd.DataFrame):
            rep_df = last.get("df").copy()
            rep_consulta = last.get("consulta")
            st.success(f"Usando último resultado: {rep_consulta}")
        else:
            st.info("Ejecuta un procesamiento, usa un trabajo terminado de la cola o carga un archivo.")
    else:
        uploaded = st.file_uploader("Cargar CSV, XLSX o ZIP de CSV para reporte", type=["csv", "xlsx", "zip"], key="report_upload", max_upload_size=1024)
        if uploaded is not None:
            rep_df, label = visualizations._read_uploaded_table(uploaded)
            rep_consulta = st.selectbox("Tipo de salida", QUERY_OPTIONS, key="report_query_type")
            st.success(f"Archivo cargado: {label}")
    if rep_df is not None and not rep_df.empty:
        numeric_cols = []
        for c in rep_df.columns:
            if pd.api.types.is_datetime64_any_dtype(rep_df[c]):
                continue
            if pd.api.types.is_numeric_dtype(rep_df[c]):
                numeric_cols.append(c)
                continue
            converted = pd.to_numeric(rep_df[c], errors="coerce")
            if converted.notna().any() and converted.notna().mean() > 0.75:
                numeric_cols.append(c)
        metric = st.selectbox("Métrica principal para hallazgos", ["Automática"] + numeric_cols, key="report_metric")
        metric_arg = None if metric == "Automática" else metric
        report = report_manager.generate_findings(rep_df, consulta=rep_consulta, metric=metric_arg)
        md = report_manager.to_markdown(report)
        st.markdown(md)
        report_path = report_manager.write_report(ROOT_DIR, report)
        st.caption(f"Reporte guardado: {report_path}")
        st.download_button("Descargar reporte Markdown", data=md.encode("utf-8"), file_name=report_path.name, mime="text/markdown")
        for name, df_tbl in (report.get("tables") or {}).items():
            if isinstance(df_tbl, pd.DataFrame) and not df_tbl.empty:
                with st.expander(f"Tabla: {name}", expanded=False):
                    st.dataframe(df_tbl.head(PREVIEW_ROWS), width="stretch")


with tab_projects:
    st.header("Modo proyecto")
    st.caption("Guarda y recupera configuraciones de trabajo. Sirve para no reingresar rutas, escenarios, casos, consulta y parámetros frecuentes.")
    current_project_config = {
        "modo": "Real PLEXOS",
        "base_dir": base_dir,
        "output_dir": output_dir,
        "api_path": api_path,
        "sample": sample,
        "phase": phase,
        "period_yearly": period_yearly,
        "period_hourly": period_hourly,
        "series_type": series_type,
        "output_mode": output_mode,
        "use_cache": use_cache,
        "export_pref": export_pref,
        "output_layout": output_layout,
        "row_threshold": row_threshold,
        "partition_mode": partition_mode,
        "selected_scenarios": selected_scenarios,
        "selected_cases": selected_cases,
        "consulta": consulta,
        "params": params,
    }
    col_p1, col_p2 = st.columns([1, 1])
    with col_p1:
        project_name = st.text_input("Nombre del proyecto", value=f"Proyecto_{consulta.replace(' ', '_')[:30]}")
        if st.button("Guardar proyecto actual", type="primary"):
            path = project_manager.save_project(ROOT_DIR, project_name, current_project_config)
            st.success(f"Proyecto guardado: {path.name}")
    with col_p2:
        projects = project_manager.list_projects(ROOT_DIR)
        if projects:
            labels = [p.stem for p in projects]
            selected_label = st.selectbox("Proyectos guardados", labels, key="project_tab_select")
            selected_path = projects[labels.index(selected_label)]
            c1, c2 = st.columns(2)
            if c1.button("Cargar proyecto seleccionado"):
                st.session_state["active_project"] = project_manager.load_project(selected_path)
                st.success("Proyecto cargado. La configuración se aplicará al recargar la app.")
                st.rerun()
            if c2.button("Eliminar proyecto seleccionado"):
                project_manager.delete_project(selected_path)
                st.warning("Proyecto eliminado.")
                st.rerun()
        else:
            st.info("No hay proyectos guardados.")
    with st.expander("Vista de configuración actual", expanded=False):
        st.json(current_project_config)

with tab_queue:
    st.header("Cola de procesamiento")
    st.caption("Los trabajos se ejecutan secuencialmente en segundo plano. Puedes cambiar de pestaña mientras continúan activos.")
    jobs = st.session_state.setdefault("processing_queue", [])

    if jobs:
        st.dataframe(pd.DataFrame(queue_manager.jobs_to_records(jobs)), width="stretch")
        running = [j for j in jobs if j.get("status") == "Procesando"]
        if running:
            st.info(f"Hay {len(running)} trabajo(s) en ejecución. El executor usa 1 worker para reducir riesgos con la API .NET de PLEXOS.")
            for j in running[:3]:
                st.progress(int(j.get("progress", 0)))
                st.caption(f"{j.get('id')} · {j.get('progress', 0)}% · {j.get('current_step', '')}")
            running_labels = [f"{j.get('id')} · {j.get('consulta')} · {j.get('current_step', '')}" for j in running]
            selected_run = st.selectbox("Trabajo en ejecución", running_labels, key="cancel_running_select")
            run_job = running[running_labels.index(selected_run)]
            if st.button("Solicitar detener ejecución", type="secondary", key="cancel_running_button"):
                run_job["cancel_requested"] = True
                run_job["current_step"] = "Detención solicitada; se detendrá al terminar la consulta/partición actual"
                st.warning("Detención solicitada. Si PLEXOS está dentro de una QueryToList, la app se detendrá cuando termine esa llamada y antes de la siguiente partición.")

        pending_cancel = [j for j in jobs if j.get("status") == "Pendiente"]
        if pending_cancel:
            with st.expander("Cancelar trabajos pendientes", expanded=False):
                labels_pending = [f"{j.get('id')} · {j.get('consulta')}" for j in pending_cancel]
                selected_pending = st.multiselect("Pendientes a cancelar", labels_pending, key="cancel_pending_select")
                if st.button("Cancelar pendientes seleccionados", key="cancel_pending_button"):
                    selected_set = set(selected_pending)
                    for label, jobp in zip(labels_pending, pending_cancel):
                        if label in selected_set:
                            fut = jobp.get("_future")
                            if fut is not None:
                                try:
                                    fut.cancel()
                                except Exception:
                                    pass
                            jobp["cancel_requested"] = True
                            jobp["status"] = "Cancelado"
                            jobp["current_step"] = "Cancelado antes de iniciar"
                            jobp["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                    st.rerun()
    else:
        st.info("La cola está vacía. Usa 'Ejecutar en segundo plano' o 'Agregar a cola' desde la pestaña Procesamiento.")

    c_run, c_refresh, c_clear = st.columns([1, 1, 1])
    start_pending = c_run.button("Iniciar pendientes", type="primary", disabled=not bool(jobs))
    refresh_queue = c_refresh.button("Actualizar estado", disabled=not bool(jobs))
    clear_queue = c_clear.button("Vaciar cola", disabled=not bool(jobs))

    if clear_queue:
        st.session_state["processing_queue"] = []
        st.rerun()
    if refresh_queue:
        st.rerun()
    if start_pending:
        count = 0
        for job in jobs:
            if job.get("status") == "Pendiente" and not job.get("cancel_requested") and job.get("_future") is None:
                future = get_background_executor().submit(_background_worker, job)
                job["_future"] = future
                count += 1
        st.success(f"Se iniciaron {count} trabajo(s) pendiente(s) en segundo plano.")

    completed = [j for j in jobs if j.get("status") == "Terminado"]
    if completed:
        st.subheader("Trabajos terminados")
        labels = [f"{j.get('id')} · {j.get('consulta')} · {Path(str(j.get('output_file'))).name}" for j in completed]
        selected = st.selectbox("Selecciona trabajo terminado", labels, key="queue_completed_select")
        job = completed[labels.index(selected)]
        c_use, c_dl, c_log = st.columns([1, 1, 1])
        if c_use.button("Usar para visualización/comparador"):
            payload = job.get("result_payload")
            if payload:
                st.session_state["last_visual_result"] = payload
                st.success("Resultado cargado como último procesamiento.")
            else:
                st.warning("Este trabajo no tiene DataFrame en memoria. Usa la pestaña de visualización para cargar el archivo exportado.")
        output_path = Path(str(job.get("output_file"))) if job.get("output_file") else None
        if output_path and output_path.exists():
            mime = "application/zip" if output_path.suffix.lower() == ".zip" else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            c_dl.download_button("Descargar resultado", data=output_path.read_bytes(), file_name=output_path.name, mime=mime, key=f"dl_{job.get('id')}")
        log_path = Path(str(job.get("log_txt"))) if job.get("log_txt") else None
        if log_path and log_path.exists():
            c_log.download_button("Descargar log", data=log_path.read_bytes(), file_name=log_path.name, mime="text/plain", key=f"log_{job.get('id')}")
        if job.get("timings"):
            with st.expander("Perfil de tiempo del trabajo seleccionado", expanded=False):
                st.dataframe(pd.DataFrame([{"Etapa": k, "Segundos": v} for k, v in job.get("timings", {}).items()]), width="stretch")

    with st.expander("Cache de resultados por parámetros", expanded=False):
        cache_df = cache_manager.cache_index(ROOT_DIR)
        if cache_df.empty:
            st.info("Aún no hay resultados cacheados.")
        else:
            st.dataframe(cache_df.head(1000), width="stretch")

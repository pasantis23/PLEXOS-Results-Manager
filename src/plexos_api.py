from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import os
import platform
import sys
import traceback
import shutil
import hashlib
import zipfile
import tempfile

import pandas as pd


REQUIRED_ASSEMBLIES = [
    "PLEXOS_NET.Core",
    "EEUTILITY",
    "EnergyExemplar.PLEXOS.Utility",
]


class PlexosImportError(RuntimeError):
    def __init__(self, message: str, diagnostics: dict[str, Any] | None = None):
        super().__init__(message)
        self.diagnostics = diagnostics or {}


def _candidate_api_paths(api_path: str) -> list[str]:
    """Devuelve la ruta entregada y alternativas comunes si existen."""
    candidates = []
    if api_path:
        candidates.append(api_path)

    common = [
        r"C:/Program Files/Energy Exemplar/PLEXOS 12.0 API/",
        r"C:/Program Files/Energy Exemplar/PLEXOS 11.0 API/",
        r"C:/Program Files/Energy Exemplar/PLEXOS 10.0 API/",
        r"C:/Program Files (x86)/Energy Exemplar/PLEXOS 11.0 API/",
        r"C:/Program Files (x86)/Energy Exemplar/PLEXOS 10.0 API/",
    ]
    for c in common:
        if c not in candidates:
            candidates.append(c)
    return candidates


def diagnose_plexos_api(api_path: str) -> dict[str, Any]:
    """Diagnóstico no destructivo de ruta, Python y assemblies de PLEXOS."""
    diag: dict[str, Any] = {
        "api_path": api_path,
        "python_executable": sys.executable,
        "python_version": sys.version.replace("\n", " "),
        "python_architecture": platform.architecture()[0],
        "platform": platform.platform(),
        "candidate_paths": [],
        "selected_path_exists": False,
        "required_files": {},
        "clr_import_ok": None,
        "assembly_load": {},
        "error": "",
        "traceback": "",
    }

    for c in _candidate_api_paths(api_path):
        p = Path(c)
        diag["candidate_paths"].append({
            "path": c,
            "exists": p.exists(),
            "contains_plexos_core": (p / "PLEXOS_NET.Core.dll").exists(),
        })

    api = Path(api_path)
    diag["selected_path_exists"] = api.exists()
    diag["required_files"] = {f"{asm}.dll": (api / f"{asm}.dll").exists() for asm in REQUIRED_ASSEMBLIES}

    try:
        import clr  # type: ignore
        diag["clr_import_ok"] = True
    except Exception as exc:
        diag["clr_import_ok"] = False
        diag["error"] = f"No se pudo importar pythonnet/clr: {exc!r}"
        diag["traceback"] = traceback.format_exc()
        return diag

    # Solo intenta cargar assemblies si la ruta seleccionada existe.
    if not api.exists():
        diag["error"] = "La ruta seleccionada de API PLEXOS no existe."
        return diag

    try:
        api_str = str(api)
        if api_str not in sys.path:
            sys.path.append(api_str)
        os.environ["PATH"] = api_str + os.pathsep + os.environ.get("PATH", "")
        if hasattr(os, "add_dll_directory"):
            try:
                os.add_dll_directory(api_str)
            except Exception:
                pass

        import clr  # type: ignore
        for asm in REQUIRED_ASSEMBLIES:
            dll_path = api / f"{asm}.dll"
            try:
                if dll_path.exists():
                    clr.AddReference(str(dll_path))
                else:
                    clr.AddReference(asm)
                diag["assembly_load"][asm] = "OK"
            except Exception as asm_exc:
                diag["assembly_load"][asm] = f"ERROR: {asm_exc!r}"
                raise
    except Exception as exc:
        diag["error"] = f"No se pudieron cargar assemblies: {exc!r}"
        diag["traceback"] = traceback.format_exc()

    return diag


def format_diagnostics_text(diag: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"Ruta API seleccionada: {diag.get('api_path')}")
    lines.append(f"Existe ruta seleccionada: {diag.get('selected_path_exists')}")
    lines.append(f"Python: {diag.get('python_version')}")
    lines.append(f"Ejecutable Python: {diag.get('python_executable')}")
    lines.append(f"Arquitectura Python: {diag.get('python_architecture')}")
    lines.append(f"pythonnet/clr OK: {diag.get('clr_import_ok')}")
    lines.append("")
    lines.append("DLL requeridas en la ruta seleccionada:")
    for k, v in diag.get("required_files", {}).items():
        lines.append(f"  - {k}: {'OK' if v else 'NO ENCONTRADA'}")
    lines.append("")
    lines.append("Carga de assemblies:")
    for k, v in diag.get("assembly_load", {}).items():
        lines.append(f"  - {k}: {v}")
    lines.append("")
    lines.append("Rutas alternativas detectadas:")
    for c in diag.get("candidate_paths", []):
        lines.append(f"  - {c['path']} | existe={c['exists']} | PLEXOS_NET.Core.dll={c['contains_plexos_core']}")
    if diag.get("error"):
        lines.append("")
        lines.append(f"Error: {diag.get('error')}")
    if diag.get("traceback"):
        lines.append("")
        lines.append("Traceback:")
        lines.append(str(diag.get("traceback")))
    return "\n".join(lines)


def _import_dotnet_attr(attr_name: str, module_candidates: list[str]) -> Any:
    """
    Importa un enum/clase .NET desde la primera namespace donde exista.

    En algunas instalaciones/versiones de PLEXOS, ciertos enums no están expuestos
    desde EEUTILITY.Enums, sino desde EnergyExemplar.PLEXOS.Utility.Enums.
    Los notebooks originales usaban imports wildcard desde ambas namespaces, por eso
    esta función replica ese comportamiento sin asumir una única ubicación.
    """
    errors: list[str] = []
    for module_name in module_candidates:
        try:
            module = __import__(module_name, fromlist=[attr_name])
            if hasattr(module, attr_name):
                return getattr(module, attr_name)
            errors.append(f"{module_name}: no contiene {attr_name}")
        except Exception as exc:
            errors.append(f"{module_name}: {exc!r}")
    raise ImportError(f"No se pudo importar {attr_name}. Intentos: " + " | ".join(errors))


def _enum_candidates() -> list[str]:
    return [
        "EEUTILITY.Enums",
        "EnergyExemplar.PLEXOS.Utility.Enums",
    ]



def _resolve_dotnet_enum(enum_container: Any, name: str, default_name: str | None = None) -> Any:
    """Resuelve un miembro de enum .NET por nombre con fallback seguro."""
    target = (name or default_name or "").strip()
    if not target:
        raise AttributeError("Nombre de enum vacío.")
    if hasattr(enum_container, target):
        return getattr(enum_container, target)
    # Búsqueda tolerante a mayúsculas/minúsculas.
    target_lower = target.lower()
    for attr in dir(enum_container):
        if attr.lower() == target_lower:
            return getattr(enum_container, attr)
    if default_name and hasattr(enum_container, default_name):
        return getattr(enum_container, default_name)
    available = [a for a in dir(enum_container) if not a.startswith("_")][:80]
    raise AttributeError(f"No existe el enum '{target}'. Disponibles aproximados: {available}")


@dataclass
class PlexosEnv:
    """
    Contenedor de referencias .NET/Pythonnet para consultar Solution.zip de PLEXOS.
    Se inicializa solo en modo REAL para que la app pueda abrir en equipos sin PLEXOS.

    Por defecto, antes de abrir una solución se copia el Solution.zip a un staging local
    corto fuera de la carpeta de la app. Esto evita fallas típicas al consultar archivos ubicados en OneDrive
    o rutas sincronizadas en la nube, donde PLEXOS/.NET puede fallar leyendo el
    directorio central del ZIP si el archivo no está completamente hidratado localmente.
    """
    api_path: str
    Solution: Any = None
    SimulationPhaseEnum: Any = None
    PeriodEnum: Any = None
    SeriesTypeEnum: Any = None
    String: Any = None
    stage_solution_zip: bool = True
    staging_dir: str | None = None

    def initialize(self) -> "PlexosEnv":
        diag = diagnose_plexos_api(self.api_path)
        if diag.get("error"):
            raise PlexosImportError(
                f"No se pudieron cargar assemblies de PLEXOS desde: {self.api_path}",
                diagnostics=diag,
            )

        try:
            import clr  # type: ignore
            from PLEXOS_NET.Core import Solution  # type: ignore
            from System import String  # type: ignore

            candidates = _enum_candidates()
            SimulationPhaseEnum = _import_dotnet_attr("SimulationPhaseEnum", candidates)
            PeriodEnum = _import_dotnet_attr("PeriodEnum", candidates)
            SeriesTypeEnum = _import_dotnet_attr("SeriesTypeEnum", candidates)
        except Exception as exc:
            diag["error"] = f"Los assemblies cargaron, pero falló la importación de clases/enums: {exc!r}"
            diag["traceback"] = traceback.format_exc()
            raise PlexosImportError(
                f"No se pudieron importar clases/enums de PLEXOS desde: {self.api_path}",
                diagnostics=diag,
            ) from exc

        self.Solution = Solution
        self.SimulationPhaseEnum = SimulationPhaseEnum
        self.PeriodEnum = PeriodEnum
        self.SeriesTypeEnum = SeriesTypeEnum
        self.String = String
        return self



    def phase(self, name: str = "LTPlan") -> Any:
        return _resolve_dotnet_enum(self.SimulationPhaseEnum, name, "LTPlan")

    def period(self, name: str = "FiscalYear") -> Any:
        return _resolve_dotnet_enum(self.PeriodEnum, name, "FiscalYear")

    def series_type(self, name: str = "Values") -> Any:
        return _resolve_dotnet_enum(self.SeriesTypeEnum, name, "Values")

    def _default_staging_dir(self) -> Path:
        """Directorio local corto para staging de Solution.zip antes de abrir con PLEXOS.

        Se evita usar `workspace/cache` dentro de la app porque, si la app está en
        OneDrive/SharePoint o en una ruta muy larga, Windows/.NET puede fallar al
        crear o leer el archivo temporal. Por defecto se usa una ruta local corta
        en LOCALAPPDATA o TEMP. También se puede forzar con la variable de entorno
        PLEXOS_STAGING_DIR.
        """
        if self.staging_dir:
            return Path(self.staging_dir)
        env_dir = os.environ.get("PLEXOS_STAGING_DIR")
        if env_dir:
            return Path(env_dir)
        base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
        return Path(base) / "PLEXOS_Resultados_Staging"

    def _stage_solution(self, solution_zip: str | Path) -> Path:
        """Copia Solution.zip a cache local y valida que el directorio central sea legible."""
        source = Path(solution_zip)
        if not source.exists():
            raise FileNotFoundError(f"No existe Solution.zip: {source}")

        if not self.stage_solution_zip:
            return source

        stat = source.stat()
        key_raw = f"{source.resolve()}|{stat.st_size}|{int(stat.st_mtime)}"
        key = hashlib.sha1(key_raw.encode("utf-8", errors="ignore")).hexdigest()[:16]
        target_dir = self._default_staging_dir()
        target_dir.mkdir(parents=True, exist_ok=True)

        # Nombre corto y estable: evita errores de Windows por rutas largas al usar
        # nombres de corridas extensos dentro de OneDrive/SharePoint.
        target = target_dir / f"solution_{key}.zip"

        if not target.exists() or target.stat().st_size != stat.st_size:
            tmp = target_dir / f"solution_{key}.tmp"
            tmp.unlink(missing_ok=True)
            try:
                target_dir.mkdir(parents=True, exist_ok=True)
                # Copia binaria simple. Evita copy2 porque puede intentar copiar
                # metadatos desde una ruta de nube y fallar aunque los bytes estén disponibles.
                with open(source, "rb") as src, open(tmp, "wb") as dst:
                    shutil.copyfileobj(src, dst, length=1024 * 1024 * 16)
                if tmp.stat().st_size != stat.st_size:
                    raise IOError(f"Copia incompleta: origen={stat.st_size} bytes, copia={tmp.stat().st_size} bytes")
                tmp.replace(target)
            except Exception as exc:
                tmp.unlink(missing_ok=True)
                raise RuntimeError(
                    "No se pudo copiar el Solution.zip a staging local antes de abrirlo con PLEXOS. "
                    "La versión anterior intentaba copiar dentro de la carpeta de la app; ahora se usa una ruta local corta. "
                    "Si el archivo original está en OneDrive/SharePoint, marca la carpeta como 'Mantener siempre en este dispositivo' "
                    "o copia las corridas a un disco local corto, por ejemplo C:/PLEXOS_runs/. "
                    f"Archivo origen: {source} | Destino staging: {tmp} | Error original: {exc}"
                ) from exc

        try:
            # Solo abrir/listar valida el directorio central del ZIP sin descomprimir todo.
            with zipfile.ZipFile(target, "r") as zf:
                _ = zf.namelist()[:1]
        except Exception as exc:
            raise RuntimeError(
                "El Solution.zip copiado a cache local no tiene un directorio central ZIP legible. "
                "Esto suele indicar que el archivo original no estaba completamente descargado desde la nube "
                "o que la corrida quedó incompleta/corrupta. Rehidrata o vuelve a copiar la corrida localmente. "
                f"Archivo original: {source} | Copia local: {target} | Error: {exc}"
            ) from exc

        return target

    def open_solution(self, solution_zip: str | Path):
        if self.Solution is None:
            self.initialize()
        staged_zip = self._stage_solution(solution_zip)
        sol = self.Solution()
        try:
            sol.Connection(str(staged_zip))
        except Exception as exc:
            msg = str(exc)
            if "Directorio central" in msg or "cloud" in msg.lower() or "nube" in msg.lower() or "timeout" in msg.lower():
                raise RuntimeError(
                    "PLEXOS no pudo abrir el Solution.zip. La causa más probable es lectura incompleta desde OneDrive/SharePoint "
                    "o archivo ZIP no hidratado localmente. Copia la carpeta de corridas a una ruta local fuera de OneDrive "
                    "y vuelve a ejecutar. "
                    f"Archivo usado por PLEXOS: {staged_zip} | Error original: {exc}"
                ) from exc
            raise
        return sol


def is_plexos_day_id_error(exc: Exception) -> bool:
    msg = str(exc)
    return "Day Id" in msg and "not found" in msg


def query_to_list_with_date_retry(
    sol: Any,
    env: PlexosEnv,
    phase: str,
    collection_id: Any,
    parent_name: str,
    child_name: str,
    period_name: str,
    series_type: str,
    property_id: str,
    date_from: Any,
    date_to: Any,
    timeslice_list: str,
    sample_list: str,
):
    """QueryToList con fallback para soluciones LTPlan que no tienen todos los Day Id.

    Primero intenta el rango exacto. Si PLEXOS responde `Day Id ... was not found`,
    reintenta reduciendo el último día del año final del rango y, como último recurso, consulta sin
    filtro de fecha para que el filtrado de año se haga luego en pandas.
    """
    attempts: list[tuple[Any, Any]] = [(date_from, date_to)]
    try:
        start_year = int(str(date_from).split("-")[-1]) if date_from is not None else None
    except Exception:
        start_year = None
    try:
        end_year = int(str(date_to).split("-")[-1]) if date_to is not None else start_year
    except Exception:
        end_year = start_year
    if start_year is not None and end_year is not None:
        # Si la consulta cubre un rango multianual, los cierres alternativos deben
        # aplicarse al último año, no al año inicial.
        for day in (30, 29, 28):
            attempts.append((date_from, f"{day:02d}-12-{end_year}"))
    attempts.append((None, None))

    seen: set[tuple[str, str]] = set()
    last_exc: Exception | None = None
    for start, end in attempts:
        key = (str(start), str(end))
        if key in seen:
            continue
        seen.add(key)
        try:
            return sol.QueryToList(
                env.phase(phase), collection_id, parent_name, child_name, env.period(period_name),
                env.series_type(series_type), str(property_id), start, end, timeslice_list, sample_list
            )
        except Exception as exc:
            last_exc = exc
            if not is_plexos_day_id_error(exc):
                raise
            continue

    raise RuntimeError(
        "PLEXOS no encontró uno o más Day Id para el rango solicitado. "
        "Se intentó con el rango anual, con cierres alternativos de diciembre y sin filtro de fecha, "
        "pero la consulta siguió fallando. Revisa que el año seleccionado exista en la solución y que "
        "la granularidad temporal corresponda al tipo de período seleccionado."
    ) from last_exc


def query_result_to_df(query_result: Any, columns: list[str], string_type: Any) -> pd.DataFrame:
    if not query_result:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame(
        [[row.GetProperty.Overloads[string_type](col) for col in columns] for row in query_result],
        columns=columns,
    )


def get_sample_id_list(sol: Any, samples: list[str]) -> str:
    ids = []
    for sample in samples:
        ids.append(str(sol.SampleName2Id(sample)))
    return ",".join(ids)


def as_number(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").fillna(0)


def parse_plexos_date(series: pd.Series) -> pd.Series:
    """Convierte fechas provenientes de PLEXOS a datetime pandas.

    Soporta strings tipo `dd-mm-YYYY HH:MM:SS` y objetos System.DateTime de .NET.
    """
    values = series.map(lambda x: x.ToString() if hasattr(x, "ToString") else x)
    parsed = pd.to_datetime(values, format="%d-%m-%Y %H:%M:%S", errors="coerce")
    if parsed.isna().all() and len(values) > 0:
        parsed = pd.to_datetime(values, dayfirst=True, errors="coerce")
    return parsed

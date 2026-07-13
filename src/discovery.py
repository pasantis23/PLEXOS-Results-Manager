from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
import pandas as pd


@dataclass(frozen=True)
class SolutionFile:
    scenario: str
    case: str
    path: Path
    file_name: str


def discover_solution_files(base_dir: str | Path) -> list[SolutionFile]:
    """
    Busca archivos *Solution.zip dentro de una estructura típica:
    base_dir / escenario / caso / *Solution.zip
    o base_dir / escenario / *Solution.zip.
    """
    base = Path(base_dir)
    if not base.exists():
        return []

    files: list[SolutionFile] = []
    for sol in sorted(base.rglob("*Solution.zip")):
        rel = sol.relative_to(base)
        parts = rel.parts
        if len(parts) >= 3:
            scenario = parts[0]
            case = parts[1]
        elif len(parts) == 2:
            scenario = parts[0]
            case = "No Aplica"
        else:
            scenario = "No Aplica"
            case = "No Aplica"
        files.append(SolutionFile(scenario=scenario, case=case, path=sol, file_name=sol.name))
    return files


def filter_solution_files(
    files: Iterable[SolutionFile],
    scenarios: list[str] | None = None,
    cases: list[str] | None = None,
) -> list[SolutionFile]:
    scenarios = scenarios or []
    cases = cases or []
    out = []
    for f in files:
        if scenarios and f.scenario not in scenarios:
            continue
        if cases and f.case not in cases:
            continue
        out.append(f)
    return out


def solution_files_to_df(files: Iterable[SolutionFile]) -> pd.DataFrame:
    rows = [
        {"Escenario": f.scenario, "Caso": f.case, "Archivo": f.file_name, "Ruta": str(f.path)}
        for f in files
    ]
    return pd.DataFrame(rows)

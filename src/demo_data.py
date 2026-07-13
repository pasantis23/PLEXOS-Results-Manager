from __future__ import annotations

import pandas as pd
import numpy as np


def fiscal_years(start=2026, end=2045):
    return list(range(start, end + 1))


def demo_costs(scenario: str, case: str) -> pd.DataFrame:
    years = fiscal_years()
    rng = np.random.default_rng(abs(hash((scenario, case, "costs"))) % 2**32)
    rows = []
    for y in years:
        rows.append({
            "Fiscal Year": y,
            "Cost of Unserved Energy": float(rng.uniform(0, 30)),
            "Total Generation Cost": float(rng.uniform(800_000, 1_400_000)),
            "Annualized Build Cost Gen": float(rng.uniform(50_000, 250_000)),
            "Annualized Build Cost Batt": float(rng.uniform(20_000, 120_000)),
            "Escenario": scenario,
            "Caso": case,
        })
    return pd.DataFrame(rows)


def demo_generation_annual(scenario: str, case: str, years: list[int] | None = None, value_col: str = "Generation [MWh]") -> pd.DataFrame:
    plants = ["Solar_Norte", "Eolica_Sur", "CCGT_Centro", "BESS_1"]
    years = years or fiscal_years()
    rng = np.random.default_rng(abs(hash((scenario, case, value_col))) % 2**32)
    rows = []
    for y in years:
        for p in plants:
            rows.append({"Central": p, "Fiscal Year": y, value_col: float(rng.uniform(100, 5000)), "Escenario": scenario, "caso": case})
    return pd.DataFrame(rows)


def demo_generation_hourly(scenario: str, case: str, years: list[int] | None = None, samples: list[str] | None = None) -> pd.DataFrame:
    plants = ["Solar_Norte", "Eolica_Sur", "CCGT_Centro", "BESS_1"]
    years = years or [2030]
    samples = samples or ["mean", "sample 1", "sample 2"]
    rng = np.random.default_rng(abs(hash((scenario, case, "generation_hourly"))) % 2**32)
    rows = []
    for y in years:
        for p in plants:
            for h in range(24):
                row = {"Central": p, "Fecha": f"{y}-01-01 {h:02d}:00"}
                for sample in samples:
                    row[f"Generation_{sample}"] = float(rng.uniform(0, 800))
                row["Escenario"] = scenario
                row["Case"] = case
                rows.append(row)
    return pd.DataFrame(rows)


def demo_capacity(scenario: str, case: str) -> pd.DataFrame:
    plants = ["Solar_Norte", "Eolica_Sur", "CCGT_Centro", "BESS_1"]
    types = ["Solar", "Eólica", "GNL", "Batería"]
    rng = np.random.default_rng(abs(hash((scenario, case, "capacity"))) % 2**32)
    rows = []
    for y in fiscal_years():
        for p, t in zip(plants, types):
            units = float(rng.choice([0, 1, 2]))
            pmax = float(rng.uniform(50, 500))
            installed = units * pmax
            collection = "Battery" if t == "Batería" else "Generator"
            rows.append({
                "Collection": collection,
                "Central": p,
                "Category": "",
                "Fiscal Year": y,
                "Installed Capacity": installed,
                "Units": units,
                "Tipo": t,
                "Estado": "Existente",
                "Barra": "Nodo Demo",
                "Potencia Máxima (MW)": pmax,
                "Energía Almacenada (MWh)": 4 * pmax if collection == "Battery" else 0,
                "Nodo Opt": "Nodo Demo Opt",
                "Tipo 2": t,
                "Costo Inv": float(rng.uniform(100, 1000)),
                "Max Units Built": 1,
                "Escenario": scenario,
                "Caso": case,
                "Capacidad (c/ batería)": units * pmax if collection == "Battery" else installed,
            })
    return pd.DataFrame(rows)


def demo_tx_plan(scenario: str, case: str) -> pd.DataFrame:
    lines = ["Nueva Linea Norte Centro", "Nueva Linea Centro Sur", "Refuerzo 500 kV"]
    rng = np.random.default_rng(abs(hash((scenario, case, "tx_plan"))) % 2**32)
    rows = []
    for y in fiscal_years():
        for line in lines:
            rows.append({"LineName": line, "Fiscal Year": y, "category_name": "Sistema-Tx-Evaluacion_Opt", "Units": float(rng.choice([0, 0, 0.2, 0.5, 1])), "Model": scenario})
    return pd.DataFrame(rows)


def demo_tx_flow(scenario: str, case: str, years: list[int] | None = None, samples: list[str] | None = None) -> pd.DataFrame:
    years = years or [2030]
    samples = samples or ["mean", "sample 1", "sample 2"]
    rng = np.random.default_rng(abs(hash((scenario, case, "tx_flow"))) % 2**32)
    rows = []
    for y in years:
        for line in ["Nueva Linea Norte Centro", "Nueva Linea Centro Sur"]:
            for h in range(24):
                row = {"LineName": line, "Fecha": f"{y}-01-01 {h:02d}:00"}
                for sample in samples:
                    row[f"Flow_{sample}"] = float(rng.uniform(-800, 800))
                row["Import Limit [MW]"] = 1500
                row["Export Limit [MW]"] = 1500
                row["Escenario"] = scenario
                row["caso"] = case
                rows.append(row)
    return pd.DataFrame(rows)


def demo_load(scenario: str, case: str, years: list[int] | None = None, samples: list[str] | None = None) -> pd.DataFrame:
    years = years or [2030]
    samples = samples or ["mean"]
    rng = np.random.default_rng(abs(hash((scenario, case, "load"))) % 2**32)
    rows = []
    for y in years:
        for node in ["Norte", "Centro", "Sur"]:
            for h in range(24):
                row = {"Barra": node, "Fecha": f"{y}-01-01 {h:02d}:00"}
                for sample in samples:
                    row[f"Load_{sample}"] = float(rng.uniform(500, 2500))
                    row[f"Battery Load_{sample}"] = float(rng.uniform(0, 200))
                row["Escenario"] = scenario
                row["Case"] = case
                rows.append(row)
    return pd.DataFrame(rows)


def demo_restrictions(scenario: str, case: str, years: list[int] | None = None, samples: list[str] | None = None) -> pd.DataFrame:
    years = years or [2030]
    samples = samples or ["sample 1", "sample 2"]
    rng = np.random.default_rng(abs(hash((scenario, case, "restr"))) % 2**32)
    rows = []
    for y in years:
        for child, cat in [("Central Demo", "Units Generating"), ("Restr Inercia", "Inercias"), ("Reservas", "Reservas-CPF-BESS")]:
            for h in range(24):
                row = {"child_name": child, "category_name": cat, "Fecha": f"{y}-01-01 {h:02d}:00"}
                for sample in samples:
                    row[f"Value_{sample}"] = float(rng.uniform(0, 10))
                row["Escenario"] = scenario
                row["Case"] = case
                rows.append(row)
    return pd.DataFrame(rows)

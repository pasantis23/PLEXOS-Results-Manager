from pathlib import Path
import os
import platform
import sys
import traceback

api = sys.argv[1] if len(sys.argv) > 1 else r"C:\Program Files\Energy Exemplar\PLEXOS 11.0 API"
required = ["PLEXOS_NET.Core", "EEUTILITY", "EnergyExemplar.PLEXOS.Utility"]

print("Python:", sys.version.replace("\n", " "))
print("Executable:", sys.executable)
print("Architecture:", platform.architecture()[0])
print("API path:", api)
print("API exists:", Path(api).exists())

for asm in required:
    print(f"{asm}.dll exists:", (Path(api) / f"{asm}.dll").exists())

try:
    import clr
    print("pythonnet/clr: OK")
except Exception:
    print("pythonnet/clr: ERROR")
    traceback.print_exc()
    raise SystemExit(1)

try:
    sys.path.append(api)
    os.environ["PATH"] = api + os.pathsep + os.environ.get("PATH", "")
    if hasattr(os, "add_dll_directory"):
        os.add_dll_directory(api)

    import clr
    for asm in required:
        dll = Path(api) / f"{asm}.dll"
        print("Loading", asm)
        if dll.exists():
            clr.AddReference(str(dll))
        else:
            clr.AddReference(asm)
        print("  OK")

    from PLEXOS_NET.Core import Solution
    from System import String

    def import_attr(attr_name, modules):
        errors = []
        for module_name in modules:
            try:
                module = __import__(module_name, fromlist=[attr_name])
                if hasattr(module, attr_name):
                    print(f"{attr_name}: OK desde {module_name}")
                    return getattr(module, attr_name)
                errors.append(f"{module_name}: no contiene {attr_name}")
            except Exception as exc:
                errors.append(f"{module_name}: {exc!r}")
        raise ImportError(f"No se pudo importar {attr_name}. Intentos: " + " | ".join(errors))

    enum_modules = ["EEUTILITY.Enums", "EnergyExemplar.PLEXOS.Utility.Enums"]
    SimulationPhaseEnum = import_attr("SimulationPhaseEnum", enum_modules)
    PeriodEnum = import_attr("PeriodEnum", enum_modules)
    SeriesTypeEnum = import_attr("SeriesTypeEnum", enum_modules)

    print("Imports: OK")
    print("PLEXOS API test: OK")
except Exception:
    print("PLEXOS API test: ERROR")
    traceback.print_exc()
    raise SystemExit(1)

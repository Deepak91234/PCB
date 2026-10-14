import json
import os
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

EXAMPLES = ROOT / "examples"


def _have_ngspice() -> bool:
    return bool(os.environ.get("PCBAGENT_NGSPICE") or shutil.which("ngspice"))


def _have_kicad() -> bool:
    try:
        from pcbagent.kicad import libs, tools
        tools.find_kicad_python()
        tools.find_kicad_cli()
        return libs.footprint_dir() is not None and libs.symbol_dir() is not None
    except Exception:
        return False


def _have_freerouting() -> bool:
    try:
        from pcbagent.kicad import tools
        tools.find_java()
        tools.find_freerouting()
        return True
    except Exception:
        return False


HAVE_NGSPICE = _have_ngspice()
HAVE_KICAD = _have_kicad()
HAVE_FREEROUTING = _have_freerouting()

needs_ngspice = pytest.mark.skipif(not HAVE_NGSPICE, reason="ngspice not installed")
needs_kicad = pytest.mark.skipif(not HAVE_KICAD, reason="KiCad (pcbnew + libraries) not installed")
needs_layout = pytest.mark.skipif(not (HAVE_KICAD and HAVE_FREEROUTING and HAVE_NGSPICE),
                                  reason="needs ngspice, KiCad and Freerouting")


@pytest.fixture(scope="session")
def sample_catalog(tmp_path_factory):
    from pcbagent.catalog import importer
    path = tmp_path_factory.mktemp("catalog") / "catalog.sqlite"
    importer.ensure_sample(path)
    return path


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch, sample_catalog):
    monkeypatch.setenv("PCBAGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PCBAGENT_CATALOG", str(sample_catalog))
    from pcbagent import models
    models._load.cache_clear()
    yield


def load_example(name: str, what: str) -> dict:
    return json.loads((EXAMPLES / name / f"{what}.json").read_text())


@pytest.fixture
def divider_spec():
    from pcbagent import spec
    return spec.normalize(load_example("divider", "spec"))


@pytest.fixture
def ldo_spec():
    from pcbagent import spec
    return spec.normalize(load_example("ldo", "spec"))

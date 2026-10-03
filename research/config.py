from pathlib import Path
import os

ROOT = Path(__file__).resolve().parent.parent


def home() -> Path:
    path = Path(os.environ.get("FACTOR_LAB_HOME", str(ROOT / "runtime"))).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def path_for(*parts: str) -> Path:
    path = home().joinpath(*parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _listed() -> set[str]:
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return set(cfg["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"])


def _on_disk() -> set[str]:
    return {
        f"src/{p.name}"
        for p in (ROOT / "src").iterdir()
        if p.is_dir() and (p / "__init__.py").exists()
    }


def test_every_top_level_src_package_is_in_the_wheel_list():
    missing = sorted(_on_disk() - _listed())
    assert not missing, f"missing from [tool.hatch...wheel].packages: {missing}"


def test_wheel_list_has_no_stale_entries():
    stale = sorted(_listed() - _on_disk())
    assert not stale, f"listed but not a package on disk: {stale}"

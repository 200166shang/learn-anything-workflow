import importlib.util
from pathlib import Path


_path = Path(__file__).parents[1] / "reference/solution.py"
_spec = importlib.util.spec_from_file_location("stale_recognition_solution", _path)
assert _spec and _spec.loader
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
keep = _module.keep


def test_normal_result_is_kept():
    assert keep({"observed_at": 98.0}, now=100.0, max_age=5.0)


def test_expired_result_is_rejected():
    assert not keep({"observed_at": 94.0}, now=100.0, max_age=5.0)


def test_exact_boundary_is_kept():
    assert keep({"observed_at": 95.0}, now=100.0, max_age=5.0)

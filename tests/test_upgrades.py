"""Tests for Tier 1 & Tier 2 crapper upgrades (PRs #22, #27, #29, #30, #34, #42)."""

from pathlib import Path

from crapper.analyze import analyze_files
from crapper.coverage import CoverageBundle, parse_jacoco_index, parse_lcov
from crapper.crap import make_entry
from crapper.languages.python import functions_in_source as python_functions
from crapper.languages.typescript import functions_in_source as ts_functions
from crapper.runners import _ensure_vitest_coverage, _package_manager, _vitest_install_command, _vitest_version


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_python_def_line_hit_on_import_does_not_cover_never_called_function():
    source = "def never_called(x):\n    return x + 1\n\n\ndef called(x):\n    return x * 2\n"
    lcov = parse_lcov("SF:src/demo/cov.py\nDA:1,1\nDA:2,0\nDA:5,1\nDA:6,1\nend_of_record\n")
    bundle = CoverageBundle(lcov=lcov)
    entries = [make_entry(fn, bundle.percent_for(fn)) for fn in python_functions(source, "src/demo/cov.py", None)]
    assert [(e.name, e.coverage, e.crap) for e in entries] == [
        ("never_called", 0.0, 2.0),
        ("called", 100.0, 1.0),
    ]


def test_python_factory_nested_route_handlers_scored_separately():
    source = """\
def build_router(router):
    @router.get("/users")
    def list_users(active: bool):
        if active:
            return [1]
        return []

    @router.api_route("/sync", methods=["get", "post"])
    def sync_all():
        return True

    def helper():
        return 1
"""
    fns = python_functions(source, "src/api/routes.py", "/proj")
    assert [(f.namespace, f.name, f.complexity) for f in fns] == [
        ("api.routes", "build_router", 1),
        ("api.routes", "GET /users", 2),
        ("api.routes", "GET|POST /sync", 1),
    ]


def test_typescript_var_class_field_and_unnamed_default_export():
    source = """\
export var viaVar = (x: number) => (x > 0 ? 1 : 0);
export class Box {
  field = (x: number) => (x > 0 ? 1 : 0);
  count = 0;
}
export default function (x: number) {
  if (x > 0) return 1;
  return 0;
}
"""
    fns = ts_functions(source, "src/demo/shapes.ts", "/proj")
    assert [(f.namespace, f.name, f.complexity) for f in fns] == [
        ("demo.shapes", "viaVar", 2),
        ("demo.shapes.Box", "field", 2),
        ("demo.shapes", "default", 2),
    ]


def test_missing_coverage_warns_naming_files(tmp_path, capsys):
    f = _write(tmp_path, "src/miss.py", "def f(x):\n    return x\n")
    analyze_files([f], tmp_path, CoverageBundle())
    err = capsys.readouterr().err
    assert "1 file(s) have no coverage data" in err
    assert "src/miss.py" in err


def test_vitest_semver_and_package_managers_and_jacoco_dtd(tmp_path, monkeypatch):
    _write(tmp_path, "node_modules/vitest/package.json", '{"version": "file:../evil"}\n')
    assert _vitest_version(tmp_path) is None
    _write(tmp_path, "node_modules/vitest/package.json", '{"version": "4.1.11-beta.1"}\n')
    assert _vitest_version(tmp_path) == "4.1.11-beta.1"

    _write(tmp_path, "pnpm-lock.yaml", "")
    assert _package_manager(tmp_path) == "pnpm"
    cmd = _vitest_install_command(tmp_path, "@vitest/coverage-v8")
    assert cmd[:2] == ["pnpm", "add"] and "--ignore-scripts" in cmd
    (tmp_path / "pnpm-lock.yaml").unlink()

    _write(tmp_path, "bun.lock", "")
    assert _package_manager(tmp_path) == "bun"
    assert _vitest_install_command(tmp_path, "@vitest/coverage-v8")[:2] == ["bun", "add"]
    (tmp_path / "bun.lock").unlink()

    cmds = []
    monkeypatch.setattr("crapper.runners.run_shell", lambda c, _cwd: cmds.append(c) or 1)
    assert _ensure_vitest_coverage(tmp_path) is False
    assert "--ignore-scripts" in cmds[0]

    xml = """<?xml version="1.0"?>
    <!DOCTYPE report PUBLIC "-//JACOCO//DTD Report 1.1//EN" "report.dtd" [<!ELEMENT report (class*)>]>
    <report><class name="demo/Board"><method name="go" line="4"><counter type="INSTRUCTION" missed="1" covered="3"/></method></class></report>"""
    assert parse_jacoco_index(xml)["demo.Board#go"][0].percent == 75.0


def test_bundled_parser_preferred_before_download(monkeypatch):
    import tree_sitter_language_pack as tslp
    from crapper.languages import treesitter

    treesitter.parser_for.cache_clear()
    downloads = []
    monkeypatch.setattr(tslp, "download", lambda langs: downloads.append(langs), raising=False)
    assert treesitter.parser_for("python") is not None
    assert downloads == []

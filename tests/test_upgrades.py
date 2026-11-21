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


def test_python_factory_coverage_excludes_nested_route_handler_bodies():
    source = """\
def build_router(router):
    @router.get("/users")
    def list_users(active: bool):
        if active:
            return [1]
        return []
    return router
"""
    # Factory runs (lines 2, 3, 7 hit), handler body (lines 4-6) never runs and has an unhit branch on line 4
    lcov = parse_lcov(
        "SF:src/api/routes.py\n"
        "DA:1,1\nDA:2,1\nDA:3,1\nDA:4,0\nDA:5,0\nDA:6,0\nDA:7,1\n"
        "BRDA:4,0,0,0\nBRDA:4,0,1,0\n"
        "end_of_record\n"
    )
    bundle = CoverageBundle(lcov=lcov)
    entries = [make_entry(fn, bundle.percent_for(fn)) for fn in python_functions(source, "src/api/routes.py", "/proj")]
    assert [(e.name, e.coverage) for e in entries] == [
        ("build_router", 100.0),
        ("GET /users", 0.0),
    ]


def test_custom_coverage_command_records_only_fresh_cobertura_and_overrides_stale_lcov(tmp_path):
    from crapper.coverage import load_bundle
    from crapper.runners import run_coverage

    cs = _write(tmp_path, "src/App.cs", "class App { int F(int x) { if (x > 0) return 1; return 0; } }\n")
    # Pre-existing stale Cobertura & stale LCOV with 0% branch
    _write(
        tmp_path,
        "coverage/stale.cobertura.xml",
        '<coverage><packages><package><classes><class name="App" filename="src/App.cs"><lines><line number="1" hits="0"/></lines></class></classes></package></packages></coverage>',
    )
    _write(
        tmp_path,
        "coverage/lcov.info",
        "SF:src/App.cs\nDA:1,0\nBRDA:1,0,0,0\nBRDA:1,0,1,0\nend_of_record\n",
    )
    # Custom command writes a fresh Cobertura report with line 1 covered (no branches)
    fresh_xml = tmp_path / "coverage" / "fresh.cobertura.xml"
    cmd = (
        f'python3 -c \'from pathlib import Path; '
        f'Path("{fresh_xml}").write_text("<coverage><packages><package><classes>'
        f'<class name=\\"App\\" filename=\\"src/App.cs\\"><lines><line number=\\"1\\" hits=\\"1\\"/></lines></class>'
        f'</classes></package></packages></coverage>")\''
    )
    assert run_coverage(tmp_path, [cs], cmd) == 0
    bundle = load_bundle(tmp_path, historical=False)
    assert bundle.lcov["src/App.cs"][1] == (1, 1)
    assert bundle.lcov["src/App.cs"].branches == {}


def test_powershell_explicit_test_file_is_included_in_coverage_script(tmp_path):
    from crapper.runners import _powershell_coverage_script

    test_file = _write(tmp_path, "tests/Calc.Tests.ps1", "Describe 'x' {}\n")
    script = _powershell_coverage_script(tmp_path, [test_file], tmp_path / "out.xml")
    assert "$cfg.CodeCoverage.ExcludeTests = $false;" in script
    assert "Calc.Tests.ps1" in script


def test_stale_other_language_manifest_ignored_and_short_cobertura_beats_stale_lcov(tmp_path):
    from crapper.coverage import load_bundle
    from crapper.languages import functions_in_file
    from crapper.runners import _write_collection_manifest

    cs_fn = functions_in_file("csharp", "class App { int F() => 1; }\n", "src/App.cs", str(tmp_path))[0]
    stale_report = _write(
        tmp_path,
        "coverage/old_mix.cobertura.xml",
        '<coverage><packages><package><classes><class name="App" filename="src/App.cs"><lines><line number="1" hits="1"/></lines></class></classes></package></packages></coverage>',
    )
    # Old powershell manifest still references old_mix.cobertura.xml; a fresh csharp-only run must NOT load it
    _write_collection_manifest(tmp_path, "powershell", "completed", [stale_report])
    _write_collection_manifest(tmp_path, "csharp", "failed", [])
    bundle_cs_only = load_bundle(tmp_path, historical=False, languages={"csharp"})
    assert bundle_cs_only.percent_for(cs_fn) is None

    # Short-keyed fresh Cobertura ("App.cs") beats stale exact-keyed LCOV ("src/App.cs")
    fresh_cs = _write(
        tmp_path,
        "target/coverage/csharp/fresh.cobertura.xml",
        '<coverage><packages><package><classes><class name="App" filename="App.cs"><lines><line number="1" hits="1"/></lines></class></classes></package></packages></coverage>',
    )
    _write(tmp_path, "coverage/lcov.info", "SF:src/App.cs\nDA:1,0\nend_of_record\nSF:src/Other.cs\nDA:1,1\nend_of_record\n")
    _write_collection_manifest(tmp_path, "csharp", "completed", [fresh_cs])
    bundle = load_bundle(tmp_path, historical=False, languages={"csharp"})
    assert bundle.percent_for(cs_fn) == 100.0
    # Omitted C# file in fresh Cobertura must NOT fall back to stale coverage/lcov.info
    other_fn = functions_in_file("csharp", "class Other { int G() => 1; }\n", "src/Other.cs", str(tmp_path))[0]
    assert bundle.percent_for(other_fn) is None


def test_run_id_blocks_stale_other_language_manifest_and_custom_lcov_is_loaded(tmp_path):
    from crapper.coverage import load_bundle
    from crapper.languages import functions_in_file
    from crapper.runners import run_coverage

    cs_file = _write(tmp_path, "src/App.cs", "class App { int F() => 1; }\n")
    ps_file = _write(tmp_path, "src/Util.ps1", "function Get-U { return 1 }\n")
    cs_fn = functions_in_file("csharp", cs_file.read_text(), "src/App.cs", str(tmp_path))[0]

    # Run 1: mixed C# + PowerShell custom command writes a Cobertura report covering App.cs
    cmd1 = (
        "python3 -c \"import pathlib; p = pathlib.Path('coverage/mix.cobertura.xml'); "
        "p.parent.mkdir(parents=True, exist_ok=True); "
        "p.write_text('<coverage><packages><package><classes>"
        "<class name=\\\"App\\\" filename=\\\"src/App.cs\\\"><lines><line number=\\\"1\\\" hits=\\\"1\\\"/></lines></class>"
        "</classes></package></packages></coverage>')\""
    )
    assert run_coverage(tmp_path, [cs_file, ps_file], cmd1) == 0

    # Run 2: C#-only custom command writes fresh LCOV covering App.cs at 0% (and does NOT touch PS manifest)
    cmd2 = (
        "python3 -c \"import pathlib; p = pathlib.Path('coverage/lcov.info'); "
        "p.parent.mkdir(parents=True, exist_ok=True); "
        "p.write_text('SF:src/App.cs\\nDA:1,0\\nend_of_record\\n')\""
    )
    assert run_coverage(tmp_path, [cs_file], cmd2) == 0
    # Even when load_bundle is called WITHOUT languages, latest_run.json blocks the stale PS manifest!
    bundle = load_bundle(tmp_path, historical=False)
    assert bundle.percent_for(cs_fn) == 0.0

    # Multiple fresh LCOV reports for the same file merge line hits by union and fall back on overlapping branches
    multi_src = "class Multi {\n  int F(int x) {\n    if (x > 0) return 1;\n    return 0;\n  }\n}\n"
    m_file = _write(tmp_path, "src/Multi.cs", multi_src)
    m_fn = functions_in_file("csharp", multi_src, "src/Multi.cs", str(tmp_path))[0]
    cmd3 = (
        "python3 -c \"import pathlib; "
        "p1 = pathlib.Path('target/coverage/a/lcov.info'); p1.parent.mkdir(parents=True, exist_ok=True); "
        "p1.write_text('SF:src/Multi.cs\\nDA:3,1\\nDA:4,0\\nBRDA:3,0,0,1\\nBRDA:3,0,1,0\\nend_of_record\\n'); "
        "p2 = pathlib.Path('target/coverage/b/lcov.info'); p2.parent.mkdir(parents=True, exist_ok=True); "
        "p2.write_text('SF:src/Multi.cs\\nDA:3,0\\nDA:4,1\\nBRDA:3,0,0,0\\nBRDA:3,0,1,1\\nend_of_record\\n')\""
    )
    assert run_coverage(tmp_path, [m_file], cmd3) == 0
    bundle3 = load_bundle(tmp_path, historical=False, languages={"csharp"})
    assert bundle3.percent_for(m_fn) == 100.0
    assert bundle3.lcov["src/Multi.cs"][3][0] > 0 and bundle3.lcov["src/Multi.cs"][4][0] > 0
    assert bundle3.lcov["src/Multi.cs"].branches == {}


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

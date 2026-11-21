"""Tests for C# complexity, scoped discovery, Cobertura provenance, and dotnet target resolution."""

import json
from pathlib import Path

import pytest

from crapper.cli import run
from crapper.coverage import load_bundle, parse_cobertura
from crapper.discover import is_test_file, iter_source_files, language_of
from crapper.languages import functions_in_file
from crapper.runners import dotnet_target


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_csharp_methods_only_bare_names_and_overloads():
    source = """\
namespace Demo.Core;

public partial class Box<T> {
    public Box() { if (true) {} }
    public int Value => 1;
    public int Auto { get; set; }
    public int Pick(int x) => x > 0 ? x : 0;
    public int Pick(string s) {
        int Local() => 1;
        return s?.Length ?? Local();
    }
    public class Inner {
        public int Run(int n) => n switch { 1 or 2 => 3, _ => 0 };
    }
}
"""
    found = functions_in_file("csharp", source, "src/Box.cs", "/proj")
    assert [(f.namespace, f.name, f.complexity, f.start_line) for f in found] == [
        ("Demo.Core.Box", "Pick", 2, 7),
        ("Demo.Core.Box", "Pick", 3, 8),
        ("Demo.Core.Box.Inner", "Run", 4, 13),
    ]
    for fn in found:
        assert fn.start_byte >= 0 and fn.end_byte > fn.start_byte


def test_csharp_global_and_nested_block_namespaces():
    block = "namespace A { namespace B { class C { int F() => 1; } } }\n"
    f1 = functions_in_file("csharp", block, "C.cs", "/proj")
    assert [(f.namespace, f.name) for f in f1] == [("A.B.C", "F")]

    global_src = "public class Root { public int Go() => 1; }\n"
    f2 = functions_in_file("csharp", global_src, "dir/Root.cs", "/proj")
    assert [(f.namespace, f.name) for f in f2] == [("Root", "Go")]


def test_csharp_ambiguous_generic_arities_raise():
    src = """\
namespace Demo;
public class Box<T> { public int A() => 1; }
public class Box<T, U> { public int B() => 2; }
"""
    with pytest.raises(ValueError, match="Ambiguous generic arities"):
        functions_in_file("csharp", src, "Box.cs", "/proj")


def test_csharp_scoped_discovery_does_not_hide_other_languages(tmp_path):
    _write(tmp_path, "App/App.csproj", '<Project Sdk="Microsoft.NET.Sdk" />\n')
    prod = _write(tmp_path, "App/Calc.cs", "class Calc { int F() => 1; }\n")
    _write(tmp_path, "App/obj/Debug/Calc.g.cs", "class Calc { int F() => 1; }\n")
    _write(tmp_path, "App/bin/Debug/Copy.cs", "class Calc { int F() => 1; }\n")
    _write(
        tmp_path,
        "Checks/Checks.csproj",
        '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><IsTestProject>true</IsTestProject></PropertyGroup></Project>\n',
    )
    _write(tmp_path, "Checks/CalcCheck.cs", "class CalcCheck { void T() {} }\n")

    rust_bin = _write(tmp_path, "crate/src/bin/main.rs", "fn main() {}\n")
    py_bin = _write(tmp_path, "bin/helper.py", "def help_me(): return 1\n")
    ps_bin = _write(tmp_path, "bin/Helper.ps1", "function Get-H { return 1 }\n")
    go_specs = _write(tmp_path, "Tools.Specs/run.go", "package s\nfunc Run() int { return 1 }\n")

    assert language_of("App/Form1.Designer.cs") is None
    assert not is_test_file(rust_bin)
    assert not is_test_file(py_bin)
    assert not is_test_file(ps_bin)
    assert not is_test_file(go_specs)

    found = {p.resolve() for p in iter_source_files([tmp_path])}
    assert {prod.resolve(), rust_bin.resolve(), py_bin.resolve(), ps_bin.resolve(), go_specs.resolve()} <= found
    assert all("Checks" not in p.parts and "obj" not in p.parts for p in found)


def test_cobertura_union_dedup_doctype_rejection_and_branch_fallback(tmp_path, capsys):
    # Rejects XML ENTITY declarations while safely stripping standard DOCTYPE headers
    with pytest.raises(ValueError, match="entities"):
        parse_cobertura('<!DOCTYPE coverage [<!ENTITY xxe "bad">]><coverage/>')
    assert parse_cobertura('<!DOCTYPE coverage SYSTEM "coverage-loose.dtd"[]><coverage/>') == {}

    # Class-level lines only; method lines do not double count; two reports union lines
    r1 = _write(
        tmp_path,
        "target/coverage/csharp/r1.cobertura.xml",
        """<coverage><packages><package><classes>
        <class name="Demo.C" filename="src/Box.cs">
          <methods><method name="F"><lines><line number="3" hits="99"/></lines></method></methods>
          <lines>
            <line number="3" hits="1" branch="true" condition-coverage="50% (1/2)"/>
            <line number="4" hits="0"/>
          </lines>
        </class>
        </classes></package></packages></coverage>""",
    )
    r2 = _write(
        tmp_path,
        "target/coverage/csharp/r2.cobertura.xml",
        """<coverage><packages><package><classes>
        <class name="Demo.C" filename="src/Box.cs">
          <lines>
            <line number="3" hits="0" branch="true" condition-coverage="50% (1/2)"/>
            <line number="4" hits="1"/>
          </lines>
        </class>
        </classes></package></packages></coverage>""",
    )
    # Without manifest, default load_bundle does NOT read stale/unmanifested Cobertura
    assert load_bundle(tmp_path, historical=False).lcov == {}

    # With completed manifest, both reports merge cleanly (lines 3 & 4 covered; branch fallback diagnostic)
    _write(
        tmp_path,
        "target/coverage/csharp/collection.json",
        json.dumps({
            "status": "completed",
            "language": "csharp",
            "root": tmp_path.resolve().as_posix(),
            "reports": [
                r1.relative_to(tmp_path).as_posix(),
                r2.relative_to(tmp_path).as_posix(),
            ],
        }),
    )
    bundle = load_bundle(tmp_path, historical=False)
    assert bundle.lcov["src/Box.cs"][3] == (1, 1)
    assert bundle.lcov["src/Box.cs"][4] == (1, 1)
    assert bundle.lcov["src/Box.cs"].branches == {}
    assert "line coverage fallback" in capsys.readouterr().err


def test_stale_testresults_report_ignored_in_fresh_mode(tmp_path):
    _write(
        tmp_path,
        "TestResults/old/coverage.cobertura.xml",
        '<coverage><packages><package><classes><class name="C" filename="src/C.cs"><lines><line number="1" hits="5"/></lines></class></classes></package></packages></coverage>',
    )
    assert load_bundle(tmp_path, historical=False).lcov == {}
    assert "src/C.cs" in load_bundle(tmp_path, historical=True).lcov


def test_dotnet_target_resolution(tmp_path):
    sln = _write(tmp_path, "App.sln", "")
    assert dotnet_target(tmp_path) == sln.resolve()
    _write(tmp_path, "Other.slnx", "")
    with pytest.raises(ValueError, match="Multiple .NET solutions"):
        dotnet_target(tmp_path)


def test_failed_csharp_coverage_exits_nonzero_without_writing_metrics(tmp_path, monkeypatch):
    _write(tmp_path, "C.cs", "class C { int F() => 1; }\n")
    monkeypatch.setattr("crapper.cli.run_coverage", lambda *_a: 1)
    assert run(["--root", str(tmp_path), "C.cs"]) == 1
    assert not (tmp_path / ".metrics/crap.edn").exists()

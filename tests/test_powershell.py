"""Tests for PowerShell extraction, namespace parity, and live Pester Cobertura collection."""

from pathlib import Path
import shutil

import pytest

from crapper.cli import run
from crapper.languages import functions_in_file


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_powershell_namespace_parity_relative_and_absolute(tmp_path):
    src = """\
function script:Get-Score {
    param($x, $y)
    if ($x -gt 0 -and $y -gt 0) {
        return $x + $y
    }
    elseif ($x -lt 0 -or $y -lt 0) {
        return 0
    }
    return 1
}

class Calculator {
    [int] Compute([int]$n) {
        if ($n -gt 10) { return $n }
        return 0
    }
}
"""
    for rel, expected_mod in [
        ("Tool.ps1", "Tool"),
        ("scripts/Deploy.ps1", "scripts.Deploy"),
        ("src/Core/Math.psm1", "Core.Math"),
    ]:
        full = _write(tmp_path, rel, src)
        rel_fns = functions_in_file("powershell", src, rel, str(tmp_path))
        abs_fns = functions_in_file("powershell", src, str(full), str(tmp_path))
        assert [(f.namespace, f.name, f.complexity) for f in rel_fns] == [
            (expected_mod, "Get-Score", 5),
            (f"{expected_mod}.Calculator", "Compute", 2),
        ]
        assert [(f.namespace, f.name, f.complexity) for f in abs_fns] == [
            (expected_mod, "Get-Score", 5),
            (f"{expected_mod}.Calculator", "Compute", 2),
        ]


def test_powershell_malformed_sibling_skipped_with_warning(capsys):
    src = """\
function Good-One {
    param($x)
    if ($x -gt 1) { return 1 }
    return 0
}
function Broken-One {
    if ($x -gt ) {
}
"""
    found = functions_in_file("powershell", src, "src/Mix.ps1", "/proj")
    assert [f.name for f in found] == ["Good-One"]
    assert "malformed PowerShell" in capsys.readouterr().err


@pytest.mark.skipif(not shutil.which("pwsh"), reason="pwsh not installed")
def test_powershell_live_pester_cobertura_collection(tmp_path, monkeypatch):
    from crapper.runners import run_coverage as real_run_coverage

    monkeypatch.setattr("crapper.cli.run_coverage", real_run_coverage)
    _write(
        tmp_path,
        "src/Calc.ps1",
        """\
function Add-Positive {
    param([int]$a, [int]$b)
    if ($a -gt 0 -and $b -gt 0) {
        return $a + $b
    }
    return 0
}
""",
    )
    _write(
        tmp_path,
        "tests/Calc.Tests.ps1",
        """\
BeforeAll {
    . "$PSScriptRoot/../src/Calc.ps1"
}
Describe 'Add-Positive' {
    It 'adds positives' {
        Add-Positive -a 2 -b 3 | Should -Be 5
    }
    It 'returns zero otherwise' {
        Add-Positive -a -1 -b 3 | Should -Be 0
    }
}
""",
    )
    code = run(["--root", str(tmp_path)])
    assert code == 0
    edn = (tmp_path / ".metrics" / "crap.edn").read_text(encoding="utf-8")
    assert ':namespace "Calc"' in edn
    assert ':name "Add-Positive"' in edn
    assert ":coverage 100.0" in edn

"""Run each language's coverage tool, then leave the reports for the loader.

Failures are reported and do not stop analysis. A language with no coverage
tool, or a failed run, scores its functions at 0%.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from crapper.discover import is_test_file, language_of

_MAVEN = [
    "mvn",
    "-q",
    "org.jacoco:jacoco-maven-plugin:0.8.12:prepare-agent",
    "test",
    "org.jacoco:jacoco-maven-plugin:0.8.12:report",
]


def _warn(message: str) -> None:
    print(message, file=sys.stderr)


def _show(command: str | list[str]) -> str:
    if isinstance(command, str):
        return command
    return " ".join(shlex.quote(part) for part in command)


def run_shell(command: str | list[str], cwd: Path) -> int:
    """Run a coverage command.

    A list is an argument vector and does not go through a shell, so a path
    with spaces or metacharacters stays one argument. A string is a command
    the user typed in `--coverage-command` and runs in a shell.
    """

    _warn(f"+ ({cwd}) {_show(command)}")
    try:
        if isinstance(command, str):
            completed = subprocess.run(command, cwd=cwd, shell=True)
        else:
            completed = subprocess.run(list(command), cwd=cwd)
    except OSError as exc:
        _warn(f"Coverage command failed to start: {exc}")
        return 127
    return completed.returncode


def _nearest(start: Path, marker: str, stop: Path) -> Path | None:
    current = start if start.is_dir() else start.parent
    stop = stop.resolve()
    while True:
        if (current / marker).is_file():
            return current
        if current == stop or current.parent == current:
            return None
        current = current.parent


def _clean_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)


_COVERAGE_DIRS = {"typescript", "rust", "go", "python", "csharp", "powershell"}


def _outside_language_dirs(coverage: Path, path: Path) -> bool:
    relative = path.relative_to(coverage)
    if path.name in _COVERAGE_DIRS:
        return False
    return not _COVERAGE_DIRS.intersection(relative.parts)


def _remove_cloverage_html(coverage: Path) -> None:
    for path in coverage.rglob("*.html"):
        if not _outside_language_dirs(coverage, path):
            continue
        path.unlink()


def _remove_empty_dirs(coverage: Path) -> None:
    directories = [path for path in coverage.rglob("*") if path.is_dir()]
    for path in sorted(directories, reverse=True):
        if not _outside_language_dirs(coverage, path):
            continue
        try:
            path.rmdir()
        except OSError:
            continue


def _clean_clojure(root: Path) -> None:
    """Remove Cloverage's HTML and top-level lcov without touching other reports.

    Other languages keep their directories. Files that are not Cloverage HTML
    stay, including anything a project keeps under target/coverage itself.
    """

    coverage = root / "target" / "coverage"
    if not coverage.is_dir():
        return
    lcov = coverage / "lcov.info"
    if lcov.is_file():
        lcov.unlink()
    _remove_cloverage_html(coverage)
    _remove_empty_dirs(coverage)


_VITEST_CONFIGS = (
    "vitest.config.ts",
    "vitest.config.mts",
    "vitest.config.cts",
    "vitest.config.js",
    "vitest.config.mjs",
    "vitest.config.cjs",
)


class PackageJsonError(Exception):
    """package.json is present and cannot be read as an object."""


def _package_json(package: Path) -> dict | None:
    path = package / "package.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PackageJsonError(f"{path}: {exc.msg}") from exc
    if not isinstance(data, dict):
        raise PackageJsonError(f"{path}: expected a JSON object")
    return data


def _uses_vitest(package: Path, scripts: dict) -> bool:
    if "vitest" in str(scripts.get("test", "")):
        return True
    return any((package / name).is_file() for name in _VITEST_CONFIGS)


_SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")


def _is_valid_semver(value: object) -> bool:
    return isinstance(value, str) and _SEMVER.match(value) is not None


def _vitest_version(package: Path) -> str | None:
    installed = package / "node_modules" / "vitest" / "package.json"
    if installed.is_file():
        try:
            data = json.loads(installed.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            _warn(f"{installed}: {exc.msg}")
            data = None
        if isinstance(data, dict) and _is_valid_semver(data.get("version")):
            return data["version"]
    return None


def _manifest_snapshot(package: Path) -> dict[Path, bytes | None]:
    snapshot: dict[Path, bytes | None] = {}
    for name in (
        "package.json",
        "package-lock.json",
        "npm-shrinkwrap.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "bun.lockb",
        "bun.lock",
    ):
        path = package / name
        snapshot[path] = path.read_bytes() if path.is_file() else None
    return snapshot


def _package_manager(package: Path) -> str:
    if (package / "pnpm-lock.yaml").is_file() or (package / "node_modules" / ".pnpm").is_dir():
        return "pnpm"
    if (package / "yarn.lock").is_file():
        return "yarn"
    if (package / "bun.lockb").is_file() or (package / "bun.lock").is_file():
        return "bun"
    data = _package_json(package) or {}
    pm = str(data.get("packageManager", ""))
    for candidate in ("pnpm", "yarn", "bun"):
        if pm.startswith(candidate):
            return candidate
    return "npm"


def _vitest_install_command(package: Path, spec: str) -> list[str]:
    manager = _package_manager(package)
    if manager == "pnpm":
        return ["pnpm", "add", "-D", "--no-lockfile", "--ignore-scripts", spec]
    if manager == "yarn":
        return ["yarn", "add", "-D", "--no-lockfile", "--ignore-scripts", spec]
    if manager == "bun":
        return ["bun", "add", "-d", "--no-save", "--ignore-scripts", spec]
    return ["npm", "install", "--no-save", "--no-package-lock", "--ignore-scripts", spec]


def _restore_manifests(snapshot: dict[Path, bytes | None]) -> None:
    for path, content in snapshot.items():
        if content is None:
            if path.is_file():
                path.unlink()
        elif not path.is_file() or path.read_bytes() != content:
            path.write_bytes(content)


def _ensure_vitest_coverage(package: Path) -> bool:
    """Vitest collects coverage itself. c8 never sees its worker processes."""

    if (package / "node_modules" / "@vitest" / "coverage-v8").is_dir():
        return True
    version = _vitest_version(package)
    spec = f"@vitest/coverage-v8@{version}" if version else "@vitest/coverage-v8"
    _warn(
        f"Installing {spec} so Vitest can write LCOV. "
        "package.json and the lockfile are restored afterward."
    )
    snapshot = _manifest_snapshot(package)
    try:
        code = run_shell(_vitest_install_command(package, spec), package)
    finally:
        _restore_manifests(snapshot)
    ready = code == 0 and (package / "node_modules" / "@vitest" / "coverage-v8").is_dir()
    if not ready:
        _warn("Vitest's coverage provider is missing. TypeScript coverage will score 0%.")
    return ready


def typescript_packages(root: Path, files: list[Path]) -> list[Path]:
    packages: set[Path] = set()
    root = root.resolve()
    for file in files:
        if language_of(file) != "typescript":
            continue
        package = _nearest(file, "package.json", root)
        if package is not None:
            packages.add(package.resolve())
    return sorted(packages)


def typescript_command(
    package: Path, files: list[Path], report_dir: Path
) -> list[str] | None:
    """Argument vector that writes LCOV into `report_dir`, or None when there is no test script."""

    data = _package_json(package)
    if data is None:
        return None
    scripts = data.get("scripts") or {}
    if not isinstance(scripts, dict):
        scripts = {}
    if "coverage" in scripts:
        return ["npm", "run", "coverage"]
    if _uses_vitest(package, scripts):
        return _vitest_coverage_command(package, files, report_dir)
    if "test" not in scripts:
        return None
    return [
        "npx",
        "--yes",
        "c8",
        "--reporter=lcov",
        "--reports-dir",
        str(report_dir),
        "npm",
        "test",
    ]


def _vitest_includes(package: Path, files: list[Path]) -> list[str]:
    includes: list[str] = []
    package = package.resolve()
    for file in files:
        if language_of(file) != "typescript" or is_test_file(file):
            continue
        try:
            relative = Path(file).resolve().relative_to(package)
        except ValueError:
            continue
        includes.append(relative.as_posix())
    return includes


def _vitest_coverage_command(package: Path, files: list[Path], report_dir: Path) -> list[str]:
    binary = package / "node_modules" / ".bin" / "vitest"
    if binary.is_file():
        command = [str(binary)]
    else:
        command = ["npx", "vitest"]
    command.extend(
        [
            "run",
            "--coverage",
            "--coverage.reporter=lcov",
            f"--coverage.reportsDirectory={report_dir}",
            "--coverage.reportOnFailure=true",
        ]
    )
    for include in _vitest_includes(package, files):
        command.append(f"--coverage.include={include}")
    return command


def _coverage_report(root: Path, module: Path, language: str) -> Path:
    root = root.resolve()
    module = module.resolve()
    if module == root:
        return root / "target" / "coverage" / language / "lcov.info"
    slug = module.relative_to(root).as_posix().replace("/", "__")
    return root / "target" / "coverage" / language / slug / "lcov.info"


def rust_modules(root: Path, files: list[Path]) -> list[Path]:
    root = root.resolve()
    return [
        module.resolve()
        for module in _modules_with(files, ".rs", "Cargo.toml", root)
    ]


def _rust_kind() -> str | None:
    if shutil.which("cargo-llvm-cov"):
        return "llvm-cov"
    if shutil.which("cargo-tarpaulin"):
        return "tarpaulin"
    _warn("Neither cargo-llvm-cov nor cargo-tarpaulin is installed. Installing cargo-llvm-cov.")
    if shutil.which("rustup"):
        code = run_shell(["rustup", "component", "add", "llvm-tools-preview"], Path.home())
        if code != 0:
            _warn("rustup component add llvm-tools-preview failed.")
    code = run_shell(["cargo", "install", "cargo-llvm-cov", "--locked"], Path.home())
    if code == 0 and shutil.which("cargo-llvm-cov"):
        return "llvm-cov"
    _warn("Rust coverage will score 0%.")
    return None


_PYTHON_MARKERS = ("pyproject.toml", "setup.cfg", "setup.py", "pytest.ini", "tox.ini")


def python_roots(root: Path, files: list[Path]) -> list[Path]:
    found: set[Path] = set()
    root = root.resolve()
    for file in files:
        if Path(file).suffix != ".py":
            continue
        module = None
        for marker in _PYTHON_MARKERS:
            module = _nearest(Path(file), marker, root)
            if module is not None:
                break
        found.add((module or root).resolve())
    return sorted(found)


def _python_executable(package: Path) -> str:
    for relative in (".venv/bin/python", "venv/bin/python"):
        candidate = package / relative
        if candidate.is_file():
            return str(candidate)
    return "python3"


def uses_pytest(package: Path) -> bool:
    if (package / "pytest.ini").is_file() or (package / "conftest.py").is_file():
        return True
    for name in ("pyproject.toml", "setup.cfg", "tox.ini"):
        path = package / name
        if path.is_file() and "pytest" in path.read_text(encoding="utf-8", errors="replace"):
            return True
    return False


def python_sources(package: Path, files: list[Path]) -> str:
    """Directories coverage.py should report even when a file was never imported."""

    tops: set[str] = set()
    package = package.resolve()
    for file in files:
        path = Path(file)
        if path.suffix != ".py" or is_test_file(path):
            continue
        try:
            relative = path.resolve().relative_to(package)
        except ValueError:
            continue
        tops.add(_source_name(relative))
    return ",".join(sorted(tops))


def _source_name(relative: Path) -> str:
    """A top-level module is named without .py, which coverage.py --source requires."""

    if len(relative.parts) == 1:
        return relative.stem
    return relative.parts[0]


def python_coverage_commands(
    py: str, kind: str, data_file: Path, report: Path, source: str
) -> tuple[list[str], list[str]]:
    run = [py, "-m", "coverage", "run", f"--data-file={data_file}"]
    if source:
        run.append(f"--source={source}")
    if kind == "pytest":
        run.extend(["-m", "pytest"])
    else:
        run.extend(["-m", "unittest", "discover", "-s", "."])
    lcov = [py, "-m", "coverage", "lcov", f"--data-file={data_file}", "-o", str(report)]
    return run, lcov


def _ensure_python_module(py: str, package: Path, module: str) -> bool:
    if run_shell([py, "-c", f"import {module}"], package) == 0:
        return True
    _warn(
        f"Installing {module} for Python coverage. "
        "The project requirements are left unchanged."
    )
    code = run_shell(
        [py, "-m", "pip", "install", "--disable-pip-version-check", module],
        package,
    )
    return code == 0


def rust_coverage_command(kind: str, report: Path) -> list[str]:
    if kind == "llvm-cov":
        return ["cargo", "llvm-cov", "--lcov", "--output-path", str(report)]
    return ["cargo", "tarpaulin", "--out", "Lcov", "--output-dir", str(report.parent)]


def _languages_in(files: list[Path]) -> set[str]:
    found = set()
    for file in files:
        language = language_of(file)
        if language:
            found.add(language)
    return found


def _modules_with(files: list[Path], suffix: str, marker: str, root: Path) -> list[Path]:
    modules: set[Path] = set()
    for file in files:
        if Path(file).suffix != suffix:
            continue
        module = _nearest(file, marker, root)
        if module is not None:
            modules.add(module)
    return sorted(modules)


def _cover_clojure(root: Path) -> None:
    if not (root / "deps.edn").is_file() and not (root / "bb.edn").is_file():
        _warn("No deps.edn or bb.edn; skipping Clojure coverage.")
        return
    _clean_clojure(root)
    code = run_shell(["clj", "-M:cov", "--lcov"], root)
    if code != 0:
        _warn("clj -M:cov --lcov failed; retrying without --lcov.")
        code = run_shell(["clj", "-M:cov"], root)
    if code != 0:
        _warn(f"Clojure coverage exited {code}. Clojure coverage will score 0%.")


def _cover_java(root: Path, files: list[Path]) -> None:
    modules = _modules_with(files, ".java", "pom.xml", root)
    if not modules:
        _warn("No pom.xml; skipping Java coverage.")
        return
    for module in modules:
        _clean_dir(module / "target" / "site" / "jacoco")
        exec_file = module / "target" / "jacoco.exec"
        if exec_file.exists():
            exec_file.unlink()
        code = run_shell(_MAVEN, module)
        if code != 0:
            _warn(f"Java coverage exited {code} in {module}. Java coverage will score 0%.")


def _cover_go(root: Path, files: list[Path]) -> None:
    modules = _modules_with(files, ".go", "go.mod", root)
    if not modules:
        _warn("No go.mod; skipping Go coverage.")
        return
    for module in modules:
        profile = module / "target" / "coverage" / "go" / "coverage.out"
        profile.parent.mkdir(parents=True, exist_ok=True)
        if profile.exists():
            profile.unlink()
        code = run_shell(["go", "test", "./...", f"-coverprofile={profile}"], module)
        if code != 0:
            _warn(f"Go coverage exited {code} in {module}. Go coverage will score 0%.")


def _prepare_report(report: Path) -> None:
    if report.parent.exists():
        shutil.rmtree(report.parent)
    report.parent.mkdir(parents=True, exist_ok=True)


def _cover_typescript(root: Path, files: list[Path]) -> None:
    packages = typescript_packages(root, files)
    if not packages:
        _warn("No package.json; skipping TypeScript coverage.")
        return
    for package in packages:
        report = _coverage_report(root, package, "typescript")
        try:
            command = typescript_command(package, files, report.parent)
        except PackageJsonError as exc:
            _warn(str(exc))
            continue
        if command is None:
            _warn(f"No package.json test script in {package}; skipping TypeScript coverage.")
            continue
        if _needs_vitest_provider(command) and not _ensure_vitest_coverage(package):
            continue
        _prepare_report(report)
        code = run_shell(command, package)
        if code != 0:
            _warn(f"TypeScript coverage exited {code} in {package}. TypeScript coverage will score 0%.")


def _needs_vitest_provider(command: list[str]) -> bool:
    if command[:3] == ["npm", "run", "coverage"]:
        return False
    return any(Path(part).name == "vitest" for part in command)


def _python_kind(py: str, package: Path) -> str:
    if not uses_pytest(package):
        return "unittest"
    if _ensure_python_module(py, package, "pytest"):
        return "pytest"
    _warn(f"pytest is missing in {package}. Falling back to unittest.")
    return "unittest"


def _record_python_lcov(
    package: Path, code: int, data_file: Path, lcov_cmd: str | list[str]
) -> None:
    if not data_file.exists():
        if code != 0:
            _warn(f"Python coverage exited {code} in {package}. Python coverage will score 0%.")
        return
    lcov_code = run_shell(lcov_cmd, package)
    if lcov_code != 0:
        _warn(f"coverage lcov exited {lcov_code} in {package}. Python coverage will score 0%.")
    elif code != 0:
        _warn(f"Python tests exited {code} in {package}. Coverage was still recorded.")


def _cover_python(root: Path, files: list[Path]) -> None:
    for package in python_roots(root, files):
        report = _coverage_report(root, package, "python")
        py = _python_executable(package)
        if not _ensure_python_module(py, package, "coverage"):
            _warn(f"coverage is missing in {package}. Python coverage will score 0%.")
            continue
        report.parent.mkdir(parents=True, exist_ok=True)
        data_file = report.parent / ".coverage"
        report.unlink(missing_ok=True)
        data_file.unlink(missing_ok=True)
        run_cmd, lcov_cmd = python_coverage_commands(
            py, _python_kind(py, package), data_file, report, python_sources(package, files)
        )
        _record_python_lcov(package, run_shell(run_cmd, package), data_file, lcov_cmd)


def _cover_rust(root: Path, files: list[Path]) -> None:
    modules = rust_modules(root, files)
    if not modules:
        _warn("No Cargo.toml; skipping Rust coverage.")
        return
    kind = _rust_kind()
    if kind is None:
        return
    for module in modules:
        report = _coverage_report(root, module, "rust")
        report.parent.mkdir(parents=True, exist_ok=True)
        report.unlink(missing_ok=True)
        code = run_shell(rust_coverage_command(kind, report), module)
        if code != 0:
            _warn(f"Rust coverage exited {code} in {module}. Rust coverage will score 0%.")


def _write_latest_run(root: Path, run_id: str, languages: set[str]) -> None:
    manifest_dir = root / "target" / "coverage"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": run_id,
        "languages": sorted(languages),
    }
    (manifest_dir / "latest_run.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )


def _write_collection_manifest(
    root: Path,
    language: str,
    status: str,
    reports: list[Path],
    *,
    run_id: str | None = None,
    lcov_reports: list[Path] | None = None,
) -> None:
    manifest_dir = root / "target" / "coverage" / language
    manifest_dir.mkdir(parents=True, exist_ok=True)
    root_resolved = root.resolve()
    rel_reports = [report.resolve().relative_to(root_resolved).as_posix() for report in reports]
    payload: dict[str, object] = {
        "status": status,
        "language": language,
        "root": root_resolved.as_posix(),
        "reports": rel_reports,
    }
    if run_id is not None:
        payload["run_id"] = run_id
    if lcov_reports:
        payload["lcov_reports"] = [
            report.resolve().relative_to(root_resolved).as_posix() for report in lcov_reports
        ]
    (manifest_dir / "collection.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )


def dotnet_target(root: Path, files: list[Path] | None = None) -> Path:
    """Resolve an unambiguous .sln/.slnx (or single .csproj fallback) within `root`."""

    root_resolved = root.resolve()
    solutions = sorted(
        p for p in root_resolved.iterdir() if p.is_file() and p.suffix.lower() in {".sln", ".slnx"}
    )
    if len(solutions) == 1:
        return solutions[0]
    if len(solutions) > 1:
        raise ValueError(
            f"Multiple .NET solutions found in {root_resolved}: "
            f"{', '.join(p.name for p in solutions)}. Specify one target or use --coverage-command."
        )
    if files:
        nested: set[Path] = set()
        for f in files:
            if language_of(f) != "csharp":
                continue
            cur = Path(f).resolve().parent
            while True:
                found = sorted(
                    p for p in cur.iterdir() if p.is_file() and p.suffix.lower() in {".sln", ".slnx"}
                )
                if found:
                    nested.update(found)
                    break
                if cur == root_resolved or cur.parent == cur:
                    break
                cur = cur.parent
        if len(nested) == 1:
            target = next(iter(nested))
            target.resolve().relative_to(root_resolved)
            return target
        if len(nested) > 1:
            raise ValueError(
                f"Ambiguous .NET solutions found: {', '.join(sorted(p.name for p in nested))}."
            )
    projects = sorted(
        p
        for p in root_resolved.rglob("*.csproj")
        if not {"bin", "obj", "TestResults"}.intersection(p.relative_to(root_resolved).parts)
    )
    if len(projects) == 1:
        return projects[0]
    raise ValueError(
        "C# requires one unambiguous solution (.sln or .slnx) or project (.csproj) "
        f"under {root_resolved}; otherwise supply --coverage-command."
    )


def _cover_csharp(root: Path, files: list[Path], *, run_id: str | None = None) -> int:
    run_dir = root / "target" / "coverage" / "csharp"
    _clean_dir(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    _write_collection_manifest(root, "csharp", "in_progress", [], run_id=run_id)
    if not shutil.which("dotnet"):
        _warn("dotnet SDK not found on PATH; C# coverage unavailable.")
        _write_collection_manifest(root, "csharp", "failed", [], run_id=run_id)
        return 1
    try:
        target = dotnet_target(root, files)
    except ValueError as exc:
        _warn(str(exc))
        _write_collection_manifest(root, "csharp", "failed", [], run_id=run_id)
        return 1
    rel_target = target.resolve().relative_to(root.resolve()).as_posix()
    code = run_shell(
        [
            "dotnet",
            "test",
            rel_target,
            "--nologo",
            "--collect:XPlat Code Coverage",
            "--results-directory",
            str(run_dir),
        ],
        root,
    )
    reports = sorted(run_dir.glob("**/*cobertura*.xml"))
    if code != 0 or not reports:
        if code == 0 and not reports:
            _warn(
                "dotnet test produced no Cobertura XML. Ensure coverlet.collector is referenced "
                "in your VSTest test project, or supply --coverage-command."
            )
        _write_collection_manifest(root, "csharp", "failed", [], run_id=run_id)
        return code or 1
    _write_collection_manifest(root, "csharp", "completed", reports, run_id=run_id)
    return 0


def _powershell_coverage_script(
    root: Path, selected_files: list[Path], report_path: Path
) -> str:
    quoted_files = ", ".join(
        "'" + str(p.resolve()).replace("'", "''") + "'" for p in selected_files
    )
    root_q = str(root.resolve()).replace("'", "''")
    report_q = str(report_path.resolve()).replace("'", "''")
    return (
        "$ErrorActionPreference = 'Stop'; "
        "$m = Get-Module -ListAvailable Pester | Sort-Object Version -Descending | Select-Object -First 1; "
        "if (-not $m -or $m.Version -lt [version]'5.7.0') { "
        "Write-Error 'Pester 5.7.0+ is required for PowerShell coverage.'; exit 1 }; "
        "Import-Module Pester -MinimumVersion 5.7.0 -Force; "
        "$cfg = New-PesterConfiguration; "
        f"$cfg.Run.Path = '{root_q}'; "
        "$cfg.Run.PassThru = $true; "
        "$cfg.CodeCoverage.Enabled = $true; "
        "$cfg.CodeCoverage.ExcludeTests = $false; "
        f"$cfg.CodeCoverage.Path = @({quoted_files}); "
        "$cfg.CodeCoverage.OutputFormat = 'Cobertura'; "
        f"$cfg.CodeCoverage.ReportRoot = '{root_q}'; "
        f"$cfg.CodeCoverage.OutputPath = '{report_q}'; "
        "$res = Invoke-Pester -Configuration $cfg; "
        "if ($res.FailedCount -gt 0 -or $res.TotalCount -eq 0) { exit 1 }"
    )


def _cover_powershell(root: Path, files: list[Path], *, run_id: str | None = None) -> int:
    run_dir = root / "target" / "coverage" / "powershell"
    _clean_dir(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    _write_collection_manifest(root, "powershell", "in_progress", [], run_id=run_id)
    if not shutil.which("pwsh"):
        _warn("pwsh not found on PATH; PowerShell coverage unavailable.")
        _write_collection_manifest(root, "powershell", "failed", [], run_id=run_id)
        return 1
    selected_files = [
        Path(f).resolve()
        for f in files
        if language_of(f) == "powershell"
    ]
    if not selected_files:
        _write_collection_manifest(root, "powershell", "failed", [], run_id=run_id)
        return 1
    report_path = run_dir / "coverage.cobertura.xml"
    script = _powershell_coverage_script(root, selected_files, report_path)
    code = run_shell(["pwsh", "-NoProfile", "-NonInteractive", "-Command", script], root)
    if code != 0 or not report_path.is_file():
        _write_collection_manifest(root, "powershell", "failed", [], run_id=run_id)
        return code or 1
    _write_collection_manifest(root, "powershell", "completed", [report_path], run_id=run_id)
    return 0


def _file_sig(p: Path) -> tuple[int, int, int, int]:
    st = p.stat()
    return (st.st_mtime_ns, st.st_ctime_ns, st.st_size, st.st_ino)


def _snapshot_cobertura_files(root: Path) -> dict[Path, tuple[int, int, int, int]]:
    snap: dict[Path, tuple[int, int, int, int]] = {}
    for base in ("target/coverage", "coverage", "TestResults"):
        d = root / base
        if not d.is_dir():
            continue
        for p in d.glob("**/*cobertura*.xml"):
            if p.is_file():
                snap[p.resolve()] = _file_sig(p)
    return snap


def _snapshot_lcov_files(root: Path) -> dict[Path, tuple[int, int, int, int]]:
    snap: dict[Path, tuple[int, int, int, int]] = {}
    for base in ("target/coverage", "coverage"):
        d = root / base
        if not d.is_dir():
            continue
        for p in d.glob("**/lcov.info"):
            if p.is_file():
                snap[p.resolve()] = _file_sig(p)
    return snap


def _record_custom_cobertura_manifests(
    root: Path,
    languages: set[str],
    before_cobertura: dict[Path, tuple[int, int, int, int]],
    before_lcov: dict[Path, tuple[int, int, int, int]],
    *,
    run_id: str,
) -> None:
    after_cobertura = _snapshot_cobertura_files(root)
    after_lcov = _snapshot_lcov_files(root)
    fresh_cobertura = sorted(p for p, sig in after_cobertura.items() if before_cobertura.get(p) != sig)
    fresh_lcov = sorted(p for p, sig in after_lcov.items() if before_lcov.get(p) != sig)
    for lang in sorted(languages & {"csharp", "powershell"}):
        if fresh_cobertura or fresh_lcov:
            _write_collection_manifest(
                root, lang, "completed", fresh_cobertura, run_id=run_id, lcov_reports=fresh_lcov
            )
        else:
            _write_collection_manifest(root, lang, "failed", [], run_id=run_id)


def run_coverage(root: Path, files: list[Path], command: str | None) -> int:
    """Generate coverage reports for the languages present in `files`.

    Returns the custom command's status, or a non-zero status when C# or
    PowerShell fresh collection fails (without discarding other languages'
    coverage). Existing languages return 0 as before.
    """

    import uuid

    root = root.resolve()
    languages = _languages_in(files)
    new_langs = languages & {"csharp", "powershell"}
    run_id = uuid.uuid4().hex
    if new_langs:
        _write_latest_run(root, run_id, new_langs)
    if command:
        before_cobertura = _snapshot_cobertura_files(root) if new_langs else {}
        before_lcov = _snapshot_lcov_files(root) if new_langs else {}
        for lang in new_langs:
            _write_collection_manifest(root, lang, "in_progress", [], run_id=run_id)
        code = run_shell(command, root)
        if code != 0:
            for lang in new_langs:
                _write_collection_manifest(root, lang, "failed", [], run_id=run_id)
            _warn(
                f"Coverage command exited {code}. "
                "Reports already on disk will not be read."
            )
            return code
        if new_langs:
            _record_custom_cobertura_manifests(
                root, new_langs, before_cobertura, before_lcov, run_id=run_id
            )
        return 0
    if "clojure" in languages:
        _cover_clojure(root)
    if "java" in languages:
        _cover_java(root, files)
    if "go" in languages:
        _cover_go(root, files)
    if "typescript" in languages:
        _cover_typescript(root, files)
    if "python" in languages:
        _cover_python(root, files)
    if "rust" in languages:
        _cover_rust(root, files)
    new_lang_status = 0
    if "csharp" in languages:
        cs_code = _cover_csharp(root, files, run_id=run_id)
        if cs_code != 0:
            new_lang_status = cs_code
    if "powershell" in languages:
        ps_code = _cover_powershell(root, files, run_id=run_id)
        if ps_code != 0 and new_lang_status == 0:
            new_lang_status = ps_code
    return new_lang_status

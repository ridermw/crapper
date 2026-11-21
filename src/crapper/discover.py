"""Find source files and decide which language each one is written in.

C# build (`bin/`, `obj/`, `TestResults/`) and test-project exclusions apply
only to `.cs` files under a `.csproj` context, never globally to other
languages. PowerShell `*.Tests.ps1` files are excluded from production metrics
while remaining runnable Pester test inputs.
"""

from functools import lru_cache
import os
from pathlib import Path
from xml.etree import ElementTree

EXTENSIONS = {
    ".clj": "clojure",
    ".cljc": "clojure",
    ".cljs": "clojure",
    ".bb": "clojure",
    ".java": "java",
    ".cs": "csharp",
    ".go": "go",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".js": "typescript",
    ".jsx": "typescript",
    ".mjs": "typescript",
    ".cjs": "typescript",
    ".rs": "rust",
    ".py": "python",
    ".ps1": "powershell",
    ".psm1": "powershell",
}

SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".idea",
    ".venv",
    "venv",
    "node_modules",
    "vendor",
    "target",
    "dist",
    "build",
    "out",
    "coverage",
    ".metrics",
    "testdata",
    "__pycache__",
    ".clj-kondo",
}

TEST_DIRS = {"test", "tests", "spec", "specs", "__tests__"}

GO_TEST_SUFFIX = "_test.go"
CLOJURE_TEST_SUFFIXES = ("_test.clj", "_test.cljc", "_test.cljs", "_test.bb")
JS_TEST_SUFFIXES = (
    ".test.ts",
    ".spec.ts",
    ".test.tsx",
    ".spec.tsx",
    ".test.mts",
    ".spec.mts",
    ".test.cts",
    ".spec.cts",
    ".test.js",
    ".spec.js",
    ".test.jsx",
    ".spec.jsx",
    ".test.mjs",
    ".spec.mjs",
    ".test.cjs",
    ".spec.cjs",
)

_CSHARP_GENERATED_SUFFIXES = (".g.cs", ".g.i.cs", ".generated.cs", ".designer.cs")
_CSHARP_TEST_PACKAGES = {
    "microsoft.net.test.sdk",
    "nunit",
    "xunit",
    "xunit.v3",
    "mstest",
    "mstest.testframework",
}
_CSHARP_BUILD_DIRS = {"bin", "obj", "TestResults"}


def language_of(path: str | Path) -> str | None:
    file_path = Path(path)
    lower = file_path.name.lower()
    if lower.endswith(".d.ts"):
        return None
    if lower.endswith(_CSHARP_GENERATED_SUFFIXES):
        return None
    return EXTENSIONS.get(file_path.suffix.lower())


def _csharp_test_name(name: str) -> bool:
    parts = name.split(".")
    return name.endswith("Tests") or not {"Test", "Tests", "Specs"}.isdisjoint(parts)


def _mstest_sdk(sdks: str) -> bool:
    return any(sdk.split("/")[0].strip().lower() == "mstest.sdk" for sdk in sdks.split(";"))


def _marks_a_test_project(project: Path) -> bool:
    try:
        root = ElementTree.parse(project).getroot()
    except (OSError, ElementTree.ParseError):
        return False
    for element in root.iter():
        tag = element.tag.rpartition("}")[2]
        if tag == "IsTestProject" and (element.text or "").strip().lower() == "true":
            return True
        package = element.get("Include", "").strip().lower()
        if tag == "PackageReference" and package in _CSHARP_TEST_PACKAGES:
            return True
        if tag == "Project" and _mstest_sdk(element.get("Sdk", "")):
            return True
        if tag == "Sdk" and _mstest_sdk(element.get("Name", "")):
            return True
    return False


@lru_cache(maxsize=None)
def _csharp_project(folder: Path) -> Path | None:
    for current in (folder, *folder.parents):
        if any(current.glob("*.csproj")):
            return current
    return None


@lru_cache(maxsize=None)
def _csharp_test_project(project: Path) -> bool:
    if _csharp_test_name(project.name):
        return True
    return any(_marks_a_test_project(csproj) for csproj in project.glob("*.csproj"))


def _csharp_left_out(file_path: Path) -> bool:
    folder = file_path.absolute().parent
    project = _csharp_project(folder)
    if project is None:
        return any(_csharp_test_name(part) for part in file_path.parts[:-1]) or any(
            part in _CSHARP_BUILD_DIRS for part in file_path.parts[:-1]
        )
    if not _CSHARP_BUILD_DIRS.isdisjoint(folder.relative_to(project).parts):
        return True
    return _csharp_test_project(project)


def is_test_file(path: str | Path) -> bool:
    file_path = Path(path)
    name = file_path.name
    lower = name.lower()
    if file_path.suffix.lower() == ".cs" and _csharp_left_out(file_path):
        return True
    if lower.endswith(".tests.ps1"):
        return True
    if name.endswith(GO_TEST_SUFFIX):
        return True
    if name.endswith(CLOJURE_TEST_SUFFIXES):
        return True
    if name.endswith(JS_TEST_SUFFIXES):
        return True
    if name == "conftest.py":
        return True
    if name.endswith("_test.py") or (name.startswith("test_") and name.endswith(".py")):
        return True
    return any(part in TEST_DIRS for part in file_path.parts)


def _walk(root: Path) -> list[Path]:
    if not root.exists():
        return []
    if root.is_file():
        return [root]
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            name for name in dirnames if name not in SKIP_DIRS and name not in TEST_DIRS
        ]
        for name in filenames:
            path = Path(dirpath) / name
            if language_of(path) is None or is_test_file(path):
                continue
            found.append(path)
    return found


def iter_source_files(roots: list[Path]) -> list[Path]:
    files: set[Path] = set()
    for root in roots:
        for path in _walk(root):
            files.add(path.resolve())
    return sorted(files, key=lambda path: path.as_posix())

"""Load Cloverage, LCOV, JaCoCo, and Go coverage profiles.

Clojure prefers Cloverage's per-line form counts, then LCOV. Java uses JaCoCo
instruction counters. Go uses statement profiles. LCOV scores a function by
its BRDA branch records when it has any, and by line hits otherwise.
`percent_for` returns None when the file is absent; analysis turns that into 0%.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

from crapper.model import Function

_SPAN = re.compile(
    r'<span[^>]*title="(\d+) out of (\d+) forms covered"[^>]*>\s*(\d+)&nbsp;'
)
_LCOV_DA = re.compile(r"DA:(\d+),(\d+)")
_LCOV_BRDA = re.compile(r"BRDA:(\d+),([^,]*),([^,]*),(-|\d+)")
_DOCTYPE = re.compile(r"<!DOCTYPE\b(?:[^>\[]|\[[^\]]*\])*>", re.IGNORECASE | re.DOTALL)
_COBERTURA_BRANCH = re.compile(r"^\s*[\d.]+%\s*\((\d+)/(\d+)\)\s*$")
_XML_ENTITY = re.compile(r"<!\s*ENTITY\b", re.IGNORECASE)


class FileCoverage(dict):
    """Line hits for one file, plus branch hits keyed by line.

    The mapping itself is `line → (covered, total)` from `DA` records, so
    existing callers can keep indexing it. `branches` aggregates each `BRDA`
    record on that line as one branch.
    """

    def __init__(self):
        super().__init__()
        self.branches: dict[int, tuple[int, int]] = {}


def normalize_path(path: str) -> str:
    text = unquote(path).replace("\\", "/")
    if text.startswith("file:"):
        text = text[5:]
    while text.startswith("./"):
        text = text[2:]
    while "//" in text:
        text = text.replace("//", "/")
    return text


def _segments(path: str) -> list[str]:
    return [part for part in normalize_path(path).split("/") if part]


def suffix_match(path: str, suffix: str) -> bool:
    path_parts = _segments(path)
    suffix_parts = _segments(suffix)
    if not suffix_parts or len(suffix_parts) > len(path_parts):
        return False
    return path_parts[-len(suffix_parts) :] == suffix_parts


def _key_items(keys: list[str]) -> list[tuple[str, str]]:
    return [(key, normalize_path(key)) for key in keys]


def _exact_key(items: list[tuple[str, str]], candidates: list[str]) -> str | None:
    for candidate in candidates:
        for original, norm in items:
            if norm == candidate:
                return original
    return None


def _nonempty_parts(candidates: list[str]) -> list[tuple[str, list[str]]]:
    found = []
    for candidate in candidates:
        parts = _segments(candidate)
        if parts:
            found.append((candidate, parts))
    return found


def _key_has_suffix(key_parts: list[str], parts: list[str]) -> bool:
    if len(key_parts) < len(parts):
        return False
    return key_parts[-len(parts) :] == parts


def _forward_matches(
    items: list[tuple[str, str]],
    parts_of: list[tuple[str, list[str]]],
    source_parts: list[tuple[str, ...]],
) -> list[str]:
    forward: list[tuple[int, str]] = []
    for _candidate, parts in parts_of:
        for original, norm in items:
            key_parts = _segments(norm)
            if not _key_has_suffix(key_parts, parts):
                continue
            if _longer_source_owns(key_parts, len(parts), source_parts):
                continue
            forward.append((len(key_parts) - len(parts), original))
    if not forward:
        return []
    smallest = min(extra for extra, _key in forward)
    return list(dict.fromkeys(key for extra, key in forward if extra == smallest))


def _key_is_proper_suffix(parts: list[str], key_parts: list[str]) -> bool:
    if not key_parts or len(key_parts) >= len(parts):
        return False
    return parts[-len(key_parts) :] == key_parts


def _reverse_key(
    items: list[tuple[str, str]],
    parts_of: list[tuple[str, list[str]]],
    source_parts: list[tuple[str, ...]],
) -> str | None:
    own = {tuple(parts) for _candidate, parts in parts_of}
    reverse: list[str] = []
    for _candidate, parts in parts_of:
        for original, norm in items:
            key_parts = _segments(norm)
            if not _key_is_proper_suffix(parts, key_parts):
                continue
            if _another_source_ends_with(key_parts, source_parts, own):
                continue
            reverse.append(original)
    unique = list(dict.fromkeys(reverse))
    if len(unique) == 1:
        return unique[0]
    return None


def _select_key(
    keys: list[str], source_path: str, source_parts: list[tuple[str, ...]]
) -> str | None:
    """Report key for one source file.

    An exact path wins. A report path may be the source path plus a prefix
    (`proj/src/a.go` for `src/a.go`). It is not a match when another source
    file is a longer suffix of that key, so `b/main.go` does not take
    `a/b/main.go`.
    """

    items = _key_items(keys)
    candidates = _candidates(source_path)
    exact = _exact_key(items, candidates)
    if exact is not None:
        return exact
    parts_of = _nonempty_parts(candidates)
    forward = _forward_matches(items, parts_of, source_parts)
    if len(forward) == 1:
        return forward[0]
    if forward:
        return None
    return _reverse_key(items, parts_of, source_parts)


def _longer_source_owns(
    key_parts: list[str], source_len: int, source_parts: list[tuple[str, ...]]
) -> bool:
    for other in source_parts:
        if len(other) <= source_len:
            continue
        if len(other) <= len(key_parts) and key_parts[-len(other) :] == list(other):
            return True
    return False


def _another_source_ends_with(
    key_parts: list[str],
    source_parts: list[tuple[str, ...]],
    own: set[tuple[str, ...]],
) -> bool:
    for other in source_parts:
        if other in own or len(other) < len(key_parts):
            continue
        if list(other[-len(key_parts) :]) == key_parts:
            return True
    return False


def _candidates(source_path: str) -> list[str]:
    relative = normalize_path(source_path)
    absolute = normalize_path(str(Path(source_path).resolve())) if source_path else relative
    no_src = re.sub(r"^src/", "", relative)
    absolute_no_src = re.sub(r"/src/", "/", absolute)
    values = [relative, absolute, no_src, absolute_no_src]
    found: list[str] = []
    for value in values:
        if value and value not in found:
            found.append(value)
    return found


def coverage_percent(covered: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return 100.0 * covered / total


def _line_excluded(line: int, excluded: tuple[tuple[int, int], ...]) -> bool:
    return any(lo <= line <= hi for lo, hi in excluded)


def percent_for_range(
    lines: dict[int, tuple[int, int]] | None,
    start: int,
    end: int,
    excluded: tuple[tuple[int, int], ...] = (),
) -> float | None:
    if lines is None:
        return None
    covered = 0
    total = 0
    for line in range(start, end + 1):
        if excluded and _line_excluded(line, excluded):
            continue
        entry = lines.get(line)
        if entry is None:
            continue
        covered += entry[0]
        total += entry[1]
    if total == 0:
        return 0.0
    return coverage_percent(covered, total)


def parse_form_coverage(html: str) -> dict[int, tuple[int, int]]:
    found: dict[int, tuple[int, int]] = {}
    for covered, total, line in _SPAN.findall(html):
        total_forms = int(total)
        if total_forms <= 0:
            continue
        found[int(line)] = (int(covered), total_forms)
    return found


def _branch_hit(taken: str) -> bool:
    if taken == "-":
        return False
    try:
        return int(taken) > 0
    except ValueError:
        return False


def _add_branch(record: FileCoverage, line: int, taken: str) -> None:
    covered, total = record.branches.get(line, (0, 0))
    record.branches[line] = (covered + (1 if _branch_hit(taken) else 0), total + 1)


def _store_lcov(out: dict[str, FileCoverage], current_file: str | None, current: FileCoverage) -> None:
    if current_file is not None:
        out[current_file] = current


def _read_lcov_line(current: FileCoverage, line: str) -> None:
    match = _LCOV_DA.match(line)
    if match:
        hits = int(match.group(2))
        current[int(match.group(1))] = (1 if hits > 0 else 0, 1)
        return
    branch = _LCOV_BRDA.match(line)
    if branch:
        _add_branch(current, int(branch.group(1)), branch.group(4))


def parse_lcov(text: str) -> dict[str, FileCoverage]:
    out: dict[str, FileCoverage] = {}
    current_file = None
    current = FileCoverage()
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("SF:"):
            _store_lcov(out, current_file, current)
            current_file = line[3:]
            current = FileCoverage()
        elif line == "end_of_record":
            _store_lcov(out, current_file, current)
            current_file = None
            current = FileCoverage()
        elif current_file is not None:
            _read_lcov_line(current, line)
    _store_lcov(out, current_file, current)
    return out


def branch_percent(
    record,
    start: int,
    end: int,
    excluded: tuple[tuple[int, int], ...] = (),
) -> float | None:
    """Branch coverage for a function, or None when that span has no branches."""

    branches = getattr(record, "branches", None)
    if not branches:
        return None
    covered = 0
    total = 0
    for line in range(start, end + 1):
        if excluded and _line_excluded(line, excluded):
            continue
        entry = branches.get(line)
        if entry is None:
            continue
        covered += entry[0]
        total += entry[1]
    if total == 0:
        return None
    return coverage_percent(covered, total)


def parse_go_profile(text: str) -> dict[str, list[tuple[int, int, int, int]]]:
    """Map file → (start_line, end_line, statements, hits)."""

    out: dict[str, list[tuple[int, int, int, int]]] = {}
    for line_number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("mode:"):
            continue
        fields = line.split()
        if len(fields) != 3 or ":" not in fields[0]:
            raise ValueError(f"invalid coverage segment on line {line_number}: {raw!r}")
        file_part, range_part = fields[0].split(":", 1)
        start_text, end_text = range_part.split(",", 1)
        start_line = int(start_text.split(".", 1)[0])
        end_line = int(end_text.split(".", 1)[0])
        statements = int(fields[1])
        hits = int(fields[2])
        out.setdefault(file_part, []).append((start_line, end_line, statements, hits))
    return out


def _profile_segments(profile, path: str, source_parts: list[tuple[str, ...]] | None = None):
    if profile is None:
        return None
    key = _select_key(list(profile), path, source_parts or [])
    if key is None:
        return None
    return profile[key]


def go_percent(
    profile: dict[str, list[tuple[int, int, int, int]]] | None,
    path: str,
    start: int,
    end: int,
    source_parts: list[tuple[str, ...]] | None = None,
) -> float | None:
    segments = _profile_segments(profile, path, source_parts)
    if segments is None:
        return None
    total = 0
    covered = 0
    for seg_start, seg_end, statements, hits in segments:
        if seg_end < start or seg_start > end:
            continue
        total += statements
        if hits > 0:
            covered += statements
    if total == 0:
        return 0.0
    return coverage_percent(covered, total)


@dataclass
class JacocoMethod:
    missed: int
    covered: int
    line: int

    @property
    def percent(self) -> float:
        return coverage_percent(self.covered, self.missed + self.covered)


def _jacoco_from_index(classes, class_names, method_name, line) -> float | None:
    pool: list[JacocoMethod] = []
    for name in class_names:
        for method in classes.get(f"{name}#{method_name}", []):
            pool.append(method)
    if not pool:
        return None
    for method in pool:
        if method.line == line:
            return method.percent
    nearest = min(pool, key=lambda method: abs(method.line - line))
    return nearest.percent


def _instruction_counter(method):
    for child in list(method):
        if child.tag == "counter" and child.get("type") == "INSTRUCTION":
            return child
    return None


def _method_line(method) -> int:
    try:
        return int(method.get("line") or "0")
    except ValueError:
        return 0


def _record_jacoco_method(found: dict[str, list[JacocoMethod]], class_name: str, method) -> None:
    if method.tag != "method":
        return
    counter = _instruction_counter(method)
    if counter is None:
        return
    key = f"{class_name}#{method.get('name', '')}"
    found.setdefault(key, []).append(
        JacocoMethod(
            missed=int(counter.get("missed") or "0"),
            covered=int(counter.get("covered") or "0"),
            line=_method_line(method),
        )
    )


def parse_jacoco_index(text: str) -> dict[str, list[JacocoMethod]]:
    """Key methods as `binary.class.name#method`."""

    cleaned = _DOCTYPE.sub("", text)
    root = ET.fromstring(cleaned)
    found: dict[str, list[JacocoMethod]] = {}
    for class_node in root.iter("class"):
        class_name = class_node.get("name", "").replace("/", ".")
        for method in list(class_node):
            _record_jacoco_method(found, class_name, method)
    return found


@dataclass
class CoverageBundle:
    lcov: dict[str, dict[int, tuple[int, int]]] = field(default_factory=dict)
    cobertura: dict[str, FileCoverage] = field(default_factory=dict)
    fresh_lcov: dict[str, dict[int, tuple[int, int]]] = field(default_factory=dict)
    fresh_cobertura_languages: frozenset[str] = frozenset()
    go_profile: dict[str, list[tuple[int, int, int, int]]] | None = None
    jacoco: dict[str, list[JacocoMethod]] | None = None
    form_html: dict[str, dict[int, tuple[int, int]]] = field(default_factory=dict)
    source_parts: list[tuple[str, ...]] = field(default_factory=list)

    def bind_sources(self, paths: list[str]) -> None:
        """Remember project files so a short path cannot take a longer file's report."""

        found: list[tuple[str, ...]] = []
        seen: set[tuple[str, ...]] = set()
        for source in paths:
            for candidate in _candidates(source):
                parts = tuple(_segments(candidate))
                if parts and parts not in seen:
                    seen.add(parts)
                    found.append(parts)
        self.source_parts = found

    def percent_for(self, function: Function) -> float | None:
        if function.language == "java":
            names = []
            if function.jacoco_class:
                names.append(function.jacoco_class)
            if function.namespace not in names:
                names.append(function.namespace)
            return _jacoco_from_index(
                self.jacoco or {}, names, function.name, function.start_line
            )
        if function.language == "go":
            return go_percent(
                self.go_profile,
                function.path,
                function.start_line,
                function.end_line,
                self.source_parts,
            )
        html = self._html_lines(function.path)
        first = _first_line(function)
        excluded = getattr(function, "excluded_ranges", ())
        if html is not None:
            return percent_for_range(html, first, function.end_line, excluded)
        # Prefer fresh Cobertura over any stale LCOV match when both exist.
        # For fresh C#/PowerShell collections, only fall back to freshly validated
        # custom LCOV from the same run, never to stale pre-existing coverage/lcov.info.
        if function.language in self.fresh_cobertura_languages:
            record = _lookup(self.cobertura, function.path, self.source_parts) if self.cobertura else None
            if record is None and self.fresh_lcov:
                record = _lookup(self.fresh_lcov, function.path, self.source_parts)
        else:
            record = _lookup(self.cobertura, function.path, self.source_parts) if self.cobertura else None
            if record is None:
                record = self._lcov_lines(function.path)
        if record is None:
            return None
        branched = branch_percent(record, first, function.end_line, excluded)
        if branched is not None:
            return branched
        return percent_for_range(record, first, function.end_line, excluded)

    def _html_lines(self, path: str) -> dict[int, tuple[int, int]] | None:
        return _lookup(self.form_html, path, self.source_parts)

    def _lcov_lines(self, path: str) -> dict[int, tuple[int, int]] | None:
        return _lookup(self.lcov, path, self.source_parts)


def _first_line(function: Function) -> int:
    """Where line and branch coverage start: the body, when the scanner records it."""

    if function.body_line > 0:
        return function.body_line
    return function.start_line


def _lookup(index: dict[str, dict], source_path: str, source_parts: list[tuple[str, ...]]):
    if not index:
        return None
    key = _select_key(list(index), source_path, source_parts)
    if key is None:
        return None
    return index[key]


def _read(path: Path) -> str | None:
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8", errors="replace")


def _lcov_paths(root: Path) -> list[Path]:
    paths = list(root.glob("target/coverage/**/lcov.info"))
    paths.extend(root.glob("coverage/**/lcov.info"))
    return paths


def _merge_texts(paths: list[Path]) -> list[str]:
    texts: list[str] = []
    seen: set[Path] = set()
    for path in paths:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        text = _read(path)
        if text:
            texts.append(text)
    return texts


def _merge_lcov(root: Path) -> dict[str, dict[int, tuple[int, int]]]:
    merged: dict[str, dict[int, tuple[int, int]]] = {}
    for text in _merge_texts(_lcov_paths(root)):
        merged.update(parse_lcov(text))
    return merged


def _merge_go(root: Path) -> dict[str, list[tuple[int, int, int, int]]]:
    paths = [
        root / "target" / "coverage" / "coverage.out",
        root / "target" / "coverage" / "go" / "coverage.out",
        root / "coverage.out",
    ]
    paths.extend(root.glob("*/target/coverage/go/coverage.out"))
    paths.extend(root.glob("*/*/target/coverage/go/coverage.out"))
    merged: dict[str, list[tuple[int, int, int, int]]] = {}
    for text in _merge_texts(paths):
        for key, segments in parse_go_profile(text).items():
            merged.setdefault(key, []).extend(segments)
    return merged


def _merge_jacoco(root: Path) -> dict[str, list[JacocoMethod]]:
    paths = [root / "target" / "site" / "jacoco" / "jacoco.xml"]
    paths.extend(root.glob("*/target/site/jacoco/jacoco.xml"))
    paths.extend(root.glob("*/*/target/site/jacoco/jacoco.xml"))
    merged: dict[str, list[JacocoMethod]] = {}
    for text in _merge_texts(paths):
        for key, methods in parse_jacoco_index(text).items():
            merged.setdefault(key, []).extend(methods)
    return merged


def _merge_forms(root: Path) -> dict[str, dict[int, tuple[int, int]]]:
    coverage_dir = root / "target" / "coverage"
    found: dict[str, dict[int, tuple[int, int]]] = {}
    if not coverage_dir.is_dir():
        return found
    for html_path in coverage_dir.rglob("*.html"):
        text = _read(html_path)
        if not text or "forms covered" not in text:
            continue
        relative = html_path.relative_to(coverage_dir).as_posix()
        relative = relative[: -len(".html")]
        found[relative] = parse_form_coverage(text)
    return found


def _cobertura_number(value: str | None, label: str, minimum: int = 0) -> int:
    try:
        number = int(value or "")
    except ValueError as exc:
        raise ValueError(f"Invalid Cobertura {label}: {value!r}") from exc
    if number < minimum:
        raise ValueError(f"Invalid Cobertura {label}: {value!r}")
    return number


def _resolve_cobertura_filename(
    filename: str, sources: list[str], project_root: Path | None
) -> str | None:
    import posixpath
    from pathlib import PureWindowsPath

    raw = normalize_path(filename)
    if len(sources) == 1 and not raw.startswith("/") and not PureWindowsPath(raw).is_absolute():
        raw = posixpath.normpath(normalize_path(sources[0]) + "/" + raw)
    if project_root is None:
        return raw
    root_resolved = project_root.resolve()
    candidate = Path(raw)
    if candidate.is_absolute():
        try:
            return candidate.resolve().relative_to(root_resolved).as_posix()
        except ValueError:
            return None
    if PureWindowsPath(raw).is_absolute():
        return None
    joined = (root_resolved / raw).resolve()
    try:
        return joined.relative_to(root_resolved).as_posix()
    except ValueError:
        return None


def parse_cobertura(
    text: str, project_root: Path | None = None
) -> dict[str, FileCoverage]:
    """Parse Coverlet/Pester Cobertura XML into FileCoverage records.

    Reads class-level `./lines/line` once (falling back to method lines only if
    a class has no direct `./lines/line` table) so duplicate class/method tables
    do not inflate coverage. Rejects DOCTYPE/ENTITY declarations.
    """

    if _XML_ENTITY.search(text):
        raise ValueError("Cobertura XML entities are not supported")
    cleaned = _DOCTYPE.sub("", text.lstrip("\ufeff"))
    root = ET.fromstring(cleaned)
    if root.tag != "coverage":
        raise ValueError("Expected Cobertura <coverage> root element")
    sources = [
        src.text.strip()
        for src in root.findall("./sources/source")
        if src.text and src.text.strip()
    ]
    found: dict[str, FileCoverage] = {}
    for cls in root.iter("class"):
        raw_name = cls.get("filename")
        if not raw_name:
            continue
        resolved_name = _resolve_cobertura_filename(raw_name, sources, project_root)
        if resolved_name is None:
            continue
        record = found.setdefault(resolved_name, FileCoverage())
        lines = cls.findall("./lines/line")
        if not lines:
            lines = cls.findall("./methods/method/lines/line")
        for row in lines:
            number = _cobertura_number(row.get("number"), "line number", 1)
            hits = _cobertura_number(row.get("hits"), "hit count", 0)
            prev = record.get(number)
            record[number] = (1 if (hits > 0 or (prev is not None and prev[0] > 0)) else 0, 1)
            cond = row.get("condition-coverage")
            if cond is not None:
                match = _COBERTURA_BRANCH.fullmatch(cond)
                if not match:
                    raise ValueError(f"Invalid Cobertura branch counts: {cond!r}")
                covered, total = map(int, match.groups())
                if total < 1 or covered > total:
                    raise ValueError(f"Invalid Cobertura branch counts: {cond!r}")
                old = record.branches.get(number)
                if old is None:
                    record.branches[number] = (covered, total)
                else:
                    record.branches[number] = (max(old[0], covered), max(old[1], total))
    return found


def _merge_cobertura_records(
    target: dict[str, FileCoverage],
    report_records: list[dict[str, FileCoverage]],
) -> None:
    """Merge same-run Cobertura reports into `target`.

    Line hits are merged by union (preserving 0-hit executable lines).
    If multiple distinct reports both provide aggregate branch counts for the
    same file/line, drop branch counts on that file to line-only rather than
    inventing branch union precision, and emit a diagnostic on stderr.
    """

    import sys

    branch_sources: dict[tuple[str, int], int] = {}
    ambiguous_branch_files: set[str] = set()
    for incoming_map in report_records:
        for filename, incoming in incoming_map.items():
            record = target.setdefault(filename, FileCoverage())
            for line, hit in incoming.items():
                prev = record.get(line)
                record[line] = (1 if (hit[0] > 0 or (prev is not None and prev[0] > 0)) else 0, 1)
            for line, (covered, total) in incoming.branches.items():
                key = (filename, line)
                seen_count = branch_sources.get(key, 0) + 1
                branch_sources[key] = seen_count
                if seen_count > 1:
                    ambiguous_branch_files.add(filename)
                elif line not in record.branches:
                    record.branches[line] = (covered, total)
    for filename in sorted(ambiguous_branch_files):
        target[filename].branches.clear()
        print(
            f"crapper: multiple Cobertura reports contain aggregate branch counts for "
            f"{filename}; using line coverage fallback.",
            file=sys.stderr,
        )


def _read_latest_run(root: Path) -> tuple[str | None, set[str] | None]:
    import json

    marker = root / "target" / "coverage" / "latest_run.json"
    if not marker.is_file():
        return None, None
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, None
    if not isinstance(data, dict):
        return None, None
    run_id = data.get("run_id")
    langs = data.get("languages")
    return (
        run_id if isinstance(run_id, str) else None,
        set(langs) if isinstance(langs, list) else None,
    )


def _validate_manifest_paths(raw: object, root_resolved: Path) -> list[Path] | None:
    if raw is None:
        return []
    if not isinstance(raw, list):
        return None
    paths: list[Path] = []
    for entry in raw:
        if not isinstance(entry, str):
            return None
        candidate = (root_resolved / entry).resolve()
        try:
            candidate.relative_to(root_resolved)
        except ValueError:
            return None
        if not candidate.is_file():
            return None
        paths.append(candidate)
    return paths


def read_collection_manifest_details(
    root: Path, language: str, *, expected_run_id: str | None = None
) -> tuple[list[Path], list[Path]] | None:
    """Return validated `(cobertura_paths, lcov_paths)` from `collection.json`."""

    import json

    manifest_path = root / "target" / "coverage" / language / "collection.json"
    if not manifest_path.is_file():
        return None
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("status") != "completed":
        return None
    if data.get("language") != language:
        return None
    if expected_run_id is not None and data.get("run_id") != expected_run_id:
        return None
    root_resolved = root.resolve()
    cobertura_paths = _validate_manifest_paths(data.get("reports"), root_resolved)
    lcov_paths = _validate_manifest_paths(data.get("lcov_reports"), root_resolved)
    if cobertura_paths is None or lcov_paths is None:
        return None
    if not cobertura_paths and not lcov_paths:
        return None
    return cobertura_paths, lcov_paths


def read_collection_manifest(
    root: Path, language: str, *, expected_run_id: str | None = None
) -> list[Path] | None:
    """Return validated Cobertura report paths from `target/coverage/<language>/collection.json`.

    Returns None when no manifest exists, its status is not `"completed"`, or its
    run_id does not match `expected_run_id`.
    """

    details = read_collection_manifest_details(root, language, expected_run_id=expected_run_id)
    if details is None or not details[0]:
        return None
    return details[0]


def _manifest_report_paths(
    root: Path, historical: bool, languages: set[str] | None = None
) -> tuple[list[Path], list[Path]]:
    cobertura_paths: list[Path] = []
    fresh_lcov_paths: list[Path] = []
    expected_run_id: str | None = None
    run_langs: set[str] | None = None
    if not historical:
        expected_run_id, run_langs = _read_latest_run(root)
    allowed = {"csharp", "powershell"}
    if languages is not None:
        allowed &= languages
    if not historical and run_langs is not None:
        allowed &= run_langs
    for language in sorted(allowed):
        details = read_collection_manifest_details(
            root, language, expected_run_id=expected_run_id
        )
        if details is not None:
            cobertura_paths.extend(details[0])
            fresh_lcov_paths.extend(details[1])
        elif historical:
            lang_dir = root / "target" / "coverage" / language
            if lang_dir.is_dir():
                cobertura_paths.extend(sorted(lang_dir.glob("**/*cobertura*.xml")))
    if historical:
        for extra in ("coverage", "TestResults"):
            extra_dir = root / extra
            if extra_dir.is_dir():
                cobertura_paths.extend(sorted(extra_dir.glob("**/*cobertura*.xml")))
    return cobertura_paths, fresh_lcov_paths


def _merge_cobertura_from_paths(root: Path, paths: list[Path]) -> dict[str, FileCoverage]:
    import sys

    if not paths:
        return {}
    report_records: list[dict[str, FileCoverage]] = []
    for text in _merge_texts(paths):
        try:
            report_records.append(parse_cobertura(text, project_root=root))
        except (ET.ParseError, ValueError) as exc:
            print(f"crapper: ignoring malformed Cobertura report: {exc}", file=sys.stderr)
    merged: dict[str, FileCoverage] = {}
    _merge_cobertura_records(merged, report_records)
    return merged


def _merge_lcov_from_paths(paths: list[Path]) -> dict[str, dict[int, tuple[int, int]]]:
    merged: dict[str, dict[int, tuple[int, int]]] = {}
    for text in _merge_texts(paths):
        merged.update(parse_lcov(text))
    return merged


def load_bundle(
    root: Path, *, historical: bool = False, languages: set[str] | None = None
) -> CoverageBundle:
    go_profile = _merge_go(root)
    jacoco = _merge_jacoco(root)
    lcov = _merge_lcov(root)
    cobertura_paths, fresh_lcov_paths = _manifest_report_paths(
        root, historical=historical, languages=languages
    )
    cobertura = _merge_cobertura_from_paths(root, cobertura_paths)
    fresh_lcov = _merge_lcov_from_paths(fresh_lcov_paths) if fresh_lcov_paths else {}
    # Keep fresh Cobertura/custom-LCOV separate for lookup precedence, and also
    # populate lcov for callers (such as mutator) that inspect bundle.lcov directly.
    for filename, record in cobertura.items():
        lcov[filename] = record
    fresh_langs = (
        frozenset()
        if historical
        else frozenset({"csharp", "powershell"} & (languages if languages is not None else {"csharp", "powershell"}))
    )
    return CoverageBundle(
        lcov=lcov,
        cobertura=cobertura,
        fresh_lcov=fresh_lcov,
        fresh_cobertura_languages=fresh_langs,
        go_profile=go_profile or None,
        jacoco=jacoco or None,
        form_html=_merge_forms(root),
    )

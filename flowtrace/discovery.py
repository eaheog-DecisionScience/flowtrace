"""Auto-discovery: given a directory (or a specific script), figures out
(a) the project root to scope tracing to, and (b) - when a directory was
given rather than a specific file - which .py file is actually the
program's entry point. This is what makes `flowtrace run my_project/`
work with no source modification and no manual script path.
"""

import ast
from pathlib import Path

from .indexer import discover_python_files

PROJECT_ROOT_MARKERS = ("pyproject.toml", "setup.py", "setup.cfg", ".git", "requirements.txt", "Pipfile")
PREFERRED_ENTRY_NAMES = ("main.py", "app.py", "__main__.py", "manage.py", "run.py", "server.py")
TEST_DIR_NAMES = {"test", "tests", "testing"}


def find_project_root(start_path, max_up=6):
    """Walk upward from start_path looking for a recognizable project-root
    marker (pyproject.toml, .git, etc). Falls back to start_path itself
    (or its parent, if start_path is a file) if nothing is found."""
    start = Path(start_path).resolve()
    current = start.parent if start.is_file() else start
    for _ in range(max_up):
        if any((current / marker).exists() for marker in PROJECT_ROOT_MARKERS):
            return current
        if current.parent == current:
            break
        current = current.parent
    return start.parent if start.is_file() else start


def _has_main_guard(path):
    """True if the file contains `if __name__ == "__main__":` at any level."""
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    if "__main__" not in source:
        return False
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if not (isinstance(test, ast.Compare) and isinstance(test.left, ast.Name) and test.left.id == "__name__"):
            continue
        for op, comparator in zip(test.ops, test.comparators):
            if isinstance(op, ast.Eq) and isinstance(comparator, ast.Constant) and comparator.value == "__main__":
                return True
    return False


def _score_candidate(path, project_root):
    rel_parts = path.relative_to(project_root).parts
    depth = len(rel_parts) - 1
    score = 0

    if path.name in PREFERRED_ENTRY_NAMES:
        score += 100
    if _has_main_guard(path):
        score += 50
    if any(p.lower() in TEST_DIR_NAMES for p in rel_parts[:-1]) or path.stem.startswith("test_") or path.stem.endswith("_test"):
        score -= 200
    if path.stem in ("setup", "conftest"):
        score -= 100
    score -= depth * 5  # prefer shallower files, all else equal

    return score


def discover_entrypoint(search_dir, extra_ignore=()):
    """Returns (best_path_or_None, project_root, ranked_candidates).

    ranked_candidates is a list of (path, score) sorted best-first, so
    callers can report runner-up candidates when the pick is ambiguous.
    """
    search_dir = Path(search_dir).resolve()
    project_root = find_project_root(search_dir)
    files = discover_python_files(search_dir, extra_ignore=extra_ignore)
    if not files:
        return None, project_root, []

    ranked = sorted(
        ((f, _score_candidate(f, project_root)) for f in files),
        key=lambda pair: (-pair[1], len(pair[0].relative_to(project_root).parts), str(pair[0])),
    )
    return ranked[0][0], project_root, ranked

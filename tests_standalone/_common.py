"""Shared helpers for the dependency-free suite. Standard library only."""
import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "staticfiles", ".vercel", "node_modules"}


def read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def project_files(*suffixes):
    """Every file in the project (minus VCS/venv/cache folders) with one of the suffixes."""
    for path in sorted(ROOT.rglob("*")):
        if path.is_file() and not (set(path.relative_to(ROOT).parts) & SKIP_DIRS) and path.suffix in suffixes:
            yield path


def load_module(rel, name):
    """Import a file by path (used for scripts/ which is not a package)."""
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def env_names(*sources):
    """Environment variable names read by the given source texts."""
    pat = re.compile(r"""(?:os\.(?:getenv|environ\.get)\(\s*|os\.environ\[\s*|_flag\(\s*)["']([A-Z][A-Z0-9_]{2,})["']""")
    found = set()
    for src in sources:
        found.update(pat.findall(src))
    return found

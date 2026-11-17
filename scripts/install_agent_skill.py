#!/usr/bin/env python3
"""Install one Agent Skill directory from a public GitHub repository.

Only SKILL.md-based instruction packages are copied. Nothing inside the skill is executed.
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

PROJECT = Path(__file__).resolve().parents[2]
SKILLS_ROOT = PROJECT / "backend" / "skills" / "community"
MAX_FILES = 500
MAX_BYTES = 8 * 1024 * 1024


def validate_repo(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "github.com" or parsed.username or parsed.password:
        raise ValueError("--repo must be an HTTPS URL on github.com")
    parts = [p for p in parsed.path.strip("/").split("/") if p]
    if len(parts) not in (2, 3) or any(p in (".", "..") for p in parts):
        raise ValueError("Use https://github.com/OWNER/REPOSITORY[.git]")
    path = "/".join(parts[:2])
    return f"https://github.com/{path}.git"


def safe_skill_path(value: str) -> PurePosixPath:
    p = PurePosixPath(value.replace("\\", "/"))
    if p.is_absolute() or not p.parts or any(x in ("", ".", "..") for x in p.parts):
        raise ValueError("--path must be a repository-relative directory without '..'")
    return p


def copy_skill(source: Path, name: str) -> Path:
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{1,63}", name):
        raise ValueError("--name must be 2-64 letters, numbers, dots, underscores or hyphens")
    skill_file = source / "SKILL.md"
    if not source.is_dir() or not skill_file.is_file():
        raise ValueError("Selected directory must contain SKILL.md")
    if skill_file.stat().st_size > 150_000:
        raise ValueError("SKILL.md exceeds 150 KB")

    target = SKILLS_ROOT / name
    if target.exists():
        raise FileExistsError(f"Skill already exists: {target}. Remove it manually before replacing.")
    count = total = 0
    for item in source.rglob("*"):
        if item.is_symlink():
            raise ValueError(f"Symlinks are not allowed in skills: {item}")
        if not item.is_file():
            continue
        rel = item.relative_to(source)
        if any(part in {".git", "node_modules", "__pycache__"} for part in rel.parts):
            continue
        if item.name.lower().startswith(".env") or item.suffix.lower() in {".pem", ".key", ".p12"}:
            continue
        count += 1
        total += item.stat().st_size
        if count > MAX_FILES or total > MAX_BYTES:
            raise ValueError("Skill package exceeds safety limits (500 files / 8 MB)")

    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, ignore=shutil.ignore_patterns(".git", "node_modules", "__pycache__", ".env*", "*.pem", "*.key", "*.p12"))
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="Public GitHub repository HTTPS URL")
    parser.add_argument("--path", required=True, help="Repository-relative directory containing SKILL.md")
    parser.add_argument("--name", required=True, help="Local skill folder name")
    args = parser.parse_args()
    try:
        repo = validate_repo(args.repo)
        skill_path = safe_skill_path(args.path)
        with tempfile.TemporaryDirectory(prefix="agent-skill-") as tmp:
            checkout = Path(tmp) / "repo"
            subprocess.run(
                ["git", "clone", "--depth", "1", "--filter=blob:none", "--sparse", repo, str(checkout)],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(checkout), "sparse-checkout", "set", skill_path.as_posix()],
                check=True,
            )
            subprocess.run(["git", "-C", str(checkout), "checkout"], check=True)
            installed = copy_skill(checkout.joinpath(*skill_path.parts), args.name)
        print(f"Installed skill: {installed}")
        print("It will be discovered automatically on the next request. Review its SKILL.md before use.")
        return 0
    except (ValueError, FileExistsError, subprocess.CalledProcessError, OSError) as exc:
        print(f"Install failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

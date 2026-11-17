"""Install and remove skills from the frontend (paste a SKILL.md or upload a .zip).

A skill is reference text for the AI. Nothing inside it is ever executed, but uploads are still untrusted input,
so names, sizes and archive paths are checked: no path traversal, no symlinks, size and file-count limits, and
only the community folder can be changed.
"""
from __future__ import annotations

import io
import posixpath
import re
import secrets
import shutil
import zipfile
from pathlib import Path, PurePosixPath

from . import skill_registry

MAX_TEXT = 200_000
MAX_ZIP_BYTES = 8 * 1024 * 1024
MAX_UNPACKED = 8 * 1024 * 1024
MAX_FILES = 300
_JUNK = ("__MACOSX/", ".git/", ".DS_Store", "Thumbs.db")


class SkillInstallError(ValueError):
    """A problem with the upload that the user can fix."""


def _community() -> Path:
    root = skill_registry.SKILLS_ROOT / "community"
    root.mkdir(parents=True, exist_ok=True)
    return root


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:60]
    if len(s) < 2:
        raise SkillInstallError("Give the skill a name of at least 2 letters or digits")
    return s


def _finish(tmp: Path, name: str, replace: bool) -> Path:
    target = _community() / name
    if target.exists():
        if not replace:
            shutil.rmtree(tmp, ignore_errors=True)
            raise SkillInstallError(f"A skill named “{name}” is already installed (tick “replace” to overwrite it)")
        shutil.rmtree(target)
    tmp.rename(target)
    skill_registry.invalidate()
    return target


def _tmp() -> Path:
    return _community() / f".incoming-{secrets.token_hex(4)}"


def install_text(name: str | None, content: str, replace: bool = False) -> dict:
    content = (content or "").replace("\r\n", "\n").strip()
    if not content:
        raise SkillInstallError("The skill text is empty")
    if len(content) > MAX_TEXT or "\x00" in content:
        raise SkillInstallError("The skill must be plain text of at most 200,000 characters")
    meta, body = skill_registry._frontmatter(content)
    title = name or meta.get("name") or ""
    folder = slug(title)
    if not meta:  # no frontmatter: build it from the first line
        first = next((ln.strip("# ").strip() for ln in content.splitlines() if ln.strip()), folder)
        content = f"---\nname: {folder}\ndescription: {first[:300]}\n---\n\n{content}"
    tmp = _tmp()
    tmp.mkdir()
    try:
        (tmp / "SKILL.md").write_text(content + "\n", encoding="utf-8")
        _finish(tmp, folder, replace)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return {"id": f"community/{folder}", "name": folder, "files": 1}


def install_zip(data: bytes, name: str | None = None, replace: bool = False) -> dict:
    if len(data) > MAX_ZIP_BYTES:
        raise SkillInstallError("The zip file is larger than 8 MB")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise SkillInstallError("That is not a valid .zip file")
    infos = [i for i in zf.infolist() if not i.is_dir() and not any(j in i.filename for j in _JUNK)]
    if not infos or len(infos) > MAX_FILES:
        raise SkillInstallError(f"The zip must contain between 1 and {MAX_FILES} files")
    if sum(i.file_size for i in infos) > MAX_UNPACKED:
        raise SkillInstallError("The zip unpacks to more than 8 MB")
    for i in infos:
        parts = PurePosixPath(i.filename).parts
        if i.filename.startswith(("/", "\\")) or "\\" in i.filename or ".." in parts or ":" in (parts[0] if parts else ""):
            raise SkillInstallError("The zip contains unsafe file paths")
        if (i.external_attr >> 16) & 0o170000 == 0o120000:
            raise SkillInstallError("The zip contains symbolic links, which are not allowed")
    skills = sorted((i for i in infos if posixpath.basename(i.filename) == "SKILL.md"), key=lambda i: i.filename.count("/"))
    if not skills or skills[0].filename.count("/") > 2:
        raise SkillInstallError("No SKILL.md found at the top of the zip (or inside one folder)")
    prefix = posixpath.dirname(skills[0].filename)
    prefix = prefix + "/" if prefix else ""
    meta, _ = skill_registry._frontmatter(zf.read(skills[0]).decode("utf-8", "replace"))
    folder = slug(name or meta.get("name") or prefix.strip("/") or "skill")
    tmp = _tmp()
    tmp.mkdir()
    total = n = 0
    try:
        for i in infos:
            if not i.filename.startswith(prefix):
                continue
            dest = (tmp / i.filename[len(prefix):]).resolve()
            if not dest.is_relative_to(tmp.resolve()):
                raise SkillInstallError("The zip contains unsafe file paths")
            with zf.open(i) as src:
                chunk = src.read(MAX_UNPACKED - total + 1)  # a zip that lies about its size cannot exceed the limit
            total += len(chunk)
            if total > MAX_UNPACKED:
                raise SkillInstallError("The zip unpacks to more than 8 MB")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(chunk)
            n += 1
        _finish(tmp, folder, replace)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return {"id": f"community/{folder}", "name": folder, "files": n}


def remove(skill_id: str) -> str:
    """Deletes an installed community skill (or a whole community bundle). Built-in skills cannot be removed."""
    parts = [p for p in (skill_id or "").split("/") if p]
    if len(parts) < 2 or parts[0] != "community" or any(p in (".", "..") or p.startswith(".") for p in parts):
        raise SkillInstallError("Only skills installed under “community” can be removed")
    root = _community().resolve()
    target = (skill_registry.SKILLS_ROOT / "/".join(parts)).resolve()
    if target == root or not target.is_relative_to(root) or not target.is_dir():
        raise SkillInstallError("That skill was not found")
    shutil.rmtree(target)
    skill_registry.invalidate()
    return "/".join(parts)

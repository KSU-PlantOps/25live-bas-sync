"""The documentation's links: every relative link and #anchor in the repository's
Markdown must lead somewhere, and every docs page the code points people at
must exist. Moving a heading or a page otherwise breaks them silently."""

import re
import unicodedata
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_SKIP = {".git", ".venv", "node_modules", ".pytest_cache", ".mypy_cache", ".ruff_cache",
         "build", "dist"}
MARKDOWN = sorted(p for p in ROOT.rglob("*.md")
                  if not _SKIP.intersection(p.relative_to(ROOT).parts))
_FENCE = re.compile(r"^(```|~~~).*?^\1", re.S | re.M)
_LINK = re.compile(r"\]\(([^)\s]+)\)|(?:href|src|srcset)=\"([^\"]+)\"")


def github_slug(heading: str) -> str:
    """The anchor GitHub gives a heading: lowercased, punctuation dropped,
    spaces as hyphens."""
    text = re.sub(r"<[^>]+>", "", heading)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text).replace("`", "")
    return "".join("-" if ch == " " else ch for ch in text.strip().lower()
                   if ch in " -_" or unicodedata.category(ch)[0] in "LN")


def anchors(path: Path) -> set:
    found: set = set()
    seen: dict = {}
    for line in _FENCE.sub("", path.read_text(encoding="utf-8")).splitlines():
        m = re.match(r"^#{1,6}\s+(.*?)\s*#*\s*$", line)
        if m:
            slug = github_slug(m.group(1))
            n = seen.get(slug, 0)
            seen[slug] = n + 1
            found.add(slug if n == 0 else f"{slug}-{n}")
        found.update(re.findall(r'<a\s+(?:id|name)="([^"]+)"', line))
    return found


def test_slugs_follow_github():
    assert github_slug("Settings — config.yaml") == "settings--configyaml"
    assert github_slug("From a pre-1.0 release") == "from-a-pre-10-release"
    assert github_slug("Moving off the `niagara` driver") == "moving-off-the-niagara-driver"


@pytest.mark.parametrize("page", MARKDOWN, ids=lambda p: str(p.relative_to(ROOT)))
def test_links_resolve(page):
    broken = []
    for m in _LINK.finditer(_FENCE.sub("", page.read_text(encoding="utf-8"))):
        link = m.group(1) or m.group(2)
        if re.match(r"^[a-z][a-z0-9+.-]*:", link):
            continue                                     # https:, mailto:
        target, _, fragment = link.partition("#")
        dest = (page.parent / target) if target else page
        if not dest.exists():
            broken.append(f"{link} (no such file)")
        elif fragment and dest.suffix == ".md" and fragment not in anchors(dest):
            broken.append(f"{link} (no such heading)")
    assert not broken, f"{page.relative_to(ROOT)}: " + ", ".join(broken)


def test_docs_named_in_the_code_exist():
    """Messages, templates and file headers send people to docs/<page>.md,
    sometimes to a heading on it."""
    sources = [*ROOT.joinpath("bassync").rglob("*.py"), *ROOT.joinpath("bassync").rglob("*.html"),
               ROOT / "Dockerfile", ROOT / "config.example.yaml", ROOT / "CHANGELOG.md"]
    named = {m for src in sources
             for m in re.findall(r"docs/([\w-]+\.md)(?:#([\w-]+))?",
                                 src.read_text(encoding="utf-8"))}
    assert named, "expected the code to point at some docs pages"
    missing = sorted(f"{page}#{frag}" if frag else page for page, frag in named
                     if not (ROOT / "docs" / page).is_file()
                     or (frag and frag not in anchors(ROOT / "docs" / page)))
    assert not missing

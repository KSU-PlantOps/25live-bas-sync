#!/usr/bin/env python3
"""Work out what a release of this repository is, for .github/workflows/release.yml.

The version lives in one place, ``bassync/__init__.py``. A release is due when
main carries a version with no ``vX.Y.Z`` tag yet. Its notes are that
version's section of CHANGELOG.md, whose heading reads

    ## [1.2.0] — 2026-09-27 — An optional release title

Subcommands:
  check   fail unless the version is releasable and CHANGELOG.md has its section
  plan    print the release's facts as GitHub Actions outputs (key=value lines)
  notes   write the release notes, the changelog section plus how to install it

Standard library only, so the workflow needs nothing installed to run it.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[2]
VERSION_FILE = ROOT / "bassync" / "__init__.py"
CHANGELOG = ROOT / "CHANGELOG.md"

_VERSION_ASSIGN = re.compile(r"""^__version__\s*=\s*["']([^"']+)["']""", re.M)
# X.Y.Z, or a PEP 440 pre-release of it: 1.3.0a1, 1.3.0b2, 1.3.0rc1.
_RELEASE_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?$")
# Any tag that names a version, including the old spellings: V1.1, V1.0RC1.
_TAG_VERSION = re.compile(r"^[vV]?(\d+)\.(\d+)(?:\.(\d+))?(.*)$")
_HEADING = re.compile(r"^## \[(?P<version>[^\]]+)\](?P<rest>.*)$")
_SEPARATOR = re.compile(r"\s+[—–-]\s+")
_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2}|unreleased)$", re.I)
_LINK_DEFINITION = re.compile(r"^\[([^\]]+)\]:\s+\S")


class ReleaseError(Exception):
    """The repository isn't in a releasable state; the message says why."""


class Version(NamedTuple):
    text: str
    number: tuple[int, int, int]
    prerelease: bool

    @property
    def minor(self) -> str:
        return f"{self.number[0]}.{self.number[1]}"


class Section(NamedTuple):
    title: str | None
    body: str


class Plan(NamedTuple):
    version: Version
    tag: str
    title: str
    existing_tag: str | None
    latest: bool
    image: str
    docker_tags: list[str]
    publish: bool
    build: bool


def parse_version(text: str) -> Version:
    m = _RELEASE_VERSION.match(text)
    if not m:
        raise ReleaseError(
            f"version {text!r} in {VERSION_FILE.relative_to(ROOT)} is not X.Y.Z "
            "or a pre-release of one (X.Y.ZrcN, X.Y.ZbN, X.Y.ZaN)")
    return Version(text, (int(m[1]), int(m[2]), int(m[3])), m[4] is not None)


def read_version(path: Path = VERSION_FILE) -> str:
    m = _VERSION_ASSIGN.search(path.read_text(encoding="utf-8"))
    if not m:
        raise ReleaseError(f"no __version__ assignment in {path}")
    return m[1]


def changelog_section(text: str, version: str) -> Section:
    """The ``## [version]`` section of a changelog: its title and its body.

    The body runs to the next level-2 heading, and carries any link reference
    definitions it uses that are defined elsewhere in the file, so its links
    still work on their own.
    """
    lines = text.splitlines()
    start = title = None
    for i, line in enumerate(lines):
        m = _HEADING.match(line)
        if m and m["version"].strip() == version:
            start = i + 1
            title = _heading_title(m["rest"])
            break
    if start is None:
        raise ReleaseError(
            f"CHANGELOG.md has no '## [{version}]' section; add one describing "
            "this release")
    body: list[str] = []
    in_fence = False
    for line in lines[start:]:
        if line.startswith("```"):
            in_fence = not in_fence
        elif not in_fence and line.startswith("## "):
            break
        body.append(line)
    text_body = "\n".join(body).strip()
    if not text_body:
        raise ReleaseError(f"CHANGELOG.md's '## [{version}]' section is empty")
    return Section(title, _with_link_definitions(text_body, lines))


def _heading_title(rest: str) -> str | None:
    """"— 2026-09-27 — Title" → "Title"; a date alone, or nothing, → None."""
    rest = rest.strip().lstrip("—–-").strip()
    if not rest:
        return None
    parts = _SEPARATOR.split(rest, maxsplit=1)
    if _DATE.match(parts[0]):
        return parts[1].strip() if len(parts) > 1 else None
    return rest


def _with_link_definitions(body: str, all_lines: list[str]) -> str:
    defined_here = {m[1].lower() for line in body.splitlines()
                    if (m := _LINK_DEFINITION.match(line))}
    extra = []
    for line in all_lines:
        m = _LINK_DEFINITION.match(line)
        if m and m[1].lower() not in defined_here and f"[{m[1]}]".lower() in body.lower():
            extra.append(line)
            defined_here.add(m[1].lower())
    return body + ("\n\n" + "\n".join(extra) if extra else "")


def existing_tag(tags: Iterable[str], version: str) -> str | None:
    """The tag already naming this version, in any capitalisation."""
    wanted = {f"v{version}".lower(), version.lower()}
    return next((t for t in tags if t.lower() in wanted), None)


def is_latest(version: Version, tags: Iterable[str]) -> bool:
    """True unless this is a pre-release or an older line's patch release."""
    if version.prerelease:
        return False
    for tag in tags:
        m = _TAG_VERSION.match(tag)
        if not m or m[4]:            # not a version, or a pre-release (V1.0RC1)
            continue
        if (int(m[1]), int(m[2]), int(m[3] or 0)) > version.number:
            return False
    return True


def image_name(repository: str) -> str:
    return f"ghcr.io/{repository.lower()}"


def docker_tags(image: str, version: Version, latest: bool) -> list[str]:
    """1.2.0 → :1.2.0, :1.2 and :latest. A pre-release gets only its own tag."""
    tags = [f"{image}:{version.text}"]
    if not version.prerelease:
        tags.append(f"{image}:{version.minor}")
    if latest:
        tags.append(f"{image}:latest")
    return tags


def remote_tags() -> list[str]:
    out = subprocess.run(
        ["git", "ls-remote", "--tags", "--refs", "origin"],
        cwd=ROOT, capture_output=True, text=True, check=True).stdout
    return [line.split("refs/tags/", 1)[1]
            for line in out.splitlines() if "refs/tags/" in line]


def make_plan(version_text: str, changelog: str, tags: list[str], repository: str,
              event: str, ref: str) -> Plan:
    """Everything release.yml needs to know, from the repository's state.

    It publishes only for a new version, and only from main on a push or a
    manual run. Pull requests and manual runs build everything as a dry run.
    """
    version = parse_version(version_text)
    section = changelog_section(changelog, version.text)
    tag = f"v{version.text}"
    title = f"{tag} — {section.title}" if section.title else tag
    found = existing_tag(tags, version.text)
    latest = is_latest(version, tags)
    image = image_name(repository)
    publish = (found is None and event in ("push", "workflow_dispatch")
               and ref == "refs/heads/main")
    return Plan(version, tag, title, found, latest, image,
                docker_tags(image, version, latest), publish,
                build=publish or event != "push")


def plan_outputs(plan: Plan) -> list[str]:
    flag = {True: "true", False: "false"}
    return [
        f"version={plan.version.text}",
        f"tag={plan.tag}",
        f"title={plan.title}",
        f"new={flag[plan.existing_tag is None]}",
        f"prerelease={flag[plan.version.prerelease]}",
        f"latest={flag[plan.latest]}",
        f"image={plan.image}",
        f"docker_tags={','.join(plan.docker_tags)}",
        f"publish={flag[plan.publish]}",
        f"build={flag[plan.build]}",
    ]


def release_notes(section: Section, tag: str, version: str, image: str,
                  repo_url: str, wheel: str | None = None) -> str:
    install = [f"**Docker:** `docker pull {image}:{version}` "
               f"([Run with Docker]({repo_url}#run-with-docker))"]
    if wheel:
        install.append(f"**pip:** download `{wheel}` below, then "
                       f"`pip install \"./{wheel}[bacnet]\"`")
    install.append(f"**Upgrading:** read [Upgrading]({repo_url}#upgrading) "
                   "in the README before the first run")
    install.append(f"Full history: [CHANGELOG.md]({repo_url}/blob/{tag}/CHANGELOG.md)")
    return section.body + "\n\n---\n\n" + "\n\n".join(install) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="fail unless the version and changelog are releasable")
    p = sub.add_parser("plan", help="print GitHub Actions outputs for release.yml")
    p.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    p.add_argument("--event", default=os.environ.get("GITHUB_EVENT_NAME", ""))
    p.add_argument("--ref", default=os.environ.get("GITHUB_REF", ""))
    p.add_argument("--tags", nargs="*", help="existing tags (default: ask origin)")
    n = sub.add_parser("notes", help="write the release notes")
    n.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    n.add_argument("--server-url", default=os.environ.get("GITHUB_SERVER_URL",
                                                          "https://github.com"))
    n.add_argument("--wheel", help="the wheel's file name, for the pip line")
    n.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        version_text = read_version()
        changelog = CHANGELOG.read_text(encoding="utf-8")
        if args.command == "check":
            version = parse_version(version_text)
            section = changelog_section(changelog, version.text)
            title = repr(section.title) if section.title else "(untitled)"
            print(f"{version.text}: releasable; CHANGELOG section {title}, "
                  f"{len(section.body.splitlines())} lines")
        elif args.command == "plan":
            if not args.repository:
                raise ReleaseError("--repository (owner/name) is required")
            tags = remote_tags() if args.tags is None else args.tags
            plan = make_plan(version_text, changelog, tags, args.repository,
                             args.event, args.ref)
            print("\n".join(plan_outputs(plan)))
            state = (f"already released as {plan.existing_tag}" if plan.existing_tag
                     else "publishing" if plan.publish else "new, but not publishing from here")
            print(f"{plan.title}: {state}", file=sys.stderr)
        else:
            if not args.repository:
                raise ReleaseError("--repository (owner/name) is required")
            version = parse_version(version_text)
            section = changelog_section(changelog, version.text)
            notes = release_notes(section, f"v{version.text}", version.text,
                                  image_name(args.repository),
                                  f"{args.server_url}/{args.repository}", args.wheel)
            args.out.write_text(notes, encoding="utf-8")
    except (ReleaseError, subprocess.CalledProcessError) as exc:
        print(f"release_info: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

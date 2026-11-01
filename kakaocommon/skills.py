"""Offline agent documentation shared by both command-line builds."""

from __future__ import annotations

import argparse
import re
from importlib.resources import files
from typing import TypedDict


class Document(TypedDict):
    topic: str
    version: str
    content: str


def configure_parser(parser: argparse.ArgumentParser) -> None:
    """Register the offline documentation action."""
    actions = parser.add_subparsers(dest="skills_action", required=True)
    get = actions.add_parser("get", help="Read the bundled agent documentation offline")
    _ = get.add_argument("topic", nargs="?", choices=("send", "read", "serve"), default=None)


def get_document(package: str, version: str, topic: str | None) -> Document:
    """Read the same document that the skill installer ships.

    Topic markers are machine-consumed boundaries, not prose headings. Common
    sections (including frontmatter and limits) accompany every selected topic.
    An omitted topic returns the installed SKILL.md unchanged.
    """
    content = files(f"{package}.agent_docs").joinpath("SKILL.md").read_text(encoding="utf-8")
    if topic is not None:
        sections = re.split(r"<!-- skills:(read|send|serve|common) -->", content)
        selected = [sections[0]]
        for marker, section in zip(sections[1::2], sections[2::2]):
            if marker in (topic, "common"):
                selected.append(section)
        content = "".join(selected)
    return {"topic": topic or "all", "version": version, "content": content}

# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Install the skill and its runtime into an agent's skill directory.

Only files this project ships are written.  An existing installation is
refused unless replacement is requested, and even then anything the user put
in the directory is left alone — the installer replaces its own payload rather
than clearing a folder it does not own.

Every shipped path is checked before anything is written, and files are staged
inside the destination first, so a failure part-way through cannot leave a
half-updated installation behind.
"""

from __future__ import annotations

import argparse
import io
import shutil
import sys
import tempfile
from pathlib import Path

DEFAULT_TARGET = Path.home() / ".agents" / "skills" / "kanao-cli"
LAUNCHER = Path("scripts/kanao-cli.py")
LEGACY_LAUNCHER = Path("scripts/kakaoread.py")


def shipped_files(root: Path) -> dict[Path, Path]:
    """Map each installed path to the source file it comes from."""
    package = root / "kanao"
    payload: dict[Path, Path] = {
        Path("SKILL.md"): root / "skill" / "SKILL.md",
        Path("LICENSE"): root / "LICENSE",
        Path("DISCLAIMER.md"): root / "DISCLAIMER.md",
        LAUNCHER: root / "skill" / LAUNCHER,
        LEGACY_LAUNCHER: root / "skill" / LEGACY_LAUNCHER,
    }
    payload.update(
        {
            Path("scripts/kanao") / source.relative_to(package): source
            for source in sorted(package.rglob("*.py"))
            if "__pycache__" not in source.relative_to(package).parts
        }
    )
    common = root.parent / "kakaocommon"
    if not (common / "__init__.py").is_file():
        # Without it the launcher installs and then cannot import anything;
        # a partial copy of the checkout must fail here, not on first use.
        raise FileNotFoundError(f"the shared runtime is missing: {common}")
    payload.update(
        {
            Path("scripts/kakaocommon") / source.relative_to(common): source
            for source in sorted(common.rglob("*.py"))
            if "__pycache__" not in source.relative_to(common).parts
        }
    )
    return payload


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target",
        type=Path,
        default=DEFAULT_TARGET,
        help=f"Exact skill directory (default: {DEFAULT_TARGET})",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace shipped files in an existing directory, keeping anything else",
    )
    arguments = parser.parse_args(argv)

    root = Path(__file__).resolve().parent

    try:
        payload = shipped_files(root)
        target = arguments.target.expanduser().resolve()
        if target.exists() and not arguments.force:
            parser.error(
                f"Destination already exists: {target}; pass --force to replace shipped files"
            )

        for relative, source in payload.items():
            if not source.is_file():
                raise FileNotFoundError(source)
            destination = target / relative
            if destination.is_symlink() or (
                destination.exists() and destination.resolve() != destination
            ):
                parser.error(f"Refusing to write through a link: {destination}")
            if destination.exists() and not destination.is_file():
                raise IsADirectoryError(destination)

        target.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".kanao-cli-install-", dir=target) as staging:
            staged_root = Path(staging)
            for relative, source in payload.items():
                staged = staged_root / relative
                staged.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, staged)
            for relative in payload:
                destination = target / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                (staged_root / relative).replace(destination)
    except OSError as error:
        print(f"Installation failed: {error}", file=sys.stderr)
        return 1

    print(f"Installed kanao-cli skill: {target}")
    print(f"Run it with: python \"{target / LAUNCHER}\" status")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""Install the macOS skill and its runtime into an agent's skill directory.

Run with Python 3.9+; no third-party packages are needed. The default is
~/.agents/skills/kakaoread. Use --target to choose an exact skill directory,
for example ~/.claude/skills/kakaoread, and --force to update an installation.
The payload contains SKILL.md, a launcher, the Python runtime, and LICENSE;
it does not copy account data or grant macOS permissions.

Only files this project ships are written.  An existing installation is
refused unless replacement is requested, and even then anything the user put
in the directory is left alone — the installer replaces its own payload rather
than clearing a folder it does not own.

Every shipped path is checked before anything is written, and files are staged
inside the destination before replacing shipped files. Each file replacement
is atomic; the installation as a whole is not a transaction.
"""

from __future__ import annotations

import argparse
import io
import shutil
import sys
import tempfile
from pathlib import Path

DEFAULT_TARGET = Path.home() / ".agents" / "skills" / "kakaoread"
LAUNCHER = Path("scripts/kakaoread.py")


def shipped_files(root: Path) -> dict[Path, Path]:
    """Map each installed path to the source file it comes from."""
    package = root / "kakaoread"
    payload: dict[Path, Path] = {
        Path("SKILL.md"): root / "skill" / "SKILL.md",
        Path("LICENSE"): root / "LICENSE",
        LAUNCHER: root / "skill" / LAUNCHER,
    }
    payload.update(
        {
            Path("scripts/kakaoread") / source.relative_to(package): source
            for source in sorted(package.rglob("*.py"))
            if "__pycache__" not in source.relative_to(package).parts
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
    payload = shipped_files(root)

    try:
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
        with tempfile.TemporaryDirectory(prefix=".kakaoread-install-", dir=target) as staging:
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

    print(f"Installed kakaoread skill: {target}")
    print(f"Run it with: python3 \"{target / LAUNCHER}\" status")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

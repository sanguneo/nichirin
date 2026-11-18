"""Package the single shared source tree in a self-contained sdist."""

from pathlib import Path
from shutil import copytree, ignore_patterns

from setuptools import setup
from setuptools.command.sdist import sdist


class SharedSourceSdist(sdist):
    """Stage the shared package inside the archive, never beside it."""

    def make_release_tree(self, base_dir: str, files: list[str]) -> None:
        if Path("kakaocommon").is_dir():
            super().make_release_tree(base_dir, files)
            return

        local_files = [name for name in files if ".." not in Path(name).parts]
        super().make_release_tree(base_dir, local_files)
        copytree(
            "../kakaocommon",
            Path(base_dir) / "kakaocommon",
            ignore=ignore_patterns("__pycache__", "*.pyc"),
        )


setup(
    package_dir={
        "kakaocommon": "kakaocommon"
        if Path("kakaocommon").is_dir()
        else "../kakaocommon",
    },
    cmdclass={"sdist": SharedSourceSdist},
)

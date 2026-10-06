"""``python install.py`` from the repository root, as before the merge.

Runs the installer of the build for this machine with the same arguments:
``macos/install.py`` (the ``kamado-cli`` skill) on a Mac, and
``windows/install.py`` (the ``kanao-cli`` skill) elsewhere.
"""

import runpy
import sys
from pathlib import Path

build = "macos" if sys.platform == "darwin" else "windows"
script = Path(__file__).resolve().parent / build / "install.py"
sys.argv[0] = str(script)
runpy.run_path(str(script), run_name="__main__")

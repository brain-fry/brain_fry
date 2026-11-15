"""The name of Cmd+Tab and the Dock (`main.set_display_name`), read back from LaunchServices with
`lsappinfo`. The child process stays a background app (no Dock icon during the tests): only
Renaud's eyes check what Cmd+Tab itself shows (DESIGN_DETAILS.md, "App", Icon)."""

import shutil
import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or not shutil.which("lsappinfo"), reason="macOS LaunchServices")

CHILD = """
import os, subprocess
from AppKit import NSApplication
from brain_fry.app import main

def name():
    asn = subprocess.run(["lsappinfo", "find", f"pid={os.getpid()}"], capture_output=True, text=True).stdout.strip()
    return subprocess.run(["lsappinfo", "info", "-only", "name", asn], capture_output=True, text=True).stdout.strip()

NSApplication.sharedApplication()
print(name())
print(main.set_display_name("Brain Fry"))
print(name())
"""


def test_cmd_tab_name_is_brain_fry():
    out = subprocess.run([sys.executable, "-c", CHILD], capture_output=True, text=True, timeout=30)
    before, ok, after = out.stdout.strip().splitlines()
    assert "Brain Fry" not in before  # "python3": the name of the file that started the process
    assert ok == "True"
    assert after == '"LSDisplayName"="Brain Fry"'

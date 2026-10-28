"""Self-update for pip/pipx installs: check PyPI at most once a day and upgrade in the background.

Installs from a local path or editable checkout (development, the VS Code extension's bundled copy) are left
alone; the extension updates through the Marketplace. Opt out with TOKENDIET_NO_AUTO_UPDATE=1.
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
from importlib import metadata
from pathlib import Path

from . import __version__

DIST = "tokendiet-mcp"
STAMP = Path.home() / ".tokendiet" / "update-check"
INTERVAL = 24 * 3600


def _ver(v):
    return tuple(int(x) for x in v.split(".")[:3] if x.isdigit())


def from_pypi():
    """True if this copy was installed from an index (no PEP 610 direct_url.json), so pip can upgrade it."""
    # a source checkout's *.egg-info can shadow the real metadata, so the code itself must be installed too
    if "site-packages" not in Path(__file__).resolve().parts:
        return False
    try:
        return metadata.distribution(DIST).read_text("direct_url.json") is None
    except metadata.PackageNotFoundError:
        return False


def latest(timeout=3):
    with urllib.request.urlopen(f"https://pypi.org/pypi/{DIST}/json", timeout=timeout) as r:
        return json.load(r)["info"]["version"]


def update(quiet=False):
    """Upgrade now if PyPI has a newer version. Returns the version installed afterwards."""
    say = (lambda *a: None) if quiet else print
    if not from_pypi():
        say(f"tokendiet {__version__}: installed from a local path or by the VS Code extension; not updating from PyPI.")
        return __version__
    new = latest()
    if _ver(new) <= _ver(__version__):
        say(f"tokendiet {__version__} is up to date.")
        return __version__
    say(f"updating tokendiet {__version__} -> {new}")
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--disable-pip-version-check", "--upgrade",
                    f"{DIST}=={new}"], check=True, capture_output=quiet)
    return new


def auto():
    """Throttled update, run detached from the session hook. Never raises."""
    try:
        if os.environ.get("TOKENDIET_NO_AUTO_UPDATE") or not from_pypi():
            return
        if STAMP.exists() and time.time() - STAMP.stat().st_mtime < INTERVAL:
            return
        STAMP.parent.mkdir(parents=True, exist_ok=True)
        STAMP.write_text(time.strftime("%Y-%m-%dT%H:%M:%S"), encoding="utf-8")
        update(quiet=True)
    except Exception:
        pass


def spawn_auto():
    """Start auto() in a detached process so the session never waits on the network or pip."""
    if os.environ.get("TOKENDIET_NO_AUTO_UPDATE") or not from_pypi():
        return
    if STAMP.exists() and time.time() - STAMP.stat().st_mtime < INTERVAL:
        return
    kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kw["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    try:
        subprocess.Popen([sys.executable, "-m", "tokendiet", "update", "--auto"], **kw)
    except OSError:
        pass

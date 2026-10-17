"""
Checks the built wheel and sdist before a release: the version matches the package (and the tag, if one is given),
and the templates and compiled translations are inside. Without them the checkout page and the German texts would
fail only after installation.

    python tools/check_dist.py dist [v0.2.0]
"""
import re
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQUIRED = [
    "pretix_btcpay/templates/pretix_btcpay/pending.html",
    "pretix_btcpay/locale/de/LC_MESSAGES/django.mo",
    "pretix_btcpay/locale/de_Informal/LC_MESSAGES/django.mo",
]


def main(dist, ref=""):
    version = re.search(r'__version__ = "([^"]+)"', (ROOT / "pretix_btcpay/__init__.py").read_text()).group(1)
    setup_version = re.search(r'version="([^"]+)"', (ROOT / "setup.py").read_text()).group(1)
    errors = []
    if setup_version != version:
        errors.append(f"setup.py has {setup_version}, pretix_btcpay has {version}")
    if ref.startswith("v") and ref != f"v{version}":
        errors.append(f"tag {ref} does not match version {version}")

    wheels = list(Path(dist).glob(f"pretix_btcpayserver-{version}-*.whl"))
    sdists = list(Path(dist).glob(f"pretix_btcpayserver-{version}.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        errors.append(f"expected one wheel and one sdist for {version}, found {sorted(p.name for p in Path(dist).iterdir())}")
    else:
        names = set(zipfile.ZipFile(wheels[0]).namelist())
        errors += [f"{wheels[0].name} lacks {name}" for name in REQUIRED if name not in names]
        prefix = f"pretix_btcpayserver-{version}/"
        names = {name.removeprefix(prefix) for name in tarfile.open(sdists[0]).getnames()}
        errors += [f"{sdists[0].name} lacks {name}" for name in REQUIRED if name not in names]

    for error in errors:
        print("error:", error)
    if errors:
        sys.exit(1)
    print(f"ok: {version}")


if __name__ == "__main__":
    main(*sys.argv[1:])

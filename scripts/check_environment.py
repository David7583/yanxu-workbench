"""Check the actual interpreter, venv and core capabilities without external services."""
from pathlib import Path
import json
import site
import sqlite3
import ssl
import sys
import tempfile
import tomllib

root = Path(__file__).resolve().parents[1]
base = Path(sys.base_prefix).resolve()
prefix = Path(sys.prefix).resolve()
assert prefix == (root / ".venv").resolve(), f"Unexpected virtualenv: {prefix}"
assert base.is_relative_to(root / ".tools" / "python"), f"External Python: {base}"
assert not site.ENABLE_USER_SITE, "User site-packages must be disabled"
assert sys.version_info[:2] == (3, 13), sys.version
with sqlite3.connect(":memory:") as connection:
    assert connection.execute("select 1").fetchone() == (1,)
with tempfile.TemporaryDirectory(dir=root / ".cache" / "tmp") as temporary:
    probe = Path(temporary) / "unicode.txt"
    probe.write_text("论文工作台", encoding="utf-8")
    assert probe.read_text(encoding="utf-8") == "论文工作台"
with (root / "pyproject.toml").open("rb") as source:
    assert tomllib.load(source)["project"]["name"] == "paper-workbench"
ssl.create_default_context()
print(json.dumps({
    "status": "ok",
    "python": sys.version.split()[0],
    "executable": sys.executable,
    "base_prefix": str(base),
    "virtualenv": str(prefix),
    "user_site_enabled": site.ENABLE_USER_SITE,
    "sqlite": sqlite3.sqlite_version,
    "tls": ssl.OPENSSL_VERSION,
}, ensure_ascii=False, indent=2))

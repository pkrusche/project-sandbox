"""Run inside the built sandbox, with and without runtime proxy permission."""

import hashlib
import json
import os
import ssl
import subprocess
import sys
from pathlib import Path

phase, proxy_url, runtime_proxy_url, fingerprint = sys.argv[1:]
failures = []


def check(condition, description):
    if not condition:
        failures.append(description)


def curl(*args):
    return subprocess.run(
        ["curl", "--fail", "--silent", "--show-error", "--max-time", "8", *args],
        capture_output=True,
        text=True,
        check=False,
    )


check(
    not Path("/etc/apt/apt.conf.d/99project-sandbox-build-proxy").exists(),
    "build apt proxy settings survived cleanup",
)
check(
    fingerprint
    in {
        hashlib.sha256(certificate).hexdigest()
        for certificate in ssl.create_default_context().get_ca_certs(binary_form=True)
    },
    "build CA did not persist in system trust",
)
if phase == "blocked":
    for name in ("http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        check(
            not os.environ.get(name) and not os.environ.get(name.upper()),
            name + " survived cleanup",
        )
    result = curl(
        "--proxy", proxy_url, "--noproxy", "", "https://pypi.org/simple/idna/"
    )
    check(
        result.returncode in (7, 28),
        "build proxy was reachable without runtime permission: " + result.stderr,
    )
else:
    check(
        os.environ.get("https_proxy") == runtime_proxy_url,
        "runtime proxy was not configured",
    )
    result = curl("https://pypi.org/simple/idna/")
    check(result.returncode == 0, "runtime proxy request failed: " + result.stderr)
    result = curl("--noproxy", "*", "https://pypi.org/simple/idna/")
    check(
        result.returncode in (7, 28),
        "direct runtime bypass was not blocked: " + result.stderr,
    )

Path(f"build-proxy-runtime-{phase}.json").write_text(
    json.dumps({"ok": not failures, "failures": failures})
)
for failure in failures:
    print("FAIL: " + failure)
raise SystemExit(bool(failures))

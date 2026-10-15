"""Runs as the unprivileged Bash agent through the generated container firewall."""

import errno
import json
import os
import socket
import subprocess
import urllib.request
from pathlib import Path


def assert_port_denied(host, port):
    # The host keeps this listener alive, so refusal is an inconclusive failure.
    try:
        with socket.create_connection((host, port), timeout=3):
            raise AssertionError("unallowed live port bypassed the firewall")
    except OSError as exc:
        assert exc.errno in (errno.EACCES, errno.EPERM, errno.EHOSTUNREACH), (
            f"expected administrative firewall rejection, got {exc}"
        )


def main():
    config = json.loads(Path("audit.json").read_text())
    gateway = config["gateway_url"] + "/models"
    key = os.environ["OPENAI_API_KEY"]
    headers = {"Authorization": "Bearer " + key, "X-Proxy-E2E": "urllib"}
    with urllib.request.urlopen(
        urllib.request.Request(gateway, headers=headers), timeout=5
    ) as reply:
        assert json.load(reply)["data"][0]["id"] == "fixture-model"

    def curl(*args):
        return subprocess.run(
            [
                "curl",
                "--globoff",
                "--fail",
                "--silent",
                "--show-error",
                "--max-time",
                "5",
                *args,
            ],
            check=True,
            capture_output=True,
            text=True,
        )

    # Explicitly use the environment's proxy/bypass decisions in all clients.
    curl(
        "--header",
        "Authorization: Bearer " + key,
        "--header",
        "X-Proxy-E2E: curl",
        gateway,
    )
    for scheme in ("http", "https"):
        result = curl(f"{scheme}://origin.test/fixture")
        assert json.loads(result.stdout)["data"][0]["id"] == "fixture-model"
    subprocess.run(
        [
            "node",
            "-e",
            """
        (async () => {
          const response = await fetch(process.argv[1], {headers: {
            Authorization: 'Bearer ' + process.env.OPENAI_API_KEY,
            'X-Proxy-E2E': 'node'
          }});
          if (!response.ok) throw new Error('gateway failed');
          for (const scheme of ['http', 'https']) {
            const reply = await fetch(scheme + '://origin.test/fixture');
            if (!reply.ok) throw new Error('proxy failed');
          }
        })().catch(error => { console.error(error); process.exit(1); });
        """,
            gateway,
        ],
        env={**os.environ, "NODE_USE_ENV_PROXY": "1"},
        check=True,
        timeout=20,
    )

    assert_port_denied(config["canary_host"], config["canary_port"])
    print(
        "PASS: direct authenticated clients, HTTP/CONNECT proxy transport, and live-port denial"
    )


if __name__ == "__main__":
    main()

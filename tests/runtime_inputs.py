"""Inspect the sources actually passed to a simulated container runtime."""

from pathlib import Path

from project_sandbox import container_cli


def mounted_source(argv: list[str], target: str) -> Path:
    for index, option in enumerate(argv[:-1]):
        if option == "--mount":
            spec = container_cli.parse_mount(argv[index + 1])
            if spec.target == target and spec.source is not None:
                return spec.source
    raise AssertionError(f"No mount for {target} in runtime command")

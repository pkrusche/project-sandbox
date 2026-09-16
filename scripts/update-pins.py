#!/usr/bin/env python3
"""Interactively update pinned dependency and tool versions.

The script intentionally updates only pins whose upstream source can also
provide the data needed to keep checksum pins in sync.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
UV_LOCK = ROOT / "uv.lock"
DOCKERFILE = ROOT / "Dockerfile"
DOCKERFILE_TEMPLATE = ROOT / "src/project_sandbox/templates/Dockerfile.j2"
DOCKERFILE_HELPER = ROOT / "src/project_sandbox/dockerfile.py"
CONFIG_AGENTS = ROOT / "src/project_sandbox/config_agents.py"

PI_NPM_PACKAGE = "@earendil-works/pi-coding-agent"

USER_AGENT = "project-sandbox-update-pins"

PYPI_PIN_RE = re.compile(
    r'(?P<quote>["\'])(?P<name>[A-Za-z0-9_.-]+(?:\[[A-Za-z0-9_,.-]+\])?)=='
    r'(?P<version>[^"\']+)(?P=quote)'
)
NPM_PIN_RE = re.compile(
    r"npm install -g (?P<package>(?:@[A-Za-z0-9_.-]+/)?[A-Za-z0-9_.-]+)@"
    r"(?P<version>[0-9][A-Za-z0-9_.+-]*)"
)
UV_IMAGE_RE = re.compile(
    r"ghcr\.io/astral-sh/uv:(?P<version>[0-9][A-Za-z0-9_.-]*)"
    # The helper in dockerfile.py splits the tag and digest across two adjacent
    # Python string literals, e.g. `...uv:0.11.23"\n        "@sha256:...`. Allow
    # that separator so the pin is matched (and updated) in both files.
    r'(?P<sep>"\s*")?'
    r"@sha256:(?P<digest>[a-f0-9]{64})"
)


@dataclass(frozen=True)
class Update:
    label: str
    current: str
    latest: str
    apply: Callable[[], None]
    changed: bool = True


@dataclass(frozen=True)
class UpstreamRelease:
    version: str
    released_at: datetime


DEFAULT_MINIMUM_AGE = timedelta(hours=24)

VERSION_RE = re.compile(
    r"^[vV]?(?P<epoch>[0-9]+!)?(?P<numbers>[0-9]+(?:\.[0-9]+)*)(?P<suffix>.*)$"
)
POST_VERSION_RE = re.compile(r"(?:^|\.)post(?P<number>[0-9]*)", re.IGNORECASE)
PRE_VERSION_RE = re.compile(
    r"^[.-]?(?P<kind>dev|alpha|a|beta|b|rc|pre|preview)"
    r"[.-]?(?P<number>[0-9]*)",
    re.IGNORECASE,
)


def request_json(url: str) -> object:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def request_bytes(
    url: str, headers: dict[str, str] | None = None
) -> tuple[bytes, dict[str, str]]:
    request_headers = {"User-Agent": USER_AGENT}
    if headers:
        request_headers.update(headers)
    req = urllib.request.Request(url, headers=request_headers)
    with urllib.request.urlopen(req, timeout=60) as response:
        return response.read(), dict(response.headers)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def prompt(update: Update, *, assume_yes: bool) -> bool:
    if assume_yes:
        print(f"Applying {update.label}: {update.current} -> {update.latest}")
        return True
    answer = input(
        f"Update {update.label}: {update.current} -> {update.latest}? [y/N] "
    )
    return answer.strip().lower() in {"y", "yes"}


def pypi_package_name(requirement_name: str) -> str:
    return requirement_name.split("[", 1)[0]


def normalize_package_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_timestamp(value: object, source: str) -> datetime:
    if not isinstance(value, str):
        raise RuntimeError(f"{source} did not include a release timestamp")
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError as exc:
        raise RuntimeError(f"{source} included an invalid release timestamp") from exc
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return timestamp.astimezone(UTC)


def release_age(release: UpstreamRelease) -> timedelta:
    return datetime.now(UTC) - release.released_at


def release_is_old_enough(
    release: UpstreamRelease, minimum_age: timedelta, *, source: str
) -> bool:
    age = release_age(release)
    if age < minimum_age:
        minimum_hours = minimum_age.total_seconds() / 3600
        age_hours = age.total_seconds() / 3600
        print(
            f"Skipping {source} {release.version}: it is only "
            f"{age_hours:.1f} hours old (minimum {minimum_hours:g} hours)."
        )
        return False
    return True


def comparable_version_key(
    version: str,
) -> tuple[int, tuple[int, ...], int, int] | None:
    match = VERSION_RE.fullmatch(version)
    if not match:
        return None
    suffix = match.group("suffix").lower()
    pre_match = PRE_VERSION_RE.match(suffix)
    if pre_match:
        rank = {
            "dev": 0,
            "a": 1,
            "alpha": 1,
            "b": 2,
            "beta": 2,
            "pre": 3,
            "preview": 3,
            "rc": 3,
        }[pre_match.group("kind").lower()]
        suffix_rank = rank
        suffix_number = int(pre_match.group("number") or "0")
    elif suffix.startswith(("+",)):
        suffix_rank = 4
        suffix_number = 0
    elif suffix.startswith((".post", "post")):
        post_match = POST_VERSION_RE.search(suffix)
        suffix_rank = 5
        suffix_number = int(post_match.group("number") or "0") if post_match else 0
    elif not suffix:
        suffix_rank = 4
        suffix_number = 0
    else:
        return None
    numbers = [int(part) for part in match.group("numbers").split(".")]
    while len(numbers) > 1 and numbers[-1] == 0:
        numbers.pop()
    epoch = int((match.group("epoch") or "0").removesuffix("!"))
    return epoch, tuple(numbers), suffix_rank, suffix_number


def version_key(version: str) -> tuple[int, tuple[int, ...], int] | None:
    comparable = comparable_version_key(version)
    if comparable is None or comparable[2] < 4:
        return None
    return comparable[0], comparable[1], comparable[3]


def stable_release_key(release: UpstreamRelease) -> tuple[int, tuple[int, ...], int]:
    key = version_key(release.version)
    if key is None:
        raise ValueError(f"Not a stable version: {release.version}")
    return key


def latest_old_enough_release(
    releases: list[UpstreamRelease], minimum_age: timedelta, *, source: str
) -> UpstreamRelease | None:
    if not releases:
        raise RuntimeError(f"{source} did not include any releases")
    stable = [release for release in releases if version_key(release.version) is not None]
    if not stable:
        print(f"Skipping {source}: no stable releases were available.")
        return None
    newest = max(stable, key=stable_release_key)
    eligible = [release for release in stable if release_age(release) >= minimum_age]
    selected = max(eligible, key=stable_release_key) if eligible else None
    if selected is None:
        age_hours = release_age(newest).total_seconds() / 3600
        minimum_hours = minimum_age.total_seconds() / 3600
        print(
            f"Skipping {source}: newest stable version {newest.version} is only "
            f"{age_hours:.1f} hours old (minimum {minimum_hours:g} hours)."
        )
    elif selected.version != newest.version:
        age_hours = release_age(newest).total_seconds() / 3600
        print(
            f"Note: {source} {newest.version} is newer but only "
            f"{age_hours:.1f} hours old; using the latest eligible version "
            f"{selected.version}."
        )
    return selected


def pin_can_move_forward(
    current: str, target: UpstreamRelease, *, source: str
) -> bool:
    current_key = comparable_version_key(current)
    target_key = comparable_version_key(target.version)
    if current_key is not None and target_key is not None and target_key <= current_key:
        print(
            f"Current {source} pin {current} is not older than eligible "
            f"version {target.version}; not moving backward."
        )
        return False
    return True


def latest_pypi_releases(name: str) -> list[UpstreamRelease]:
    # PyPI lookups are synchronous and can take long enough that the script
    # otherwise appears idle, especially while checking every uv.lock package.
    print(f"Checking PyPI for {name}...", flush=True)
    data = request_json(f"https://pypi.org/pypi/{urllib.parse.quote(name)}/json")
    if not isinstance(data, dict) or not isinstance(data.get("info"), dict):
        raise RuntimeError(f"Unexpected PyPI response for {name}")
    releases = data.get("releases")
    if not isinstance(releases, dict):
        raise RuntimeError(f"PyPI response for {name} did not include releases")

    result: list[UpstreamRelease] = []
    for version, files in releases.items():
        if not isinstance(version, str) or not isinstance(files, list):
            continue
        timestamps = [
            parse_timestamp(
                file.get("upload_time_iso_8601", file.get("upload_time")),
                f"PyPI response for {name}",
            )
            for file in files
            if isinstance(file, dict)
        ]
        if timestamps:
            # Use the last uploaded artifact so the whole release has had time
            # to settle before its pin is adopted.
            result.append(UpstreamRelease(version, max(timestamps)))
    if not result:
        raise RuntimeError(f"PyPI response for {name} did not include upload times")
    return result


def latest_pypi_release(name: str) -> UpstreamRelease:
    return max(latest_pypi_releases(name), key=stable_release_key)


def latest_pypi_version(name: str) -> str:
    return latest_pypi_release(name).version


def latest_npm_releases(package: str) -> list[UpstreamRelease]:
    data = request_json(
        f"https://registry.npmjs.org/{urllib.parse.quote(package, safe='@/')}"
    )
    if not isinstance(data, dict) or not isinstance(data.get("dist-tags"), dict):
        raise RuntimeError(f"Unexpected npm response for {package}")
    times = data.get("time")
    versions = data.get("versions")
    if not isinstance(times, dict) or not isinstance(versions, dict):
        raise RuntimeError(f"npm response for {package} did not include release metadata")
    result = [
        UpstreamRelease(
            version,
            parse_timestamp(times.get(version), f"npm response for {package}"),
        )
        for version in versions
        if isinstance(version, str) and version in times
    ]
    if not result:
        raise RuntimeError(f"npm response for {package} did not include release times")
    return result


def latest_npm_release(package: str) -> UpstreamRelease:
    return max(latest_npm_releases(package), key=stable_release_key)


def latest_npm_version(package: str) -> str:
    return latest_npm_release(package).version


def latest_node_releases() -> list[UpstreamRelease]:
    data = request_json("https://nodejs.org/dist/index.json")
    if not isinstance(data, list) or not data:
        raise RuntimeError("Unexpected Node.js release index response")
    result = []
    for item in data:
        if not isinstance(item, dict):
            continue
        version = item.get("version")
        if isinstance(version, str):
            result.append(
                UpstreamRelease(
                    version,
                    parse_timestamp(item.get("date"), "Node.js release index"),
                )
            )
    if not result:
        raise RuntimeError("Node.js release index did not include releases")
    return result


def latest_node_release() -> UpstreamRelease:
    return max(latest_node_releases(), key=stable_release_key)


def latest_node_version() -> str:
    return latest_node_release().version


def node_sha256s(version: str) -> dict[str, str]:
    body, _ = request_bytes(f"https://nodejs.org/dist/{version}/SHASUMS256.txt")
    result: dict[str, str] = {}
    for line in body.decode("utf-8").splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        digest, filename = parts
        for arch in ("x64", "arm64"):
            if filename == f"node-{version}-linux-{arch}.tar.xz":
                result[arch] = digest
    missing = {"x64", "arm64"} - set(result)
    if missing:
        raise RuntimeError(
            f"Missing Node.js checksums for {version}: {', '.join(sorted(missing))}"
        )
    return result


def latest_github_releases(
    owner: str,
    repo: str,
    minimum_age: timedelta = DEFAULT_MINIMUM_AGE,
) -> list[UpstreamRelease]:
    result: list[UpstreamRelease] = []
    cutoff = datetime.now(UTC) - minimum_age
    for page in range(1, 101):
        data = request_json(
            f"https://api.github.com/repos/{owner}/{repo}/releases"
            f"?per_page=100&page={page}"
        )
        if not isinstance(data, list):
            raise RuntimeError(f"Unexpected GitHub release response for {owner}/{repo}")
        for release in data:
            if not isinstance(release, dict):
                continue
            if release.get("draft") or release.get("prerelease"):
                continue
            tag = release.get("tag_name")
            if isinstance(tag, str):
                result.append(
                    UpstreamRelease(
                        tag,
                        parse_timestamp(
                            release.get("published_at"),
                            f"GitHub release response for {owner}/{repo}",
                        ),
                    )
                )
        if len(data) < 100 or any(
            release.released_at <= cutoff for release in result
        ):
            break
    if not result:
        raise RuntimeError(f"GitHub release response for {owner}/{repo} had no releases")
    return result


def latest_github_release_info(owner: str, repo: str) -> UpstreamRelease:
    return max(latest_github_releases(owner, repo), key=stable_release_key)


def latest_github_release(owner: str, repo: str) -> str:
    return latest_github_release_info(owner, repo).version


def download_sha256(url: str) -> str:
    digest = hashlib.sha256()
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as response:
        while chunk := response.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def jj_sha256s(version: str) -> dict[str, str]:
    base = f"https://github.com/jj-vcs/jj/releases/download/{version}"
    return {
        "aarch64": download_sha256(
            f"{base}/jj-{version}-aarch64-unknown-linux-musl.tar.gz"
        ),
        "x86_64": download_sha256(
            f"{base}/jj-{version}-x86_64-unknown-linux-musl.tar.gz"
        ),
    }


def docker_bearer_token(www_authenticate: str) -> str:
    scheme, _, params_text = www_authenticate.partition(" ")
    if scheme.lower() != "bearer":
        raise RuntimeError("Docker registry did not request bearer authentication")
    params: dict[str, str] = {}
    for part in re.findall(r'(\w+)="([^"]*)"', params_text):
        params[part[0]] = part[1]
    realm = params.pop("realm")
    token_url = realm + "?" + urllib.parse.urlencode(params)
    data = request_json(token_url)
    if not isinstance(data, dict) or not isinstance(data.get("token"), str):
        raise RuntimeError("Docker registry token response did not include token")
    return data["token"]


def ghcr_manifest_digest(repository: str, tag: str) -> str:
    url = f"https://ghcr.io/v2/{repository}/manifests/{tag}"
    headers = {
        "Accept": (
            "application/vnd.oci.image.index.v1+json, "
            "application/vnd.docker.distribution.manifest.list.v2+json, "
            "application/vnd.oci.image.manifest.v1+json, "
            "application/vnd.docker.distribution.manifest.v2+json"
        )
    }
    try:
        _, response_headers = request_bytes(url, headers=headers)
    except urllib.error.HTTPError as exc:
        if exc.code != 401:
            raise
        token = docker_bearer_token(exc.headers["WWW-Authenticate"])
        headers["Authorization"] = f"Bearer {token}"
        _, response_headers = request_bytes(url, headers=headers)
    digest = next(
        (
            v
            for k, v in response_headers.items()
            if k.lower() == "docker-content-digest"
        ),
        None,
    )
    if not digest:
        raise RuntimeError(
            f"Registry response for ghcr.io/{repository}:{tag} did not include a digest"
        )
    return digest.removeprefix("sha256:")


def replace_exact(path: Path, old: str, new: str) -> None:
    text = read(path)
    if old not in text:
        raise RuntimeError(f"Expected text was not found in {path}: {old}")
    write(path, text.replace(old, new))


def replace_regex(
    path: Path, pattern: re.Pattern[str], repl: str, *, count: int = 0
) -> None:
    text = read(path)
    new_text, changed = pattern.subn(repl, text, count=count)
    if changed == 0:
        raise RuntimeError(f"Pattern was not found in {path}: {pattern.pattern}")
    write(path, new_text)


def collect_pypi_updates(
    minimum_age: timedelta = DEFAULT_MINIMUM_AGE,
) -> list[Update]:
    text = read(PYPROJECT)
    updates: list[Update] = []
    seen: set[str] = set()
    for match in PYPI_PIN_RE.finditer(text):
        requirement_name = match.group("name")
        package = pypi_package_name(requirement_name)
        if package in seen:
            continue
        seen.add(package)
        current = match.group("version")
        releases = latest_pypi_releases(package)
        release = latest_old_enough_release(
            releases, minimum_age, source=f"PyPI {requirement_name}"
        )
        if release is None:
            continue
        latest = release.version
        if current == latest:
            print(f"Current PyPI pin: {requirement_name}=={current}")
            continue
        if not pin_can_move_forward(
            current, release, source=f"PyPI {requirement_name}"
        ):
            continue

        def apply(
            package: str = package,
            requirement_name: str = requirement_name,
            current: str = current,
            latest: str = latest,
        ) -> None:
            pattern = re.compile(
                rf'(?P<quote>["\']){re.escape(requirement_name)}=={re.escape(current)}(?P=quote)'
            )
            replace_regex(
                PYPROJECT, pattern, rf"\g<quote>{requirement_name}=={latest}\g<quote>"
            )

        updates.append(Update(f"PyPI {requirement_name}", current, latest, apply))
    return updates


def direct_pypi_pins() -> set[str]:
    return {
        normalize_package_name(pypi_package_name(match.group("name")))
        for match in PYPI_PIN_RE.finditer(read(PYPROJECT))
    }


def collect_uv_lock_updates(
    minimum_age: timedelta = DEFAULT_MINIMUM_AGE,
) -> list[Update]:
    if not UV_LOCK.is_file():
        return []
    if shutil.which("uv") is None:
        print(
            "uv not found on PATH; uv.lock-only package pins cannot be updated.",
            file=sys.stderr,
        )
        return []

    data = tomllib.loads(read(UV_LOCK))
    packages = data.get("package")
    if not isinstance(packages, list):
        raise RuntimeError("uv.lock did not contain a package list")

    direct = direct_pypi_pins()
    project_name = ""
    pyproject = tomllib.loads(read(PYPROJECT))
    if isinstance(pyproject.get("project"), dict):
        project = pyproject["project"]
        if isinstance(project.get("name"), str):
            project_name = normalize_package_name(project["name"])

    updates: list[Update] = []
    seen: set[str] = set()
    for package in packages:
        if not isinstance(package, dict):
            continue
        name = package.get("name")
        current = package.get("version")
        if not isinstance(name, str) or not isinstance(current, str):
            continue
        normalized = normalize_package_name(name)
        if normalized in seen or normalized in direct or normalized == project_name:
            continue
        seen.add(normalized)
        releases = latest_pypi_releases(name)
        release = latest_old_enough_release(
            releases, minimum_age, source=f"uv.lock {name}"
        )
        if release is None:
            continue
        latest = release.version
        if current == latest:
            print(f"Current uv.lock pin: {name}=={current}")
            continue
        if not pin_can_move_forward(current, release, source=f"uv.lock {name}"):
            continue

        def apply(name: str = name) -> None:
            subprocess.run(
                ["uv", "lock", "--upgrade-package", name], cwd=ROOT, check=True
            )

        updates.append(Update(f"uv.lock PyPI {name}", current, latest, apply))
    return updates


def collect_npm_updates(
    minimum_age: timedelta = DEFAULT_MINIMUM_AGE,
) -> list[Update]:
    text = read(DOCKERFILE_TEMPLATE)
    updates: list[Update] = []
    seen: set[str] = set()
    for match in NPM_PIN_RE.finditer(text):
        package = match.group("package")
        if package in seen:
            continue
        seen.add(package)
        current = match.group("version")
        releases = latest_npm_releases(package)
        release = latest_old_enough_release(
            releases, minimum_age, source=f"npm {package}"
        )
        if release is None:
            continue
        latest = release.version
        if current == latest:
            print(f"Current npm pin: {package}@{current}")
            continue
        if not pin_can_move_forward(current, release, source=f"npm {package}"):
            continue

        def apply(
            package: str = package, current: str = current, latest: str = latest
        ) -> None:
            replace_exact(
                DOCKERFILE_TEMPLATE, f"{package}@{current}", f"{package}@{latest}"
            )
            if package == PI_NPM_PACKAGE:
                replace_exact(
                    CONFIG_AGENTS,
                    f'_PI_NPM_VERSION_PIN = "{current}"',
                    f'_PI_NPM_VERSION_PIN = "{latest}"',
                )

        updates.append(Update(f"npm {package}", current, latest, apply))
    return updates


def collect_node_update(
    minimum_age: timedelta = DEFAULT_MINIMUM_AGE,
) -> list[Update]:
    text = read(DOCKERFILE_TEMPLATE)
    version_match = re.search(r'NODE_VERSION="(?P<version>v[0-9][^"]+)"', text)
    x64_match = re.search(
        r'x86_64\) NODE_ARCH="x64"; \\\n\s+NODE_SHA256="(?P<sha>[a-f0-9]{64})"', text
    )
    arm64_match = re.search(
        r'aarch64\|arm64\) NODE_ARCH="arm64"; \\\n\s+NODE_SHA256="(?P<sha>[a-f0-9]{64})"',
        text,
    )
    if not version_match or not x64_match or not arm64_match:
        raise RuntimeError(
            "Could not find Node.js version and checksums in Dockerfile template"
        )
    current = version_match.group("version")
    releases = latest_node_releases()
    release = latest_old_enough_release(releases, minimum_age, source="Node.js")
    if release is None:
        return []
    latest = release.version
    if current == latest:
        print(f"Current Node.js pin: {current}")
        return []
    if not pin_can_move_forward(current, release, source="Node.js"):
        return []

    def apply() -> None:
        checksums = node_sha256s(latest)
        updated = read(DOCKERFILE_TEMPLATE)
        updated = updated.replace(
            f'NODE_VERSION="{current}"', f'NODE_VERSION="{latest}"'
        )
        updated = updated.replace(x64_match.group("sha"), checksums["x64"])
        updated = updated.replace(arm64_match.group("sha"), checksums["arm64"])
        write(DOCKERFILE_TEMPLATE, updated)

    return [Update("Node.js binary and SHA256 pins", current, latest, apply)]


def collect_jj_update(
    minimum_age: timedelta = DEFAULT_MINIMUM_AGE,
) -> list[Update]:
    text = read(DOCKERFILE_TEMPLATE)
    version_match = re.search(r'JJ_VERSION="(?P<version>v[0-9][^"]+)"', text)
    arm64_match = re.search(
        r'arm64\|aarch64\) JJ_ARCH="aarch64"; \\\n\s+JJ_SHA256="(?P<sha>[a-f0-9]{64})"',
        text,
    )
    x64_match = re.search(
        r'x86_64\) JJ_ARCH="x86_64"; \\\n\s+JJ_SHA256="(?P<sha>[a-f0-9]{64})"', text
    )
    if not version_match or not arm64_match or not x64_match:
        raise RuntimeError(
            "Could not find jj version and checksums in Dockerfile template"
        )
    current = version_match.group("version")
    releases = latest_github_releases("jj-vcs", "jj", minimum_age)
    release = latest_old_enough_release(releases, minimum_age, source="jj")
    if release is None:
        return []
    latest = release.version
    if current == latest:
        print(f"Current jj pin: {current}")
        return []
    if not pin_can_move_forward(current, release, source="jj"):
        return []

    def apply() -> None:
        checksums = jj_sha256s(latest)
        updated = read(DOCKERFILE_TEMPLATE)
        updated = updated.replace(f'JJ_VERSION="{current}"', f'JJ_VERSION="{latest}"')
        updated = updated.replace(arm64_match.group("sha"), checksums["aarch64"])
        updated = updated.replace(x64_match.group("sha"), checksums["x86_64"])
        write(DOCKERFILE_TEMPLATE, updated)

    return [Update("jj binary and SHA256 pins", current, latest, apply)]


def collect_uv_image_update(
    minimum_age: timedelta = DEFAULT_MINIMUM_AGE,
) -> list[Update]:
    candidates = [DOCKERFILE, DOCKERFILE_HELPER]
    matches: list[tuple[Path, str, str]] = []
    for path in candidates:
        for match in UV_IMAGE_RE.finditer(read(path)):
            matches.append((path, match.group("version"), match.group("digest")))
    if not matches:
        raise RuntimeError("Could not find ghcr.io/astral-sh/uv image pins")
    current_versions = {version for _, version, _ in matches}
    if len(current_versions) != 1:
        raise RuntimeError(
            f"Found multiple uv image versions: {', '.join(sorted(current_versions))}"
        )
    current_digests = {digest for _, _, digest in matches}
    if len(current_digests) != 1:
        raise RuntimeError(
            "Found multiple uv image digests; update them manually first"
        )

    current = next(iter(current_versions))
    current_digest = next(iter(current_digests))
    releases = latest_github_releases("astral-sh", "uv", minimum_age)
    release = latest_old_enough_release(releases, minimum_age, source="uv image")
    if release is None:
        return []
    latest = release.version.removeprefix("v")
    latest_digest = ghcr_manifest_digest("astral-sh/uv", latest)
    latest_value = f"{latest}@sha256:{latest_digest}"
    current_value = f"{current}@sha256:{current_digest}"
    if current_value == latest_value:
        print(f"Current uv image pin: {current_value}")
        return []
    if not pin_can_move_forward(current, release, source="uv image"):
        return []

    def apply() -> None:
        def repl(match: re.Match[str]) -> str:
            sep = match.group("sep") or ""
            return f"ghcr.io/astral-sh/uv:{latest}{sep}@sha256:{latest_digest}"

        for path, _, _ in matches:
            text = read(path)
            text = UV_IMAGE_RE.sub(repl, text)
            write(path, text)

    return [Update("uv image tag and digest", current_value, latest_value, apply)]


def run_uv_lock() -> None:
    if shutil.which("uv") is None:
        print(
            "uv not found on PATH; pyproject.toml changed, but uv.lock was not regenerated.",
            file=sys.stderr,
        )
        return
    subprocess.run(["uv", "lock"], cwd=ROOT, check=True)


def nonnegative_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError("must be a finite, non-negative number")
    return parsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="apply every available update without prompting",
    )
    parser.add_argument(
        "--min-age-hours",
        type=nonnegative_float,
        default=24.0,
        metavar="HOURS",
        help="only update to releases at least this many hours old (default: 24)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    minimum_age = timedelta(hours=args.min_age_hours)
    collectors: list[Callable[[timedelta], list[Update]]] = [
        collect_pypi_updates,
        collect_uv_lock_updates,
        collect_npm_updates,
        collect_node_update,
        collect_jj_update,
        collect_uv_image_update,
    ]
    updates: list[Update] = []
    for collector in collectors:
        updates.extend(collector(minimum_age))
    if not updates:
        print("All known pins are already current.")
        return 0

    applied_pypi = False
    applied_count = 0
    for update in updates:
        if not prompt(update, assume_yes=args.yes):
            print(f"Skipped {update.label}")
            continue
        update.apply()
        applied_count += 1
        applied_pypi = applied_pypi or update.label.startswith("PyPI ")

    if applied_pypi:
        run_uv_lock()

    print(f"Applied {applied_count} update(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

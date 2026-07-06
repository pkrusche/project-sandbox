#!/usr/bin/env bash
# Automated release script for project-sandbox.
#
# Steps:
#   1. Verify the working copy is clean.
#   2. Run Ruff and pytest checks.
#   3. Confirm / bump the version in pyproject.toml.
#   4. Create a GitHub release and tag via the gh CLI.
#
# Progress is tracked in .release-status/ (git-ignored) so that you can
# resume after a failed step without re-running earlier ones.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

STATUS_DIR="$ROOT/.release-status"
mkdir -p "$STATUS_DIR"

# --- arg parsing loop ---

while [[ $# -gt 0 ]]; do
    case "$1" in
        --help|-h)
            echo "Usage: $0 [--help]"
            echo "Automated release script for project-sandbox."
            echo
            echo "Steps:"
            echo "  1. Verify the working copy is clean."
            echo "  2. Run Ruff and pytest checks."
            echo "  3. Confirm / bump the version in pyproject.toml."
            echo "  4. Create a GitHub release and tag via the gh CLI."
            exit 0
            ;;
        *)
            die "Unknown argument: $1"
            ;;
    esac
done

# ── helpers ──────────────────────────────────────────────────────────────────

step_done() { [[ -f "$STATUS_DIR/$1" ]]; }
mark_done() { touch "$STATUS_DIR/$1"; }

die() { echo "ERROR: $*" >&2; exit 1; }

# ── working-copy cleanliness check ───────────────────────────────────────────

check_clean() {
    # Prefer jj when the repo is managed by Jujutsu.
    if command -v jj &>/dev/null && jj root &>/dev/null 2>&1; then
        # in jj we check that the current revision is empty and the parent revision
        # is immutable and bookmarked as main
        if [[ -z "$(jj log -r '@ & empty()' --no-graph --template 'commit_id')" ]]; then
            echo "Working copy has uncommitted changes (jj):" >&2
            jj status >&2
            die "Commit or stage changes before releasing."
        fi
        if [[ -z "$(jj log -r '@- & main & main@origin & immutable()' \
            --no-graph --template 'commit_id')" ]]; then
            die "The parent revision must be immutable main and pushed to origin."
        fi
    else
        # for git we check the working copy is clean and that we're on
        # branch main, which is in sync with origin/main
        if [[ -n "$(git status --porcelain)" ]]; then
            echo "Working copy has uncommitted changes (git):" >&2
            git status --short >&2
            die "Commit or stage changes before releasing."
        fi
        if [[ "$(git branch --show-current)" != "main" ]]; then
            die "Releases must be made from the main branch."
        fi
        if ! git show-ref --verify --quiet refs/remotes/origin/main; then
            die "origin/main is not available. Fetch from origin before releasing."
        fi
        if [[ "$(git rev-parse HEAD)" != "$(git rev-parse refs/remotes/origin/main)" ]]; then
            die "The main branch must be pushed to and in sync with origin/main."
        fi
    fi
}

# ── step 1: working copy clean (always checked) ───────────────────────────────

echo "==> [1/4] Checking working copy is clean …"
check_clean
mark_done "01-clean"
echo "    OK"

# extract commit ID from git or jj - depending on whether we have a .jj directory
if command -v jj &>/dev/null && jj root &>/dev/null 2>&1; then
    COMMIT_ID=$(jj log -r @ -n 1 --no-graph --template 'commit_id')
else
    COMMIT_ID=$(git rev-parse HEAD)
fi

# ── step 2: run checks ────────────────────────────────────────────────────────

if ! step_done "02-checks-${COMMIT_ID}"; then
    echo "==> [2/4] Running Ruff and pytest …"
    "$ROOT/scripts/check-ruff.sh"
    uv run pytest -q
    mark_done "02-checks-${COMMIT_ID}"
    echo "    OK"
fi

# ── step 3: version bump ──────────────────────────────────────────────────────

if ! step_done "03-version"; then
    CURRENT_VERSION=$(uv version --short)
    echo "==> [3/4] Version bump"
    echo "    Current version: $CURRENT_VERSION"
    read -r -p "    Bump (major/minor/patch/alpha/beta/rc/post/dev; blank to keep): " VERSION_BUMP

    if [[ -n "$VERSION_BUMP" ]]; then
        case "$VERSION_BUMP" in
            major|minor|patch|stable|alpha|beta|rc|post|dev) ;;
            *) die "Unknown version bump '$VERSION_BUMP'." ;;
        esac
        uv version --bump "$VERSION_BUMP"
        NEW_VERSION=$(uv version --short)
        echo "    Updated project version to $NEW_VERSION — please commit the version bump now."
        echo "    Re-run this script once the version bump is committed."
        exit 0
    fi

    NEW_VERSION="$CURRENT_VERSION"

    # Record the resolved version for later steps.
    echo "$NEW_VERSION" > "$STATUS_DIR/version"
    mark_done "03-version"
    echo "    Version: $NEW_VERSION"
fi

RELEASE_VERSION=$(cat "$STATUS_DIR/version")

# ── step 4: GitHub release ────────────────────────────────────────────────────

if ! step_done "04-gh-release"; then
    if ! command -v gh &>/dev/null; then
        die "'gh' CLI not found. Install it from https://cli.github.com/ and authenticate."
    fi

    RELEASE_TAG="v$RELEASE_VERSION"
    if remote_tag=$(git ls-remote --exit-code --tags origin "refs/tags/$RELEASE_TAG" 2>&1); then
        die "GitHub tag $RELEASE_TAG already exists."
    else
        status=$?
        if [[ $status -ne 2 ]]; then
            echo "$remote_tag" >&2
            die "Could not check whether GitHub tag $RELEASE_TAG exists."
        fi
    fi

    if release_lookup=$(gh api "repos/{owner}/{repo}/releases/tags/$RELEASE_TAG" 2>&1); then
        die "GitHub release $RELEASE_TAG already exists."
    elif ! grep -q 'HTTP 404' <<<"$release_lookup"; then
        echo "$release_lookup" >&2
        die "Could not check whether GitHub release $RELEASE_TAG exists."
    fi

    echo "==> [4/4] Creating GitHub release $RELEASE_TAG …"
    gh release create "v$RELEASE_VERSION" \
        --title "v$RELEASE_VERSION" \
        --generate-notes
    mark_done "04-gh-release"
    echo "    GitHub release v$RELEASE_VERSION created."
fi

# ── done ──────────────────────────────────────────────────────────────────────

echo
echo "Release v$RELEASE_VERSION complete."
echo "You may remove .release-status/ to reset the release state:"
echo "  rm -rf .release-status/"

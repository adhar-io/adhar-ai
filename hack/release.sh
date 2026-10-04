#!/usr/bin/env bash
#
# release.sh — cut a release of adhar-ai.
#
#   ./hack/release.sh 0.3.0            # bump, verify, commit, tag, push
#   ./hack/release.sh 0.3.0 --dry-run  # everything except commit/tag/push
#
# What a release IS here:
#   1. ONE version, in every place that states it: pyproject.toml,
#      src/adhar_ai/__init__.py, uv.lock, and the README's version badge.
#      A test asserts they agree, so a release cannot ship with the README
#      saying one number and /healthz another.
#   2. A commit that changes only those files, and an annotated tag `vX.Y.Z`.
#   3. The push of main AND the tag. The tag is what publishes
#      ghcr.io/adhar-io/adhar-ai-*:X.Y.Z and creates the GitHub Release
#      (.github/workflows/images.yml and release.yml); the push of main is
#      what moves `:latest`, which is what the platform deploys.
#
# What it refuses to do:
#   * release from a dirty tree — the release commit must contain only the
#     version change, or the tag describes work nobody reviewed as a release;
#   * release from any branch but main — `:latest` tracks main;
#   * release a version that is not greater than the current one;
#   * release without the full test suite, lint and type check passing.
#
# The platform repository is NOT touched. It deploys `:latest` with
# imagePullPolicy: Always, so it needs no change to pick up a release; what it
# needs is a rollout, which `docs/RELEASING.md` describes.
set -euo pipefail

VERSION="${1:-}"
DRY_RUN="${2:-}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${ROOT}"

red()   { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
step()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
die()   { red "error: $*"; exit 1; }

[[ -n "${VERSION}" ]] || die "usage: $0 X.Y.Z [--dry-run]"
[[ "${VERSION}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "version must be X.Y.Z, got ${VERSION}"
[[ -z "${DRY_RUN}" || "${DRY_RUN}" == "--dry-run" ]] || die "unknown argument ${DRY_RUN}"

CURRENT="$(grep -m1 '^version = ' pyproject.toml | sed 's/version = "\(.*\)"/\1/')"
step "Releasing adhar-ai ${CURRENT} -> ${VERSION}"

# ----------------------------------------------------------------- guards ---
[[ "$(git branch --show-current)" == "main" ]] || die "releases are cut from main (\`:latest\` tracks main)"
[[ -z "$(git status --porcelain)" ]] || die "working tree is dirty; a release commit must contain only the version change"
git fetch -q origin main
[[ "$(git rev-parse HEAD)" == "$(git rev-parse origin/main)" ]] || die "local main differs from origin/main; pull or push first"
git rev-parse -q --verify "refs/tags/v${VERSION}" >/dev/null && die "tag v${VERSION} already exists"

# Greater than the current version, numerically.
if [[ "$(printf '%s\n%s\n' "${CURRENT}" "${VERSION}" | sort -V | tail -1)" != "${VERSION}" || "${CURRENT}" == "${VERSION}" ]]; then
  die "${VERSION} is not greater than the current ${CURRENT}"
fi

# ------------------------------------------------------------------- bump ---
step "Writing ${VERSION} into every place that states a version"
sed -i.bak "s/^version = \"${CURRENT}\"/version = \"${VERSION}\"/" pyproject.toml && rm pyproject.toml.bak
sed -i.bak "s/^__version__ = \"${CURRENT}\"/__version__ = \"${VERSION}\"/" src/adhar_ai/__init__.py && rm src/adhar_ai/__init__.py.bak
# The README badge, which is the version a reader sees first.
sed -i.bak "s|/release-v${CURRENT}-|/release-v${VERSION}-|; s|/badge/release-[0-9.]*-|/badge/release-${VERSION}-|" README.md && rm README.md.bak
# The lock records the project's own version.
uv lock -q

for f in pyproject.toml src/adhar_ai/__init__.py README.md uv.lock; do
  grep -q "${VERSION}" "$f" || die "${f} does not contain ${VERSION} after the bump"
done
green "  pyproject.toml, src/adhar_ai/__init__.py, README.md, uv.lock"

# ----------------------------------------------------------------- verify ---
step "Verifying (the same gates CI runs)"
uv run ruff check src tests
uv run mypy src
uv run adhar-ai tools | diff -q contract/tools.json - >/dev/null || die "contract/tools.json is stale; regenerate it in its own commit first"
uv run pytest -q -x
green "  lint, types, contract and ${CURRENT}->${VERSION} drift tests pass"

# ---------------------------------------------------------- commit + tag ---
if [[ "${DRY_RUN}" == "--dry-run" ]]; then
  step "Dry run — restoring the tree"
  git checkout -q -- pyproject.toml src/adhar_ai/__init__.py README.md uv.lock
  green "  nothing committed, tagged or pushed"
  exit 0
fi

step "Committing and tagging v${VERSION}"
git add pyproject.toml src/adhar_ai/__init__.py README.md uv.lock
git commit -q -m "release: v${VERSION}

Version ${VERSION} in pyproject, the package, the lock and the README badge,
asserted equal by the suite. The tag publishes ghcr.io/adhar-io/adhar-ai-*:${VERSION}
and the GitHub Release; the push of main moves \`:latest\`, which the platform
deploys."
git tag -a "v${VERSION}" -m "adhar-ai v${VERSION}"

step "Pushing main and v${VERSION}"
git push -q origin main
git push -q origin "v${VERSION}"

green "
Released v${VERSION}.

  images   ghcr.io/adhar-io/adhar-ai-{runtime,mcp-<domain>}:${VERSION}  (and :latest)
  release  https://github.com/adhar-io/adhar-ai/releases/tag/v${VERSION}
  watch    https://github.com/adhar-io/adhar-ai/actions

The platform deploys :latest and needs no manifest change. To make the running
cluster pick this up:

  kubectl rollout restart deployment -n adhar-system -l adhar.io/origin=adhar-ai

and confirm with /healthz, which reports the version and commit it is running.
"

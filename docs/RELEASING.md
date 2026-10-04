# Releasing

> One command cuts a release. Everything it does is checked, and everything it
> refuses to do is listed here.

```bash
./hack/release.sh                  # next patch (0.3.0 -> 0.3.1): bump, verify, commit, tag, push
./hack/release.sh --dry-run        # the same, without commit, tag and push
./hack/release.sh 0.4.0            # a deliberate minor or major bump
```

Releases are routine and most are fixes, so **the default is the next patch**.
A minor or major bump is a decision, and is spelled out on the command line.

---

## 1. What a release is

| Artefact | Where | Produced by |
|---|---|---|
| One version number | `pyproject.toml`, `src/adhar_ai/__init__.py`, `uv.lock`, the README's `adhar-ai` badge | `hack/release.sh`, asserted equal by the test suite |
| An annotated tag `vX.Y.Z` on `main` | GitHub | `hack/release.sh` |
| Eight images at `:X.Y.Z`, `:latest` and `:sha-<commit>` | `ghcr.io/adhar-io/adhar-ai-{runtime,mcp-<domain>}` | `.github/workflows/images.yml`, on the tag and on the push to `main` |
| Keyless signatures and an SPDX SBOM on each image | the registry, by digest | `images.yml` |
| A GitHub Release with notes, the tool contract and the SBOM | the Releases page | `.github/workflows/release.yml`, after `images.yml` succeeds |

Every image reports the version and the commit it was built from at
`GET /healthz`, so a running pod can be checked against the release it is
supposed to be.

---

## 2. What the script refuses

Each refusal exists because the alternative produced a bad release at least
once somewhere.

- **A dirty tree.** The release commit must contain only the version change,
  or the tag describes work nobody reviewed *as a release*.
- **Any branch but `main`.** The platform deploys `:latest`, and `:latest`
  tracks `main`. A release from a branch would publish images the cluster
  never runs.
- **Local `main` behind or ahead of `origin/main`.** Pull or push first.
- **A version not greater than the current one**, or a tag that already exists.
- **A stale tool contract.** `contract/tools.json` is the schema the Go CLI is
  written against. It is regenerated in its own commit, reviewed as the
  breaking change it may be, never silently inside a release.
- **A failing suite, lint or type check.** The same gates CI runs.

---

## 3. What the version means

`MAJOR.MINOR.PATCH`, where the thing being versioned is **what the platform can
rely on**:

- **PATCH** — a fix. No tool added or changed, no new knowledge kind, no config
  key. A cluster on `:latest` picks it up on the next rollout and nothing an
  operator wrote needs to change.
- **MINOR** — a capability. A new tool (the contract grows), a new knowledge
  source, a new ConfigMap block, a new route. Additive: existing config keeps
  working.
- **MAJOR** — a tool removed or its arguments changed, a config key renamed, a
  guarantee changed. The Go CLI contract breaks.

The autonomy guarantee — *read tools read, write tools open a pull request,
nothing applies to a cluster* — is not versioned. It does not change.

---

## 4. The README is part of the release

The README states the current version in exactly one badge, the `adhar-ai`
one, and `hack/release.sh` rewrites it in the same commit as `pyproject.toml`.
There used to be a second badge beside it, and the two disagreed. A test
(`tests/test_config_and_provenance.py`) asserts the three agree, and
`release.yml` refuses to publish a Release whose tag the README does not show.

So the README cannot say one version while `/healthz` says another. If you
bump by hand instead of with the script, the suite tells you what you missed.

---

## 5. After the release: the platform

The platform package (`platform/stack/packages/ai/adhar-ai` in the `adhar`
repository) pins **no version**. Every Deployment runs `:latest` with
`imagePullPolicy: Always`, exactly as `adhar-console` does, and the package
contract declares `appVersion: latest`. A release therefore needs **no change
to the platform repository** to be available.

What it needs is a **rollout**. An unchanged Deployment spec is not drift, so
ArgoCD will not restart pods when a new `:latest` is pushed:

```bash
kubectl rollout restart deployment -n adhar-system -l adhar.io/origin=adhar-ai
kubectl -n adhar-system rollout status deployment/adhar-ai-runtime
```

The label selects exactly the eight adhar-ai Deployments. Do not use
`app.kubernetes.io/part-of=adhar-ai`: the vLLM package shares it, and
restarting a model server to refresh an agent is not the same job.

Confirm with the runtime itself:

```bash
curl -s https://agent.<host>/healthz | jq '{version, revision}'
```

Two platform-side records are worth updating when a release is significant:
the package contract's `version` (the marketplace schema says it tracks the
app version being packaged), and a line in the platform's release notes. Both
are documentation, not deployment.

---

## 6. If something goes wrong

- **`images.yml` failed after the tag was pushed.** Fix on `main`, then
  re-run the workflow for the tag from the Actions page. The tag itself is
  correct; only the build failed.
- **A bad release reached `:latest`.** Roll the cluster back by pinning the
  previous version's digest in the Deployments — the Releases page lists the
  digests — then fix forward on `main`. Do not delete or move a tag; a tag is
  a record.
- **The Release page is missing.** `release.yml` runs only after `images.yml`
  succeeds for a `v*` tag. Check that run first.

---

## See also

- **[PRODUCTION.md](PRODUCTION.md)** — what is watched once it is running
- **[OPERATIONS.md](OPERATIONS.md)** — every setting, and the health surface
- **[TOOLS.md](TOOLS.md)** — the contract a MAJOR bump changes

"""The platform-configuration sources: manifests, environments, the CLI.

What is tested is what a wrong answer would come from:

* a Secret value in the knowledge base (never);
* a template manifest that silently yields nothing (the Keycloak database
  lived in one);
* a dependency that is declared in a ConfigMap rather than an env var;
* a Gitea tree read in one page when the repository has two;
* a kind filter the lexical fallback ignores.

Rendering is asserted on FACTS — the image, the secret, the hostname — not on
the exact prose, so a wording change does not fail the suite.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from adhar_ai.rag.documents import KIND_WEIGHTS, Chunk
from adhar_ai.rag.lexical import LexicalIndex
from adhar_ai.rag.manifests import (
    package_of,
    parse_manifests,
    references,
    render,
    render_package_overview,
)
from adhar_ai.rag.platform_sources import CliSource, EnvironmentSource, ManifestsSource
from adhar_ai.rag.trees import GiteaTree, LocalTree, tree_for

# ---------------------------------------------------------------- fixtures ---

DEPLOYMENT = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: keycloak
  namespace: adhar-system
  labels:
    app.kubernetes.io/part-of: identity
  annotations:
    argocd.argoproj.io/sync-wave: "5"
spec:
  replicas: 2
  template:
    metadata:
      labels: {app: keycloak}
    spec:
      serviceAccountName: keycloak
      containers:
        - name: keycloak
          image: quay.io/keycloak/keycloak:26.0
          args: ["start", "--optimized"]
          ports: [{containerPort: 8080}]
          readinessProbe: {httpGet: {path: /health/ready, port: 8080}}
          env:
            - name: KC_DB_PASSWORD
              valueFrom: {secretKeyRef: {name: keycloak-db-app, key: password}}
            - name: KC_DB_URL_HOST
              value: keycloak-db-rw.adhar-system.svc.cluster.local
          envFrom:
            - configMapRef: {name: keycloak-config}
      volumes:
        - name: tls
          secret: {secretName: keycloak-tls}
"""

SECRET = """\
apiVersion: v1
kind: Secret
metadata:
  name: harbor-push
  namespace: adhar-system
type: kubernetes.io/basic-auth
stringData:
  username: robot$pusher
  password: hunter2-very-secret
data:
  token: aG90LXRva2Vu
"""

CNPG_TEMPLATE = """\
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata:
  name: keycloak-db
  namespace: adhar-system
spec:
  instances: {{ if .EnableHAMode }}2{{ else }}1{{ end }}
  {{- if .HasDNS01 }}
  storage:
    size: 5Gi
  {{- end }}
  bootstrap:
    initdb:
      database: keycloak
      owner: {{ .DBOwner }}
"""

ROUTE = """\
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: console
  namespace: adhar-system
spec:
  parentRefs: [{name: adhar-gateway}]
  hostnames: [console.adhar.localtest.me]
  rules:
    - backendRefs: [{name: console, port: 80}]
"""

EXTERNAL_SECRET = """\
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: adhar-ai-llm
  namespace: adhar-system
spec:
  refreshInterval: 1h
  secretStoreRef: {name: vault, kind: ClusterSecretStore}
  target: {name: adhar-ai-llm}
  data:
    - secretKey: API_KEY
      remoteRef: {key: adhar-ai/llm, property: API_KEY}
"""

CONFIGMAP_WITH_DB = """\
apiVersion: v1
kind: ConfigMap
metadata:
  name: keycloak-config
  namespace: adhar-system
data:
  keycloak.conf: |
    db=postgres
    db-url=jdbc:postgresql://keycloak-db-rw.adhar-system.svc.cluster.local:5432/keycloak
  admin-token: should-not-be-shown-even-though-this-is-a-configmap
"""


def _resource(text: str, path: str = "security/keycloak/manifests/install.yaml"):
    resources = parse_manifests(path, text)
    assert resources, "fixture did not parse"
    return resources[0]


# -------------------------------------------------------------- manifests ---


def test_a_path_names_its_package_the_same_way_from_either_tree():
    assert package_of("security/keycloak/manifests/install.yaml") == "security/keycloak"
    assert package_of("ai/adhar-ai/manifests/gpu/install.yaml") == "ai/adhar-ai"
    assert package_of("core/adhar-console/README.md") == "core/adhar-console"


def test_a_workload_renders_its_facts_not_its_structure():
    r = _resource(DEPLOYMENT)
    refs = references(r)
    text = render(r, refs)

    assert "quay.io/keycloak/keycloak:26.0" in text
    assert "`keycloak-db-app`" in text and "`keycloak-tls`" in text
    assert "`keycloak-config`" in text
    assert "/health/ready" in text
    assert "sync wave 5" in text
    assert "part of `identity`" in text
    # The CNPG convention and the -rw hostname both point at the same cluster.
    assert refs.databases == ["keycloak-db"]
    assert "Binds the CNPG database cluster(s): `keycloak-db`" in text


def test_secret_values_are_never_rendered_from_any_block():
    """The one rule that is a safety rule. `stringData` AND `data`."""
    text = render(_resource(SECRET))
    assert "hunter2-very-secret" not in text
    assert "robot$pusher" not in text
    assert "aG90LXRva2Vu" not in text
    # Key NAMES are useful and allowed.
    assert "`password`" in text and "`username`" in text and "`token`" in text
    assert "Values are not recorded" in text


def test_a_configmap_inlines_configuration_but_never_a_sensitive_key():
    text = render(_resource(CONFIGMAP_WITH_DB))
    assert "db-url=jdbc:postgresql://keycloak-db-rw" in text, "config should be inlined"
    assert "should-not-be-shown" not in text, "a key named *token* must not be inlined"
    assert "`admin-token`" in text, "but its name may be listed"


def test_a_database_declared_in_a_configmap_is_a_dependency():
    """Keycloak's db-url lives in a ConfigMap, not an env var. Same dependency."""
    assert references(_resource(CONFIGMAP_WITH_DB)).databases == ["keycloak-db"]


def test_a_go_template_manifest_parses_and_keeps_both_branches():
    """The Keycloak database lived in `install.yaml.tmpl` and yielded nothing.

    An inline `{{ if }}2{{ else }}1{{ end }}` is honestly "2 or 1"; either alone
    would be a guess about the environment.
    """
    r = _resource(CNPG_TEMPLATE, "security/keycloak/manifests/install.yaml.tmpl")
    assert r.kind == "Cluster"
    text = render(r)
    assert "2 or 1 instance" in text
    assert "`keycloak`" in text  # initdb database
    assert "<DBOwner>" in text  # a substituted value is shown as a placeholder
    assert "`keycloak-db-rw`" in text and "`keycloak-db-app`" in text


def test_a_route_renders_hostname_gateway_and_backend():
    r = _resource(ROUTE, "core/adhar-console/manifests/httproute.yaml")
    text = render(r)
    assert "https://console.adhar.localtest.me" in text
    assert "`adhar-gateway`" in text
    assert "`console:80`" in text
    assert references(r).hostnames == ["console.adhar.localtest.me"]


def test_an_external_secret_names_its_store_target_and_remote_keys():
    r = _resource(EXTERNAL_SECRET, "ai/adhar-ai/manifests/llm.yaml")
    text = render(r)
    assert "ClusterSecretStore `vault`" in text
    assert "Secret `adhar-ai-llm`" in text
    assert "`adhar-ai/llm` / `API_KEY`" in text
    assert references(r).provides_secret == "adhar-ai-llm"


def test_a_package_overview_gathers_what_the_package_deploys():
    resources = (
        parse_manifests("security/keycloak/manifests/install.yaml", DEPLOYMENT)
        + parse_manifests("security/keycloak/manifests/db.yaml.tmpl", CNPG_TEMPLATE)
        + parse_manifests("security/keycloak/manifests/route.yaml", ROUTE)
    )
    text = render_package_overview("security/keycloak", resources)
    assert "3 resource(s)" in text
    assert "quay.io/keycloak/keycloak:26.0" in text
    assert "console.adhar.localtest.me" in text
    assert "CNPG cluster `keycloak-db`" in text
    assert "`keycloak-db-app`" in text


def test_a_file_that_is_not_kubernetes_yields_nothing_not_an_error():
    assert parse_manifests("x/y/manifests/kustomization.yaml", "resources:\n- a.yaml\n") == []
    assert parse_manifests("x/y/manifests/broken.yaml", "a: [unclosed") == []
    assert parse_manifests("x/y/manifests/empty.yaml", "") == []


def test_the_new_kinds_are_registered():
    """A kind missing here is retrieved at weight 1.0 and, worse, is not a
    name an agent's `knowledge_kinds` can ask for."""
    for kind in ("manifest", "environment", "cli"):
        assert kind in KIND_WEIGHTS


# ---------------------------------------------------------------- sources ---


@pytest.fixture
def platform(tmp_path: Path) -> Path:
    """A miniature platform checkout with the real directory shape."""
    pk = tmp_path / "packages"
    (pk / "security/keycloak/manifests").mkdir(parents=True)
    (pk / "security/keycloak/manifests/install.yaml").write_text(DEPLOYMENT)
    (pk / "security/keycloak/manifests/db.yaml.tmpl").write_text(CNPG_TEMPLATE)
    (pk / "security/keycloak/README.md").write_text("# Keycloak\n\nRotate the realm key yearly.\n")
    (pk / "core/adhar-console/manifests").mkdir(parents=True)
    (pk / "core/adhar-console/manifests/httproute.yaml").write_text(ROUTE)
    (pk / "core/adhar-console/manifests/rbac.yaml").write_text(
        "apiVersion: v1\nkind: ServiceAccount\nmetadata: {name: console, namespace: adhar-system}\n"
    )
    env = tmp_path / "environments"
    (env / "local").mkdir(parents=True)
    def row(name: str, enabled: str, category: str) -> str:
        return (
            f"  - {{name: {name}, enabled: '{enabled}', namespace: adhar-system, "
            f"category: {category}, manifestPath: {category}/{name}/manifests}}\n"
        )

    (env / "local/config.yaml").write_text(
        "environment: local\ntype: nonprod\npackages:\n"
        + row("keycloak", "true", "security")
        + row("adhar-console", "true", "core")
        + row("falco", "false", "security")
    )
    (env / "production").mkdir()
    (env / "production/config.yaml").write_text(
        "environment: prod\ntype: prod\npackages:\n" + row("falco", "true", "security")
    )
    cmd = tmp_path / "cmd"
    (cmd / "get").mkdir(parents=True)
    (cmd / "main.go").write_text(
        "package main\nfunc init() {\n\tAddCommand(\n"
        "\t\tget.GetCmd, // resources\n\t\tup.UpCmd,\n\t)\n}\n"
    )
    (cmd / "get/get.go").write_text(
        textwrap.dedent(
            '''\
            package get
            var GetCmd = &cobra.Command{
            \tUse:   "get",
            \tShort: "Get information about Adhar platform resources",
            }
            func init() {
            \tGetCmd.AddCommand(platformHealthCmd)
            }
            '''
        )
    )
    (cmd / "get/platform_health.go").write_text(
        textwrap.dedent(
            '''\
            package get
            var platformHealthCmd = &cobra.Command{
            \tUse:     "platform-health",
            \tAliases: []string{"health", "ph"},
            \tShort:   "Show the health of every platform package",
            \tLong: `Show the health of every platform package.

            Examples:
              adhar get platform-health        # Table
              adhar get platform-health -o json`,
            \tRunE: runPlatformHealth,
            }
            '''
        )
    )
    (cmd / "get/get_test.go").write_text(
        'package get\nvar fakeCmd = &cobra.Command{\n\tUse: "should-not-appear",\n}\n'
    )
    return tmp_path


async def test_manifests_source_emits_resources_an_overview_and_the_readme(platform: Path):
    source = ManifestsSource(LocalTree(platform / "packages"))
    docs = await source.documents()
    ids = {d.doc_id for d in docs}

    assert "manifest:security/keycloak/Deployment/adhar-system/keycloak" in ids
    assert "manifest:security/keycloak/Cluster/adhar-system/keycloak-db" in ids, "the template"
    assert "manifest:security/keycloak/overview" in ids
    assert "manifest:security/keycloak/README" in ids
    # RBAC noise is on the overview but gets no document of its own.
    assert not any("/ServiceAccount/" in i for i in ids)
    assert all(d.origin == "manifests" for d in docs)
    readme = next(d for d in docs if d.doc_id.endswith("/README"))
    assert readme.kind == "doc" and "Rotate the realm key" in readme.text


async def test_manifests_source_keeps_the_parse_for_the_graph(platform: Path):
    source = ManifestsSource(LocalTree(platform / "packages"))
    await source.documents()
    assert {r.kind for r in source.resources} >= {"Deployment", "Cluster", "HTTPRoute"}


async def test_environment_source_says_what_is_on_where_and_uses_the_directory_name(platform: Path):
    docs = await EnvironmentSource(LocalTree(platform / "environments")).documents()
    by_id = {d.doc_id: d for d in docs}

    local = by_id["environment:local"]
    assert local.kind == "environment"
    assert "2 of 3 packages enabled" in local.text
    assert "`keycloak`" in local.text and "`adhar-console`" in local.text
    assert "## Disabled packages" in local.text and "`falco`" in local.text
    assert local.metadata["enabled"] == ["adhar-console", "keycloak"]

    # `production/config.yaml` says `environment: prod`; both names are kept.
    prod = by_id["environment:production"]
    assert "(`prod`)" in prod.text
    assert prod.metadata["alias"] == "prod"


async def test_cli_source_renders_the_command_tree_with_help_and_skips_tests(platform: Path):
    docs = await CliSource(LocalTree(platform), prefix="cmd").documents()
    by_id = {d.doc_id: d for d in docs}

    get = by_id["cli:adhar-get"]
    assert get.kind == "cli"
    assert "## `adhar get platform-health`" in get.text, "parent resolved through AddCommand"
    assert "Aliases: `health`, `ph`" in get.text
    assert "adhar get platform-health -o json" in get.text, "the raw-string Long survived"
    assert "should-not-appear" not in get.text, "a _test.go literal must be ignored"
    assert get.metadata["commands"] == ["adhar get", "adhar get platform-health"]


async def test_a_source_with_no_tree_contributes_nothing_rather_than_failing():
    assert await ManifestsSource(None).documents() == []
    assert await EnvironmentSource(None).documents() == []
    assert await CliSource(None).documents() == []


# ------------------------------------------------------------------- trees ---


class FakeGitea:
    """Two pages of tree, like the real `packages` repository."""

    def __init__(self) -> None:
        self.tree_calls: list[dict] = []
        self.reads: list[str] = []

    async def get_branch_sha(self, repo: str, branch: str = "main") -> str:
        return "a" * 40

    async def _request(self, method: str, path: str, **kwargs):  # pragma: no cover
        raise AssertionError("GiteaTree must go through get_tree/get_raw")

    async def get_tree(self, repo: str, ref: str = "main", *, recursive: bool = True):
        self.tree_calls.append({"repo": repo, "ref": ref})
        return [
            {"type": "blob", "path": "a/x/manifests/one.yaml", "size": 10, "sha": "s1"},
            {"type": "tree", "path": "a/x/manifests", "size": 0, "sha": "t1"},
            {"type": "blob", "path": "a/x/README.md", "size": 5, "sha": "s2"},
            {"type": "blob", "path": "huge/manifests/crd.yaml", "size": 10_000_000, "sha": "s3"},
        ]

    async def get_raw(self, repo: str, path: str, ref: str = "main") -> str:
        self.reads.append(path)
        if path.endswith("one.yaml"):
            return "kind: ConfigMap\napiVersion: v1\nmetadata: {name: one}\n"
        raise RuntimeError("boom")


async def test_a_gitea_tree_lists_blobs_only_filters_and_skips_huge_files():
    tree = GiteaTree(FakeGitea(), "packages")
    files = await tree.files((".yaml",))
    assert [f.path for f in files] == ["a/x/manifests/one.yaml"], "no trees, no README, no 10MB CRD"
    assert files[0].sha == "s1"
    assert tree.label == "gitea:packages@main"


async def test_a_gitea_tree_reads_concurrently_and_drops_only_the_file_that_failed():
    client = FakeGitea()
    tree = GiteaTree(client, "packages")
    texts = await tree.read_many(["a/x/manifests/one.yaml", "a/x/README.md"])
    assert set(texts) == {"a/x/manifests/one.yaml"}
    assert sorted(client.reads) == ["a/x/README.md", "a/x/manifests/one.yaml"]


async def test_the_real_client_pages_a_truncated_tree(monkeypatch):
    """Gitea returns 1,000 entries and `truncated: true`; the rest must be
    fetched or the last 227 files of `packages` silently vanish."""
    from adhar_ai.clients.gitea import GiteaClient
    from adhar_ai.config import GiteaConfig

    def page(start: int, stop: int) -> dict:
        blobs = [{"path": f"f{i}", "type": "blob"} for i in range(start, stop)]
        return {"tree": blobs, "truncated": True, "total_count": 1227}

    pages = {1: page(0, 1000), 2: page(1000, 1227)}
    seen: list[int] = []

    class Resp:
        def __init__(self, payload):
            self._p = payload

        def json(self):
            return self._p

    async def fake_request(self, method, path, **kwargs):
        page = int(kwargs["params"]["page"])
        seen.append(page)
        return Resp(pages[page])

    monkeypatch.setattr(GiteaClient, "_request", fake_request)
    client = GiteaClient(GiteaConfig(api_url="http://gitea:3000"))
    entries = await client.get_tree("packages", "b" * 40)
    assert len(entries) == 1227
    assert seen == [1, 2]


def test_tree_for_prefers_a_checkout_that_exists_then_gitea(tmp_path: Path):
    assert isinstance(tree_for(tmp_path, FakeGitea(), "packages"), LocalTree)
    assert isinstance(tree_for(tmp_path / "missing", FakeGitea(), "packages"), GiteaTree)
    assert isinstance(tree_for(None, FakeGitea(), "packages"), GiteaTree)
    assert tree_for(tmp_path / "missing", None, "packages") is None


# ---------------------------------------------------------- lexical kinds ---


def test_the_lexical_fallback_honours_the_kind_filter():
    """It did not. An agent scoped to runbooks read manifests the moment the
    database was down — the one time the scope was most likely to matter."""
    chunks = [
        Chunk("d1", 0, "manifest a", "manifest", "manifests", "keycloak binds the db-app secret"),
        Chunk("d2", 0, "runbook b", "runbook", "docs", "keycloak restart steps for the db secret"),
    ]
    index = LexicalIndex.from_chunks(chunks)
    everything = [c.kind for c, _ in index.search("keycloak secret", k=5)]
    assert set(everything) == {"manifest", "runbook"}
    only_runbooks = [c.kind for c, _ in index.search("keycloak secret", k=5, kinds=("runbook",))]
    assert only_runbooks == ["runbook"]


# ------------------------------------------------------- tokenizer, ids ---


def test_the_tokenizer_emits_a_compound_and_its_parts():
    """People write "adhar console" and "API key"; the manifests say
    `adhar-console` and `API_KEY`. Both must be emitted or the two never meet."""
    from adhar_ai.rag.lexical import tokenize

    tokens = tokenize("adhar-console reads API_KEY from app.kubernetes.io")
    assert "adhar-console" in tokens and "adhar" in tokens and "console" in tokens
    assert "api_key" in tokens and "api" in tokens and "key" in tokens
    assert "app.kubernetes.io" in tokens and "kubernetes" in tokens


def test_the_tokenizer_stems_timidly():
    from adhar_ai.rag.lexical import tokenize

    tokens = tokenize("deployed deploys deploying")
    assert tokens.count("deploy") == 3, "all three inflections meet at the stem"
    assert "deployed" in tokens, "the exact word is kept alongside its stem"
    # Short words and non-alphabetic identifiers are left alone.
    assert tokenize("pods") == ["pods"]
    assert tokenize("v1beta1") == ["v1beta1"]


async def test_two_commands_with_one_variable_name_in_different_packages_stay_apart(tmp_path: Path):
    """`cmd/health/health.go` and `cmd/db/health.go` both declare `healthCmd`.
    Keyed by variable alone, the second overwrote the first's parent and
    `adhar db health` rendered as `adhar health`."""
    cmd = tmp_path / "cmd"
    (cmd / "health").mkdir(parents=True)
    (cmd / "db").mkdir()
    (cmd / "main.go").write_text(
        "package main\nfunc init() {\n\tAddCommand(health.HealthCmd, db.DbCmd)\n}\n"
    )
    (cmd / "health/health.go").write_text(
        'package health\nvar HealthCmd = &cobra.Command{\n\tUse: "health",\n'
        '\tShort: "Platform health",\n}\n'
    )
    (cmd / "db/db.go").write_text(
        'package db\nvar DbCmd = &cobra.Command{\n\tUse: "db",\n\tShort: "Databases",\n}\n'
        "func init() {\n\tDbCmd.AddCommand(healthCmd)\n}\n"
    )
    (cmd / "db/health.go").write_text(
        'package db\nvar healthCmd = &cobra.Command{\n\tUse: "health",\n'
        '\tShort: "Database health",\n}\n'
    )
    docs = await CliSource(LocalTree(tmp_path), prefix="cmd").documents()
    commands = sorted(c for d in docs for c in d.metadata["commands"])
    assert commands == ["adhar db", "adhar db health", "adhar health"]


async def test_duplicate_manifest_objects_get_distinct_stable_ids(tmp_path: Path):
    """The same Deployment in a cpu and a gpu variant used to share one id and
    overwrite each other on every refresh, re-embedding both every time."""
    pk = tmp_path / "packages"
    for variant in ("cpu", "gpu"):
        (pk / f"ai/vllm/manifests/{variant}").mkdir(parents=True)
        (pk / f"ai/vllm/manifests/{variant}/install.yaml").write_text(
            "apiVersion: apps/v1\nkind: Deployment\n"
            "metadata: {name: vllm, namespace: adhar-system}\n"
            "spec:\n  template:\n    spec:\n      containers:\n"
            f"        - {{name: v, image: vllm:{variant}}}\n"
        )
    first = await ManifestsSource(LocalTree(pk)).documents()
    second = await ManifestsSource(LocalTree(pk)).documents()
    ids = [d.doc_id for d in first if "/Deployment/" in d.doc_id]
    assert len(ids) == 2 and len(set(ids)) == 2
    assert ids == [d.doc_id for d in second if "/Deployment/" in d.doc_id], "stable across runs"

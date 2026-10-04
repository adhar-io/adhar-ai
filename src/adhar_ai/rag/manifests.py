"""Turning Kubernetes manifests into things a model can read.

A manifest is the platform's actual configuration: which image a workload runs,
which Secret it mounts, which database it binds, which hostname exposes it.
That is precisely the knowledge the agent lacked — it knew *why* the platform
was shaped the way it is, from the ADRs, and almost nothing about *how*.

YAML does not retrieve well as text. A Deployment is forty lines of structure
around five facts, and an embedding of the structure says "Kubernetes" rather
than "Keycloak binds the `keycloak-db-app` secret". So each resource is parsed
and **rendered as prose**: the facts, named, in a sentence or a bullet each.
The same parse yields the references the knowledge graph is built from.

Two rules are safety rules rather than quality ones:

* **Secret values are never rendered.** Only key names. A `stringData` block in
  a manifest is a credential in a repository, and the knowledge base must not
  become a second place it can be read from.
* **ConfigMap values are rendered only when they look like configuration** —
  YAML, JSON, plain settings — and never for keys whose names suggest a secret
  that landed in the wrong object.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("adhar_ai.rag.manifests")

#: Kinds that run containers. Rendered with images, env references and mounts.
WORKLOAD_KINDS = frozenset({"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"})

#: Kinds whose spec is reference-heavy and rendered in full detail.
DETAILED_KINDS = WORKLOAD_KINDS | {
    "Service",
    "HTTPRoute",
    "Cluster",  # CNPG
    "ExternalSecret",
    "Certificate",
    "ConfigMap",
    "Secret",
    "Application",
    "ApplicationSet",
    "ClusterPolicy",
    "Policy",
    "NetworkPolicy",
    "ServiceMonitor",
    "Ingress",
    "Namespace",
    "ScheduledBackup",
}

#: ConfigMap keys never rendered by value, whatever they contain.
SENSITIVE_KEY = re.compile(
    r"(secret|token|password|passwd|credential|apikey|api_key|private)", re.I
)

#: ConfigMap values longer than this are summarised rather than inlined.
MAX_INLINE_VALUE = 4000

#: Go-template directives, so a `.yaml.tmpl` parses as YAML. Whole control
#: lines are dropped; an INLINE conditional keeps both branches joined with
#: "or", because `instances: {{ if .EnableHAMode }}2{{ else }}1{{ end }}` is
#: honestly described as "2 or 1 instances" and dishonestly as either alone;
#: value placeholders become `<Name>` so the prose says where a value is
#: substituted at render time.
TEMPLATE_CONTROL = re.compile(r"^\s*\{\{-?\s*(if|else|end|range|with|define|template)\b.*?\}\}\s*$")
TEMPLATE_INLINE_IF_ELSE = re.compile(
    r"\{\{-?\s*if\b[^}]*\}\}(?P<a>[^{]*)\{\{-?\s*else\s*-?\}\}(?P<b>[^{]*)\{\{-?\s*end\s*-?\}\}"
)
TEMPLATE_INLINE_IF = re.compile(r"\{\{-?\s*if\b[^}]*\}\}(?P<a>[^{]*)\{\{-?\s*end\s*-?\}\}")
TEMPLATE_VALUE = re.compile(r"\{\{-?\s*\.?([A-Za-z0-9_.]+)\s*-?\}\}")

#: A CNPG cluster is reached through its `<name>-rw` Service. That hostname,
#: wherever it appears — an env value, a ConfigMap setting, a container
#: argument — is the most reliable sign that something depends on that database.
DB_HOST = re.compile(r"\b([a-z0-9-]+)-rw\.[a-z0-9-]+\.svc")


@dataclass(slots=True)
class Resource:
    """One parsed Kubernetes object, with where it came from."""

    api_version: str
    kind: str
    name: str
    namespace: str
    path: str
    package: str
    labels: dict[str, str] = field(default_factory=dict)
    annotations: dict[str, str] = field(default_factory=dict)
    spec: dict[str, Any] = field(default_factory=dict)
    data: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def ref(self) -> str:
        where = f" in `{self.namespace}`" if self.namespace else ""
        return f"{self.kind} `{self.name}`{where}"


@dataclass(slots=True)
class References:
    """What a resource points at. The edges of the knowledge graph."""

    images: list[str] = field(default_factory=list)
    secrets: list[str] = field(default_factory=list)
    configmaps: list[str] = field(default_factory=list)
    services: list[str] = field(default_factory=list)
    hostnames: list[str] = field(default_factory=list)
    databases: list[str] = field(default_factory=list)
    service_account: str = ""
    selector: dict[str, str] = field(default_factory=dict)
    pod_labels: dict[str, str] = field(default_factory=dict)
    #: For an ExternalSecret: the Secret it writes.
    provides_secret: str = ""


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


def package_of(path: str) -> str:
    """`security/keycloak/manifests/install.yaml` -> `security/keycloak`.

    Both trees agree on this shape: the Gitea `packages` repository IS the
    `platform/stack/packages` directory, so a path has the category first and
    the package second whichever tree it came from.
    """
    parts = path.split("/")
    if "manifests" in parts:
        idx = parts.index("manifests")
        if idx >= 2:
            return f"{parts[idx - 2]}/{parts[idx - 1]}"
    if len(parts) >= 2:
        return f"{parts[0]}/{parts[1]}"
    return parts[0]


def _detemplate(text: str) -> str:
    lines = [line for line in text.splitlines() if not TEMPLATE_CONTROL.match(line)]
    text = "\n".join(lines)
    text = TEMPLATE_INLINE_IF_ELSE.sub(
        lambda m: f"{m.group('a').strip()} or {m.group('b').strip()}", text
    )
    text = TEMPLATE_INLINE_IF.sub(lambda m: m.group("a").strip(), text)
    return TEMPLATE_VALUE.sub(lambda m: f"<{m.group(1)}>", text)


def parse_manifests(path: str, text: str) -> list[Resource]:
    """Every object in one manifest file. A file that will not parse yields
    nothing and logs why; it must not take the other 600 with it."""
    import yaml

    if path.endswith(".tmpl"):
        text = _detemplate(text)
    try:
        docs = list(yaml.safe_load_all(text))
    except yaml.YAMLError as exc:
        log.debug("manifest %s did not parse: %s", path, exc)
        return []

    package = package_of(path)
    out: list[Resource] = []
    for doc in docs:
        if not isinstance(doc, dict) or not doc.get("kind"):
            continue
        meta = doc.get("metadata") or {}
        if not isinstance(meta, dict):
            continue
        name = str(meta.get("name") or meta.get("generateName") or "").strip()
        if not name:
            continue
        out.append(
            Resource(
                api_version=str(doc.get("apiVersion") or ""),
                kind=str(doc["kind"]),
                name=name,
                namespace=str(meta.get("namespace") or ""),
                path=path,
                package=package,
                labels={str(k): str(v) for k, v in (meta.get("labels") or {}).items()},
                annotations={str(k): str(v) for k, v in (meta.get("annotations") or {}).items()},
                spec=dict(spec) if isinstance((spec := doc.get("spec")), dict) else {},
                data=dict(data) if isinstance((data := doc.get("data")), dict) else {},
                raw=doc,
            )
        )
    return out


# --------------------------------------------------------------------------- #
# References
# --------------------------------------------------------------------------- #


def _pod_template(resource: Resource) -> dict[str, Any]:
    spec = resource.spec
    if resource.kind == "CronJob":
        spec = ((spec.get("jobTemplate") or {}).get("spec") or {})
    template = spec.get("template") or {}
    return template if isinstance(template, dict) else {}


def _containers(pod_spec: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for key in ("initContainers", "containers"):
        for c in pod_spec.get(key) or []:
            if isinstance(c, dict):
                out.append(c)
    return out


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(i for i in items if i))


def references(resource: Resource) -> References:
    """What a resource depends on, read from its spec."""
    refs = References()
    kind, spec = resource.kind, resource.spec

    if kind in WORKLOAD_KINDS:
        template = _pod_template(resource)
        pod_spec = template.get("spec") or {}
        pod_meta_labels = (template.get("metadata") or {}).get("labels") or {}
        refs.pod_labels = {str(k): str(v) for k, v in pod_meta_labels.items()}
        refs.service_account = str(pod_spec.get("serviceAccountName") or "")
        for c in _containers(pod_spec):
            if c.get("image"):
                refs.images.append(str(c["image"]))
            for env in c.get("env") or []:
                source = (env.get("valueFrom") or {}) if isinstance(env, dict) else {}
                if "secretKeyRef" in source:
                    refs.secrets.append(str(source["secretKeyRef"].get("name") or ""))
                if "configMapKeyRef" in source:
                    refs.configmaps.append(str(source["configMapKeyRef"].get("name") or ""))
                value = str(env.get("value") or "") if isinstance(env, dict) else ""
                refs.databases += DB_HOST.findall(value)
            for arg in (c.get("args") or []) + (c.get("command") or []):
                refs.databases += DB_HOST.findall(str(arg))
            for env_from in c.get("envFrom") or []:
                if "secretRef" in env_from:
                    refs.secrets.append(str(env_from["secretRef"].get("name") or ""))
                if "configMapRef" in env_from:
                    refs.configmaps.append(str(env_from["configMapRef"].get("name") or ""))
        for volume in pod_spec.get("volumes") or []:
            if not isinstance(volume, dict):
                continue
            if "secret" in volume:
                refs.secrets.append(str(volume["secret"].get("secretName") or ""))
            if "configMap" in volume:
                refs.configmaps.append(str(volume["configMap"].get("name") or ""))
            projected = (volume.get("projected") or {}).get("sources") or []
            for source in projected:
                if "secret" in source:
                    refs.secrets.append(str(source["secret"].get("name") or ""))
                if "configMap" in source:
                    refs.configmaps.append(str(source["configMap"].get("name") or ""))
        # The CNPG convention: the app secret is `<cluster>-app`.
        for secret in list(refs.secrets):
            if secret.endswith("-app"):
                refs.databases.append(secret[: -len("-app")])

    elif kind == "Service":
        refs.selector = {str(k): str(v) for k, v in (spec.get("selector") or {}).items()}

    elif kind == "ConfigMap":
        # Keycloak's `db-url=jdbc:postgresql://keycloak-db-rw...` lives in a
        # ConfigMap, not an env var. The dependency is just as real.
        for value in resource.data.values():
            refs.databases += DB_HOST.findall(str(value))

    elif kind in ("HTTPRoute", "GRPCRoute", "TLSRoute"):
        refs.hostnames = [str(h) for h in spec.get("hostnames") or []]
        for rule in spec.get("rules") or []:
            for backend in (rule or {}).get("backendRefs") or []:
                if isinstance(backend, dict) and backend.get("name"):
                    port = backend.get("port")
                    name = str(backend["name"])
                    refs.services.append(f"{name}:{port}" if port else name)

    elif kind == "Ingress":
        for rule in spec.get("rules") or []:
            if (rule or {}).get("host"):
                refs.hostnames.append(str(rule["host"]))

    elif kind == "ExternalSecret":
        refs.provides_secret = str((spec.get("target") or {}).get("name") or resource.name)

    elif kind == "Certificate":
        refs.hostnames = [str(h) for h in spec.get("dnsNames") or []]
        if spec.get("secretName"):
            refs.provides_secret = str(spec["secretName"])

    refs.images = _dedupe(refs.images)
    refs.secrets = _dedupe(refs.secrets)
    refs.configmaps = _dedupe(refs.configmaps)
    refs.services = _dedupe(refs.services)
    refs.hostnames = _dedupe(refs.hostnames)
    refs.databases = _dedupe(refs.databases)
    return refs


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def _bullets(items: list[str]) -> list[str]:
    return [f"- {item}" for item in items]


def _wave(resource: Resource) -> str:
    return resource.annotations.get("argocd.argoproj.io/sync-wave", "")


def _render_workload(resource: Resource, refs: References) -> list[str]:
    spec = resource.spec
    pod_spec = _pod_template(resource).get("spec") or {}
    lines: list[str] = []
    if resource.kind == "CronJob":
        lines.append(f"Runs on schedule `{spec.get('schedule')}`.")
    elif "replicas" in spec:
        lines.append(f"Runs {spec.get('replicas')} replica(s).")
    if refs.service_account:
        lines.append(f"Runs as ServiceAccount `{refs.service_account}`.")

    for c in _containers(pod_spec):
        name = c.get("name", "?")
        image = c.get("image", "?")
        args = c.get("args") or c.get("command") or []
        shown = " ".join(str(a) for a in args[:6])
        ports = ", ".join(
            str(p.get("containerPort")) for p in (c.get("ports") or []) if isinstance(p, dict)
        )
        line = f"Container `{name}` runs image `{image}`"
        if shown:
            line += f" with `{shown}`"
        if ports:
            line += f", listening on {ports}"
        lines.append(line + ".")
        for probe in ("readinessProbe", "livenessProbe"):
            http = ((c.get(probe) or {}).get("httpGet") or {})
            if http.get("path"):
                lines.append(f"Its {probe[:-5]} probe is `GET {http['path']}`.")

    if refs.secrets:
        lines.append("Reads Secrets: " + ", ".join(f"`{s}`" for s in refs.secrets) + ".")
    if refs.configmaps:
        lines.append("Reads ConfigMaps: " + ", ".join(f"`{s}`" for s in refs.configmaps) + ".")
    if refs.databases:
        names = ", ".join(f"`{d}`" for d in refs.databases)
        lines.append(f"Binds the CNPG database cluster(s): {names}.")
    return lines


def _render_service(resource: Resource, refs: References) -> list[str]:
    spec = resource.spec
    lines = [f"Type `{spec.get('type', 'ClusterIP')}`."]
    ports = []
    for p in spec.get("ports") or []:
        if isinstance(p, dict):
            ports.append(f"{p.get('port')}→{p.get('targetPort', p.get('port'))}")
    if ports:
        lines.append("Ports: " + ", ".join(ports) + ".")
    if refs.selector:
        selector = ", ".join(f"`{k}={v}`" for k, v in refs.selector.items())
        lines.append(f"Selects pods labelled {selector}.")
    return lines


def _render_route(resource: Resource, refs: References) -> list[str]:
    lines: list[str] = []
    if refs.hostnames:
        urls = ", ".join(f"`https://{h}`" for h in refs.hostnames)
        # "URL" is the word people ask with; "hostname" is the word the spec
        # uses. The page says both so either question finds it.
        lines.append(f"URL: {urls} (hostname{'s' if len(refs.hostnames) > 1 else ''} "
                     + ", ".join(f"`{h}`" for h in refs.hostnames) + ").")
    parents = [
        f"`{p.get('name')}`" for p in resource.spec.get("parentRefs") or [] if isinstance(p, dict)
    ]
    if parents:
        lines.append("Attached to Gateway " + ", ".join(parents) + ".")
    if refs.services:
        lines.append("Routes to Service " + ", ".join(f"`{s}`" for s in refs.services) + ".")
    return lines


def _render_cnpg(resource: Resource) -> list[str]:
    spec = resource.spec
    initdb = (spec.get("bootstrap") or {}).get("initdb") or {}
    size = (spec.get("storage") or {}).get("size", "?")
    lines = [
        f"PostgreSQL cluster with {spec.get('instances', '?')} instance(s), "
        f"image `{spec.get('imageName', 'default')}`, storage `{size}`."
    ]
    if initdb:
        lines.append(
            f"Bootstraps database `{initdb.get('database')}` owned by `{initdb.get('owner')}`."
        )
    lines.append(
        f"Applications connect through Service `{resource.name}-rw` and read credentials from "
        f"Secret `{resource.name}-app` (keys `username`, `password`, `dbname`, `host`, `port`)."
    )
    return lines


def _render_external_secret(resource: Resource, refs: References) -> list[str]:
    spec = resource.spec
    store = spec.get("secretStoreRef") or {}
    keys: list[str] = []
    for item in spec.get("data") or []:
        remote = (item or {}).get("remoteRef") or {}
        key, prop = remote.get("key"), remote.get("property")
        keys.append(f"`{key}`" + (f" / `{prop}`" if prop else ""))
    lines = [
        f"Materialises Secret `{refs.provides_secret}` from {store.get('kind', 'SecretStore')} "
        f"`{store.get('name')}`."
    ]
    if keys:
        lines.append("Remote keys: " + ", ".join(keys) + ".")
    if spec.get("refreshInterval"):
        lines.append(f"Refreshes every `{spec['refreshInterval']}`.")
    return lines


def _render_certificate(resource: Resource, refs: References) -> list[str]:
    issuer = resource.spec.get("issuerRef") or {}
    lines = []
    if refs.hostnames:
        lines.append("Covers: " + ", ".join(f"`{h}`" for h in refs.hostnames) + ".")
    lines.append(
        f"Issued by {issuer.get('kind', 'Issuer')} `{issuer.get('name')}` into Secret "
        f"`{refs.provides_secret}`."
    )
    return lines


def _looks_like_config(key: str, value: str) -> bool:
    if SENSITIVE_KEY.search(key):
        return False
    config_suffixes = (".yaml", ".yml", ".json", ".toml", ".conf", ".ini", ".txt", ".properties")
    return key.endswith(config_suffixes) or ("\n" in value and len(value) <= MAX_INLINE_VALUE)


def _render_configmap(resource: Resource) -> list[str]:
    lines: list[str] = []
    keys = sorted(resource.data)
    if keys:
        lines.append("Keys: " + ", ".join(f"`{k}`" for k in keys) + ".")
    for key in keys:
        value = resource.data[key]
        text = value if isinstance(value, str) else str(value)
        if _looks_like_config(key, text) and len(text) <= MAX_INLINE_VALUE:
            lines += ["", f"### `{key}`", "", "```", text.rstrip(), "```"]
    return lines


def _render_secret(resource: Resource) -> list[str]:
    """Key NAMES only. Never the values, whatever block they are in."""
    string_data = resource.raw.get("stringData")
    keys = sorted(set(resource.data) | set(string_data if isinstance(string_data, dict) else {}))
    lines = [f"Type `{resource.raw.get('type', 'Opaque')}`."]
    if keys:
        names = ", ".join(f"`{k}`" for k in keys)
        lines.append(f"Keys: {names}. Values are not recorded here.")
    return lines


def _render_argo(resource: Resource) -> list[str]:
    spec = resource.spec
    lines = []
    source = spec.get("source") or {}
    if not source and spec.get("sources"):
        source = spec["sources"][0] if isinstance(spec["sources"], list) else {}
    if source:
        revision = source.get("targetRevision", "HEAD")
        lines.append(
            f"Source `{source.get('repoURL')}` path `{source.get('path')}` at `{revision}`."
        )
    dest = spec.get("destination") or {}
    if dest:
        lines.append(f"Deploys into namespace `{dest.get('namespace')}`.")
    sync = (spec.get("syncPolicy") or {})
    if sync.get("automated"):
        auto = sync["automated"]
        prune, heal = auto.get("prune", False), auto.get("selfHeal", False)
        lines.append(f"Automated sync (prune={prune}, selfHeal={heal}).")
    return lines


def _render_policy(resource: Resource) -> list[str]:
    action = resource.spec.get("validationFailureAction", "Audit")
    lines = [f"Validation failure action: `{action}`."]
    for rule in resource.spec.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        message = ((rule.get("validate") or {}).get("message") or "").strip()
        kinds = ", ".join(
            str(k)
            for r in ((rule.get("match") or {}).get("any") or []) + [rule.get("match") or {}]
            for k in ((r.get("resources") or {}).get("kinds") or [])
        )
        line = f"Rule `{rule.get('name')}`"
        if kinds:
            line += f" on {kinds}"
        if message:
            line += f": {message}"
        lines.append(line + ".")
    return lines


def _render_networkpolicy(resource: Resource) -> list[str]:
    spec = resource.spec
    selector = ", ".join(
        f"`{k}={v}`" for k, v in ((spec.get("podSelector") or {}).get("matchLabels") or {}).items()
    )
    lines = [f"Applies to pods labelled {selector or 'all pods in the namespace'}."]
    types = spec.get("policyTypes") or []
    if types:
        lines.append("Restricts: " + ", ".join(str(t) for t in types) + ".")
    ingress, egress = len(spec.get("ingress") or []), len(spec.get("egress") or [])
    lines.append(f"{ingress} ingress rule(s), {egress} egress rule(s).")
    return lines


def _render_generic(resource: Resource) -> list[str]:
    keys = sorted(k for k in resource.spec if not isinstance(resource.spec[k], (dict, list)))
    facts = [f"`{k}`: `{resource.spec[k]}`" for k in keys[:12]]
    nested = sorted(k for k in resource.spec if isinstance(resource.spec[k], (dict, list)))
    lines = []
    if facts:
        lines.append("Spec: " + ", ".join(facts) + ".")
    if nested:
        lines.append("Also configures: " + ", ".join(f"`{k}`" for k in nested[:12]) + ".")
    return lines


def render(resource: Resource, refs: References | None = None) -> str:
    """Prose for one resource. Facts first, provenance last."""
    refs = refs or references(resource)
    kind = resource.kind
    head = [f"# {resource.ref}", "", f"Part of package `{resource.package}`."]

    body: list[str]
    if kind in WORKLOAD_KINDS:
        body = _render_workload(resource, refs)
    elif kind == "Service":
        body = _render_service(resource, refs)
    elif kind in ("HTTPRoute", "GRPCRoute", "TLSRoute", "Ingress"):
        body = _render_route(resource, refs)
    elif kind == "Cluster" and "postgresql.cnpg.io" in resource.api_version:
        body = _render_cnpg(resource)
    elif kind == "ExternalSecret":
        body = _render_external_secret(resource, refs)
    elif kind == "Certificate":
        body = _render_certificate(resource, refs)
    elif kind == "ConfigMap":
        body = _render_configmap(resource)
    elif kind == "Secret":
        body = _render_secret(resource)
    elif kind in ("Application", "ApplicationSet"):
        body = _render_argo(resource)
    elif kind in ("ClusterPolicy", "Policy"):
        body = _render_policy(resource)
    elif kind == "NetworkPolicy":
        body = _render_networkpolicy(resource)
    else:
        body = _render_generic(resource)

    tail: list[str] = []
    wave = _wave(resource)
    if wave:
        tail.append(f"ArgoCD sync wave {wave}.")
    part_of = resource.labels.get("app.kubernetes.io/part-of")
    component = resource.labels.get("adhar.io/component")
    if part_of or component:
        bits = []
        if part_of:
            bits.append(f"part of `{part_of}`")
        if component:
            bits.append(f"component `{component}`")
        tail.append("Labelled " + ", ".join(bits) + ".")
    tail.append(f"Defined in `{resource.path}`.")

    return "\n".join(head + [""] + body + [""] + tail).strip() + "\n"


# --------------------------------------------------------------------------- #
# Package overview
# --------------------------------------------------------------------------- #


def render_package_overview(
    package: str, resources: list[Resource], contract: dict[str, Any] | None = None
) -> str:
    """One document per package: everything it deploys, on one page.

    This is the chunk that answers "how is X deployed and what does it need?"
    — the question that used to retrieve cert-manager and llm-d when asked
    about adhar-ai, because no single document described the package as a
    whole.
    """
    by_kind: dict[str, list[Resource]] = {}
    images: list[str] = []
    hostnames: list[str] = []
    secrets: list[str] = []
    databases: list[str] = []
    for r in resources:
        by_kind.setdefault(r.kind, []).append(r)
        refs = references(r)
        images += refs.images
        hostnames += refs.hostnames
        secrets += refs.secrets
        databases += refs.databases
        if r.kind == "Cluster" and "postgresql.cnpg.io" in r.api_version:
            databases.append(r.name)

    lines = [
        f"# Package `{package}`: what it deploys",
        "",
        # Said in the words people use to ask, so the page that answers "how
        # is X deployed and what does it depend on" is found by that question.
        f"How `{package}` is deployed, and what it depends on.",
        "",
    ]
    if contract:
        # The contract is the package's own description of itself: what it is
        # for and what it needs. On the same page as what it deploys, the two
        # halves of "how is X deployed and what does it depend on" meet.
        if description := str(contract.get("description") or "").strip():
            lines += [description, ""]
        deps = [d for d in (contract.get("dependencies") or []) if isinstance(d, dict)]
        if deps:
            named = ", ".join(
                f"`{d.get('category')}/{d.get('name')}`"
                + (" (optional)" if d.get("optional") else "")
                for d in deps
            )
            lines.append(f"Depends on {named}.")
        facts = []
        if contract.get("stability"):
            facts.append(f"stability `{contract['stability']}`")
        if contract.get("version"):
            facts.append(f"version `{contract['version']}`")
        if (contract.get("resources") or {}).get("localSafe") is not None:
            facts.append(f"safe to run locally: {contract['resources']['localSafe']}")
        if facts:
            lines.append("Contract: " + ", ".join(facts) + ".")
        lines.append("")
    lines.append(
        f"{len(resources)} resource(s) across {len(by_kind)} kind(s): "
        + ", ".join(f"{len(v)} {k}" for k, v in sorted(by_kind.items()))
        + "."
    )
    lines.append("")
    for kind in sorted(by_kind):
        names = ", ".join(f"`{r.name}`" for r in by_kind[kind][:20])
        lines.append(f"- **{kind}**: {names}")
    if images := _dedupe(images):
        lines += ["", "## Images", ""] + _bullets([f"`{i}`" for i in images])
    if hostnames := _dedupe(hostnames):
        lines += ["", "## URLs", ""] + _bullets([f"`https://{h}`" for h in hostnames])
    if databases := _dedupe(databases):
        lines += ["", "## Databases", ""] + _bullets([f"CNPG cluster `{d}`" for d in databases])
    if secrets := _dedupe(secrets):
        lines += ["", "## Secrets it reads", ""] + _bullets([f"`{s}`" for s in secrets])
    return "\n".join(lines) + "\n"

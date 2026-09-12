"""mcp-security — Kyverno policy posture from PolicyReports, plus exception PRs.

Reads `wgpolicyk8s.io/v1alpha2` PolicyReports / ClusterPolicyReports and
`kyverno.io` ClusterPolicies through the Kubernetes API (the RBAC ceiling grants
get/list/watch on both groups). The only write tool drafts a policy exception as
a Gitea PR.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from ..clients.kube import get_kube_client
from ..config import MCPConfig
from ..provenance import ORIGIN_LABELS
from .common.pr import open_pr, render_yaml_document
from .common.tools import access_tools

DOMAIN = "security"

WGPOLICY_GROUP, WGPOLICY_VERSION = "wgpolicyk8s.io", "v1alpha2"
KYVERNO_GROUP, KYVERNO_VERSION = "kyverno.io", "v1"


def _results(report: dict[str, Any]) -> list[dict[str, Any]]:
    meta = report.get("metadata") or {}
    out = []
    for item in report.get("results") or []:
        resources = item.get("resources") or [{}]
        out.append(
            {
                "policy": item.get("policy"),
                "rule": item.get("rule"),
                "result": item.get("result"),
                "severity": (item.get("properties") or {}).get("severity")
                or item.get("severity"),
                "message": item.get("message"),
                "category": item.get("category"),
                "report": meta.get("name"),
                "namespace": meta.get("namespace"),
                "resource": "/".join(
                    filter(None, [resources[0].get("kind"), resources[0].get("name")])
                ),
            }
        )
    return out


def _all_reports(namespace: str | None) -> list[dict[str, Any]]:
    kube = get_kube_client()
    reports: list[dict[str, Any]] = []
    reports.extend(
        kube.list_custom(WGPOLICY_GROUP, WGPOLICY_VERSION, "policyreports", namespace)
    )
    if namespace is None:
        reports.extend(
            kube.list_custom(WGPOLICY_GROUP, WGPOLICY_VERSION, "clusterpolicyreports", None)
        )
    return reports


def register(mcp: Any, cfg: MCPConfig) -> None:
    read, write = access_tools(mcp, DOMAIN)

    @read
    async def findings(
        namespace: str | None = None, result: str = "fail", limit: int = 100
    ) -> dict[str, Any]:
        """Kyverno PolicyReport findings.

        result: "fail" (default), "warn", "pass", "error", or "all".
        """
        rows: list[dict[str, Any]] = []
        for report in _all_reports(namespace):
            rows.extend(_results(report))
        if result != "all":
            rows = [r for r in rows if r.get("result") == result]
        by_policy = Counter(str(r.get("policy")) for r in rows)
        return {
            "count": len(rows),
            "by_policy": dict(by_policy.most_common(20)),
            "findings": rows[:limit],
        }

    @read
    async def policy_explain(policy: str) -> dict[str, Any]:
        """Explain one Kyverno ClusterPolicy: its rules, match/exclude blocks,
        failure action, and how many resources currently violate it."""
        kube = get_kube_client()
        obj = kube.get_custom(KYVERNO_GROUP, KYVERNO_VERSION, "clusterpolicies", policy, None)
        spec = obj.get("spec") or {}
        meta = obj.get("metadata") or {}
        violations = [
            r
            for report in _all_reports(None)
            for r in _results(report)
            if r.get("policy") == policy and r.get("result") == "fail"
        ]
        return {
            "policy": policy,
            "title": (meta.get("annotations") or {}).get("policies.kyverno.io/title"),
            "description": (meta.get("annotations") or {}).get(
                "policies.kyverno.io/description"
            ),
            "validation_failure_action": spec.get("validationFailureAction"),
            "background": spec.get("background"),
            "rules": [
                {
                    "name": r.get("name"),
                    "match": r.get("match"),
                    "exclude": r.get("exclude"),
                    "message": (r.get("validate") or {}).get("message"),
                }
                for r in (spec.get("rules") or [])
            ],
            "current_violations": len(violations),
            "sample_violations": violations[:10],
        }

    @read
    async def posture(namespace: str | None = None) -> dict[str, Any]:
        """Aggregate policy posture: pass/fail/warn counts by severity and policy."""
        rows = [r for report in _all_reports(namespace) for r in _results(report)]
        return {
            "scope": namespace or "cluster-wide",
            "total_results": len(rows),
            "by_result": dict(Counter(str(r.get("result")) for r in rows)),
            "by_severity": dict(
                Counter(str(r.get("severity")) for r in rows if r.get("result") == "fail")
            ),
            "failing_policies": dict(
                Counter(
                    str(r.get("policy")) for r in rows if r.get("result") == "fail"
                ).most_common(20)
            ),
        }

    @write
    async def propose_exception(
        policy: str,
        rules: list[str],
        namespace: str,
        match_kinds: list[str],
        match_names: list[str],
        why: str,
        ttl_days: int = 30,
        repo: str = "packages",
        path: str | None = None,
    ) -> dict[str, Any]:
        """Draft a Kyverno PolicyException as a Gitea PR — never applied directly.

        The exception carries an explicit expiry so it cannot silently become
        permanent; a human merges it, and ArgoCD applies it.
        """
        import datetime as _dt

        expires = (
            _dt.datetime.now(_dt.UTC) + _dt.timedelta(days=ttl_days)
        ).strftime("%Y-%m-%d")
        name = f"adhar-ai-{policy}-{namespace}"[:63].strip("-")
        manifest = {
            "apiVersion": "kyverno.io/v2",
            "kind": "PolicyException",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "labels": dict(ORIGIN_LABELS),
                "annotations": {
                    "adhar.io/exception-expires": expires,
                    "adhar.io/exception-rationale": why,
                },
            },
            "spec": {
                "exceptions": [{"policyName": policy, "ruleNames": rules}],
                "match": {
                    "any": [
                        {
                            "resources": {
                                "kinds": match_kinds,
                                "names": match_names,
                                "namespaces": [namespace],
                            }
                        }
                    ]
                },
            },
        }
        target = path or f"security/kyverno-policies/manifests/exceptions/{name}.yaml"
        ref = await open_pr(
            cfg.gitea,
            repo,
            [{"path": target, "content": render_yaml_document(manifest)}],
            title=f"policy exception for {policy} in {namespace} (expires {expires})",
            why=f"{why}\n\nExpires: {expires} ({ttl_days}d TTL).",
            tool="propose_exception",
        )
        return ref.as_dict()

"""Adhar AI MCP tool servers — one module per domain.

Read tools call the real platform APIs (Kubernetes, ArgoCD, Gitea, Prometheus,
Loki, Tempo, Kyverno PolicyReports, OpenCost). Write tools open a Gitea pull
request and nothing else: there is deliberately no `kubectl_apply`, `argo_sync`,
`helm_install`, or cloud-mutation tool anywhere in this package (ADR-0024 §2).
"""

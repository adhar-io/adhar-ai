"""mcp-delivery — the path from a commit to a running revision.

The gap this closes. The other six domains could tell you that a Deployment is
unhealthy (`cluster`), that an Application is OutOfSync (`gitops`) and that a
policy refused an image (`security`) — but not *why the build that was supposed
to produce that image failed*. That is the single most common question a
developer on an internal platform asks, and before this domain existed the
honest answer was "I cannot see your pipeline".

What it reads, all of it already granted to the `adhar-ai` ClusterRole
(get/list/watch, no mutating verb anywhere):

| Thing | API | Package |
|---|---|---|
| Tekton `PipelineRun` / `TaskRun` | `tekton.dev/v1` | `application/tekton` |
| kpack `Image` / `Build` | `kpack.io/v1alpha2` | `application/buildpack` |
| Kargo `Promotion` / `Stage` | `kargo.akuity.io/v1alpha1` | `application/kargo` |
| Preview environments | `argoproj.io/v1alpha1` Applications labelled
  `adhar.io/preview` | `application/adhar-preview-environments` |

**Every one of those packages is optional.** A platform with no Tekton is not a
broken platform, so a missing CRD is reported as `installed: false` with the
package that would provide it — a fact, not a stack trace, and distinguishable
by the model from "installed and nothing is failing". Any other API error is
re-raised: an RBAC refusal or an unreachable API server must not be flattened
into "nothing found".

**Read-only, so this domain is not in `WRITE_DOMAINS`.** Fixing a build means
changing a repository, and that path already exists exactly once, in
`gitops.propose_change`. A second write path here would be a second thing to
audit for the same outcome.

## Why summaries and not objects

A `PipelineRun` is 20–80 KB of YAML; ten of them is more than the model's whole
context budget for grounding. Worse, the answer is almost always four fields
deep: which task failed, which step inside it, its exit code, and the last lines
it printed. So each tool returns the decision-shaped subset and names the pod to
follow up on, and `build_logs` is the one that goes and gets the lines.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from ..clients.kube import get_kube_client
from ..config import MCPConfig
from .common.tools import access_tools

log = logging.getLogger("adhar_ai.mcp.delivery")

DOMAIN = "delivery"

TEKTON = ("tekton.dev", "v1")
KPACK = ("kpack.io", "v1alpha2")
KARGO = ("kargo.akuity.io", "v1alpha1")
ARGO = ("argoproj.io", "v1alpha1")

#: Which package ships the CRD, so an absent one tells you what to enable
#: rather than only that it is missing.
PROVIDERS = {
    "pipelineruns": "application/tekton",
    "taskruns": "application/tekton",
    "images": "application/buildpack (kpack)",
    "builds": "application/buildpack (kpack)",
    "promotions": "application/kargo",
    "stages": "application/kargo",
    "applications": "core/argocd",
}

#: kpack runs the buildpack lifecycle as one container per phase, in this order,
#: and reports their container states positionally in `status.stepStates`. The
#: names are what `build_logs` needs to pick a container out of the build pod.
KPACK_STEPS = ("prepare", "analyze", "detect", "restore", "build", "export")

#: Tekton labels every TaskRun with the PipelineRun that owns it. There is no
#: label selector on the read-only client, so the filter happens here.
PIPELINE_RUN_LABEL = "tekton.dev/pipelineRun"
PIPELINE_TASK_LABEL = "tekton.dev/pipelineTask"


# --------------------------------------------------------------------------- #
# Reading optional APIs honestly
# --------------------------------------------------------------------------- #


def _absent(exc: BaseException) -> bool:
    """True when the API says this kind does not exist on this cluster.

    Duck-typed on the exception rather than importing
    `kubernetes.client.ApiException`: this module must stay importable with the
    fake client and without the Kubernetes extra, and the attribute is stable
    across every client version that sets it.
    """
    status = getattr(exc, "status", None)
    if status in (404, "404"):
        return True
    text = str(exc).lower()
    return "404" in text and ("not found" in text or "could not find" in text)


def _list(plural: str, api: tuple[str, str], namespace: str | None) -> list[dict[str, Any]]:
    """List one optional kind, or raise `_Missing` when the CRD is not installed."""
    group, version = api
    try:
        return get_kube_client().list_custom(group, version, plural, namespace)
    except Exception as exc:  # noqa: BLE001 — classified immediately below
        if _absent(exc):
            raise _Missing(plural, f"{group}/{version}") from exc
        raise


class _Missing(RuntimeError):
    def __init__(self, plural: str, api: str) -> None:
        self.plural, self.api = plural, api
        super().__init__(f"{plural} ({api}) is not installed")

    def as_result(self, **extra: Any) -> dict[str, Any]:
        return {
            "installed": False,
            "api": self.api,
            "note": (
                f"{self.plural} ({self.api}) is not installed on this platform. "
                f"Enable the {PROVIDERS.get(self.plural, 'providing')} package to use this tool."
            ),
            **extra,
        }


# --------------------------------------------------------------------------- #
# Shapes
# --------------------------------------------------------------------------- #


def _meta(obj: dict[str, Any]) -> dict[str, Any]:
    return obj.get("metadata") or {}


def _labels(obj: dict[str, Any]) -> dict[str, Any]:
    return _meta(obj).get("labels") or {}


def _condition(obj: dict[str, Any], kind: str = "Succeeded") -> dict[str, Any]:
    for cond in (obj.get("status") or {}).get("conditions") or []:
        if cond.get("type") == kind:
            return dict(cond)
    return {}


def _outcome(obj: dict[str, Any]) -> str:
    """Tekton and kpack both encode the whole result in one condition's status:
    `True` succeeded, `False` failed, `Unknown` still going. Rendering that as a
    word costs nothing and stops the model inferring it from a boolean."""
    cond = _condition(obj)
    return {
        "True": "Succeeded",
        "False": "Failed",
        "Unknown": "Running",
    }.get(str(cond.get("status")), "Unknown")


def _time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _duration(status: dict[str, Any]) -> float | None:
    """Seconds from start to completion, or to now for a run still going."""
    started = _time(status.get("startTime"))
    if started is None:
        return None
    finished = _time(status.get("completionTime"))
    if finished is None:
        finished = datetime.now(tz=started.tzinfo or UTC)
    return round((finished - started).total_seconds(), 1)


def _failed_step(task_run: dict[str, Any]) -> dict[str, Any]:
    """The step that ended non-zero, with its exit code and reason.

    Tekton reports every step's container state in `status.steps[]`. The first
    non-zero exit is the cause; the ones after it never ran.
    """
    for step in (task_run.get("status") or {}).get("steps") or []:
        terminated = step.get("terminated") or {}
        code = terminated.get("exitCode")
        if code not in (None, 0):
            return {
                "step": step.get("name"),
                "container": step.get("container") or f"step-{step.get('name')}",
                "exit_code": code,
                "reason": terminated.get("reason"),
            }
    return {}


def _task_run_summary(task_run: dict[str, Any]) -> dict[str, Any]:
    status = task_run.get("status") or {}
    cond = _condition(task_run)
    row = {
        "name": _meta(task_run).get("name"),
        "task": (
            _labels(task_run).get(PIPELINE_TASK_LABEL)
            or (status.get("taskSpec") or {}).get("displayName")
        ),
        "outcome": _outcome(task_run),
        "reason": cond.get("reason"),
        "duration_seconds": _duration(status),
        # The pod is what `build_logs` needs; naming it here saves a round trip.
        "pod": status.get("podName"),
    }
    if failed := _failed_step(task_run):
        row["failed_step"] = failed
    if row["outcome"] == "Failed" and cond.get("message"):
        row["message"] = str(cond["message"])[:400]
    return row


def _pipeline_run_summary(run: dict[str, Any]) -> dict[str, Any]:
    status, cond = run.get("status") or {}, _condition(run)
    labels = _labels(run)
    spec_ref = (run.get("spec") or {}).get("pipelineRef") or {}
    return {
        "name": _meta(run).get("name"),
        "namespace": _meta(run).get("namespace"),
        "pipeline": spec_ref.get("name") or (status.get("pipelineSpec") or {}).get("displayName"),
        "outcome": _outcome(run),
        "reason": cond.get("reason"),
        "message": str(cond.get("message") or "")[:300] or None,
        "started": status.get("startTime"),
        "duration_seconds": _duration(status),
        # Tekton's own triggers put the repo and revision in labels; the Adhar
        # supply chain sets them, and they are how a developer recognises
        # their own run among fifty.
        "repo": labels.get("tekton.dev/repo") or labels.get("adhar.io/repository"),
        "revision": labels.get("adhar.io/revision") or labels.get("tekton.dev/revision"),
        "tasks": len(status.get("childReferences") or []),
    }


def _kpack_build_summary(build: dict[str, Any]) -> dict[str, Any]:
    status, cond = build.get("status") or {}, _condition(build)
    done = list(status.get("stepsCompleted") or [])
    row = {
        "name": _meta(build).get("name"),
        "namespace": _meta(build).get("namespace"),
        "outcome": _outcome(build),
        "reason": cond.get("reason"),
        "message": str(cond.get("message") or "")[:300] or None,
        "duration_seconds": _duration(status),
        "steps_completed": done,
        "image": status.get("latestImage"),
        "pod": status.get("podName"),
    }
    # kpack reports container states positionally against the lifecycle phases,
    # so the failing phase is the first non-zero exit, named by position.
    for index, state in enumerate(status.get("stepStates") or []):
        terminated = state.get("terminated") or {}
        code = terminated.get("exitCode")
        if code not in (None, 0):
            phase = done[index] if index < len(done) else (
                KPACK_STEPS[index] if index < len(KPACK_STEPS) else f"step-{index}"
            )
            row["failed_step"] = {
                "step": phase,
                "container": phase,
                "exit_code": code,
                "reason": terminated.get("reason"),
            }
            break
    return row


def _preview_summary(app: dict[str, Any]) -> dict[str, Any]:
    labels, status = _labels(app), app.get("status") or {}
    return {
        "application": _meta(app).get("name"),
        "repo": labels.get("adhar.io/preview-repo"),
        "pull_request": labels.get("adhar.io/preview-pr"),
        "url": (_meta(app).get("annotations") or {}).get("adhar.io/preview-url"),
        "head_sha": (_meta(app).get("annotations") or {}).get("adhar.io/preview-head-sha"),
        "namespace": ((app.get("spec") or {}).get("destination") or {}).get("namespace"),
        "sync": (status.get("sync") or {}).get("status"),
        "health": (status.get("health") or {}).get("status"),
    }


def _newest_first(rows: list[dict[str, Any]], key: str = "started") -> list[dict[str, Any]]:
    return sorted(rows, key=lambda r: str(r.get(key) or ""), reverse=True)


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #


def register(mcp: Any, cfg: MCPConfig) -> None:
    read, _write = access_tools(mcp, DOMAIN)

    @read
    async def pipeline_runs(
        namespace: str | None = None,
        repo: str | None = None,
        only_failed: bool = False,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Tekton PipelineRuns, newest first: outcome, duration, and why a run failed.

        This is the CI view — "did my build run, and did it pass". For the cause
        of a failure, follow up with `pipeline_run` on the name, which names the
        failing task and step, then `build_logs` for its output.

        namespace: restrict to one namespace (omit for cluster-wide).
        repo: substring-match the run name and the repo label, so a developer
            can find their own service's runs without knowing the generated
            suffix Tekton appends.
        only_failed: return just the runs that did not succeed.
        limit: most runs to return; the counts always cover everything found.
        """
        try:
            raw = _list("pipelineruns", TEKTON, namespace)
        except _Missing as missing:
            return missing.as_result(count=0, runs=[])
        rows = [_pipeline_run_summary(r) for r in raw]
        if repo:
            needle = repo.lower()
            rows = [
                r
                for r in rows
                if needle in str(r.get("name") or "").lower()
                or needle in str(r.get("repo") or "").lower()
            ]
        counts = {
            "total": len(rows),
            "failed": sum(1 for r in rows if r["outcome"] == "Failed"),
            "running": sum(1 for r in rows if r["outcome"] == "Running"),
            "succeeded": sum(1 for r in rows if r["outcome"] == "Succeeded"),
        }
        if only_failed:
            rows = [r for r in rows if r["outcome"] == "Failed"]
        return {"installed": True, "count": counts["total"], "counts": counts,
                "runs": _newest_first(rows)[: max(1, limit)]}

    @read
    async def pipeline_run(name: str, namespace: str) -> dict[str, Any]:
        """One PipelineRun in depth: every task, and for a failure the exact step.

        Resolves the run's TaskRuns (Tekton labels them with the owning run) and
        reports each one's outcome, duration and pod. When a step exited
        non-zero it is called out as `cause` with its exit code and the pod and
        container to read logs from — which is what `build_logs` takes.

        name: the PipelineRun's name, as `pipeline_runs` reports it.
        namespace: the namespace it ran in.
        """
        try:
            runs = _list("pipelineruns", TEKTON, namespace)
        except _Missing as missing:
            return missing.as_result(name=name, namespace=namespace)
        match = next((r for r in runs if _meta(r).get("name") == name), None)
        if match is None:
            return {
                "installed": True,
                "found": False,
                "note": f"no PipelineRun named {name!r} in {namespace}",
            }
        summary = _pipeline_run_summary(match)
        try:
            task_runs = _list("taskruns", TEKTON, namespace)
        except _Missing:
            task_runs = []
        owned = [t for t in task_runs if _labels(t).get(PIPELINE_RUN_LABEL) == name]
        tasks = [_task_run_summary(t) for t in owned]
        result = {"installed": True, "found": True, **summary, "task_runs": tasks}
        # The cause, lifted to the top level. A model that has to find the one
        # failing entry in a list of twelve sometimes reports the wrong one.
        cause = next((t for t in tasks if t.get("failed_step")), None)
        if cause:
            result["cause"] = {
                "task": cause.get("task") or cause.get("name"),
                "task_run": cause.get("name"),
                "pod": cause.get("pod"),
                **cause["failed_step"],
                "logs_with": "build_logs(namespace, name=<task_run>, step=<step>)",
            }
        return result

    @read
    async def builds(
        namespace: str | None = None,
        image: str | None = None,
        only_failed: bool = False,
        limit: int = 20,
    ) -> dict[str, Any]:
        """kpack image builds: what the buildpack lifecycle produced, or where it stopped.

        The golden paths build with kpack rather than a Dockerfile, so "my image
        did not update" is answered here: each `Image` resource's latest build,
        the phase it reached (`prepare`, `detect`, `build`, `export`, …), the
        digest it pushed, and the failing phase when there is one.

        namespace: restrict to one namespace (omit for cluster-wide).
        image: substring-match the kpack Image (usually the service name).
        only_failed: return just the builds that did not succeed.
        limit: most builds to return.
        """
        try:
            raw = _list("builds", KPACK, namespace)
        except _Missing as missing:
            return missing.as_result(count=0, builds=[])
        rows = [_kpack_build_summary(b) for b in raw]
        # The kpack Image resource carries the desired state and the build
        # counter; a build row without it cannot say "this is the latest".
        latest: dict[str, Any] = {}
        try:
            for img in _list("images", KPACK, namespace):
                latest[str(_meta(img).get("name"))] = {
                    "latest_image": (img.get("status") or {}).get("latestImage"),
                    "latest_build": (img.get("status") or {}).get("latestBuildRef"),
                    "build_reason": (img.get("status") or {}).get("latestBuildReason"),
                    "ready": _outcome(img) == "Succeeded",
                }
        except _Missing:
            latest = {}
        for row in rows:
            owner = str(row.get("name") or "")
            for name, state in latest.items():
                # kpack names builds `<image>-build-<n>-<suffix>`.
                if owner.startswith(f"{name}-build-"):
                    row["image_resource"] = name
                    row["is_latest"] = state.get("latest_build") == owner
                    row["build_reason"] = state.get("build_reason")
                    break
        if image:
            needle = image.lower()
            rows = [
                r
                for r in rows
                if needle in str(r.get("name") or "").lower()
                or needle in str(r.get("image_resource") or "").lower()
            ]
        counts = {
            "total": len(rows),
            "failed": sum(1 for r in rows if r["outcome"] == "Failed"),
            "building": sum(1 for r in rows if r["outcome"] == "Running"),
        }
        if only_failed:
            rows = [r for r in rows if r["outcome"] == "Failed"]
        return {
            "installed": True,
            "count": counts["total"],
            "counts": counts,
            "images": latest,
            "builds": _newest_first(rows, "name")[: max(1, limit)],
        }

    @read
    async def build_logs(
        namespace: str,
        name: str,
        step: str | None = None,
        tail_lines: int = 120,
    ) -> dict[str, Any]:
        """The output of a build step — the lines that say what actually went wrong.

        `name` is a TaskRun, a kpack Build, or a pod. Each of the first two
        records the pod it ran in, and each step of a build is a container in
        that pod, so this resolves name → pod → container and tails it. With no
        `step`, the FAILING step is chosen when there is one, which is the step
        a reader wants in every case where they are reading logs at all.

        namespace: the namespace the build ran in.
        name: TaskRun name, kpack Build name, or a pod name.
        step: a specific step/phase (Tekton step name, or a kpack phase such as
            `build` or `export`).
        tail_lines: lines from the end of the container's output.
        """
        kube = get_kube_client()
        resolved: dict[str, Any] = {"via": "pod", "pod": name, "container": step}

        def _resolve(objects: list[dict[str, Any]], kind: str) -> dict[str, Any] | None:
            obj = next((o for o in objects if _meta(o).get("name") == name), None)
            if obj is None:
                return None
            status = obj.get("status") or {}
            failed = (
                _failed_step(obj)
                if kind == "taskrun"
                else (_kpack_build_summary(obj).get("failed_step") or {})
            )
            container = step or failed.get("container")
            if kind == "taskrun" and step:
                # Tekton prefixes every step container with `step-`; accept the
                # bare step name a human would type.
                container = step if step.startswith("step-") else f"step-{step}"
            return {
                "via": kind,
                "pod": status.get("podName"),
                "container": container,
                "outcome": _outcome(obj),
                **({"failed_step": failed} if failed else {}),
            }

        for plural, api, kind in (
            ("taskruns", TEKTON, "taskrun"),
            ("builds", KPACK, "build"),
        ):
            try:
                found = _resolve(_list(plural, api, namespace), kind)
            except _Missing:
                continue
            if found:
                resolved = found
                break

        if not resolved.get("pod"):
            return {
                "found": False,
                "note": (
                    f"{name!r} in {namespace} is not a TaskRun, a kpack Build or a pod "
                    "with a recorded pod name; nothing to read logs from"
                ),
                **resolved,
            }
        text = kube.pod_logs(
            namespace, str(resolved["pod"]), resolved.get("container"), max(1, tail_lines)
        )
        return {
            "found": True,
            "namespace": namespace,
            "source": name,
            **resolved,
            "lines": text.splitlines(),
        }

    @read
    async def promotions(
        namespace: str | None = None, stage: str | None = None, limit: int = 20
    ) -> dict[str, Any]:
        """Kargo promotions: what is moving to which environment, and whether it landed.

        `gitops.sync_status` says what a cluster is running; this says what was
        being promoted into it and by whom. A stuck promotion and an OutOfSync
        Application are different problems with different fixes.

        namespace: the Kargo project namespace (omit for all).
        stage: restrict to one stage (`dev`, `test`, `prod`, …).
        limit: most promotions to return.
        """
        try:
            raw = _list("promotions", KARGO, namespace)
        except _Missing as missing:
            return missing.as_result(count=0, promotions=[])
        rows = []
        for promo in raw:
            spec, status = promo.get("spec") or {}, promo.get("status") or {}
            rows.append(
                {
                    "name": _meta(promo).get("name"),
                    "namespace": _meta(promo).get("namespace"),
                    "stage": spec.get("stage"),
                    "freight": spec.get("freight"),
                    "phase": status.get("phase"),
                    "message": str(status.get("message") or "")[:300] or None,
                    "started": status.get("startedAt") or _meta(promo).get("creationTimestamp"),
                }
            )
        if stage:
            rows = [r for r in rows if r.get("stage") == stage]
        counts = {
            "total": len(rows),
            "failed": sum(1 for r in rows if str(r.get("phase")) in {"Failed", "Errored"}),
            "running": sum(1 for r in rows if str(r.get("phase")) in {"Running", "Pending"}),
        }
        return {
            "installed": True,
            "count": counts["total"],
            "counts": counts,
            "promotions": _newest_first(rows)[: max(1, limit)],
        }

    @read
    async def previews(repo: str | None = None) -> dict[str, Any]:
        """Preview environments: where an open pull request is deployed right now.

        Every preview is an ArgoCD Application labelled `adhar.io/preview=true`
        by the preview ApplicationSet (ADR-0017), carrying its repo, PR number
        and URL. This answers "is my PR up yet, and what is its address" without
        the developer knowing the naming convention.

        repo: restrict to one repository.
        """
        try:
            apps = _list("applications", ARGO, "adhar-system")
        except _Missing as missing:
            return missing.as_result(count=0, previews=[])
        rows = [
            _preview_summary(a)
            for a in apps
            if str(_labels(a).get("adhar.io/preview")).lower() == "true"
        ]
        if repo:
            rows = [r for r in rows if r.get("repo") == repo]
        return {
            "installed": True,
            "count": len(rows),
            "unhealthy": [r for r in rows if r.get("health") not in {"Healthy", None}],
            "previews": sorted(
                rows, key=lambda r: (str(r.get("repo")), str(r.get("pull_request")))
            ),
        }

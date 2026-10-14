"""mcp-delivery: the commit-to-revision path.

Fixtures are shaped like the real objects — `tekton.dev/v1` PipelineRuns and
TaskRuns, `kpack.io/v1alpha2` Builds and Images, `kargo.akuity.io/v1alpha1`
Promotions, and the preview Applications the ADR-0017 ApplicationSet writes —
because the value of this domain is entirely in reading those shapes correctly.
"""

from __future__ import annotations

import pytest

from adhar_ai.mcp.delivery import TEKTON, _list, _Missing

from .conftest import FakeApiError, call, server_for

pytestmark = pytest.mark.usefixtures("fake_kube")


# --------------------------------------------------------------------------- #
# Fixtures, shaped like the real CRs
# --------------------------------------------------------------------------- #

PIPELINE_RUN = {
    "metadata": {
        "name": "checkout-build-xk2p9",
        "namespace": "team-web",
        "labels": {"adhar.io/repository": "checkout", "adhar.io/revision": "abc1234"},
    },
    "spec": {"pipelineRef": {"name": "build-and-push"}},
    "status": {
        "conditions": [
            {
                "type": "Succeeded",
                "status": "False",
                "reason": "Failed",
                "message": "Tasks Completed: 2 (Failed: 1, Cancelled 0), Skipped: 1",
            }
        ],
        "startTime": "2026-10-14T09:00:00Z",
        "completionTime": "2026-10-14T09:04:30Z",
        "childReferences": [
            {"kind": "TaskRun", "name": "checkout-build-xk2p9-fetch", "pipelineTaskName": "fetch"},
            {
                "kind": "TaskRun",
                "name": "checkout-build-xk2p9-compile",
                "pipelineTaskName": "compile",
            },
        ],
    },
}

PIPELINE_RUN_OK = {
    "metadata": {"name": "billing-build-aa11", "namespace": "team-fin", "labels": {}},
    "spec": {"pipelineRef": {"name": "build-and-push"}},
    "status": {
        "conditions": [{"type": "Succeeded", "status": "True", "reason": "Succeeded"}],
        "startTime": "2026-10-14T08:00:00Z",
        "completionTime": "2026-10-14T08:02:00Z",
        "childReferences": [],
    },
}

TASK_RUN_OK = {
    "metadata": {
        "name": "checkout-build-xk2p9-fetch",
        "namespace": "team-web",
        "labels": {
            "tekton.dev/pipelineRun": "checkout-build-xk2p9",
            "tekton.dev/pipelineTask": "fetch",
        },
    },
    "status": {
        "conditions": [{"type": "Succeeded", "status": "True", "reason": "Succeeded"}],
        "podName": "checkout-build-xk2p9-fetch-pod",
        "startTime": "2026-10-14T09:00:05Z",
        "completionTime": "2026-10-14T09:00:25Z",
        "steps": [{"name": "clone", "container": "step-clone", "terminated": {"exitCode": 0}}],
    },
}

TASK_RUN_FAILED = {
    "metadata": {
        "name": "checkout-build-xk2p9-compile",
        "namespace": "team-web",
        "labels": {
            "tekton.dev/pipelineRun": "checkout-build-xk2p9",
            "tekton.dev/pipelineTask": "compile",
        },
    },
    "status": {
        "conditions": [
            {
                "type": "Succeeded",
                "status": "False",
                "reason": "Failed",
                "message": 'step "step-go-build" exited with code 2',
            }
        ],
        "podName": "checkout-build-xk2p9-compile-pod",
        "startTime": "2026-10-14T09:00:30Z",
        "completionTime": "2026-10-14T09:04:20Z",
        "steps": [
            {"name": "deps", "container": "step-deps", "terminated": {"exitCode": 0}},
            {
                "name": "go-build",
                "container": "step-go-build",
                "terminated": {"exitCode": 2, "reason": "Error"},
            },
        ],
    },
}

KPACK_BUILD = {
    "metadata": {"name": "checkout-build-4-k9vd2", "namespace": "team-web"},
    "status": {
        "conditions": [{"type": "Succeeded", "status": "False", "reason": "BuildFailed"}],
        "podName": "checkout-build-4-k9vd2-build-pod",
        "startTime": "2026-10-14T09:10:00Z",
        "completionTime": "2026-10-14T09:13:00Z",
        "stepsCompleted": ["prepare", "analyze", "detect", "restore", "build"],
        "stepStates": [
            {"terminated": {"exitCode": 0}},
            {"terminated": {"exitCode": 0}},
            {"terminated": {"exitCode": 0}},
            {"terminated": {"exitCode": 0}},
            {"terminated": {"exitCode": 1, "reason": "Error"}},
        ],
    },
}

KPACK_IMAGE = {
    "metadata": {"name": "checkout", "namespace": "team-web"},
    "status": {
        "conditions": [{"type": "Ready", "status": "False"}],
        "latestImage": "harbor.adhar.localtest.me/team-web/checkout@sha256:dead",
        "latestBuildRef": "checkout-build-4-k9vd2",
        "latestBuildReason": "COMMIT",
    },
}

PROMOTION = {
    "metadata": {"name": "checkout-prod-01", "namespace": "kargo-checkout"},
    "spec": {"stage": "prod", "freight": "f-abc123"},
    "status": {
        "phase": "Failed",
        "message": "health check failed",
        "startedAt": "2026-10-14T10:00:00Z",
    },
}

PREVIEW_APP = {
    "metadata": {
        "name": "checkout-pr-42",
        "namespace": "adhar-system",
        "labels": {
            "adhar.io/preview": "true",
            "adhar.io/preview-repo": "checkout",
            "adhar.io/preview-pr": "42",
        },
        "annotations": {
            "adhar.io/preview-url": "https://checkout-pr-42.adhar.localtest.me:8443",
            "adhar.io/preview-head-sha": "abc1234",
        },
    },
    "spec": {"destination": {"namespace": "preview-checkout-42"}},
    "status": {"sync": {"status": "Synced"}, "health": {"status": "Degraded"}},
}

NON_PREVIEW_APP = {
    "metadata": {"name": "vault", "namespace": "adhar-system", "labels": {}},
    "spec": {"destination": {"namespace": "adhar-system"}},
    "status": {"sync": {"status": "Synced"}, "health": {"status": "Healthy"}},
}


@pytest.fixture
def delivery(mcp_cfg, fake_kube):
    fake_kube.custom["pipelineruns"] = [PIPELINE_RUN, PIPELINE_RUN_OK]
    fake_kube.custom["taskruns"] = [TASK_RUN_OK, TASK_RUN_FAILED]
    fake_kube.custom["builds"] = [KPACK_BUILD]
    fake_kube.custom["images"] = [KPACK_IMAGE]
    fake_kube.custom["promotions"] = [PROMOTION]
    fake_kube.custom["applications"] = [PREVIEW_APP, NON_PREVIEW_APP]
    return server_for("delivery", mcp_cfg)


# --------------------------------------------------------------------------- #
# Pipelines
# --------------------------------------------------------------------------- #


async def test_pipeline_runs_summarise_outcome_and_duration(delivery):
    result = await call(delivery, "pipeline_runs", {})
    assert result["installed"] is True
    assert result["counts"] == {"total": 2, "failed": 1, "running": 0, "succeeded": 1}
    newest = result["runs"][0]
    assert newest["name"] == "checkout-build-xk2p9"
    assert newest["outcome"] == "Failed"
    assert newest["pipeline"] == "build-and-push"
    assert newest["duration_seconds"] == 270.0
    assert newest["repo"] == "checkout"


async def test_pipeline_runs_filters_by_repo_and_failure(delivery):
    mine = await call(delivery, "pipeline_runs", {"repo": "billing"})
    assert [r["name"] for r in mine["runs"]] == ["billing-build-aa11"]

    failed = await call(delivery, "pipeline_runs", {"only_failed": True})
    assert [r["name"] for r in failed["runs"]] == ["checkout-build-xk2p9"]
    # The counts still describe everything, so "1 of 2 failed" survives the filter.
    assert failed["counts"]["total"] == 2


async def test_pipeline_run_lifts_the_failing_step_to_the_top(delivery):
    """A model given twelve task rows sometimes reports the wrong one as the
    cause. The cause is therefore named once, with what it needs to read logs."""
    result = await call(
        delivery, "pipeline_run", {"name": "checkout-build-xk2p9", "namespace": "team-web"}
    )
    assert result["found"] is True
    assert [t["task"] for t in result["task_runs"]] == ["fetch", "compile"]

    cause = result["cause"]
    assert cause["task"] == "compile"
    assert cause["step"] == "go-build"
    assert cause["exit_code"] == 2
    assert cause["container"] == "step-go-build"
    assert cause["pod"] == "checkout-build-xk2p9-compile-pod"


async def test_a_pipeline_run_that_does_not_exist_says_so(delivery):
    result = await call(delivery, "pipeline_run", {"name": "nope", "namespace": "team-web"})
    assert result["found"] is False
    assert "nope" in result["note"]


# --------------------------------------------------------------------------- #
# Optional packages: absent is a fact, forbidden is an error
# --------------------------------------------------------------------------- #


async def test_a_missing_crd_names_the_package_instead_of_raising(mcp_cfg, fake_kube):
    """A platform without Tekton is not a broken platform. The model must be
    able to tell "no CI installed" from "CI installed and nothing failed"."""
    fake_kube.absent = {"pipelineruns", "taskruns", "builds", "images", "promotions"}
    server = server_for("delivery", mcp_cfg)

    runs = await call(server, "pipeline_runs", {})
    assert runs["installed"] is False
    assert runs["count"] == 0
    assert "application/tekton" in runs["note"]

    builds = await call(server, "builds", {})
    assert builds["installed"] is False
    assert "kpack" in builds["note"]

    promos = await call(server, "promotions", {})
    assert promos["installed"] is False
    assert "application/kargo" in promos["note"]


async def test_an_rbac_refusal_is_not_reported_as_not_installed(mcp_cfg, fake_kube):
    """403 means the agent cannot see it; flattening that into "nothing found"
    would make a permissions problem look like a healthy cluster.

    Asserted on `_list`, which is where the classification lives, and then
    through the server — where the SDK wraps any crash, so what matters is that
    the answer is an error at all rather than a tidy "not installed"."""
    fake_kube.forbidden = {"pipelineruns"}
    with pytest.raises(FakeApiError):
        _list("pipelineruns", TEKTON, None)

    fake_kube.forbidden = set()
    fake_kube.absent = {"pipelineruns"}
    with pytest.raises(_Missing):
        _list("pipelineruns", TEKTON, None)

    fake_kube.absent = set()
    fake_kube.forbidden = {"pipelineruns"}
    server = server_for("delivery", mcp_cfg)
    with pytest.raises(Exception) as raised:  # noqa: B017 — the SDK wraps it
        await call(server, "pipeline_runs", {})
    assert "not installed" not in str(raised.value).lower()


# --------------------------------------------------------------------------- #
# kpack
# --------------------------------------------------------------------------- #


async def test_builds_name_the_failing_lifecycle_phase(delivery):
    result = await call(delivery, "builds", {})
    assert result["counts"] == {"total": 1, "failed": 1, "building": 0}
    build = result["builds"][0]
    assert build["outcome"] == "Failed"
    # Five phases completed, and the fifth is the one that exited non-zero.
    assert build["failed_step"]["step"] == "build"
    assert build["failed_step"]["exit_code"] == 1
    # Tied back to its Image resource, so "is this the latest build" is answerable.
    assert build["image_resource"] == "checkout"
    assert build["is_latest"] is True
    assert build["build_reason"] == "COMMIT"


async def test_builds_filter_by_image_name(delivery):
    assert (await call(delivery, "builds", {"image": "checkout"}))["count"] == 1
    assert (await call(delivery, "builds", {"image": "billing"}))["count"] == 0


# --------------------------------------------------------------------------- #
# Logs — the tool the whole domain exists for
# --------------------------------------------------------------------------- #


async def test_build_logs_resolve_the_failing_container_without_being_told(delivery):
    result = await call(
        delivery, "build_logs", {"namespace": "team-web", "name": "checkout-build-xk2p9-compile"}
    )
    assert result["found"] is True
    assert result["via"] == "taskrun"
    assert result["pod"] == "checkout-build-xk2p9-compile-pod"
    assert result["container"] == "step-go-build"
    assert result["lines"][-1] == "panic: boom"


async def test_build_logs_accept_a_bare_step_name(delivery):
    """Tekton containers are `step-<name>`; a human types `deps`."""
    result = await call(
        delivery,
        "build_logs",
        {"namespace": "team-web", "name": "checkout-build-xk2p9-compile", "step": "deps"},
    )
    assert result["container"] == "step-deps"


async def test_build_logs_resolve_a_kpack_build_by_phase(delivery):
    result = await call(
        delivery, "build_logs", {"namespace": "team-web", "name": "checkout-build-4-k9vd2"}
    )
    assert result["via"] == "build"
    assert result["pod"] == "checkout-build-4-k9vd2-build-pod"
    assert result["container"] == "build"


async def test_build_logs_fall_back_to_treating_the_name_as_a_pod(delivery):
    """Documented behaviour: the name may be a TaskRun, a Build or a pod. A pod
    that does not exist is the real client's 404, not a guess made here."""
    result = await call(delivery, "build_logs", {"namespace": "team-web", "name": "some-pod"})
    assert result["via"] == "pod"
    assert result["pod"] == "some-pod"


async def test_build_logs_say_so_when_there_is_no_pod_to_read(delivery, fake_kube):
    """A TaskRun that never scheduled a pod has no logs. Saying that beats
    tailing something else and presenting it as the build's output."""
    fake_kube.custom["taskruns"] = [
        {
            "metadata": {"name": "never-ran", "namespace": "team-web", "labels": {}},
            "status": {"conditions": [{"type": "Succeeded", "status": "Unknown"}]},
        }
    ]
    result = await call(delivery, "build_logs", {"namespace": "team-web", "name": "never-ran"})
    assert result["found"] is False
    assert "never-ran" in result["note"]


# --------------------------------------------------------------------------- #
# Promotions and previews
# --------------------------------------------------------------------------- #


async def test_promotions_report_phase_and_stage(delivery):
    result = await call(delivery, "promotions", {})
    assert result["counts"]["failed"] == 1
    promo = result["promotions"][0]
    assert promo["stage"] == "prod"
    assert promo["freight"] == "f-abc123"
    assert promo["phase"] == "Failed"

    assert (await call(delivery, "promotions", {"stage": "dev"}))["count"] == 0


async def test_previews_answer_where_my_pull_request_is_deployed(delivery):
    result = await call(delivery, "previews", {})
    assert result["count"] == 1, "only Applications labelled adhar.io/preview count"
    preview = result["previews"][0]
    assert preview["repo"] == "checkout"
    assert preview["pull_request"] == "42"
    assert preview["url"] == "https://checkout-pr-42.adhar.localtest.me:8443"
    assert preview["namespace"] == "preview-checkout-42"
    assert preview["head_sha"] == "abc1234"
    # Synced but Degraded is the state worth surfacing: it deployed and is broken.
    assert result["unhealthy"] == [preview]


async def test_previews_filter_by_repo(delivery):
    assert (await call(delivery, "previews", {"repo": "checkout"}))["count"] == 1
    assert (await call(delivery, "previews", {"repo": "billing"}))["count"] == 0

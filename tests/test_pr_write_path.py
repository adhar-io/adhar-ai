"""The PR write path, against a mocked Gitea REST API."""

from __future__ import annotations

import base64
import json

import httpx
import pytest
import respx

from adhar_ai.clients.errors import WriteNotPermitted
from adhar_ai.mcp.common.policy import guard_write
from adhar_ai.mcp.common.pr import open_pr
from adhar_ai.provenance import BRANCH_PREFIX, ORIGIN_LABEL_KEY, PR_LABEL, PR_TITLE_PREFIX

from .conftest import GITEA_URL, call, server_for

API = f"{GITEA_URL}/api/v1"


def _mock_gitea(mock, pr_number: int = 42) -> dict:
    captured: dict = {"files": [], "pr": None, "branch": None}

    def branch(request):
        captured["branch"] = json.loads(request.content)
        return httpx.Response(201, json={"name": captured["branch"]["new_branch_name"]})

    def put_file(request):
        body = json.loads(request.content)
        captured["files"].append(
            {
                "path": request.url.path.split("/contents/", 1)[1],
                "content": base64.b64decode(body["content"]).decode(),
                "branch": body["branch"],
                "message": body["message"],
            }
        )
        return httpx.Response(201, json={"content": {"sha": "deadbeef"}})

    def pull(request):
        captured["pr"] = json.loads(request.content)
        return httpx.Response(
            201,
            json={
                "number": pr_number,
                "html_url": f"https://gitea.adhar.localtest.me:8443/adhar/packages/pulls/{pr_number}",
            },
        )

    mock.post(f"{API}/repos/adhar/packages/branches").mock(side_effect=branch)
    mock.get(url__regex=rf"{API}/repos/adhar/packages/contents/.*").mock(
        return_value=httpx.Response(404, json={"message": "not found"})
    )
    mock.post(url__regex=rf"{API}/repos/adhar/packages/contents/.*").mock(side_effect=put_file)
    mock.post(f"{API}/repos/adhar/packages/pulls").mock(side_effect=pull)
    mock.get(f"{API}/repos/adhar/packages/labels").mock(return_value=httpx.Response(200, json=[]))
    mock.post(f"{API}/repos/adhar/packages/labels").mock(
        return_value=httpx.Response(201, json={"id": 9, "name": PR_LABEL})
    )
    mock.post(url__regex=rf"{API}/repos/adhar/packages/issues/\d+/labels").mock(
        return_value=httpx.Response(200, json=[])
    )
    return captured


@respx.mock
async def test_open_pr_creates_branch_commit_and_pr(gitea_cfg):
    captured = _mock_gitea(respx)

    ref = await open_pr(
        gitea_cfg,
        "packages",
        [{"path": "application/demo/manifests/app.yaml", "content": "kind: Deployment\n"}],
        title="raise demo memory limit",
        why="OOMKilled 12 times in the last hour (kube_pod_container_status_restarts_total).",
        tool="propose_change",
        model="claude-sonnet-5",
        user="alice",
    )

    assert ref.number == 42
    assert ref.branch.startswith(BRANCH_PREFIX)
    assert ref.files == ["application/demo/manifests/app.yaml"]

    assert captured["branch"]["old_branch_name"] == "main"
    assert captured["files"][0]["content"] == "kind: Deployment\n"
    assert "Proposed-by: adhar-ai (model=claude-sonnet-5)" in captured["files"][0]["message"]
    assert "Requested-by: alice" in captured["files"][0]["message"]

    pr = captured["pr"]
    assert pr["title"].startswith(PR_TITLE_PREFIX)
    assert pr["base"] == "main"
    assert pr["head"].startswith(BRANCH_PREFIX)
    assert ORIGIN_LABEL_KEY in pr["body"]
    assert "OOMKilled 12 times" in pr["body"]
    assert "kubectl apply" in pr["body"]


@respx.mock
async def test_propose_change_tool_opens_a_pr(mcp_cfg):
    captured = _mock_gitea(respx, pr_number=7)
    server = server_for("gitops", mcp_cfg)

    out = await call(
        server,
        "propose_change",
        {
            "repo": "packages",
            "changes": [{"path": "application/demo/values.yaml", "content": "replicas: 2\n"}],
            "title": "scale demo to 2",
            "why": "single replica has no availability headroom",
        },
    )
    assert out["number"] == 7
    assert out["url"].endswith("/pulls/7")
    assert captured["files"][0]["path"] == "application/demo/values.yaml"


@respx.mock
async def test_existing_file_is_updated_with_its_sha(gitea_cfg):
    """A path that already exists must PUT with the blob SHA, not POST."""
    seen = {"method": None, "sha": None}
    respx.post(f"{API}/repos/adhar/packages/branches").mock(
        return_value=httpx.Response(201, json={"name": "b"})
    )
    respx.get(url__regex=rf"{API}/repos/adhar/packages/contents/.*").mock(
        return_value=httpx.Response(
            200, json={"sha": "cafe1234", "content": "", "encoding": "base64"}
        )
    )

    def put_file(request):
        seen["method"] = request.method
        seen["sha"] = json.loads(request.content).get("sha")
        return httpx.Response(200, json={"content": {"sha": "new"}})

    respx.put(url__regex=rf"{API}/repos/adhar/packages/contents/.*").mock(
        side_effect=put_file
    )
    respx.post(f"{API}/repos/adhar/packages/pulls").mock(
        return_value=httpx.Response(201, json={"number": 1, "html_url": "u"})
    )
    respx.get(f"{API}/repos/adhar/packages/labels").mock(return_value=httpx.Response(200, json=[]))
    respx.post(f"{API}/repos/adhar/packages/labels").mock(
        return_value=httpx.Response(201, json={"id": 1, "name": PR_LABEL})
    )
    respx.post(url__regex=rf"{API}/repos/adhar/packages/issues/\d+/labels").mock(
        return_value=httpx.Response(200, json=[])
    )

    await open_pr(
        gitea_cfg,
        "packages",
        [{"path": "application/demo/values.yaml", "content": "x: 1\n"}],
        title="t",
        why="w",
        tool="propose_change",
    )
    assert seen["method"] == "PUT"
    assert seen["sha"] == "cafe1234"


async def test_read_only_server_refuses_writes(readonly_gitea_cfg):
    with pytest.raises(WriteNotPermitted, match="read-only"):
        await open_pr(
            readonly_gitea_cfg,
            "packages",
            [{"path": "packages/x.yaml", "content": "a"}],
            title="t",
            why="w",
            tool="propose_change",
        )


async def test_missing_bot_token_refuses_writes(gitea_cfg):
    gitea_cfg.bot_token = ""
    with pytest.raises(WriteNotPermitted, match="bot token"):
        await open_pr(
            gitea_cfg,
            "packages",
            [{"path": "packages/x.yaml", "content": "a"}],
            title="t",
            why="w",
            tool="propose_change",
        )


@pytest.mark.parametrize(
    "repo,path,match",
    [
        ("secrets", "security/x.yaml", "not in the allowed set"),
        ("argocd", "x.yaml", "not in the allowed set"),
        ("packages", "../../etc/passwd", "illegal file path"),
        ("packages", "..", "illegal file path"),
        ("packages", "", "illegal file path"),
    ],
)
async def test_policy_rejects_out_of_scope_writes(gitea_cfg, repo, path, match):
    with pytest.raises(WriteNotPermitted, match=match):
        await open_pr(
            gitea_cfg, repo, [{"path": path, "content": "x"}], title="t", why="w", tool="t"
        )


def test_guard_write_enforces_path_prefixes(gitea_cfg):
    """Narrowing writePolicy.allowedPathPrefixes must actually bite."""
    narrow = ("packages/security/",)
    assert guard_write(
        gitea_cfg, "packages", ["security/kyverno-policies/x.yaml"], prefixes=narrow
    ) == ["security/kyverno-policies/x.yaml"]
    with pytest.raises(WriteNotPermitted, match="outside the allowed prefixes"):
        guard_write(gitea_cfg, "packages", ["data/minio/x.yaml"], prefixes=narrow)


def test_guard_write_requires_at_least_one_file(gitea_cfg):
    with pytest.raises(WriteNotPermitted, match="at least one file"):
        guard_write(gitea_cfg, "packages", [])


async def test_no_network_call_when_policy_denies(gitea_cfg):
    """A denied write must fail before any HTTP request is issued."""
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(url__regex=".*").mock(return_value=httpx.Response(201, json={}))
        with pytest.raises(WriteNotPermitted):
            await open_pr(
                gitea_cfg, "forbidden-repo", [{"path": "a.yaml", "content": "x"}], "t", "w", "t"
            )
        assert route.call_count == 0

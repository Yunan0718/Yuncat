"""routes/vcs.py —— GitHub / GitLab PR(MR) 统一路由"""
import logging

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api/v1", tags=["vcs"])
_log = logging.getLogger("yuncat.vcs")


REQUIRED_CREATE_FIELDS = {"provider", "owner", "repo", "title", "head", "base"}


@router.get("/vcs/providers")
async def vcs_providers(request: Request):
    return await request.app.state.vcs_service.get_providers()


@router.get("/vcs/prs")
async def vcs_list_prs(
    provider: str,
    owner: str,
    repo: str,
    state: str = "open",
    request: Request = None,
):
    if not provider or not owner or not repo:
        raise HTTPException(400, "Missing 'provider', 'owner' or 'repo'")
    client = await request.app.state.vcs_service.get_client(provider)
    if not client:
        raise HTTPException(400, f"VCS provider '{provider}' not configured")
    return await client.list_pull_requests(owner, repo, state)


@router.post("/vcs/prs")
async def vcs_create_pr(payload: dict, request: Request):
    missing = REQUIRED_CREATE_FIELDS - set(payload.keys())
    if missing:
        raise HTTPException(400, f"Missing fields: {', '.join(sorted(missing))}")

    provider = payload["provider"]
    client = await request.app.state.vcs_service.get_client(provider)
    if not client:
        raise HTTPException(400, f"VCS provider '{provider}' not configured")

    return await client.create_pull_request(
        owner=payload["owner"],
        repo=payload["repo"],
        title=payload["title"],
        body=payload.get("body", ""),
        head=payload["head"],
        base=payload["base"],
        draft=payload.get("draft", False),
        reviewers=payload.get("reviewers") or [],
    )


@router.post("/vcs/prs/{pr_id}/comment")
async def vcs_add_comment(pr_id: str, payload: dict, request: Request):
    provider = payload.get("provider", "")
    owner = payload.get("owner", "")
    repo = payload.get("repo", "")
    body = payload.get("body", "")
    if not all([provider, owner, repo, body]):
        raise HTTPException(400, "Missing 'provider', 'owner', 'repo' or 'body'")
    client = await request.app.state.vcs_service.get_client(provider)
    if not client:
        raise HTTPException(400, f"VCS provider '{provider}' not configured")
    return await client.add_comment(owner, repo, pr_id, body)


@router.post("/vcs/prs/{pr_id}/merge")
async def vcs_merge_pr(pr_id: str, payload: dict, request: Request):
    provider = payload.get("provider", "")
    owner = payload.get("owner", "")
    repo = payload.get("repo", "")
    if not all([provider, owner, repo]):
        raise HTTPException(400, "Missing 'provider', 'owner' or 'repo'")
    client = await request.app.state.vcs_service.get_client(provider)
    if not client:
        raise HTTPException(400, f"VCS provider '{provider}' not configured")
    return await client.merge_pull_request(
        owner, repo, pr_id, method=payload.get("method", "merge"),
    )


@router.post("/vcs/prs/{pr_id}/close")
async def vcs_close_pr(pr_id: str, payload: dict, request: Request):
    """Task 7.24: 关闭 PR/MR——可选带评论（close with comment）。"""
    provider = payload.get("provider", "")
    owner = payload.get("owner", "")
    repo = payload.get("repo", "")
    if not all([provider, owner, repo]):
        raise HTTPException(400, "Missing 'provider', 'owner' or 'repo'")
    client = await request.app.state.vcs_service.get_client(provider)
    if not client:
        raise HTTPException(400, f"VCS provider '{provider}' not configured")
    return await client.close_pull_request(
        owner, repo, pr_id, comment=payload.get("body", ""),
    )


@router.post("/vcs/prs/{pr_id}/review")
async def vcs_review_pr(pr_id: str, payload: dict, request: Request):
    """Task 7.24: 提交 PR/MR 审查——APPROVE / REQUEST_CHANGES / COMMENT。"""
    provider = payload.get("provider", "")
    owner = payload.get("owner", "")
    repo = payload.get("repo", "")
    if not all([provider, owner, repo]):
        raise HTTPException(400, "Missing 'provider', 'owner' or 'repo'")
    client = await request.app.state.vcs_service.get_client(provider)
    if not client:
        raise HTTPException(400, f"VCS provider '{provider}' not configured")
    return await client.review_pull_request(
        owner, repo, pr_id,
        event=payload.get("event", "APPROVE"),
        body=payload.get("body", ""),
    )

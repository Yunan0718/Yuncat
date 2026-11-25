"""routes/snapshot.py —— Side-git 快照 + Harness 录制回放 API

从 main.py 提取（Phase 19），M1 拆分。
"""
from fastapi import APIRouter, Request

from config import SNAPSHOT_RECORDINGS_LIMIT

router = APIRouter(prefix="/api/v1", tags=["snapshot"])


@router.get("/snapshots/{task_id}")
async def list_snapshots(task_id: str, request: Request):
    return await request.app.state.snapshot_service.list_snapshots(task_id)


@router.post("/snapshots/{task_id}/restore")
async def restore_snapshots(task_id: str, request: Request):
    return await request.app.state.snapshot_service.restore_turn(task_id)


@router.get("/harness/recordings")
async def list_recordings(limit: int = SNAPSHOT_RECORDINGS_LIMIT, request: Request = None):
    return await request.app.state.harness_recorder.list_recordings(limit)


@router.get("/harness/recordings/{rid}")
async def get_recording(rid: str, request: Request):
    return await request.app.state.harness_recorder.get_recording(rid)


@router.post("/harness/recordings/{rid}/replay")
async def replay_recording(rid: str, request: Request):
    return await request.app.state.harness_recorder.replay(rid)

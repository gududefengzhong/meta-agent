"""Compatibility router that aggregates task sub-routers."""

from __future__ import annotations

from fastapi import APIRouter

from meta_agent.api.routers import task_commands, task_queries

router = APIRouter(tags=["tasks"])
router.include_router(task_commands.router)
router.include_router(task_queries.router)

"""Role-chain pipeline — declarative replacement for the hardcoded
PO → worker → critic → merge flow."""

from __future__ import annotations

from forge_loop.pipeline.dag import (
    DAG,
    DAGNode,
    ValidationError,
    build_dag,
)
from forge_loop.pipeline.executor import (
    ExecutionResult,
    PipelineExecutor,
    RoleHandler,
    StepContext,
    StepOutcome,
)
from forge_loop.pipeline.loader import (
    ChainStep,
    Condition,
    PipelineLoadError,
    PipelineSpec,
    load_pipeline,
    parse_pipeline,
)

__all__ = [
    "ChainStep",
    "Condition",
    "DAG",
    "DAGNode",
    "ExecutionResult",
    "PipelineExecutor",
    "PipelineLoadError",
    "PipelineSpec",
    "RoleHandler",
    "StepContext",
    "StepOutcome",
    "ValidationError",
    "build_dag",
    "load_pipeline",
    "parse_pipeline",
]

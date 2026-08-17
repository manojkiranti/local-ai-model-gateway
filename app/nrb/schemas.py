"""Request/response models for the NRB operations API.

Thin by construction: `RunOut` is `pipeline.RunView.as_dict()` given a shape, and
nothing here computes anything. The counters and job maps are `dict[str, int]`
rather than enumerated models on purpose — a stage may add a counter without a
schema change, and pinning them here would make `nrb_pipeline_runs.counters` and
this file two places to edit for one number.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .pipeline import STAGES


class RunOut(BaseModel):
    """One pipeline run. Exactly `pipeline.RunView.as_dict()`.

    `status` is one of `running` / `awaiting_jobs` / `succeeded` / `partial` /
    `failed`, and the middle-of-the-list one is the important one for a UI:
    **staging finished, the RAG worker has not.** `jobs` is frozen once the run
    is terminal (§24.2), so polling a finished run never changes what it says.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    trigger: str
    requested_by: str | None
    status: str
    stage: str
    department: str | None
    scope: dict[str, Any]
    counters: dict[str, Any]
    error: str | None
    jobs: dict[str, int]
    created_at: str | None
    started_at: str | None
    finished_at: str | None


class RunTriggerIn(BaseModel):
    """What an admin may ask for. A SUBSET of `PipelineScope`, deliberately.

    `all_files` is absent and unreachable over HTTP: the CLI has `--all` because
    an operator at a terminal is making a considered decision, and the
    `RAG_DOCS_DIR` duplication question is still open before any full-corpus run
    (§20.7 item 2). So the API requires a bound — see `_require_a_bound`.
    """

    model_config = ConfigDict(extra="forbid")

    department: str | None = Field(
        default=None, max_length=64,
        description="Department the rag stage ingests into. Required when "
                    "'rag' is among the stages.",
    )
    stages: list[str] = Field(
        default_factory=lambda: list(STAGES),
        description="Which stages to run, in the pipeline's own order.",
    )
    keys: list[str] = Field(default_factory=list, max_length=5000)
    sections: list[str] = Field(default_factory=list, max_length=64)
    owners: list[str] = Field(default_factory=list, max_length=64)
    years: list[int] = Field(default_factory=list, max_length=64)
    resource_types: list[str] = Field(default_factory=list, max_length=16)
    extensions: list[str] = Field(default_factory=list, max_length=16)
    limit: int | None = Field(default=None, ge=1, le=5000)
    retry_failed: bool = Field(
        default=False,
        description="Requeue FAILED documents in scope. Not a recovery refresh: "
                    "cached unresolved recoveries are purged by a separate "
                    "operator command, never by an update.",
    )

    @model_validator(mode="after")
    def _validate(self) -> "RunTriggerIn":
        unknown = [s for s in self.stages if s not in STAGES]
        if unknown:
            raise ValueError(f"unknown stage(s): {', '.join(sorted(unknown))}")
        if not self.stages:
            raise ValueError("at least one stage is required")
        if "rag" in self.stages and not self.department:
            raise ValueError("the rag stage needs a department")
        if not any(
            (self.keys, self.sections, self.owners, self.years,
             self.resource_types, self.extensions, self.limit)
        ):
            # THE full-corpus guard. Stated as a validation error rather than a
            # 403 because it is a malformed request, not a permission problem:
            # the caller has to say WHICH slice of an 18,266-file corpus they
            # mean. `--all` exists only on the CLI.
            raise ValueError(
                "a bounded scope is required: give keys, sections, owners, "
                "years, resource_types, extensions or limit. An unbounded "
                "full-corpus run is deliberately not available over HTTP."
            )
        return self


class RunTriggerOut(BaseModel):
    """The trigger's answer, and the SAME shape whether it started or not.

    `started=false` with a `run` means an NRB update was already in progress and
    this is it — returned with 409 rather than 500, and with the identical body
    schema so a client parses one thing.
    """

    started: bool
    run: RunOut


class NRBStatusOut(BaseModel):
    """Operational state, composed from the authoritative tables. No new truth.

    Every block is somebody else's number: `pipeline` from `nrb_pipeline_runs`,
    `catalog`/`files` from `app/nrb/catalog.py`'s existing count helpers, and
    `rag` from `documents` / `ingest_jobs`. Nothing is cached and nothing is
    stored.
    """

    active_run: RunOut | None = Field(
        default=None,
        description="The run currently `running` or `awaiting_jobs`, if any. "
                    "Non-null means a trigger would be refused.",
    )
    latest_run: RunOut | None = None
    catalog: dict[str, int]
    files: dict[str, int]
    rag: dict[str, Any]

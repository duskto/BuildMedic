from typing import Literal
from pydantic import BaseModel, Field, ConfigDict

Lifecycle = Literal["queued", "running", "completed", "failed", "interrupted", "cancelling", "cancelled"]
Phase = Literal["ingest", "preflight", "reproduce", "diagnose", "refine", "validate", "report"]
TERMINAL = {"completed", "failed", "interrupted", "cancelled"}
PHASES = ["ingest", "preflight", "reproduce", "diagnose", "refine", "validate", "report"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Source(StrictModel):
    type: Literal["demo", "github_actions"] = "demo"
    case_id: str | None = None
    run_url: str | None = None
    job_id: int | None = None
    run_attempt: int | None = Field(None, ge=1)


class Limits(StrictModel):
    max_wall_seconds: int = Field(600, ge=30, le=1200)
    max_model_calls: int = Field(8, ge=1, le=12)
    max_tokens: int = Field(48000, ge=8192, le=100000)


class CreateTask(StrictModel):
    source: Source
    mode: Literal["standard", "adaptive"] = "standard"
    limits: Limits = Field(default_factory=Limits)


class Preview(StrictModel):
    run_url: str


class ValidationRequest(StrictModel):
    candidate_id: str


class RootCause(StrictModel):
    description: str = Field(min_length=1, max_length=3000)
    evidence_ids: list[str] = Field(min_length=1, max_length=30)
    certainty: Literal["supported", "hypothesis", "insufficient"]


class Diagnosis(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    failure_category: str = Field(max_length=100)
    summary: str = Field(min_length=1, max_length=1000)
    root_cause: RootCause
    limitations: list[str] = Field(default_factory=list, max_length=20)
    next_action: str = Field(max_length=1000)
    patch: str = Field(default="", max_length=40000)


class ExecutionRequest(StrictModel):
    execution_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    case_id: str
    check_id: Literal["target", "regression"]
    patch: str = Field(default="", max_length=40000)

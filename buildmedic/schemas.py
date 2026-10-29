from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Lifecycle = Literal["queued", "running", "completed", "failed", "interrupted", "cancelling", "cancelled"]
Phase = Literal["ingest", "preflight", "reproduce", "diagnose", "refine", "validate", "report"]
TERMINAL = {"completed", "failed", "interrupted", "cancelled"}
PHASES = ["ingest", "preflight", "reproduce", "diagnose", "refine", "validate", "report"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModelCheck(StrictModel):
    provider: str
    model_id: str
    status: Literal["available", "unverified"]
    checked_at: float
    message: str


class Source(StrictModel):
    type: Literal["demo", "github_actions"] = "demo"
    case_id: str | None = None
    run_url: str | None = None
    job_id: int | None = None
    run_attempt: int | None = Field(None, ge=1)

    @model_validator(mode="after")
    def valid_source(self):
        if self.type == "demo" and (not self.case_id or self.run_url or self.job_id or self.run_attempt):
            raise ValueError("演示任务必须提供 case_id，不能混入远程运行信息")
        if self.type == "github_actions" and (
            not self.run_url or not self.job_id or not self.run_attempt or self.case_id
        ):
            raise ValueError("GitHub 任务必须固定 run_url、job_id 和 run_attempt")
        return self


class Limits(StrictModel):
    max_wall_seconds: int = Field(600, ge=30, le=1200)
    max_model_calls: int = Field(8, ge=1, le=32)
    max_tokens: int = Field(48000, ge=8192, le=250000)


class CreateTask(StrictModel):
    source: Source
    mode: Literal["standard", "adaptive"] = "standard"
    limits: Limits = Field(default_factory=Limits)
    experience: bool = Field(False, strict=True)
    agent_tools: bool = Field(False, strict=True)

    @model_validator(mode="after")
    def experience_scope(self):
        if self.experience and (self.mode != "adaptive" or self.source.type != "demo"):
            raise ValueError("经验库仅支持自适应演示任务")
        if not self.agent_tools and self.limits.max_model_calls > 12:
            raise ValueError("超过 12 次的调用预算仅用于主动开启工具诊断的任务")
        extended_context = self.experience or (
            self.source.type == "github_actions" and self.mode == "adaptive"
        )
        if not self.agent_tools and self.limits.max_tokens > (150000 if extended_context else 100000):
            raise ValueError("Token 预算超过当前模式上限；150,000 仅用于经验反思或登记远程自适应任务")
        return self


class Preview(StrictModel):
    run_url: str


class ValidationRequest(StrictModel):
    candidate_id: str


class RootCause(StrictModel):
    description: str = Field(min_length=1, max_length=3000)
    evidence_ids: list[str] = Field(min_length=1, max_length=30)
    certainty: Literal["supported", "hypothesis", "insufficient"]


class SourceEdit(StrictModel):
    path: str = Field(min_length=1, max_length=200)
    before: str = Field(min_length=1, max_length=20000)
    after: str = Field(max_length=20000)


class Diagnosis(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    failure_category: str = Field(max_length=100)
    summary: str = Field(min_length=1, max_length=1000)
    root_cause: RootCause
    limitations: list[str] = Field(default_factory=list, max_length=20)
    next_action: str = Field(max_length=1000)
    patch: str = Field(default="", max_length=40000)
    edits: list[SourceEdit] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def one_delivery_format(self):
        if self.patch and self.edits:
            raise ValueError("patch 与 edits 只能选择一种交付格式")
        return self


class NativeToolFunction(StrictModel):
    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
    arguments: str = Field(max_length=60000, strict=True)

    @model_validator(mode="after")
    def utf8_arguments(self):
        self.arguments.encode("utf-8")
        return self


class NativeToolCall(StrictModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")
    type: Literal["function"]
    function: NativeToolFunction


class NativeAssistant(StrictModel):
    role: Literal["assistant"] = "assistant"
    content: None = None  # Assistant prose and private reasoning are never retained in tool mode.
    tool_calls: list[NativeToolCall] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def unique_calls(self):
        ids = [call.id for call in self.tool_calls]
        if len(set(ids)) != len(ids):
            raise ValueError("工具调用 ID 重复")
        return self


class RemoteSnapshot(StrictModel):
    profile_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,79}$")
    profile_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", max_length=200)
    head_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    snapshot_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class ExecutionRequest(StrictModel):
    execution_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    case_id: str | None = None
    check_id: Literal["target", "regression"]
    patch: str = Field(default="", max_length=40000)
    remote: RemoteSnapshot | None = None

    @model_validator(mode="after")
    def one_snapshot_source(self):
        if (self.case_id is None) == (self.remote is None):
            raise ValueError("执行必须且只能指定案例或已登记远程快照")
        return self


Outcome = Literal["repaired", "diagnosed_only", "unresolved", "blocked", "not_reproduced", "budget_exhausted"]


class TaskSnapshot(BaseModel):
    """Backend-owned durable snapshot contract; additive execution metadata is allowed."""

    model_config = ConfigDict(extra="allow")
    id: str
    title: str
    repo: str
    source_type: Literal["demo", "github_actions"]
    case_id: str | None
    head_sha: str | None
    snapshot_sha256: str | None = None
    run_id: int | None
    run_attempt: int | None
    job_id: int | None
    profile_version: str | None
    runner_profile: RemoteSnapshot | None = None
    source_snapshot: dict | None = None
    execution_label: str
    parent_task_id: str | None
    mode: Literal["standard", "adaptive"]
    agent_tools: bool = False
    experiment_binding: dict | None = None
    lifecycle: Lifecycle
    phase: Phase
    phase_history: list[Phase] = Field(default_factory=list)
    outcome: Outcome | None
    validation_status: Literal["not_run", "passed", "failed", "blocked"]
    reproduction_status: Literal["reproduced", "not_reproduced", "blocked", "not_run"]
    message: str
    state_version: int
    last_event_seq: int
    created_at: float
    updated_at: float
    evidence: list[dict]
    candidates: list[dict]
    validations: list[dict]
    executions: list[dict]
    model_calls: list[dict] = Field(default_factory=list)
    agent_actions: list[dict] = Field(default_factory=list)
    harness2_sha: str | None = None
    harness: dict | None = None
    experience: dict | None = None
    rollouts: list[dict]
    diagnosis: dict | None
    artifacts: list[dict]
    usage: dict
    limits: Limits


class TaskPage(BaseModel):
    items: list[TaskSnapshot]
    total: int
    page: int
    page_size: int


class TaskEvent(BaseModel):
    model_config = ConfigDict(extra="allow")
    task_id: str
    seq: int = Field(ge=1)
    state_version: int = Field(ge=1)
    at: float


class Envelope[T](BaseModel):
    data: T
    request_id: str

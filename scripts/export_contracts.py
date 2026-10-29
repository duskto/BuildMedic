"""Export backend-owned schemas for review and external clients."""

import json
from pathlib import Path

from buildmedic.api import app
from buildmedic.diagnosis_protocol import ReviewReceipt, ReviewRequest
from buildmedic.evaluation import SPEC_ADAPTER
from buildmedic.evaluation_analysis import EvaluationAnalysis
from buildmedic.evaluation_dataset import DatasetManifest
from buildmedic.frozen_guidance import FrozenGuidance
from buildmedic.logs import LogIndex, LogWindow
from buildmedic.runner_runtime import ExecutionTermination
from buildmedic.schemas import CreateTask, Diagnosis, ExecutionRequest, TaskEvent, TaskSnapshot
from buildmedic.scoring_protocol import GroupedScoreReceipt, GroupedScoreRequest, ScoreReceipt, ScoreRequest

root = Path(__file__).resolve().parents[1] / "packages/contracts"
root.mkdir(parents=True, exist_ok=True)
for name, schema in {
    "openapi": app.openapi(),
    "create-task": CreateTask.model_json_schema(),
    "diagnosis": Diagnosis.model_json_schema(),
    "runner-execution": ExecutionRequest.model_json_schema(),
    "execution-termination": ExecutionTermination.model_json_schema(),
    "task-snapshot": TaskSnapshot.model_json_schema(),
    "task-event": TaskEvent.model_json_schema(),
    "experiment-spec": SPEC_ADAPTER.json_schema(),
    "experiment-analysis": EvaluationAnalysis.model_json_schema(),
    "log-index": LogIndex.model_json_schema(),
    "log-window": LogWindow.model_json_schema(),
    "score-request": ScoreRequest.model_json_schema(),
    "score-receipt": ScoreReceipt.model_json_schema(),
    "grouped-score-request": GroupedScoreRequest.model_json_schema(),
    "grouped-score-receipt": GroupedScoreReceipt.model_json_schema(),
    "dataset-manifest": DatasetManifest.model_json_schema(),
    "frozen-guidance": FrozenGuidance.model_json_schema(),
    "diagnosis-review-request": ReviewRequest.model_json_schema(),
    "diagnosis-review-receipt": ReviewReceipt.model_json_schema(),
}.items():
    (root / (name + ".schema.json")).write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n")
print("Exported contracts to", root)

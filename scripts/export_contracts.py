"""Export backend-owned schemas for review and external clients."""

import json
from pathlib import Path

from buildmedic.api import app
from buildmedic.evaluation import SPEC_ADAPTER
from buildmedic.schemas import CreateTask, Diagnosis, ExecutionRequest, TaskEvent, TaskSnapshot
from buildmedic.scoring_protocol import ScoreReceipt, ScoreRequest

root = Path(__file__).resolve().parents[1] / "packages/contracts"
root.mkdir(parents=True, exist_ok=True)
for name, schema in {
    "openapi": app.openapi(),
    "create-task": CreateTask.model_json_schema(),
    "diagnosis": Diagnosis.model_json_schema(),
    "runner-execution": ExecutionRequest.model_json_schema(),
    "task-snapshot": TaskSnapshot.model_json_schema(),
    "task-event": TaskEvent.model_json_schema(),
    "experiment-spec": SPEC_ADAPTER.json_schema(),
    "score-request": ScoreRequest.model_json_schema(),
    "score-receipt": ScoreReceipt.model_json_schema(),
}.items():
    (root / (name + ".schema.json")).write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n")
print("Exported contracts to", root)

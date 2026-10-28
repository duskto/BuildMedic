"""Public, fixed container gateway. Expected answers never enter this process."""

import importlib
import json
import sys

MODULES = {"pagination": "demo.pagination", "imports": "demo.invoice", "signature": "demo.shipping"}
ALLOWED = {
    "pagination": {"paginate"},
    "imports": {"invoice_total"},
    "signature": {"shipping_cost", "checkout", "quote_order"},
}
sys.path.insert(0, "/candidate")
request = json.loads(sys.stdin.buffer.read(16001))
case = request["case_id"]
answers = []
for call in request["calls"]:
    try:
        if call["entrypoint"] not in ALLOWED[case]:
            raise ValueError("unregistered entrypoint")
        function = getattr(importlib.import_module(MODULES[case]), call["entrypoint"])
        result = {"ok": True, "value": function(*call["args"], **call["kwargs"])}
        json.dumps(result, allow_nan=False)
    except BaseException as exc:  # noqa: BLE001 - Candidate SystemExit must become an observation, never terminate the gateway.
        result = {
            "ok": False,
            "exception": type(exc).__name__
            if type(exc).__name__ in {"ValueError", "TypeError", "ModuleNotFoundError", "ImportError"}
            else "Exception",
        }
    answers.append(result)
print(json.dumps(answers, allow_nan=False))

"""Fixed black-box gateway. Only inputs enter; expected answers stay with the coordinator."""

import importlib
import json
import sys

MODULES = {"pagination": "demo.pagination", "imports": "demo.invoice", "signature": "demo.shipping"}
ALLOWED = {
    "pagination": {"paginate"},
    "imports": {"invoice_total"},
    "signature": {"shipping_cost", "checkout", "quote_order"},
    "mutable-default": {"append_value"},
    "csv-quoting": {"encode_row"},
    "merge-precedence": {"merge_config"},
    "stable-dedup": {"unique"},
    "fullmatch": {"valid_identifier"},
    "clamp": {"clamp"},
    "weighted-mean": {"weighted_mean"},
    "graph-cycle": {"has_cycle"},
    "leap-year": {"is_leap"},
    "utf8-size": {"fits_utf8"},
    "version-order": {"sorted_versions"},
    "missing-sentinel": {"value_or_default"},
    "boolean-parser": {"parse_boolean"},
    "zip-length": {"pair_records"},
    "rollback": {"rollback_mapping"},
    "fraction-sign": {"reduce_fraction"},
    "path-containment": {"is_within"},
}
MODULES.update({case: "demo.subject" for case in ALLOWED if case not in MODULES})


class RollbackSignal(Exception):
    pass


def rollback_mapping(module, initial, updates, removed):
    """Fixed input-only scenario: mutate a mapping, raise, and observe resulting state."""
    mapping = dict(initial)
    try:
        with module.transaction(mapping):
            mapping.update(updates)
            for key in removed:
                mapping.pop(key, None)
            raise RollbackSignal()
    except RollbackSignal:
        pass
    return mapping


def run(request):
    case = request["case_id"]
    answers = []
    for call in request["calls"]:
        try:
            if call["entrypoint"] not in ALLOWED[case]:
                raise ValueError("unregistered entrypoint")
            module = importlib.import_module(MODULES[case])
            if case == "rollback":
                value = rollback_mapping(module, *call["args"], **call["kwargs"])
            else:
                function = getattr(module, call["entrypoint"])
                value = function(*call["args"], **call["kwargs"])
        except BaseException as exc:  # noqa: BLE001 - Candidate SystemExit becomes an observation.
            result = {
                "ok": False,
                "exception": type(exc).__name__
                if type(exc).__name__ in {"ValueError", "TypeError", "ModuleNotFoundError", "ImportError"}
                else "Exception",
            }
        else:
            try:
                # Snapshot mutable results before a later call changes an earlier observation.
                result = json.loads(json.dumps({"ok": True, "value": value}, allow_nan=False))
            except BaseException:  # noqa: BLE001 - Encoding failure is not an expected function exception.
                result = {"ok": False, "exception": "ObservationError"}
        answers.append(result)
    return answers


def main():
    sys.path.insert(0, "/candidate")
    request = json.loads(sys.stdin.buffer.read(16001))
    print(json.dumps(run(request), allow_nan=False))


if __name__ == "__main__":
    main()

"""Check configured model access; --generate explicitly performs one small paid call."""

import argparse
import json

from buildmedic.clients import ModelClient
from buildmedic.config import settings
from buildmedic.errors import AppError, Blocked
from buildmedic.worker import run_async


async def check(generate=False):
    model = ModelClient()
    try:
        info = await model.probe()
        print(json.dumps(info, ensure_ascii=False))
        if generate:
            # Bound this standalone smoke request independently of task budgets.
            cfg = settings()
            original = cfg.max_output_tokens
            cfg.max_output_tokens = 512
            try:
                content, usage = await model.complete(
                    [
                        {"role": "system", "content": 'Output only a JSON object. Example: {"ok":true}'},
                        {"role": "user", "content": 'Return {"ok":true} without additional fields.'},
                    ]
                )
            finally:
                cfg.max_output_tokens = original
            if json.loads(content) != {"ok": True}:
                raise AppError("model_probe_content", "生成响应未通过最小 JSON 校验", 502)
            print(json.dumps({"generation": "passed", **model.metadata, "usage": usage}, ensure_ascii=False))
        return 0 if generate or info["status"] == "available" else 2
    except (AppError, Blocked, ValueError) as exc:
        # Never print provider bodies, raw requests, configuration values or tracebacks.
        print(
            json.dumps(
                {
                    "status": "failed",
                    "code": getattr(exc, "code", "model_probe_failed"),
                    "message": str(exc) if isinstance(exc, (AppError, Blocked)) else "JSON 校验失败",
                },
                ensure_ascii=False,
            )
        )
        return 1
    finally:
        await model.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--generate",
        action="store_true",
        help="perform one paid JSON completion, limited to 512 output tokens",
    )
    args = parser.parse_args()
    return run_async(check(args.generate))


if __name__ == "__main__":
    raise SystemExit(main())

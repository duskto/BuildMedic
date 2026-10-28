"""Run/resume a preregistered development comparison through the ordinary API/Worker."""

import argparse
import json
import os
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

from buildmedic.evaluation import ExperimentSpec, digest


def save(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as output:
        json.dump(state, output, ensure_ascii=False, indent=2)
    temporary.chmod(0o600)
    temporary.replace(path)
    path.chmod(0o600)


def request(client, method, path, **kwargs):
    response = client.request(method, path, **kwargs)
    if response.is_error:
        error = response.json().get("error", {})
        raise RuntimeError(f"{error.get('code', response.status_code)}: {error.get('message', '请求失败')}")
    return response.json()["data"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument(
        "--state", type=Path, required=True, help="private resumable API session; keep under var/"
    )
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--cancel", action="store_true")
    args = parser.parse_args()
    parsed = urlparse(args.url)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        parser.error("--url 必须是受信任 API 的根地址，不能包含凭据、查询或 fragment")
    spec = ExperimentSpec.model_validate_json(args.plan.read_text())
    manifest = spec.model_dump(mode="json")
    state = (
        json.loads(args.state.read_text())
        if args.state.exists()
        else {
            "manifest_sha256": digest(manifest),
            "url": args.url,
            "idempotency_key": os.urandom(16).hex(),
            "cookie": None,
            "experiment_id": None,
        }
    )
    if state["manifest_sha256"] != digest(manifest) or state["url"] != args.url:
        parser.error("状态文件与清单或 API 地址不匹配；请使用另一状态路径")
    with httpx.Client(base_url=args.url, timeout=30, follow_redirects=False) as client:
        if state["cookie"]:
            client.cookies.set("buildmedic_session", state["cookie"])
        else:
            request(client, "GET", "/api/v1/demo-cases")
            state["cookie"] = client.cookies.get("buildmedic_session")
            if not state["cookie"]:
                raise RuntimeError("API 未建立会话")
        save(args.state, state)  # Save identity before the first mutating request.
        if not state["experiment_id"]:
            result = request(
                client,
                "POST",
                "/api/v1/experiments",
                json=manifest,
                headers={"Idempotency-Key": state["idempotency_key"]},
            )
            state["experiment_id"] = result["id"]
            save(args.state, state)
        eid = state["experiment_id"]
        print(f"评测 {eid}；固定 {len(spec.cases)} 个开发案例 × {spec.repeats} 次 × 2 组", flush=True)
        if args.cancel:
            request(client, "POST", f"/api/v1/experiments/{eid}/cancel")
            print("已停止后续任务，并请求取消当前任务。", flush=True)
            return
        previous = None
        while True:
            result = request(client, "POST", f"/api/v1/experiments/{eid}/advance")
            tid = result["task_id"]
            if not tid:
                report = request(client, "GET", f"/api/v1/experiments/{eid}")
                path = args.state.with_name(args.state.stem + "-report.json")
                exported = client.get(f"/api/v1/experiments/{eid}/report")
                exported.raise_for_status()
                save(path, exported.json())
                print(
                    f"{result['status']}: {report['finished']}/{report['planned']}；报告 {path}", flush=True
                )
                return
            if tid != previous:
                print(f"执行任务 {tid}", flush=True)
                previous = tid
            time.sleep(2)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("已停止本地等待；后台任务保留。重用同一状态文件可恢复，或用 --cancel 取消。")

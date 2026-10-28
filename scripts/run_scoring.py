"""Run/resume detached independent scoring for an existing completed experiment; no model requests."""

import argparse
import json
import time
from pathlib import Path

import httpx
from run_evaluation import request, save


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True, help="existing private evaluation session")
    args = parser.parse_args()
    state = json.loads(args.state.read_text())
    eid = state["experiment_id"]
    with httpx.Client(
        base_url=state["url"],
        cookies={"buildmedic_session": state["cookie"]},
        follow_redirects=False,
        timeout=30,
    ) as client:
        result = request(client, "POST", f"/api/v1/experiments/{eid}/score")
        while result["status"] not in {"completed", "failed"}:
            if result.get("error_code"):
                raise RuntimeError("独立评分服务暂不可用；原请求已保留，恢复配置后重用此命令")
            time.sleep(2)
            result = request(client, "GET", f"/api/v1/experiments/{eid}/score")
        path = args.state.with_name(args.state.stem + "-score.json")
        save(path, result)
        print(f"评分 {result['id']}：{result['status']}；报告 {path}")
        for row in result["summary"]:
            if row["repair_applicable"]:
                print(
                    f"{row['mode']}: 行为与公开回归通过 {row['behavior_verified']}/{row['planned']}，评分异常 {row['evaluation_errors']}"
                )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("停止本地等待；评分任务保留，可用同一状态文件恢复。")

"""Docker facts and cleanup acknowledgements, without exposing daemon diagnostics."""

import hashlib
import json
import re
import subprocess

from pydantic import BaseModel, ConfigDict


class ExecutionTermination(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cause: str = "runner_error"
    cli_exit_code: int | None = None
    container_exit_code: int | None = None
    oom_killed: bool | None = None
    signal: int | None = None
    possible_signal: int | None = None
    container_status: str | None = None
    state_confirmed: bool = False
    cleanup_confirmed: bool = False
    cleanup_error: str | None = None


class DockerContainer:
    def __init__(self, root, execution_id, request_hash, *, legacy=False):
        self.name = "bm-" + execution_id
        self.legacy = legacy
        self.labels = {
            "io.buildmedic.runner": hashlib.sha256(str(root).encode()).hexdigest(),
            "io.buildmedic.execution": execution_id,
            "io.buildmedic.request": request_hash,
        }
        self.creation_uncertain = False
        self.create_exit_code = None
        self.container_id = None

    def label_args(self):
        return [arg for key, value in self.labels.items() for arg in ("--label", f"{key}={value}")]

    def run(self, argv):
        try:
            return subprocess.run(argv, capture_output=True, text=True, timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return None

    def create(self, argv):
        result = self.run(argv)
        self.creation_uncertain = result is None
        self.create_exit_code = result.returncode if result is not None else None
        if (
            result is not None
            and result.returncode == 0
            and re.fullmatch(r"[a-f0-9]{64}", result.stdout.strip())
        ):
            self.container_id = result.stdout.strip()
        return result is not None and result.returncode == 0

    def inspect(self):
        result = self.run(
            [
                "docker",
                "container",
                "inspect",
                "--format",
                '{"id":"{{.Id}}","state":{{json .State}},"labels":{{json .Config.Labels}}}',
                self.name,
            ]
        )
        if result is None:
            return "unavailable", None
        if result.returncode != 0:
            # An inspect failure alone is not proof that the container no longer exists.
            listed = self.run(
                [
                    "docker",
                    "container",
                    "ls",
                    "-aq",
                    "--filter",
                    f"name=^/{self.name}$",
                ]
            )
            if listed is not None and listed.returncode == 0 and not listed.stdout.strip():
                return "absent", None
            return "unavailable", None
        try:
            if len(result.stdout) > 64000:
                return "unavailable", None
            value = json.loads(result.stdout)
            labels = value["labels"] or {}
            owned = all(labels.get(k) == v for k, v in self.labels.items())
            if not owned and not (self.legacy and not any(k.startswith("io.buildmedic.") for k in labels)):
                return "foreign", None
            if not re.fullmatch(r"[a-f0-9]{64}", value.get("id", "")):
                return "unavailable", None
            self.container_id = value["id"]
            state = value["state"]
            if (
                not isinstance(state, dict)
                or type(state.get("OOMKilled")) is not bool
                or type(state.get("ExitCode")) is not int
                or type(state.get("Running")) is not bool
                or not isinstance(state.get("Status"), str)
            ):
                return "unavailable", None
            return "owned", state
        except (ValueError, KeyError, TypeError, AttributeError):
            return "unavailable", None

    def kill(self):
        ownership, _ = self.inspect()
        if ownership == "owned":
            self.run(["docker", "kill", self.container_id])

    def cleanup(self):
        ownership, _ = self.inspect()
        if ownership == "absent":
            return (False, "create_unconfirmed") if self.creation_uncertain else (True, None)
        if ownership != "owned":
            return False, "container_identity_mismatch" if ownership == "foreign" else "docker_unavailable"
        removed = self.run(["docker", "rm", "-f", self.container_id])
        if removed is not None and removed.returncode == 0:
            return True, None
        ownership, _ = self.inspect()
        return (True, None) if ownership == "absent" else (False, "container_remove_failed")


def classify_exit(backend, returncode, termination, state=None, requested=None):
    termination.cli_exit_code = returncode if backend == "docker" else None
    if state is not None:
        termination.oom_killed = state["OOMKilled"]
        termination.container_exit_code = state["ExitCode"] if not state["Running"] else None
        termination.container_status = state["Status"]
        termination.state_confirmed = not state["Running"] and state["Status"] == "exited"
    if requested:
        if backend == "trusted-local" and returncode < 0:
            termination.signal = -returncode
        elif state and state["ExitCode"] == 137:
            termination.signal = 9
        return requested
    if backend == "trusted-local":
        termination.state_confirmed = True
        if returncode < 0:
            termination.signal = -returncode
            return "signal"
        return "exited"
    if state is None:
        return "container_state_unavailable"
    if state["OOMKilled"]:
        if state["ExitCode"] == 137:
            termination.signal = 9
        return "oom_killed"
    if not termination.state_confirmed or state.get("Error") or state["ExitCode"] != returncode:
        return "runtime_error"
    if 129 <= returncode <= 192:
        # Docker exposes an exit code, not the waitpid signal. Explicit exit(137) is ambiguous.
        termination.possible_signal = returncode - 128
        return "abnormal_exit"
    return "exited"

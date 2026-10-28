import subprocess
from pathlib import Path

root = Path(__file__).resolve().parents[1]
subprocess.run(
    [
        "docker",
        "build",
        "-f",
        str(root / "infra/runner.Dockerfile"),
        "-t",
        "buildmedic-pytest:local",
        str(root),
    ],
    check=True,
)
image = subprocess.check_output(
    ["docker", "image", "inspect", "buildmedic-pytest:local", "--format", "{{.Id}}"], text=True
).strip()
print("Set the following server configuration after reviewing the built image:")
print("BUILDMEDIC_RUNNER_IMAGE=" + image)

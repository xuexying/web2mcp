"""The ego-browser backend: run a round through the Node bridge ``ego_explorer.mjs``.

This preserves the original behavior exactly — the round logic lives in the .mjs module,
executed inside ``ego-browser nodejs``. Available only where the ``ego-browser`` executable
is installed (macOS / Windows).
"""

import json
import shutil
import subprocess
from pathlib import Path

from . import BrowserBackend, register


@register("ego")
def _factory():
    return EgoBackend()


class EgoBackend(BrowserBackend):
    name = "ego"

    def __init__(self, *, runner=subprocess.run):
        self._runner = runner

    def available(self):
        return shutil.which("ego-browser") is not None

    def unavailable_reason(self):
        return (
            "ego-browser executable not found. Install it from "
            "https://github.com/citrolabs/ego-lite, or use --backend playwright "
            "(pip install 'web2mcp[record]')."
        )

    def run_round(self, root, run_id, *, timeout):
        root = Path(root)
        input_path = str(root / "rounds" / (run_id + ".input.json"))
        bridge = (Path(__file__).resolve().parents[1] / "ego_explorer.mjs").as_uri()
        script = root / "rounds" / (run_id + ".mjs")
        script.write_text(
            "const { run } = await import("
            + json.dumps(bridge)
            + ");\nawait run("
            + json.dumps(input_path)
            + ");\n",
            encoding="utf-8",
        )
        script.chmod(0o600)
        completed = self._runner(
            ["ego-browser", "nodejs", "-e", "await import(" + json.dumps(script.as_uri()) + ")"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = root / "rounds" / (run_id + ".output.json")
        if completed.returncode != 0 or not output.exists():
            return {
                "ok": False,
                "stdout": getattr(completed, "stdout", ""),
                "stderr": getattr(completed, "stderr", ""),
            }
        return {"ok": True}

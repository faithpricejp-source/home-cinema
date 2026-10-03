"""前端回归：tests/frontend/ 下的 Node 脚本（假 DOM + 假 XHR，只用 Node 内置模块）。
每个脚本断言一条正确行为，失败时 exit 1。没有 node 时跳过。"""

import shutil
import subprocess
from pathlib import Path

import pytest

FRONTEND = Path(__file__).parent / "frontend"
NODE = shutil.which("node")
SCRIPTS = sorted(p.name for p in FRONTEND.glob("*.js"))


@pytest.mark.skipif(NODE is None, reason="node 不在 PATH")
@pytest.mark.parametrize("script", SCRIPTS)
def test_frontend_script(script):
    r = subprocess.run([NODE, str(FRONTEND / script)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]

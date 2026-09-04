"""真机验收：Loop 端到端（真实 Postgres + Redis + API + LoopWorker）。

API HTTP 创建 → Redis Streams 入队 → LoopWorker（桩 LLM）认领 →
租约 → engine 执行 → 终态落库 → 查询确认 CONVERGED。
"""

import asyncio
import json
import urllib.request
from decimal import Decimal

from ariadne.config import get_settings
from ariadne.loop_module.engine import LLMResponse
from ariadne.worker.loop_worker import LoopWorker

API = "http://api:8000"
KEY = "ak_local_dev_key"


class StubLLM:
    async def complete(self, prompt: str, *, model: str) -> LLMResponse:
        return LLMResponse(
            output="def add(a, b):\n    return a + b",
            input_tokens=100,
            output_tokens=200,
            claimed_done=True,
            model=model,
            cost_usd=Decimal("0.01"),
        )


def api(method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)


async def main() -> None:
    created = api(
        "POST",
        "/v1/loops",
        {
            "task": "写一个返回两数之和的 Python 函数",
            "mode": "quality",
            "assertions": [
                {
                    "id": "has_def",
                    "kind": "regex",
                    "spec": {"pattern": r"def\s+\w+\s*\("},
                    "weight": 1.0,
                    "blocking": True,
                }
            ],
            "budget": {"max_iterations": 3, "max_total_tokens": 50000},
        },
    )
    loop_id = created["loop_id"]
    print("created:", created)

    worker = LoopWorker(get_settings(), llm=StubLLM())  # type: ignore[arg-type]
    await worker._queue.connect()
    for _ in range(5):
        await worker._poll()
        state = api("GET", f"/v1/loops/{loop_id}")
        if state["final_state"]:
            break
        await asyncio.sleep(2)
    await worker.close()

    final = api("GET", f"/v1/loops/{loop_id}")
    print("final state:", final["state"], "final:", final["final_state"])
    print("iterations:", final["iteration"], "tokens:", final["cumulative_tokens"])
    iters = api("GET", f"/v1/loops/{loop_id}/iterations")
    print("checkpoints:", len(iters["iterations"]))
    assert final["final_state"] == "CONVERGED", f"终态不对: {final['final_state']}"
    print("LOOP E2E: PASS")


asyncio.run(main())

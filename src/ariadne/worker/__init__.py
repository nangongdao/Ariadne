"""采集 / Loop / Eval Worker：消费队列、加工、执行。

三类 Worker 独立伸缩（M6 §5）：
  - collector：高吞吐，CPU 轻量，按 span 速率伸缩
  - loop：长任务，单任务分钟级，按并发 Loop 数伸缩
  - eval：CPU/LLM 密集，批量评测，按实验并发伸缩
"""

from ariadne.worker.collector import CollectorWorker, run_collector
from ariadne.worker.eval_worker import EvalWorker, run_eval_worker
from ariadne.worker.processor import ProcessResult, SpanProcessor

__all__ = [
    "CollectorWorker",
    "EvalWorker",
    "ProcessResult",
    "SpanProcessor",
    "run_collector",
    "run_eval_worker",
]

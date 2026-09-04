"""Loop 闭环验收的缺陷用例集（M3 验收项 5/6/4）。

组织方式与 `redteam_cases.py` 同构：语料与断言分离，语料按**缺陷类型**
枚举，断言在 `test_loop_benchmark.py` 里做统计。

每个用例是一个真实的代码缺陷 + 一份 pytest 断言 + 模型逐轮给出的修复
尝试。跑的时候产出物真落盘、pytest 真执行 —— 这是 COMMAND 断言（信号
最硬的那类）第一次进入闭环验收，此前 `test_loop_engine.py` 的端到端用例
只能用 REGEX 断言代理，理由写在它的 docstring 里。

## 模型行为怎么来的

没有真实 provider key，模型响应是**脚本化**的。这决定了两个指标的性质
不同，必须分开看：

- **假完成拦截率**（验收项 4）是**真验收**。它测的是"模型自称完成时
  Verifier 能否独立判定"，被测对象是 Ralph 机制本身，与模型智能无关。
  每个用例的每一轮都设 `claimed_done=True`（真实 LLM 确实爱说"完成了"），
  未达标的那些轮次即假完成样本。
- **闭环达标率 / 平均轮次**（验收项 5/6）在脚本模型下测的是**机制正确性**：
  给定模型在第 N 轮给出正确实现，闭环能否识别并停止；给定模型始终不对，
  能否正确终止而不假收敛。它**不能**替代"真实 LLM 能修好多少缺陷"这一
  能力验收 —— 后者需要 provider key，见 test_loop_benchmark 的模块说明。

## 用例分布

17 个可修复用例（覆盖 1/2/3 轮收敛）+ 3 个不可修复用例（验证不假收敛）。
不可修复的那些同样重要：闭环最危险的失败模式不是"修不好"，是"没修好
却说修好了"。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RepairCase:
    """一个代码修复用例。

    attempts 是模型逐轮给出的**文件完整内容**。用完整内容而非补丁，与
    `FencedCodeWriter` 的全量覆盖策略一致。
    """

    name: str
    # 缺陷类型，用于分组统计
    category: str
    filename: str
    buggy: str
    tests: str
    # 模型各轮给出的实现。最后一个会被重复使用直到轮次耗尽
    attempts: tuple[str, ...]
    # 期望闭环达标
    converges: bool
    # 期望轮次（converges 为真时有意义）
    expected_iterations: int = 0


def _case(
    name: str,
    category: str,
    buggy: str,
    tests: str,
    attempts: tuple[str, ...],
    *,
    converges: bool = True,
    expected_iterations: int = 0,
) -> RepairCase:
    return RepairCase(
        name=name,
        category=category,
        filename="solution.py",
        buggy=buggy,
        tests=tests,
        attempts=attempts,
        converges=converges,
        expected_iterations=expected_iterations,
    )


# ---------- 一轮收敛（模型首次尝试即正确） ----------

ONE_SHOT: tuple[RepairCase, ...] = (
    _case(
        "wrong_operator",
        "逻辑错误",
        "def add(a, b):\n    return a - b\n",
        "from solution import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"
        "    assert add(-1, 1) == 0\n",
        ("def add(a, b):\n    return a + b\n",),
        expected_iterations=1,
    ),
    _case(
        "integer_division",
        "数值语义",
        "def mean(xs):\n    return sum(xs) // len(xs)\n",
        "from solution import mean\n\ndef test_mean():\n"
        "    assert mean([1, 2]) == 1.5\n",
        ("def mean(xs):\n    return sum(xs) / len(xs)\n",),
        expected_iterations=1,
    ),
    _case(
        "case_sensitive_compare",
        "字符串处理",
        "def same(a, b):\n    return a == b\n",
        "from solution import same\n\ndef test_same():\n"
        "    assert same('Hello', 'hello')\n    assert not same('a', 'b')\n",
        ("def same(a, b):\n    return a.lower() == b.lower()\n",),
        expected_iterations=1,
    ),
    _case(
        "missing_strip",
        "字符串处理",
        "def parse_int(s):\n    return int(s)\n",
        "from solution import parse_int\n\ndef test_parse():\n"
        "    assert parse_int('  42 ') == 42\n",
        ("def parse_int(s):\n    return int(s.strip())\n",),
        expected_iterations=1,
    ),
    _case(
        "wrong_comparison",
        "逻辑错误",
        "def is_adult(age):\n    return age > 18\n",
        "from solution import is_adult\n\ndef test_adult():\n"
        "    assert is_adult(18)\n    assert not is_adult(17)\n",
        ("def is_adult(age):\n    return age >= 18\n",),
        expected_iterations=1,
    ),
)


# ---------- 两轮收敛（首轮部分修复，次轮正确） ----------

TWO_SHOT: tuple[RepairCase, ...] = (
    _case(
        "empty_list_crash",
        "边界条件",
        "def first(xs):\n    return xs[0]\n",
        "from solution import first\n\ndef test_first():\n"
        "    assert first([1, 2]) == 1\n    assert first([]) is None\n",
        (
            # 第 1 轮：改了但仍然崩（用了错误的判空方式）
            "def first(xs):\n    if xs[0]:\n        return xs[0]\n    return None\n",
            "def first(xs):\n    if not xs:\n        return None\n    return xs[0]\n",
        ),
        expected_iterations=2,
    ),
    _case(
        "off_by_one",
        "边界条件",
        "def last_n(xs, n):\n    return xs[-n - 1 :]\n",
        "from solution import last_n\n\ndef test_last_n():\n"
        "    assert last_n([1, 2, 3, 4], 2) == [3, 4]\n"
        "    assert last_n([1], 1) == [1]\n",
        (
            "def last_n(xs, n):\n    return xs[-n + 1 :]\n",
            "def last_n(xs, n):\n    return xs[-n:] if n else []\n",
        ),
        expected_iterations=2,
    ),
    _case(
        "none_not_handled",
        "空值处理",
        "def length(s):\n    return len(s)\n",
        "from solution import length\n\ndef test_length():\n"
        "    assert length('abc') == 3\n    assert length(None) == 0\n",
        (
            "def length(s):\n    return len(s) if s != '' else 0\n",
            "def length(s):\n    return len(s) if s else 0\n",
        ),
        expected_iterations=2,
    ),
    _case(
        "mutable_default",
        "可变默认参数",
        "def collect(item, bucket=[]):\n    bucket.append(item)\n    return bucket\n",
        "from solution import collect\n\ndef test_collect():\n"
        "    assert collect(1) == [1]\n    assert collect(2) == [2]\n",
        (
            # 第 1 轮：换了名字但仍是可变默认值
            "def collect(item, bucket=[]):\n    b = bucket\n    b.append(item)\n    return b\n",
            "def collect(item, bucket=None):\n    bucket = [] if bucket is None else bucket\n"
            "    bucket.append(item)\n    return bucket\n",
        ),
        expected_iterations=2,
    ),
    _case(
        "unhandled_exception",
        "异常处理",
        "def safe_div(a, b):\n    return a / b\n",
        "from solution import safe_div\n\ndef test_div():\n"
        "    assert safe_div(6, 3) == 2\n    assert safe_div(1, 0) is None\n",
        (
            "def safe_div(a, b):\n    if b < 0:\n        return None\n    return a / b\n",
            "def safe_div(a, b):\n    if b == 0:\n        return None\n    return a / b\n",
        ),
        expected_iterations=2,
    ),
    _case(
        "wrong_sort_key",
        "排序",
        "def by_length(words):\n    return sorted(words)\n",
        "from solution import by_length\n\ndef test_sort():\n"
        "    assert by_length(['ccc', 'a', 'bb']) == ['a', 'bb', 'ccc']\n",
        (
            "def by_length(words):\n    return sorted(words, reverse=True)\n",
            "def by_length(words):\n    return sorted(words, key=len)\n",
        ),
        expected_iterations=2,
    ),
    _case(
        "dict_keyerror",
        "空值处理",
        "def lookup(d, k):\n    return d[k]\n",
        "from solution import lookup\n\ndef test_lookup():\n"
        "    assert lookup({'a': 1}, 'a') == 1\n"
        "    assert lookup({'a': 1}, 'z') == 0\n",
        (
            "def lookup(d, k):\n    return d[k] if d else 0\n",
            "def lookup(d, k):\n    return d.get(k, 0)\n",
        ),
        expected_iterations=2,
    ),
)


# ---------- 三轮收敛（需要两次修正） ----------

THREE_SHOT: tuple[RepairCase, ...] = (
    _case(
        "recursion_no_base",
        "递归",
        "def fact(n):\n    return n * fact(n - 1)\n",
        "from solution import fact\n\ndef test_fact():\n"
        "    assert fact(0) == 1\n    assert fact(5) == 120\n",
        (
            "def fact(n):\n    if n == 1:\n        return 1\n    return n * fact(n - 1)\n",
            "def fact(n):\n    if n <= 1:\n        return n\n    return n * fact(n - 1)\n",
            "def fact(n):\n    if n <= 1:\n        return 1\n    return n * fact(n - 1)\n",
        ),
        expected_iterations=3,
    ),
    _case(
        "fizzbuzz_order",
        "逻辑错误",
        "def fizzbuzz(n):\n    if n % 3 == 0:\n        return 'Fizz'\n"
        "    if n % 5 == 0:\n        return 'Buzz'\n    return str(n)\n",
        "from solution import fizzbuzz\n\ndef test_fb():\n"
        "    assert fizzbuzz(15) == 'FizzBuzz'\n    assert fizzbuzz(3) == 'Fizz'\n"
        "    assert fizzbuzz(5) == 'Buzz'\n    assert fizzbuzz(7) == '7'\n",
        (
            "def fizzbuzz(n):\n    if n % 5 == 0:\n        return 'Buzz'\n"
            "    if n % 3 == 0:\n        return 'Fizz'\n    return str(n)\n",
            "def fizzbuzz(n):\n    if n % 15 == 0:\n        return 'Fizz'\n"
            "    if n % 3 == 0:\n        return 'Fizz'\n"
            "    if n % 5 == 0:\n        return 'Buzz'\n    return str(n)\n",
            "def fizzbuzz(n):\n    if n % 15 == 0:\n        return 'FizzBuzz'\n"
            "    if n % 3 == 0:\n        return 'Fizz'\n"
            "    if n % 5 == 0:\n        return 'Buzz'\n    return str(n)\n",
        ),
        expected_iterations=3,
    ),
    _case(
        "dedupe_preserve_order",
        "集合语义",
        "def dedupe(xs):\n    return list(set(xs))\n",
        "from solution import dedupe\n\ndef test_dedupe():\n"
        "    assert dedupe([3, 1, 3, 2, 1]) == [3, 1, 2]\n",
        (
            "def dedupe(xs):\n    return sorted(set(xs))\n",
            "def dedupe(xs):\n    return list(reversed(sorted(set(xs))))\n",
            "def dedupe(xs):\n    seen = []\n    for x in xs:\n"
            "        if x not in seen:\n            seen.append(x)\n    return seen\n",
        ),
        expected_iterations=3,
    ),
    _case(
        "binary_search_bounds",
        "边界条件",
        "def bsearch(xs, t):\n    lo, hi = 0, len(xs)\n"
        "    while lo < hi:\n        mid = (lo + hi) // 2\n"
        "        if xs[mid] == t:\n            return mid\n"
        "        if xs[mid] < t:\n            hi = mid\n"
        "        else:\n            lo = mid + 1\n    return -1\n",
        "from solution import bsearch\n\ndef test_bs():\n"
        "    assert bsearch([1, 3, 5, 7], 5) == 2\n"
        "    assert bsearch([1, 3, 5, 7], 1) == 0\n"
        "    assert bsearch([1, 3, 5, 7], 9) == -1\n",
        (
            "def bsearch(xs, t):\n    lo, hi = 0, len(xs)\n"
            "    while lo < hi:\n        mid = (lo + hi) // 2\n"
            "        if xs[mid] == t:\n            return mid\n"
            "        if xs[mid] < t:\n            lo = mid\n"
            "        else:\n            hi = mid\n    return -1\n",
            "def bsearch(xs, t):\n    lo, hi = 0, len(xs) - 1\n"
            "    while lo < hi:\n        mid = (lo + hi) // 2\n"
            "        if xs[mid] == t:\n            return mid\n"
            "        if xs[mid] < t:\n            lo = mid + 1\n"
            "        else:\n            hi = mid - 1\n    return -1\n",
            "def bsearch(xs, t):\n    lo, hi = 0, len(xs) - 1\n"
            "    while lo <= hi:\n        mid = (lo + hi) // 2\n"
            "        if xs[mid] == t:\n            return mid\n"
            "        if xs[mid] < t:\n            lo = mid + 1\n"
            "        else:\n            hi = mid - 1\n    return -1\n",
        ),
        expected_iterations=3,
    ),
    _case(
        "flatten_depth",
        "递归",
        "def flatten(xs):\n    return [y for x in xs for y in x]\n",
        "from solution import flatten\n\ndef test_flatten():\n"
        "    assert flatten([1, [2, [3, 4]], 5]) == [1, 2, 3, 4, 5]\n",
        (
            "def flatten(xs):\n    out = []\n    for x in xs:\n"
            "        if isinstance(x, list):\n            out += x\n"
            "        else:\n            out.append(x)\n    return out\n",
            "def flatten(xs):\n    out = []\n    for x in xs:\n"
            "        if isinstance(x, list):\n            out += flatten(x)\n"
            "    return out\n",
            "def flatten(xs):\n    out = []\n    for x in xs:\n"
            "        if isinstance(x, list):\n            out += flatten(x)\n"
            "        else:\n            out.append(x)\n    return out\n",
        ),
        expected_iterations=3,
    ),
)


# ---------- 不可修复（模型始终给不出正确实现） ----------
#
# 这些用例验证闭环最危险的失败模式**不会**发生：模型每轮都
# claimed_done=True，但断言从未通过 —— Loop 必须终止在非收敛终态，
# 绝不能因为模型自称完成就判成功。

NEVER_CONVERGES: tuple[RepairCase, ...] = (
    _case(
        "persistent_wrong_answer",
        "假完成",
        "def add(a, b):\n    return a - b\n",
        "from solution import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        (
            "def add(a, b):\n    return a - b\n",
            "def add(a, b):\n    return a * b\n",
            "def add(a, b):\n    return a - b\n",
            "def add(a, b):\n    return abs(a - b)\n",
        ),
        converges=False,
    ),
    _case(
        "syntax_error_loop",
        "假完成",
        "def parse(s):\n    return int(s)\n",
        "from solution import parse\n\ndef test_parse():\n    assert parse('7') == 7\n",
        (
            "def parse(s)\n    return int(s)\n",
            "def parse(s):\nreturn int(s)\n",
            "def parse(s):\n    return int(s\n",
        ),
        converges=False,
    ),
    _case(
        "wrong_signature",
        "假完成",
        "def greet():\n    return 'hi'\n",
        "from solution import greet\n\ndef test_greet():\n"
        "    assert greet('bob') == 'hi bob'\n",
        (
            "def greet():\n    return 'hi bob'\n",
            "def greet():\n    return 'hi'\n",
            "def greet():\n    return 'hello bob'\n",
        ),
        converges=False,
    ),
)


ALL_CASES: tuple[RepairCase, ...] = (
    *ONE_SHOT,
    *TWO_SHOT,
    *THREE_SHOT,
    *NEVER_CONVERGES,
)

REPAIRABLE: tuple[RepairCase, ...] = (*ONE_SHOT, *TWO_SHOT, *THREE_SHOT)


def fenced(source: str, filename: str = "solution.py") -> str:
    """把实现包成模型的自然输出形式（带路径标注的围栏块）。"""
    return (
        f"我已经修好了这个问题。\n\n"
        f"```python path={filename}\n{source}```\n\n"
        f"现在应该可以通过测试了。"
    )


__all__ = [
    "ALL_CASES",
    "NEVER_CONVERGES",
    "ONE_SHOT",
    "REPAIRABLE",
    "THREE_SHOT",
    "TWO_SHOT",
    "RepairCase",
    "fenced",
]

"""真实 LLM 能力验收的缺陷集（未经调参）。

与 loop_cases.py 的区别：
- 不包含 `attempts` 字段（LLM 自由生成）
- 缺陷来自真实项目模式（非人工设计的玩具问题）
- 按难度分层：EASY / MEDIUM / HARD
- reference_fix 仅供人类审阅，不传给 LLM

每个缺陷是一个独立的 Python 模块 + pytest 测试，LLM 需要：
1. 理解测试意图
2. 定位缺陷
3. 给出正确实现
4. 通过所有断言
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CapabilityCase:
    """LLM 能力验收用例。

    与 RepairCase 的区别：无 attempts 字段，LLM 自由生成。
    """

    name: str
    category: str  # 缺陷类型
    difficulty: str  # easy / medium / hard
    filename: str
    buggy: str  # 缺陷代码
    tests: str  # pytest 测试
    reference_fix: str  # 参考修复（仅供人类审阅）


# ========== EASY 难度（5 个）==========

EASY: tuple[CapabilityCase, ...] = (
    CapabilityCase(
        name="off_by_one_range",
        category="边界条件",
        difficulty="easy",
        filename="solution.py",
        buggy="""def count_range(start, end):
    '''返回 [start, end] 范围内整数的个数（包含两端）'''
    return end - start
""",
        tests="""from solution import count_range

def test_count_range():
    assert count_range(1, 5) == 5
    assert count_range(0, 0) == 1
    assert count_range(-2, 2) == 5
""",
        reference_fix="""def count_range(start, end):
    '''返回 [start, end] 范围内整数的个数（包含两端）'''
    return end - start + 1
""",
    ),
    CapabilityCase(
        name="string_concatenation",
        category="类型错误",
        difficulty="easy",
        filename="solution.py",
        buggy="""def build_url(base, path, params):
    '''构造 URL：base + path + ?key=value&...'''
    query = '&'.join([k + '=' + v for k, v in params.items()])
    return base + path + '?' + query
""",
        tests="""from solution import build_url

def test_build_url():
    assert build_url('https://api.com', '/users', {'id': 123, 'active': True}) == \
        'https://api.com/users?id=123&active=True'
    assert build_url('http://localhost', '/search', {'q': 'test', 'limit': 10}) == \
        'http://localhost/search?q=test&limit=10'
""",
        reference_fix="""def build_url(base, path, params):
    '''构造 URL：base + path + ?key=value&...'''
    query = '&'.join([k + '=' + str(v) for k, v in params.items()])
    return base + path + '?' + query
""",
    ),
    CapabilityCase(
        name="list_mutation",
        category="逻辑错误",
        difficulty="easy",
        filename="solution.py",
        buggy="""def remove_duplicates(items):
    '''返回去重后的列表，保持原始顺序'''
    seen = []
    for item in items:
        if item not in seen:
            seen.append(item)
    items.clear()
    items.extend(seen)
    return items
""",
        tests="""from solution import remove_duplicates

def test_remove_duplicates():
    original = [1, 2, 2, 3, 1, 4]
    result = remove_duplicates(original)
    assert result == [1, 2, 3, 4]
    # 原列表不应被修改
    assert original == [1, 2, 2, 3, 1, 4]
""",
        reference_fix="""def remove_duplicates(items):
    '''返回去重后的列表，保持原始顺序'''
    seen = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen
""",
    ),
    CapabilityCase(
        name="empty_list_edge_case",
        category="边界条件",
        difficulty="easy",
        filename="solution.py",
        buggy="""def find_max(numbers):
    '''返回列表中的最大值'''
    max_val = numbers[0]
    for num in numbers:
        if num > max_val:
            max_val = num
    return max_val
""",
        tests="""from solution import find_max

def test_find_max():
    assert find_max([3, 1, 4, 1, 5]) == 5
    assert find_max([-10, -20, -5]) == -5
    assert find_max([42]) == 42
    # 空列表应返回 None 而非崩溃
    assert find_max([]) is None
""",
        reference_fix="""def find_max(numbers):
    '''返回列表中的最大值'''
    if not numbers:
        return None
    max_val = numbers[0]
    for num in numbers:
        if num > max_val:
            max_val = num
    return max_val
""",
    ),
    CapabilityCase(
        name="integer_division_precision",
        category="数值语义",
        difficulty="easy",
        filename="solution.py",
        buggy="""def calculate_average(scores):
    '''计算平均分'''
    return sum(scores) // len(scores)
""",
        tests="""from solution import calculate_average

def test_calculate_average():
    assert calculate_average([80, 90, 100]) == 90.0
    assert calculate_average([75, 85]) == 80.0
    assert calculate_average([100]) == 100.0
""",
        reference_fix="""def calculate_average(scores):
    '''计算平均分'''
    return sum(scores) / len(scores)
""",
    ),
)

# ========== MEDIUM 难度（5 个）==========

MEDIUM: tuple[CapabilityCase, ...] = (
    CapabilityCase(
        name="mutable_default_argument",
        category="Python 语义陷阱",
        difficulty="medium",
        filename="solution.py",
        buggy="""def add_item(item, collection=[]):
    '''将 item 加入集合并返回'''
    collection.append(item)
    return collection
""",
        tests="""from solution import add_item

def test_add_item():
    # 每次调用应该独立
    result1 = add_item('a')
    assert result1 == ['a']

    result2 = add_item('b')
    assert result2 == ['b'], f"Expected ['b'], got {result2}"

    # 显式传入列表应正常工作
    my_list = ['x']
    result3 = add_item('y', my_list)
    assert result3 == ['x', 'y']
""",
        reference_fix="""def add_item(item, collection=None):
    '''将 item 加入集合并返回'''
    if collection is None:
        collection = []
    collection.append(item)
    return collection
""",
    ),
    CapabilityCase(
        name="dictionary_key_error",
        category="异常处理",
        difficulty="medium",
        filename="solution.py",
        buggy="""def get_nested_value(data, keys):
    '''从嵌套字典中获取值，keys 是路径列表'''
    result = data
    for key in keys:
        result = result[key]
    return result
""",
        tests="""from solution import get_nested_value

def test_get_nested_value():
    data = {'user': {'profile': {'name': 'Alice', 'age': 30}}}

    assert get_nested_value(data, ['user', 'profile', 'name']) == 'Alice'
    assert get_nested_value(data, ['user', 'profile', 'age']) == 30

    # 路径不存在应返回 None 而非抛异常
    assert get_nested_value(data, ['user', 'missing']) is None
    assert get_nested_value(data, ['nonexistent', 'path']) is None
""",
        reference_fix="""def get_nested_value(data, keys):
    '''从嵌套字典中获取值，keys 是路径列表'''
    result = data
    for key in keys:
        if not isinstance(result, dict) or key not in result:
            return None
        result = result[key]
    return result
""",
    ),
    CapabilityCase(
        name="sorting_with_none",
        category="边界条件",
        difficulty="medium",
        filename="solution.py",
        buggy="""def sort_with_none_last(items):
    '''排序，None 值放最后'''
    return sorted(items, key=lambda x: (x is None, x))
""",
        tests="""from solution import sort_with_none_last

def test_sort_with_none_last():
    assert sort_with_none_last([3, 1, None, 2, None, 0]) == [0, 1, 2, 3, None, None]
    assert sort_with_none_last([None, None]) == [None, None]
    assert sort_with_none_last([5, 4, 3]) == [3, 4, 5]
    assert sort_with_none_last([]) == []
""",
        reference_fix="""def sort_with_none_last(items):
    '''排序，None 值放最后'''
    return sorted(items, key=lambda x: (x is None, x if x is not None else 0))
""",
    ),
    CapabilityCase(
        name="recursive_sum_termination",
        category="递归错误",
        difficulty="medium",
        filename="solution.py",
        buggy="""def sum_nested_list(items):
    '''递归求和嵌套列表'''
    total = 0
    for item in items:
        if isinstance(item, list):
            total += sum_nested_list(item)
        total += item
    return total
""",
        tests="""from solution import sum_nested_list

def test_sum_nested_list():
    assert sum_nested_list([1, 2, 3]) == 6
    assert sum_nested_list([1, [2, 3], 4]) == 10
    assert sum_nested_list([[1, 2], [3, [4, 5]]]) == 15
    assert sum_nested_list([]) == 0
""",
        reference_fix="""def sum_nested_list(items):
    '''递归求和嵌套列表'''
    total = 0
    for item in items:
        if isinstance(item, list):
            total += sum_nested_list(item)
        else:
            total += item
    return total
""",
    ),
    CapabilityCase(
        name="cache_invalidation",
        category="状态管理",
        difficulty="medium",
        filename="solution.py",
        buggy="""class DataCache:
    def __init__(self):
        self.cache = {}

    def get(self, key):
        return self.cache.get(key)

    def set(self, key, value):
        self.cache[key] = value

    def delete(self, key):
        del self.cache[key]

    def clear(self):
        # 清空所有缓存
        for key in self.cache.keys():
            del self.cache[key]
""",
        tests="""from solution import DataCache

def test_data_cache():
    cache = DataCache()
    cache.set('a', 1)
    cache.set('b', 2)
    cache.set('c', 3)

    assert cache.get('a') == 1

    # clear 应该清空所有项
    cache.clear()
    assert cache.get('a') is None
    assert cache.get('b') is None
    assert cache.get('c') is None
""",
        reference_fix="""class DataCache:
    def __init__(self):
        self.cache = {}

    def get(self, key):
        return self.cache.get(key)

    def set(self, key, value):
        self.cache[key] = value

    def delete(self, key):
        if key in self.cache:
            del self.cache[key]

    def clear(self):
        # 清空所有缓存
        self.cache.clear()
""",
    ),
)

# ========== HARD 难度（3 个）==========

HARD: tuple[CapabilityCase, ...] = (
    CapabilityCase(
        name="lru_cache_eviction",
        category="算法实现",
        difficulty="hard",
        filename="solution.py",
        buggy="""class LRUCache:
    '''固定容量的 LRU 缓存'''
    def __init__(self, capacity):
        self.capacity = capacity
        self.cache = {}
        self.order = []

    def get(self, key):
        if key not in self.cache:
            return None
        # 移到最近使用
        self.order.remove(key)
        self.order.append(key)
        return self.cache[key]

    def put(self, key, value):
        if key in self.cache:
            self.order.remove(key)
        elif len(self.cache) >= self.capacity:
            # 移除最久未使用的
            oldest = self.order[0]
            self.order.pop(0)
            del self.cache[oldest]

        self.cache[key] = value
        self.order.append(key)
""",
        tests="""from solution import LRUCache

def test_lru_cache():
    cache = LRUCache(2)

    cache.put('a', 1)
    cache.put('b', 2)
    assert cache.get('a') == 1

    # 容量满时，应淘汰最久未使用的 'b'
    cache.put('c', 3)
    assert cache.get('b') is None
    assert cache.get('a') == 1
    assert cache.get('c') == 3

    # 再次访问后，'a' 变为最近使用，下次应淘汰 'c'
    cache.get('a')
    cache.put('d', 4)
    assert cache.get('c') is None
    assert cache.get('a') == 1
    assert cache.get('d') == 4
""",
        reference_fix="""from collections import OrderedDict

class LRUCache:
    '''固定容量的 LRU 缓存'''
    def __init__(self, capacity):
        self.capacity = capacity
        self.cache = OrderedDict()

    def get(self, key):
        if key not in self.cache:
            return None
        # 移到最近使用
        self.cache.move_to_end(key)
        return self.cache[key]

    def put(self, key, value):
        if key in self.cache:
            self.cache.move_to_end(key)
        else:
            if len(self.cache) >= self.capacity:
                # 移除最久未使用的
                self.cache.popitem(last=False)

        self.cache[key] = value
""",
    ),
    CapabilityCase(
        name="async_race_condition",
        category="并发错误",
        difficulty="hard",
        filename="solution.py",
        buggy="""import asyncio

class Counter:
    def __init__(self):
        self.value = 0

    async def increment(self):
        current = self.value
        await asyncio.sleep(0.001)  # 模拟异步操作
        self.value = current + 1

    async def get(self):
        return self.value

async def run_concurrent_increments(counter, n):
    '''并发执行 n 次 increment'''
    tasks = [counter.increment() for _ in range(n)]
    await asyncio.gather(*tasks)
    return await counter.get()
""",
        tests="""import asyncio
from solution import Counter, run_concurrent_increments

def test_concurrent_increments():
    '''测试并发计数器'''
    counter = Counter()
    result = asyncio.run(run_concurrent_increments(counter, 10))
    # 并发执行 10 次 increment，最终值应为 10
    assert result == 10, f"Expected 10, got {result}"
""",
        reference_fix="""import asyncio

class Counter:
    def __init__(self):
        self.value = 0
        self.lock = asyncio.Lock()

    async def increment(self):
        async with self.lock:
            current = self.value
            await asyncio.sleep(0.001)  # 模拟异步操作
            self.value = current + 1

    async def get(self):
        return self.value

async def run_concurrent_increments(counter, n):
    '''并发执行 n 次 increment'''
    tasks = [counter.increment() for _ in range(n)]
    await asyncio.gather(*tasks)
    return await counter.get()
""",
    ),
    CapabilityCase(
        name="memory_leak_circular_ref",
        category="资源管理",
        difficulty="hard",
        filename="solution.py",
        buggy="""import weakref

class Node:
    '''双向链表节点'''
    def __init__(self, value):
        self.value = value
        self.prev = None
        self.next = None

    def __del__(self):
        # 追踪析构（用于测试）
        _deleted_nodes.append(self.value)

_deleted_nodes = []

def create_circular_list(values):
    '''创建循环双向链表'''
    nodes = [Node(v) for v in values]
    for i in range(len(nodes)):
        nodes[i].next = nodes[(i + 1) % len(nodes)]
        nodes[i].prev = nodes[(i - 1) % len(nodes)]
    return nodes[0]

def break_circular_list(head):
    '''断开循环引用以允许 GC'''
    if head is None:
        return

    current = head
    visited = set()

    while current and id(current) not in visited:
        visited.add(id(current))
        current = current.next

    # 断开最后一个节点的 next 引用
    if current:
        current.prev.next = None
""",
        tests="""import gc
from solution import create_circular_list, break_circular_list, _deleted_nodes

def test_circular_reference_cleanup():
    '''测试循环引用是否被正确清理'''
    _deleted_nodes.clear()

    # 创建循环链表
    head = create_circular_list([1, 2, 3])

    # 断开循环引用
    break_circular_list(head)

    # 删除所有引用
    del head

    # 强制 GC
    gc.collect()

    # 所有节点应该被析构
    assert sorted(_deleted_nodes) == [1, 2, 3], \
        f"Expected all nodes deleted, got {_deleted_nodes}"
""",
        reference_fix="""import weakref

class Node:
    '''双向链表节点'''
    def __init__(self, value):
        self.value = value
        self.prev = None
        self.next = None

    def __del__(self):
        # 追踪析构（用于测试）
        _deleted_nodes.append(self.value)

_deleted_nodes = []

def create_circular_list(values):
    '''创建循环双向链表'''
    nodes = [Node(v) for v in values]
    for i in range(len(nodes)):
        nodes[i].next = nodes[(i + 1) % len(nodes)]
        nodes[i].prev = nodes[(i - 1) % len(nodes)]
    return nodes[0]

def break_circular_list(head):
    '''断开循环引用以允许 GC'''
    if head is None:
        return

    current = head
    visited = set()

    while current and id(current) not in visited:
        visited.add(id(current))
        next_node = current.next
        # 断开双向引用
        current.prev = None
        current.next = None
        current = next_node
""",
    ),
)

# 按难度组织的完整用例集
ALL_CASES = EASY + MEDIUM + HARD

# 按难度分组
BY_DIFFICULTY = {
    "easy": EASY,
    "medium": MEDIUM,
    "hard": HARD,
}

# 按类别分组
BY_CATEGORY: dict[str, list[CapabilityCase]] = {}
for case in ALL_CASES:
    BY_CATEGORY.setdefault(case.category, []).append(case)


__all__ = [
    "ALL_CASES",
    "BY_CATEGORY",
    "BY_DIFFICULTY",
    "EASY",
    "HARD",
    "MEDIUM",
    "CapabilityCase",
]

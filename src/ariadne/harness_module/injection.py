"""Prompt injection 检测 —— 规范化 + 词法族匹配。

M4-spec 第 153 行把检测方案列为待决策项（判据：红队用例集召回率与 p99 延迟）。
本模块是该决策的落地，选型与否决理由见 docs/M4-spec.md 第 7 节。

两级结构：
1. `normalize()` 把文本折叠到比较用的规范形（零宽剥离 → NFKC → 同形字归一 →
   casefold → 空白折叠），字符级混淆在这里被结构性消掉，不靠枚举短语。
2. 各手法族在规范形上匹配，返回**手法标签列表**（与 `detect_pii` 同形状）。
   返回标签而非布尔：审计日志要能看出命中哪类手法，否则记录没有分诊价值。

误报控制的总原则：**结构信号必须配上意图信号**。角色标记（`system:`）、伪造
边界（`--- END USER INPUT ---`）、越狱词（`unrestricted`）单独出现全部放行 ——
讨论 prompt 工程、粘 YAML 片段都会自然带上它们。同理"泄漏系统提示词"只作
共现信号不单独判定：本平台的正当用途就包含分析和改写 prompt。
"""

from __future__ import annotations

import base64
import binascii
import re
import unicodedata

__all__ = ["INJECTION_TECHNIQUES", "detect_injection", "normalize"]

# 扫描长度上限，与 functions.MAX_REGEX_INPUT_CHARS 同口径，避免超大输入拖慢求值
MAX_SCAN_CHARS = 200_000

# base64 候选串的处理上限。解码是兜底路径，成本必须有界
_MAX_B64_TOKENS = 8

INJECTION_TECHNIQUES = (
    "instruction-override",
    "roleplay-override",
    "delimiter-forgery",
    "obfuscation",
    "encoded-payload",
)

# 零宽与不可见字符：夹在词中间能拆散任何字面短语，且渲染后完全看不出来。
# NFKC 不处理这些（它们是格式字符，不是兼容等价字符），必须显式剥离。
_INVISIBLE_CHARS = "​‌‍‎‏⁠﻿­᠎؜"
_INVISIBLE = str.maketrans(dict.fromkeys(_INVISIBLE_CHARS, ""))

# 同形字：视觉相同但码位不同。NFKC 不做跨字母表折叠（西里尔 о 与拉丁 o
# 不是兼容等价关系），所以要显式建映射。只收最常用的几组 —— 完整
# confusables 表有数千条，收益递减且会引入误折叠。
_HOMOGLYPHS = str.maketrans(
    {
        "а": "a", "е": "e", "о": "o", "р": "p",
        "с": "c", "у": "y", "х": "x", "і": "i",
        "А": "A", "Е": "E", "О": "O", "Р": "P",
        "С": "C", "У": "Y", "Х": "X",
        "α": "a", "ο": "o", "ρ": "p", "υ": "y",
        "Α": "A", "Ε": "E", "Ο": "O", "Ρ": "P",
    }
)


def normalize(text: str) -> str:
    """折叠到比较用的规范形。

    换行折叠成空格：`.{0,20}` 这类邻近窗口默认不跨行，攻击者只要在词间敲
    回车就能断开匹配。折叠后邻近关系按"词距"成立，与是否换行无关。
    """
    if not isinstance(text, str):
        return ""
    if len(text) > MAX_SCAN_CHARS:
        text = text[:MAX_SCAN_CHARS]
    folded = unicodedata.normalize("NFKC", text.translate(_INVISIBLE))
    folded = folded.translate(_HOMOGLYPHS).casefold()
    return re.sub(r"\s+", " ", folded).strip()


# --- 手法族 1：指令覆盖 = 祈使动词 × 指令类宾语 ---------------------------
# 拆成动词与宾语两维而非枚举完整短语：同义空间是开放集合，穷举短语跟不上，
# 但"动词 × 宾语"的笛卡尔积一条正则就能覆盖，且加一个同义词只改一处。

_OVERRIDE_VERB = (
    r"(?:ignor\w{0,10}|disregard\w{0,10}|forget|overrid\w{0,10}|bypass\w{0,10}|circumvent\w{0,10}"
    # 刻意不收 drop：SQL DDL 的高频动词（drop the constraint / drop table），
    # 与「指令类宾语」凑一起的误报率远高于它能救回的召回
    r"|skip|discard|abandon|violat\w{0,10}|disobey|nevermind|never\s+mind"
    # 否定式祈使：语义等价于「忽略」，但不含任何「忽略」类动词
    r"|(?:do\s+not|don't|dont|no\s+longer|stop)\s+"
    r"(?:follow\w{0,10}|obey\w{0,10}|adher\w{0,10}\s+to|comply\w{0,10}\s+with|respect\w{0,10})"
    r"|忽略|无视|忘掉|忘记|跳过|放弃|违反|不要遵[守循]|不再遵[守循]|别管)"
)

# 强宾语：本身就指向"模型的既有指令"，无需限定词。
_TARGET_STRONG = (
    r"(?:system\s+(?:prompt|message|instruction)s?|initial\s+(?:prompt|instruction)s?"
    r"|(?:previous|prior|above|earlier|preceding|original|foregoing)\s+"
    r"(?:instruction|directive|prompt|rule|guideline|constraint|order|command)s?"
    # 「你被告知的一切」：绕开所有指令类名词的语义改写
    r"|everything\s+(?:you\s+(?:were|was|have\s+been)\s+(?:told|instructed|said)"
    r"|(?:above|before|prior))"
    r"|what\s+you\s+(?:were|was|have\s+been)\s+(?:told|instructed)"
    r"|系统提示词?|系统消息|初始(?:指令|提示)|(?:之前|上面|前面|先前|上述|原[来先])"
    r"的?(?:所有)?(?:指令|命令|规则|提示词?|设定|约束|限制)"
    r"|(?:上面|之前|前面)的?(?:一切|所有内容))"
)

# 弱宾语：在开发场景里是日常词（lint rule、约束条件），必须带
# 所属限定（your / the ... above）才算注入信号。
# 「忽略这条 lint 规则」不应被拦，「忽略你的规则」应该。
_TARGET_QUALIFIED = (
    # 限定词只收**自指**的（your/its）。收 `the` 会把 "ignore the rule"、
    # "drop the constraint"、"skip the command" 这类日常开发表述全部误伤 ——
    # 它们在迁移脚本、lint 配置、CI 讨论里随处可见。
    r"(?:(?:your|all\s+your|its)\s+"
    r"(?:instruction|directive|prompt|rule|guideline|constraint|restriction"
    r"|order|command|setting)s?"
    r"|all\s+(?:earlier|previous|prior|above|preceding)\s+\w{0,10}\s*"
    r"(?:instruction|directive|rule|constraint|restriction)s?"
    r"|你的?(?:所有)?(?:指令|命令|规则|提示词?|设定|约束|限制))"
)

# 动词与宾语的最大间隔。给 40 字符：容得下 "ignore all of the previous"
# 这类插入语，又不至于把整段话里两个无关词凑成一次命中。
_GAP = r"[^\n]{0,40}?"

def _compile_pair(pattern: str) -> tuple[re.Pattern[str], re.Pattern[str]]:
    """编译严格版与「词边界宽松」版。

    宽松版把 `\\s+` 降级为 `\\s*`，只用于**已确认含混淆证据**的文本：
    零宽剥离和去空格会把 `previous instructions` 压成 `previousinstructions`，
    严格版的 `\\s+` 匹配不上。但无条件放宽会让 `ignorePreviousInstructions`
    这类驼峰标识符命中 —— 讨论注入防护的正常代码评审里就有这种名字，
    而这条规则的动作是 block/critical，误伤代价高。
    """
    return (
        re.compile(pattern, re.IGNORECASE),
        re.compile(pattern.replace(r"\s+", r"\s*"), re.IGNORECASE),
    )


_INSTRUCTION_OVERRIDE, _INSTRUCTION_OVERRIDE_LOOSE = _compile_pair(
    rf"{_OVERRIDE_VERB}{_GAP}(?:{_TARGET_STRONG}|{_TARGET_QUALIFIED})"
)

# --- 手法族 2：身份改写 -----------------------------------------------------
# 「你现在是 X」类。必须带**时间转折**（now / from now on / 现在 / 从此）：
# 少了它，"you are a helpful assistant" 和 "你是我的助手" 这类正常表述会命中。
_IDENTITY_SHIFT = (
    r"(?:you\s+(?:are|act\s+as|will\s+be|shall\s+be|have)\s+(?:now\s+)?"
    r"|from\s+now\s+on[,\s]+you\s+(?:are|will\s+be)\s+"
    r"|pretend\s+(?:that\s+)?you\s+are\s+"
    # 裸 act as / roleplay as：省掉主语的祈使形式
    r"|(?:act|behave|respond|roleplay|role-play)\s+as\s+(?:a\s+|an\s+|the\s+)?"
    r"|你(?:现在|从现在起|从此|接下来)(?:就)?是|从现在开始[，,]?你是"
    r"|假[设装]你是|扮演)"
)

# 已知越狱人格。这些名字在越狱社区是专有名词，几乎不会在正常对话里
# 以「你现在是 X」的句式出现。
_JAILBREAK_PERSONA = r"(?:dan|aim|stan|dude|kevin|developer\s+mode|do\s+anything\s+now)"

# 「无限制」类声明：越狱提示的标志性措辞。
_UNRESTRICTED = (
    r"(?:(?:no|without|free\s+from|free\s+of)\s+"
    r"(?:restrictions?|limitations?|rules?|filters?|guidelines?|constraints?|ethics)"
    r"|unrestricted|unfiltered|jailbr\w{0,10}|无(?:任何)?(?:限制|约束|过滤)"
    r"|没有(?:任何)?(?:限制|约束|过滤)|不受(?:任何)?(?:限制|约束)|越狱)"
)

# --- 手法族 5：断言式新指令 -------------------------------------------------
# 「your new instruction is...」/「new instructions follow:」—— 不含任何否定
# 或覆盖动词，靠直接宣告新指令生效。名词限定在 instruction/directive/prompt：
# 放宽到 rule/task 会命中「the new rule is to always lint」这类正常表述。
_NEW_INSTRUCTION_CLAIM = re.compile(
    r"(?:your|the|a)?\s*new\s+(?:instruction|directive|system\s+prompt)s?\s*"
    r"(?:is|are|follow\w{0,10}|:|：)"
    r"|(?:你的?)?新(?:的)?(?:指令|命令|系统提示词?)(?:是|如下|为)",
    re.IGNORECASE,
)

# 新身份的角色词：系统/管理员/助手等。命中它才算身份改写，
# 「你现在是在哪个分支上开发」这类问句不会命中。
_AUTHORITY_ROLE = (
    r"(?:(?:new\s+)?(?:system|admin\w{0,10}|root|superuser|developer|operator)"
    r"|新的?(?:系统|助手|管理员|开发者|操作员)|系统管理员|管理员|超级用户)"
)

# 带时间转折的身份改写。角色词那一档必须用这个：
# 「you are the system architect」是正常的角色设定，「you are NOW the system
# admin」才是改写。少了时间限定会把前者一起拦掉。
_IDENTITY_SHIFT_TEMPORAL = (
    r"(?:you\s+(?:are|will\s+be|shall\s+be)\s+now\s+"
    r"|from\s+now\s+on[,\s]+you\s+(?:are|will\s+be)\s+"
    r"|你(?:现在|从现在起|从此|接下来)(?:就)?是|从现在开始[，,]?你是)"
)

_UNRESTRICTED_RE = re.compile(_UNRESTRICTED, re.IGNORECASE)

# 越狱人格与「无限制」声明本身已足够可疑，不要求时间转折。
_PERSONA_CLAIM, _PERSONA_CLAIM_LOOSE = _compile_pair(
    rf"{_IDENTITY_SHIFT}{_GAP}(?:{_JAILBREAK_PERSONA}|{_UNRESTRICTED})"
)

# 权限角色需要时间转折共现。
_AUTHORITY_CLAIM = re.compile(
    rf"{_IDENTITY_SHIFT_TEMPORAL}{_GAP}{_AUTHORITY_ROLE}",
    re.IGNORECASE,
)

# --- 手法族 3：分隔符/角色伪造 ---------------------------------------------
# 伪造对话协议结构，冒充系统消息。对策主体是隔离层（docs/10 第 5 节）——
# 检测层只做纵深防御，因此判定刻意收紧到「结构 + 意图」共现。

# 行首角色标记。限定行首：正文里出现 "system: xxx" 多是在讲配置。
_ROLE_MARKER = re.compile(
    r"(?:^|\n)\s*(?:#{0,3}\s*)?(?:system|assistant|developer)\s*[:：]"
    r"|<\|?(?:im_start|im_end|system|endoftext)\|?>"
    r"|\[(?:system|inst|/inst)\]",
    re.IGNORECASE,
)

# 伪造的输入边界：声称用户输入到此结束，后面的内容属于更高权限层。
_FAKE_BOUNDARY = re.compile(
    r"(?:-{2,}|={2,}|#{2,}|\*{2,})\s*"
    r"(?:end\s+(?:of\s+)?(?:user\s+)?(?:input|prompt|message|context)"
    r"|user\s+input\s+ends?|begin\s+system|system\s+(?:prompt|message)"
    r"|用户输入(?:结束|到此)|以下是?系统)",
    re.IGNORECASE,
)

# 意图信号：索取系统提示词。只作共现信号 —— 本平台的正当用途就包含
# 分析与改写 prompt，单独命中会误伤大量正常请求。
_EXFIL_INTENT = re.compile(
    r"(?:reveal|print|show|repeat|output|disclose|dump|tell\s+me|expose|leak|display)"
    rf"{_GAP}"
    r"(?:your|the|initial|original|full|exact|entire)?\s*"
    r"(?:system\s+)?(?:prompt|instruction|directive|rule|guideline|configuration)s?"
    r"|(?:输出|打印|显示|重复|告诉我|泄[露漏]|复述)[^\n]{0,20}"
    r"(?:系统)?(?:提示词?|指令|规则|配置|设定)",
    re.IGNORECASE,
)

# --- 手法族 4：字符间隔混淆 -------------------------------------------------
_B64_TOKEN = re.compile(r"[A-Za-z0-9+/]{16,}={0,2}")


# --- 成本设界：触发词预扫 + 窗口归约 --------------------------------------
# 各手法族的模式都带邻近窗口与多重选择支，单条在 50KB 上约 1-10ms，八条合计
# 约 0.7ms/KB —— 200KB 输入会到 ~140ms，**超过逐规则 100ms 超时**，而超时是
# fail-closed 判成命中，等于把合法长输入误拦。所以先用一遍廉价扫描找出
# 触发词位置，再只在其邻域上跑完整模式集。
#
# 触发词表由上面同一批模式串**拼接推导**，不手写维护：手写的话，往
# `_OVERRIDE_VERB` 加一个同义词而忘了同步触发表，召回会无声下降，
# 而所有测试仍然通过（因为旧用例的触发词还在表里）。推导保证超集关系。
# 预扫用**纯子串检索**而非正则并集：实测 200KB 上，91 个关键词的 `in` 检索
# 合计 5ms，而等价的正则并集要 160ms（单条完整模式也要 44ms）。Python 的 `re`
# 对巨型选择支没有 Aho-Corasick 之类的优化，把所有模式并起来只会更慢。
_META = re.compile(r"\\[a-zA-Z]|\(\?:|\{\d+(?:,\d*)?\}|[()\[\]|?*+.^$\\]")


def _literal_runs(pattern: str) -> set[str]:
    """从正则源码里抽出字面量片段。

    正则构造（`\\s+`、`(?:`、字符类、量词）全替成分隔符，剩下的连续字面量
    就是预扫关键词。拉丁文取 ≥3 字符、CJK 取 ≥2 —— CJK 词本身就短。
    """
    runs: set[str] = set()
    for part in _META.sub("\x00", pattern).split("\x00"):
        token = part.strip(" -:：<>/{},")
        if not token:
            continue
        is_cjk = any(ord(ch) > 0x2E80 for ch in token)
        if len(token) >= (2 if is_cjk else 3):
            runs.add(token.casefold())
    return runs


# 关键词由各模式串**推导**而来，不手写维护：手写的话，往 `_OVERRIDE_VERB`
# 加一个同义词而忘了同步预扫表，召回会无声下降 —— 旧用例的关键词还在表里，
# 所有测试照样通过。推导保证「模式能匹配的串，必含至少一个关键词」。
_PREFILTER_KEYWORDS: tuple[str, ...] = tuple(
    sorted(
        set().union(
            *(
                _literal_runs(p)
                for p in (
                    _OVERRIDE_VERB,
                    _TARGET_STRONG,
                    _TARGET_QUALIFIED,
                    _IDENTITY_SHIFT,
                    _IDENTITY_SHIFT_TEMPORAL,
                    _JAILBREAK_PERSONA,
                    _UNRESTRICTED,
                    _AUTHORITY_ROLE,
                    _EXFIL_INTENT.pattern,
                    _NEW_INSTRUCTION_CLAIM.pattern,
                    _ROLE_MARKER.pattern,
                    _FAKE_BOUNDARY.pattern,
                )
            )
        )
    )
)

# 触发词邻域窗口。动词最长约 20 字符、_GAP 40、宾语约 40 —— 前后各留
# 这个量足以容下任何单次匹配。
_WINDOW_BEFORE = 120
_WINDOW_AFTER = 200
# 归约后的总长度预算。按长度而非窗口数设界，成本上界才与文本无关：
# 24KB 上跑完整模式集约 17ms，留足 100ms 逐规则超时的余量。
_REDUCED_BUDGET = 24_000
# 每个关键词最多取的命中数。避免某个高频词（"prompt"、"rule"）独占预算，
# 把其他关键词的邻域挤出扫描范围
_HITS_PER_KEYWORD = 12
# 走归约的长度门槛。低于它全扫更快（归约要先跑一遍关键词检索）
_REDUCE_THRESHOLD = 4_000
# 混淆路径的输入上限，见 detect_injection 里的说明
_OBFUSCATION_BUDGET = 24_000


def _has_keyword(text: str) -> bool:
    """预扫。关键词是各模式的推导超集，全不命中即不可能匹配任何模式。"""
    return any(kw in text for kw in _PREFILTER_KEYWORDS)


def _bounded(text: str) -> str:
    """短文本原样返回，长文本归约到关键词邻域 —— 让扫描成本与长度解耦。"""
    return text if len(text) <= _REDUCE_THRESHOLD else _reduce_to_windows(text)


def _reduce_to_windows(normalized: str) -> str:
    """把长文本归约成触发词邻域的拼接，成本上界与文本长度解耦。

    窗口间用换行分隔：各模式的 `_GAP` 是 `[^\\n]{0,40}`，不跨行，因此拼接
    不会把窗口 A 末尾的动词和窗口 B 开头的宾语凑成一次假匹配。
    """
    positions: list[int] = []
    for keyword in _PREFILTER_KEYWORDS:
        start = 0
        for _ in range(_HITS_PER_KEYWORD):
            idx = normalized.find(keyword, start)
            if idx < 0:
                break
            positions.append(idx)
            start = idx + len(keyword)
    if not positions:
        return ""
    positions.sort()
    spans: list[tuple[int, int]] = []
    total = 0
    for pos in positions:
        start = max(0, pos - _WINDOW_BEFORE)
        end = min(len(normalized), pos + _WINDOW_AFTER)
        if spans and start <= spans[-1][1]:
            grown = max(spans[-1][1], end)
            total += grown - spans[-1][1]
            spans[-1] = (spans[-1][0], grown)
        else:
            spans.append((start, end))
            total += end - start
        if total >= _REDUCED_BUDGET:
            break
    return "\n".join(normalized[s:e] for s, e in spans)


def _looks_spaced_out(normalized: str) -> bool:
    """判断是否「逐字符插空格」式混淆。

    先判形态再去空格扫描，而不是无条件去空格：去空格会把相邻词粘成新词，
    在正常文本上凭空造出匹配（"...ignore. Previous instructions were..."
    粘起来就成了字面短语）。要求单字符词占多数才认定为混淆。
    """
    tokens = normalized.split(" ")
    if len(tokens) < 6:
        return False
    singles = sum(1 for t in tokens if len(t) == 1)
    return singles >= 6 and singles >= len(tokens) * 0.5


def _decode_b64_candidates(text: str) -> list[str]:
    """抽取并解码 base64 候选串。

    在**原文**上抽取而非规范形：base64 大小写敏感，casefold 会破坏载荷。
    """
    out: list[str] = []
    for token in _B64_TOKEN.findall(text[:MAX_SCAN_CHARS])[:_MAX_B64_TOKENS]:
        padded = token + "=" * (-len(token) % 4)
        try:
            raw = base64.b64decode(padded, validate=True)
        except (binascii.Error, ValueError):
            continue
        try:
            decoded = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        # 解码出乱码（哈希、二进制）没有扫描价值
        if decoded.isprintable():
            out.append(decoded)
    return out


def detect_injection(text: str) -> list[str]:
    """检测 prompt injection，返回命中的手法标签（去重排序）。

    与 `detect_pii` 同形状：规则侧写 `detect_injection(input.text).size() > 0`。
    """
    if not isinstance(text, str) or not text:
        return []
    normalized = normalize(text)
    found: set[str] = set()
    # 预扫：不含任何关键词就不可能命中任何模式（关键词是模式的推导超集），
    # 跳过模式扫描。长文本绝大多数走这条路 —— 200KB 上 19ms 而非 160ms。
    # 只跳过模式扫描而不直接返回：后面两条路径（混淆、base64）各有自己的
    # 文本形态，在规范形上找不到关键词不代表它们也找不到。
    if _has_keyword(normalized):
        found = _scan(_bounded(normalized))
    # 混淆路径：词边界已被破坏，严格模式匹配不上，只在有混淆证据时放宽。
    # 预扫要在**去空格形**上重做：逐字母插空格的文本，规范形里
    # 连 "ignor" 都不成立，用规范形预扫会把这条路径挡掉。
    if any(ch in text for ch in _INVISIBLE_CHARS) or _looks_spaced_out(normalized):
        # 截断而非归约：这条路径的输入是「去掉全部空白后的文本」，逐字母混淆的
        # 载荷天然短（24KB 去空格内容对应 48KB+ 源文本）。不截断的话 200KB 全
        # 间隔字符的输入要多花 40ms，逼近 100ms fail-closed 线。
        # 已知缺口：混淆载荷落在这个窗口之外会漏，见 docs/M4-spec 第 7 节。
        target = _bounded(normalized.replace(" ", "")[:_OBFUSCATION_BUDGET])
        if _INSTRUCTION_OVERRIDE_LOOSE.search(target) or _PERSONA_CLAIM_LOOSE.search(target):
            found.add("obfuscation")
    # 编码载荷是兜底路径：表层已命中就不必解码，省掉常见情况的开销
    if not found:
        for decoded in _decode_b64_candidates(text):
            if _scan(normalize(decoded)):
                found.add("encoded-payload")
                break
    return sorted(found)


def _scan(normalized: str) -> set[str]:
    """在规范形上跑各手法族。分离出来供 base64 解码后复用。"""
    found: set[str] = set()
    if _INSTRUCTION_OVERRIDE.search(normalized):
        found.add("instruction-override")
    if _PERSONA_CLAIM.search(normalized) or _AUTHORITY_CLAIM.search(normalized):
        found.add("roleplay-override")
    if _NEW_INSTRUCTION_CLAIM.search(normalized):
        found.add("instruction-override")
    if _is_delimiter_forgery(normalized):
        found.add("delimiter-forgery")
    return found


def _is_delimiter_forgery(normalized: str) -> bool:
    """分隔符伪造判定，分两档强度。

    伪造边界（`--- END USER INPUT ---`）单独成立：没有正当用途，正常文本
    不会声明"用户输入到此结束"。行首角色标记（`system:`）只作弱信号，
    需要意图共现 —— 它在 YAML、日志、prompt 模板讨论里都是常客。
    """
    if _FAKE_BOUNDARY.search(normalized):
        return True
    if not _ROLE_MARKER.search(normalized):
        return False
    return bool(_EXFIL_INTENT.search(normalized)) or bool(_UNRESTRICTED_RE.search(normalized))

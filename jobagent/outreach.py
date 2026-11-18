"""首条联系消息的生成器（对外话术的唯一出口）。

要解决的问题（用户 2026-10-17 任务书）：不同岗位收到的是同一段话，开头、技术名词
和结尾提问高度重复。这里的做法：

1. 先理解这一条 JD 想解决什么，再从"能对上的那一到两条"里挑一个切入口，
   一条消息最多两个优势点；
2. 开头按"结构"而不是按固定句子变化：有时从 JD 原话进，有时先讲做出来的东西，
   有时先给一个定位；同一批消息的开头指纹不重复；
3. 结尾三种落点轮换：问一个跟这条 JD 有关的具体问题、直接给一个可验证的动作、
   或者只表达意愿；固定提问（"你们现在最急的是哪一块"这类）不再出现；
4. 长度按 50—120 字（按读起来的长度算）生成，太长了先砍第二个优势点；
5. 只有"有证据"的才说做过；会做但没做过的说"能做"；没有任何证据的说"先做一版验证"，
   绝不把待验证说成已具备。

生成结果会自带依据（用了哪条 JD 原话、哪条证据、什么轨道），方便审计和回归对比。
"""

from __future__ import annotations

import hashlib
import random
import re
from typing import Any

from . import pursuit as pursuit_mod
from .greeter import (
    MGMT_SPEAK, SPEAK, SPEAK_SHORT, _quote_from_jd, _shorten_quote, short_jd_pick,
    pick_spoken_for, speakable_capabilities, spoken_line,
)
from .msgquality import (
    BANNED_QUESTIONS, bigrams, char_len, dejargon, head_key, similarity, similarity_sets,
)
from .profile import Profile

# ---------------------------------------------------------------------------
# 开头：按结构分家族，避免"每条都从同一句话开始"
# ---------------------------------------------------------------------------

GREETINGS = ("你好，", "您好，", "你好，", "")

# 开头的小引子：一句带过"我怎么看到这个岗的"，然后直接说做成过什么。
# 不再用"这块我是做过的：…"这类固定引子——十几家都这么开头，一眼就是群发。
CONNECTORS = (
    "",
    "看到你们招{role}。",
    "刷到你们这个岗。",
    "看到你们在做{dir}这块。",
    "这个岗跟我做的事挺像。",
    "你们招的这个方向我一直在做。",
    "你们这个岗我看了下。",
    "看到你们招{role}。",
    "看了下你们的岗位。",
    "正好看到你们在招{dir}。",
    "你们这个{dir}的岗我挺感兴趣。",
    "这个方向跟我平时做的重叠不少。",
    "你们这个岗，我直接说做过的部分。",
    "这几件事我平时就在做。",
    "看下来，你们要的东西跟我手里的项目对得上。",
)

# 对方的 JD 里点到这些东西时，把话头勾到他们身上（"你们用 MySQL 的话…"），
# 这是让每条消息看起来是"给这家写的"最直接的一招。
JD_ANCHORS: dict[str, tuple[tuple[re.Pattern[str], str], ...]] = {
    "Python 应用与脚本开发": ((re.compile(r"(mysql|sqlite|postgres)", re.I), "你们用 {v} 的话"),
                            (re.compile(r"(django|fastapi|flask)", re.I), "你们用 {v} 的话")),
    "大模型 API 与提示词工程": ((re.compile(r"(deepseek|qwen|豆包|gpt|claude|通义)", re.I), "你们用 {v} 的话"),
                             (re.compile(r"(大模型|llm)", re.I), "你们要做大模型这块的话")),
    "Agent 与工作流编排": ((re.compile(r"(dify|coze|n8n)", re.I), "你们在 {v} 上做的话"),
                         (re.compile(r"(agent|智能体)", re.I), "你们做 Agent 的话")),
    "RAG 与检索": ((re.compile(r"(知识库|向量库|检索)", re.I), "你们要做知识库的话"),),
    "评测与数据质量": ((re.compile(r"(标注|评测|质检)", re.I), "你们做标注评测的话"),),
    "浏览器自动化与采集": ((re.compile(r"(流程自动化|自动化流程|重复性业务流程)", re.I),
                          "你们要把流程做自动化的话"),
                         (re.compile(r"(采集|爬虫|抓取)", re.I), "你们要采数据的话")),
    "前端页面基础": ((re.compile(r"(vue)", re.I), "你们用 {v} 的话"),
                     (re.compile(r"(react)", re.I), "你们用 {v} 的话"),
                     (re.compile(r"(小程序)", re.I), "你们做小程序的话")),
    "业务与数据理解": ((re.compile(r"(订单|库存|对账|进销存)", re.I), "你们做{v}这块的话"),),
    "训练数据与语料质量": ((re.compile(r"(训练数据|训练语料|语料)", re.I), "你们要做训练数据的话"),
                          (re.compile(r"(数据集|数据清洗)", re.I), "你们要做{v}的话")),
}

ROLE_NOISE = re.compile(r"(急招|急聘|诚聘|招聘|双休|包吃住|可培养|无经验可投|应届|高薪|长期有效|"
                        r"五险一金|月休|大小周|兼职|全职|实习|双休日|经验|优先|以上|不限|"
                        r"可投|包住|急|底薪\s*\d+|\d+\s*[kK千])+")

# 只有这些方向说起来像"岗位"（"看到你们在招前端"），
# 其余方向（ERP/方案/数据评测）配上"招"就变成病句，干脆不给它们这条引子
DIRECTION_AS_ROLE = {"Agent", "前端", "后端", "全栈", "数据采集", "流程自动化", "大模型应用"}

# 岗位类型 → 聊天里怎么称呼这个岗（不引 JD 原文，"你们写的「…」"读起来像表单）
ROLE_OF_TYPE = {
    "AI Agent / 智能体": "Agent",
    "AI 应用 / 大模型": "大模型应用",
    "AI 客服 / 对话系统": "客服系统",
    "RPA / 流程自动化": "流程自动化",
    "ERP / 业务系统": "ERP",
    "前端": "前端",
    "后端 / 接口": "后端",
    "全栈": "全栈",
    "数据 / 评测 / 标注": "数据评测",
    "采集 / 爬虫": "数据采集",
    "技术方案 / 售前": "方案",
    "自动化 / 交付": "自动化交付",
}


def _evidenced_phrase(capability: str, grade: str, *, variant: int = 0) -> str:
    """有证据时怎么说：直接讲做成过什么，不加"这块我是做过的"这种引子。"""
    return spoken_line(capability, variant)


def _pick_spoken(capability: str, used: dict[str, int] | None, *, offset: int = 0) -> str:
    """同一项能力有多句口语说法：优先用这批里还没用过的，避免几家收到同一句。"""
    from .greeter import SPOKEN

    variants = SPOKEN.get(capability)
    if not variants:
        return SPEAK_SHORT.get(capability, "") or SPEAK.get(capability, "")
    used = used or {}
    ranked = sorted(range(len(variants)),
                    key=lambda i: (used.get(variants[i], 0), (i - offset) % len(variants)))
    return variants[ranked[0]]


def _capability_pressure(capability: str, used: dict[str, int] | None) -> int:
    """这项能力的说法在这批里已经被用掉多少：用来"轮着讲不同的能力"。"""
    from .greeter import SPOKEN, SPEAK_SHORT, SPEAK

    variants = SPOKEN.get(capability)
    if not variants:
        single = SPEAK_SHORT.get(capability, "") or SPEAK.get(capability, "")
        return (used or {}).get(single, 0)
    used = used or {}
    return min(used.get(line, 0) for line in variants)


def _least_used(lines: tuple[str, ...] | list[str], used: dict[str, int] | None) -> str:
    """在一组现成说法里挑用得最少的那句。"""
    candidates = [line for line in lines if line]
    if not candidates:
        return ""
    used = used or {}
    return min(candidates, key=lambda line: (used.get(line, 0), len(line)))


def _capable_phrase(quote: str, speak: str) -> str:
    return speak


def _assisted_phrase(quote: str) -> str:
    """没有证据项时的说法：只说能上手做，不引 JD 原话（引了像在念表单）。"""
    return "这类活我上手就能做，需求清楚的话很快能出一版"


ASSISTED_LINES = (
    "这类活我上手就能做，需求清楚的话很快能出一版",
    "这类活我能接，先把范围说清楚，我按小段做给你看",
    "你们要的这类东西我做过同类的，换个场景接着用就行",
    "这块我按能上手的做法来：先拆开，一段一段验",
)


# 岗位类型 → 最该先讲的那项能力（避免所有岗位张口都是"我写过 Python"）
TYPE_CAPABILITY: dict[str, str] = {
    "AI Agent / 智能体": "Agent 与工作流编排",
    "AI 应用 / 大模型": "大模型 API 与提示词工程",
    "AI 客服 / 对话系统": "RAG 与检索",
    "RPA / 流程自动化": "浏览器自动化与采集",
    "采集 / 爬虫": "浏览器自动化与采集",
    "数据 / 评测 / 标注": "评测与数据质量",
    "前端": "前端页面基础",
    "全栈": "前端页面基础",
    "后端 / 接口": "Python 应用与脚本开发",
    "ERP / 业务系统": "业务与数据理解",
    "技术方案 / 售前": "Agent 与工作流编排",
}

# 能力 → 领域标签：第二句话不能跟第一句讲同一个领域
CAPABILITY_AREA: dict[str, str] = {
    "Python 应用与脚本开发": "code",
    "大模型 API 与提示词工程": "llm",
    "Agent 与工作流编排": "llm",
    "RAG 与检索": "llm",
    "评测与数据质量": "data",
    "浏览器自动化与采集": "code",
    "测试与工程规范": "code",
    "前端页面基础": "front",
    "图像批处理与规则检查": "data",
    "业务与数据理解": "data",
}


def _lead_of(hook: dict[str, Any]) -> str:
    """这条切入口的"话头"（用于判断批内是不是又用了同一句开场）。"""
    lead = str(hook.get("lead") or "")
    if lead:
        return lead
    phrase = str(hook.get("phrase") or "")
    return phrase.split("：")[0][:8]


def _quotable_task(text: str) -> bool:
    """这段 JD 原文能不能当引文：要是工作内容，不是学历/年限/福利那类。"""
    from .analyze import JUNK_ZH, SKILL_HINT
    from .greeter import QUOTE_JUNK, QUOTE_HEADING

    text = (text or "").strip()
    if not (6 <= len(text) <= 60):
        return False
    if QUOTE_JUNK.search(text) or JUNK_ZH.search(text) or QUOTE_HEADING.search(text):
        return False
    return bool(SKILL_HINT.search(text))


def _assisted_quote(text: str, *, limit: int = 14) -> str:
    """给兜底说法挑一句读得顺的 JD 片段。

    要求句常常很长（"熟悉人工智能基本原理与模型训练"），直接按字数截会截成
    "…与模型训…"这种半个词。这里先按顿号/与和及切开，取一段完整的短句。
    """
    from .greeter import QUOTE_LEAD

    cleaned = QUOTE_LEAD.sub("", (text or "").strip()).strip("，,。；;、 ")
    if not cleaned:
        return ""
    for chunk in re.split(r"[、，,；;：:]+|(?:与|和|及)", cleaned):
        chunk = chunk.strip()
        if 3 <= len(chunk) <= limit and _quotable_task(chunk):
            return chunk
    if 3 <= len(cleaned) <= limit and _quotable_task(cleaned):
        return cleaned
    shortened = _shorten_quote(cleaned, limit)
    # 兜底截断会截出"…与模型训…"这种半个词：宁可这句不引，也不发半句话
    return shortened if len(shortened) >= 4 and not shortened.endswith("…") else ""


# ---------------------------------------------------------------------------
# 结尾：问 JD 自己的问题，或给一个能被验证的动作
# ---------------------------------------------------------------------------

# 每个问题族：(问句, 跟一句自己的做法)。问句只留一个问号，附加说明跟在后面。
QUESTION_FAMILIES: dict[str, list[tuple[str, str]]] = {
    "frontend": [("你们现在用的是 Vue 还是 React", ""),
                 ("前端是要改现有页面，还是新做一个模块", "")],
    "backend": [("后端用的是 FastAPI、Flask 还是别的", ""),
                ("接口是新做，还是接现有的", "")],
    "data": [("数据现在落在 MySQL 还是别的库里", ""),
             ("数据是你们自己维护，还是从别的系统同步过来", "")],
    "crawl": [("要采的是公开页面，还是登录后的后台数据", "登录的我只读取数"),
              ("这个数据大概多久跑一次", "定时和失败重试我可以一起做上")],
    "service": [("现在问题分类是人工定，还是已经有规则了", ""),
                ("你们是想先把答疑做起来，还是先整理口径", "")],
    "rpa": [("手上的流程是维护现成的，还是从零搭", "两种都行"),
            ("你们最想先自动化掉哪一段", "")],
    "erp": [("你们用的是金蝶、用友，还是自研那套", ""),
            ("是在现有系统上加功能，还是重搭一套", "")],
    "agent": [("模型是自己调接口，还是走内部平台", "两种我都写过"),
              ("先把一条流程跑通，还是做完整套", ""),
              ("这个岗现在最想先补的是开发、部署还是场景落地", "")],
    "rag": [("知识库里的文档大概多少，谁在维护", ""),
            ("找不到依据的时候，你们希望怎么处理", "我的做法是直接说不知道")],
    "eval": [("标的是哪类数据，一天大概多少量", ""),
             ("现在质检是抽检还是全检", "两种我都跑过")],
    "presale": [("方案主要是给客户看，还是内部立项用", ""),
                ("需求现在是谁在提", "")],
    "deploy": [("是跑在你们服务器上，还是本机也要一份", ""),
               ("上线是你们自己运维，还是要一起跟", "")],
    "test": [("用例现在是手跑为主，还是已经有自动化了", "我可以接着写"),
             ("测试环境是独立的，还是跟开发共用", "")],
    "dataset": [("训练数据这块是谁在整理，一天大概多少量", ""),
                ("语料是你们自己攒的，还是外面买的", "两条我都处理过")],
}

# 能力 → 问题族（先按这条消息真正讲的那项能力问，问得才对得上）
CAPABILITY_QUESTIONS = {
    "前端页面基础": "frontend",
    "Python 应用与脚本开发": "backend",
    "业务与数据理解": "data",
    "浏览器自动化与采集": "crawl",
    "RAG 与检索": "rag",
    "大模型 API 与提示词工程": "agent",
    "Agent 与工作流编排": "agent",
    "评测与数据质量": "eval",
    "测试与工程规范": "test",
    "图像批处理与规则检查": "eval",
    "训练数据与语料质量": "dataset",
}

# 标题里出现这些词，用对应的问题族（按标题而不是正文判断，避免"JD 里提了一句页面"就往前端问）
TITLE_QUESTIONS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(采集|爬虫|抓取)", re.I), "crawl"),
    (re.compile(r"(评测|标注|数据质量)", re.I), "eval"),
    (re.compile(r"(训练师|训练数据|训练语料|语料|数据集)", re.I), "dataset"),
    (re.compile(r"(rpa|流程自动化|影刀|云扩|uipath)", re.I), "rpa"),
    (re.compile(r"(erp|金蝶|用友|odoo|进销存)", re.I), "erp"),
    (re.compile(r"(客服|工单|对话|问答)", re.I), "service"),
    (re.compile(r"(agent|智能体|工作流|dify|coze|n8n)", re.I), "agent"),
    (re.compile(r"(方案|售前|解决方案)", re.I), "presale"),
    (re.compile(r"(全栈|前后端)", re.I), "frontend"),
    (re.compile(r"(前端|vue|react|小程序)", re.I), "frontend"),
    (re.compile(r"(后端|服务端|接口)", re.I), "backend"),
    (re.compile(r"(测试|qa)", re.I), "test"),
    (re.compile(r"(运维|部署|docker)", re.I), "deploy"),
    (re.compile(r"(知识库|rag|检索)", re.I), "rag"),
    (re.compile(r"(ai 应用|大模型|llm)", re.I), "agent"),
    (re.compile(r"(自动化|脚本|流程)", re.I), "rpa"),
    (re.compile(r"(python|开发|工程师)", re.I), "backend"),
]

GENERIC_QUESTIONS = [
    ("这个岗是从零做，还是接手现有的", "两种都行"),
    ("你们现在最卡的是哪一步", ""),
    ("验收标准是你们定，还是双方一起定", ""),
    # 2026-10-19 补：HR 看到消息不回的常见原因是问句太技术，他答不上来。
    # 这几条是 HR 一句话就能回的，轮换着用。
    ("你们团队现在几个人，最缺哪一块", ""),
    ("这个岗是长期招，还是先把这一块补上", ""),
]

# 不给问题，直接给一个能被验证的动作（有时比提问更能推动对话）
ACTION_ENDINGS = [
    "先做个小样给你看，好不好用你一看就知道。",
    "你把场景说一下，我先跑一版发你。",
    "我这边随时能跑一版，结果直接给你看。",
    "有现成的样例给我一份，我先跑通一段。",
    "需求你要是方便说细一点，我今天就能出一版。",
    "先跑一小段验证思路，跑完再往下加。",
    "你把最难的那步说给我，我先试试能不能跑通。",
    "方便的话给我一份真实数据，我拿它跑一版。",
    "我先把流程画出来发你，你看对不对得上。",
    "要是需要，我可以先做个能点的demo给你。",
    # 2026-10-19 补：给 HR 一个不用懂技术也能推进的动作——转给技术看。
    "我把项目结构和跑法整理一份发你，你转给技术看就行。",
    "我先做个小样，你转给技术看看合不合适。",
    "你把岗位的技术栈发我，我按那个准备一版。",
]

# 只表达意愿 + 明确下一步，不提问
STATEMENT_ENDINGS = [
    "合适的话聊聊，我时间好安排。",
    "你们要人就开始，我按你们的节奏来。",
    "方便的话约一下，我演示一遍给你看。",
    "我这边的活能随时开始，你们定节奏。",
    "有需要的话我先把东西整理一份发你。",
    "你们在招人就行，剩下我配合。",
    "随时可以聊，我在泉州，远程也行。",
    "不着急的话也可以先看看我做的东西。",
]

QUESTION_PREFIX = ("", "顺便问下：", "先问一句：")

# 招聘专员/HR 通常答不了太深的实现问题。先把决定权留给他们能回的内容；
# 真正的技术栈和实现路径，等对方转给负责人后再问。
RECRUITER_QUESTIONS = [
    ("这个岗现在最想先补的是开发、部署还是场景落地", ""),
    ("新人进来是跟着现有项目做，还是独立负责一块", ""),
    ("这个岗是团队扩编，还是补一个具体缺口", ""),
]

RECRUITER_ROLE = re.compile(
    r"(^|[/·\s])(hr|hrbp|hrm|招聘|人事|人才|猎头|recruit)",
    re.I,
)
DECISION_ROLE = re.compile(
    r"(老板|创始人|ceo|cto|coo|负责人|总监|合伙人|主管|经理|架构师|技术)",
    re.I,
)


def recipient_persona(job: Any) -> str:
    """按招聘人的身份决定提问深浅：HR 能转发，负责人能判断实现。"""
    get = (lambda key, default="": job[key] if not isinstance(job, dict) else job.get(key, default))
    title = re.sub(r"\s+", " ", str(get("hr_title") or "")).strip()
    if not title:
        return "unknown"
    if RECRUITER_ROLE.search(title):
        return "recruiter"
    if DECISION_ROLE.search(title):
        return "decision_maker"
    return "unknown"


def _ending_question(job: Any, capability: str, *, used_questions: set[str],
                     family_hint: str = "", persona: str = "unknown") -> str:
    """按这条消息讲的那件事挑问句：讲了什么就问什么，问不到再按标题，最后通用。

    "讲部署却问你们定不定节奏"这种错位，就是因为问句只按岗位标题选，
    没跟正文里真正在讲的那项能力对上。
    """
    get = (lambda key, default="": job[key] if not isinstance(job, dict) else job.get(key, default))
    title = str(get("title") or "")
    if persona == "recruiter":
        for question, remark in RECRUITER_QUESTIONS:
            if question not in used_questions:
                return (question, remark)
        return RECRUITER_QUESTIONS[0]
    families: list[str] = []
    if family_hint:
        families.append(family_hint)
    if capability:
        mapped = CAPABILITY_QUESTIONS.get(capability)
        if mapped and mapped not in families:
            families.append(mapped)
    for pattern, family in TITLE_QUESTIONS:
        if not pattern.search(title):
            continue
        if family not in families:
            families.append(family)
        break   # 只取最具体的那个：否则"…工程师"会被最后的通配式也算成后端
    for family in families:
        for question, remark in QUESTION_FAMILIES.get(family, []):
            if question not in used_questions and question not in BANNED_QUESTIONS:
                return (question, remark)
    # 属于这个岗位的问法都用过了：退回通用问句，别串到别的岗位类型的问法上
    for question, remark in GENERIC_QUESTIONS:
        if question not in used_questions and question not in BANNED_QUESTIONS:
            return (question, remark)
    for question, remark in GENERIC_QUESTIONS:
        if question not in BANNED_QUESTIONS:
            return (question, remark)
    return ("你们现在最想让这个岗位先解决什么问题", "")


# ---------------------------------------------------------------------------
# 生成
# ---------------------------------------------------------------------------


def _hook_candidates(job: Any, analysis: dict[str, Any], profile: Profile,
                     pursue: dict[str, Any], *, variant: int = 0,
                     used_lines: dict[str, int] | None = None) -> list[dict[str, Any]]:
    """把可说的切入口按"最该先说"的顺序排出来，最多取两个。"""
    get = (lambda key, default="": job[key] if not isinstance(job, dict) else job.get(key, default))
    hooks: list[dict[str, Any]] = []
    matched = [item for item in (analysis.get("matched") or []) if item.get("capability")]
    title = str(get("title") or "")
    wanted = TYPE_CAPABILITY.get(str(pursue.get("type") or ""), "")
    speakable = speakable_capabilities()

    def rank(item: dict[str, Any]) -> tuple[int, int, int, int]:
        """先看岗位标题和类型点到哪项能力，再看证据等级，最后挑句子短的。

        排序键第一位就是"标题里有没有提"：否则 grade=A 的 Python 会盖住
        "AI Agent 岗" 真正该讲的 Agent 那条，所有岗位开头就都变成同一句。
        """
        name = str(item.get("capability") or "")
        cap = profile.capability(name)
        keywords = [str(k).lower() for k in (cap.keywords if cap else [])]
        in_title = 0 if any(kw and kw in title.lower() for kw in keywords) else 1
        in_type = 0 if name and name == wanted else 1
        # 同一批里已经讲过很多次的能力往后排，让"轮着讲不同的事"更均匀
        pressure = 1 if _capability_pressure(name, used_lines) > 0 else 0
        return (in_title, in_type, pressure, 0 if item.get("grade") == "A" else 1,
                len(str(item.get("text") or "")))

    for item in sorted(matched, key=rank):
        capability = str(item.get("capability") or "")
        text = str(item.get("text") or "")
        # 同一句 JD 要求可能同时提到 Python 和大模型，analyze 按命中数只会归到一个能力上。
        # 岗位类型点到的那项能力如果也出现在这句原文里，就按它来说（说的仍是这句 JD）。
        if wanted and wanted != capability and wanted in SPEAK:
            cap = profile.capability(wanted)
            if cap and any(str(kw).lower() in text.lower() for kw in cap.keywords if kw):
                capability = wanted
        # 只讲 A/B 级能力：等级是 C/D 的一律不能对外说（等级表是唯一事实源）
        if capability not in SPEAK or capability not in speakable:
            continue
        quote = _quote_from_jd(text, capability, profile)
        if len(quote) < 4:
            continue
        grade = str(item.get("grade") or "")
        phrase = pick_spoken_for(capability, f"{quote} {text}", used_lines)
        hooks.append({
            "kind": "evidence",
            "capability": capability,
            "grade": grade,
            "quote": quote,
            "phrase": phrase,
            "lead": phrase.split("：")[0],
            "basis": str(item.get("basis") or ""),
        })
        if len(hooks) >= 2:
            break

    if not hooks:
        short = short_jd_pick(job, profile)
        if short:
            hooks.append({
                "kind": "evidence", "capability": short["capability"], "grade": "B",
                "quote": short["quote"],
                "phrase": _pick_spoken(short["capability"], used_lines, offset=variant),
                "basis": "",
            })

    first_area = CAPABILITY_AREA.get(str((hooks[0] if hooks else {}).get("capability") or ""), "")
    for focus in pursue.get("focus") or []:
        if focus.get("kind") == "can_do":
            lines = tuple(focus.get("lines") or ()) or (str(focus.get("speak") or ""),)
            speak = _least_used(lines, used_lines)
            area = _area_of_speak(speak)
            if area and area == first_area:
                continue  # 跟第一句讲同一个领域就不重复说
            quote = str(focus.get("quote") or "")
            hooks.append({"kind": "can_do", "capability": "", "grade": "",
                          "quote": quote if len(quote) >= 4 else "",
                          "family": str(focus.get("question_family") or ""),
                          "phrase": _capable_phrase(quote, speak),
                          "basis": "按 JD 原话说能做，不算项目经历"})
        elif focus.get("kind") == "mgmt":
            quote = str(focus.get("quote") or "")
            speak = _least_used(tuple(MGMT_SPEAK), used_lines)
            hooks.append({"kind": "mgmt", "capability": "", "grade": "",
                          "quote": quote,
                          "phrase": (f"「{quote}」这块我能顶：{speak}。" if len(quote) >= 4
                                     else f"{speak}。"),
                          "basis": "用户本人经历，照 JD 原话说"})
    if not hooks:
        # 岗位类型点到的那项能力本身就是我们做过的（例如"数据标注/评测"岗），
        # 就算 JD 的句子没写出我们的关键词，也讲这项——这是"发，而且讲得出东西"的底。
        if wanted and wanted in SPEAK and wanted in speakable:
            hooks.append({"kind": "adjacent", "capability": wanted, "grade": "",
                          "quote": "",
                          "phrase": pick_spoken_for(
                              wanted, f"{title} {get('description') or ''}", used_lines),
                          "basis": "岗位类型对应的能力有实物，只说做过的部分"})

    if not hooks:
        # 没有直接命中项时，先找一件"最接近的真实做过的事"（只看 JD 里提到没提到
        # 这项技能的词，不声称做过他们那件事），比说"我有把握"有信息量得多。
        adjacent = _adjacent_capability(job, profile, speakable)
        if adjacent:
            hooks.append({"kind": "adjacent", "capability": adjacent, "grade": "",
                          "quote": "",
                          "phrase": pick_spoken_for(adjacent, str(get("description") or ""),
                                                    used_lines),
                          "basis": "JD 里没直接对上，但这块本身有实物，只讲做过的部分"})
    if not hooks:
        # 兜底说法也要引一句真的像工作内容的 JD 原文（2026-10-17 实测引到过
        # "学历不限…"这种句子），引不到就不引
        task = ""
        for item in pursue.get("considered") or []:
            text = str(item.get("task") or "").strip()
            if item.get("level") in ("可复用能力", "正在补全") and _quotable_task(text):
                task = text
                break
        quote = _assisted_quote(task) if task else ""
        hooks.append({"kind": "ai_assisted", "capability": "", "grade": "",
                      "quote": quote,
                      "phrase": _least_used(ASSISTED_LINES, used_lines),
                      "basis": "本机没有成品证据，不能提做过，只能提试做"})
    # 去重：同一个说法只出现一次，否则消息里会出现两遍一模一样的话
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for hook in hooks:
        key = f"{hook['kind']}|{hook['phrase']}"
        if key in seen:
            continue
        seen.add(key)
        unique.append(hook)
    return unique


def compose_first_contact(
    job: Any,
    analysis: dict[str, Any],
    profile: Profile,
    *,
    pursue: dict[str, Any] | None = None,
    variant: int = 0,
    seed: str = "",
    used_openers: dict[str, int] | None = None,
    used_endings: dict[str, int] | None = None,
    used_questions: dict[str, int] | None = None,
    used_leads: dict[str, int] | None = None,
    used_lines: dict[str, int] | None = None,
    avoid_texts: list[str] | None = None,
    similarity_limit: float = 0.5,
    prefer_ending_kind: str = "",
) -> dict[str, Any]:
    """生成一条首条消息。返回 {text, stopped, reasons, ...}。"""
    get = (lambda key, default="": job[key] if not isinstance(job, dict) else job.get(key, default))
    pursue = pursue or pursuit_mod.assess(job, analysis, profile)
    if pursue.get("decision") == "stop":
        return {"text": "", "stopped": True, "reasons": pursue.get("stops") or [],
                "focus": [], "kind": "", "track": pursue.get("track", ""), "metrics": {}}

    hooks = _hook_candidates(job, analysis, profile, pursue, variant=variant,
                             used_lines=used_lines)
    used_openers = used_openers if used_openers is not None else {}
    used_endings = used_endings if used_endings is not None else {}
    used_leads = used_leads if used_leads is not None else {}
    avoid_texts = [t for t in (avoid_texts or []) if t]
    # 候选很多，逐条切词太慢：先把"这批已经发出去的"2-gram 集合算好
    avoid_sets = [bigrams(text) for text in avoid_texts]
    seed = seed or str(get("external_id") or get("id") or get("title") or "")
    rng = random.Random(hashlib.sha256(f"{seed}#{variant}".encode()).hexdigest())

    track = str(pursue.get("track") or "A")
    greetings = list(GREETINGS)
    # 引子里说岗位名：优先用他挂出来的标题（人在聊天里就是这么提的），
    # 标题整理不干净就换成类型方向，避免"看到你们招数据评测"这种张冠李戴。
    role = _title_role(get("title"))
    direction = ROLE_OF_TYPE.get(str(pursue.get("type") or ""), "")
    connectors: list[str] = []
    for template in CONNECTORS:
        if "{role}" in template and not role:
            continue
        if "{dir}" in template and direction not in DIRECTION_AS_ROLE:
            continue
        connectors.append(template.format(role=role, dir=direction))
    # 能勾上对方 JD 的引子优先（"你们用 MySQL 的话，"），它比通用引子更像专门写给这家
    anchor = _jd_anchor(job, str((hooks[0] or {}).get("capability") or ""))
    if anchor:
        connectors.insert(0, anchor)
    # 招呼语和小引子轮换，避免十几家开头都是同一个样子
    rng.shuffle(greetings)
    rng.shuffle(connectors)

    # 优势点组合：默认只说一条（任务书要求"最多一到两条，不罗列"），
    # 只有当一条太短、撑不起一段话时才加第二条，而且不能跟第一条同领域。
    combos: list[list[dict[str, Any]]] = [[hooks[0]]]
    if len(hooks) > 1:
        first_area = CAPABILITY_AREA.get(str(hooks[0].get("capability") or ""),
                                         _area_of_speak(str(hooks[0].get("phrase") or "")))
        second_area = CAPABILITY_AREA.get(str(hooks[1].get("capability") or ""),
                                          _area_of_speak(str(hooks[1].get("phrase") or "")))
        if second_area != first_area or hooks[1]["kind"] != hooks[0]["kind"]:
            combos.append([hooks[0], hooks[1]])

    endings: list[tuple[str, str]] = []
    primary_capability = str((hooks[0] or {}).get("capability") or "")
    family_hint = str((hooks[0] or {}).get("family") or "")
    if not family_hint and hooks:
        # 没显式族时，看这条消息引的 JD 原话/说法在讲什么，问句跟着它走：
        # 讲"流程自动化"就问流程，讲"采集"就问数据源，别张冠李戴
        # 只看引到的 JD 原话：自己的说法里几乎句句带"流程"，拿它判断会全跑到流程族去
        blob = str(hooks[0].get("quote") or "")
        if re.search(r"(流程自动化|自动化流程|重复性业务流程|RPA)", blob, re.I):
            family_hint = "rpa"
        elif re.search(r"(采集|爬虫|抓取)", blob):
            family_hint = "crawl"
    question, remark = _ending_question(job, primary_capability,
                                       used_questions=set(used_questions or {}),
                                       family_hint=family_hint,
                                       persona=recipient_persona(job))
    prefix = QUESTION_PREFIX[(variant + len(seed)) % len(QUESTION_PREFIX)]
    endings.append(("question", f"{prefix}{question}？" + (f"{remark}。" if remark else "")))
    # 每种落点多给几个候选，批次内才能真的避开已经用过的收尾
    for offset in range(4):
        endings.append(("action", ACTION_ENDINGS[(variant + offset) % len(ACTION_ENDINGS)]))
        endings.append(("statement",
                        STATEMENT_ENDINGS[(variant + offset) % len(STATEMENT_ENDINGS)]))
    # 优先用这批里用得少的落点，避免十几条消息全以提问结尾（用户 2026-10-17）
    if prefer_ending_kind:
        preferred = [item for item in endings if item[0] == prefer_ending_kind]
        if preferred:
            if used_endings:
                unused = [item for item in preferred
                          if not used_endings.get(item[1], 0)]
                if unused:
                    preferred = unused
            endings = preferred
    else:
        rng.shuffle(endings)

    # 涉及线上系统/安全/架构这类岗位：收窄到能独立验证的那一段，
    # 但不写"我不拍胸脯"这种自我降级的说法，改成正面讲交付方式。
    scope_line = ("这种体量的活我习惯先交一段能独立跑通的，跑通一段再往下铺。"
                  if pursue.get("narrow_scope") else "")

    best: dict[str, Any] | None = None
    # 不能用"分数必须大于某个阈值"来判定失败：批内重复惩罚会让所有候选都变负分，
    # 那样会出现"明明有话说却生成不出来"。这里只取最高分，分数再低也照发。
    best_score = float("-inf")
    # 两段式：先用便宜的指标（长度、占用次数）粗筛，再对少量候选做去术语/相似度检查。
    # 一条 JD 会展开上百个候选组合，逐个跑正则会让几十家的批次慢到几分钟。
    rough: list[tuple[float, dict[str, Any]]] = []
    for combo in combos:
        if not combo or not combo[0].get("phrase"):
            continue
        for greeting in greetings:
            for connector in connectors:
                for ending_kind, ending in endings:
                    # 结构：招呼 + 小引子 + 做成过什么（一到两句）+ 一个落点
                    body = "。".join(hook["phrase"] for hook in combo) + "。"
                    raw = _join_parts(greeting, connector, body, scope_line, ending)
                    lead = connector.strip() or "（无引子）"
                    rough_score = 0.0
                    if connector == anchor:
                        rough_score += 1.2   # 勾住对方 JD 的引子优先
                    rough_score -= 2.0 * (used_leads or {}).get(lead, 0)
                    rough_score -= 1.0 * (used_endings or {}).get(ending, 0)
                    rough_score -= 1.0 * (len(combo) - 1)      # 多说一条就多一分罗列感
                    rough_score -= 0.8 * (used_endings or {}).get(ending, 0)
                    approx = len(raw) - raw.count(" ")
                    if 50 <= approx <= 100:
                        rough_score += 1.5
                    elif approx < 50:
                        rough_score -= 2.0
                    if prefer_ending_kind and ending_kind == prefer_ending_kind:
                        rough_score += 1.2
                    rough.append((rough_score, {"raw": raw, "combo": combo, "lead": lead,
                                                "ending_kind": ending_kind, "ending": ending}))
    rough.sort(key=lambda item: -item[0])
    for rough_score, item in rough[:30]:
        text = _polish(dejargon(item["raw"]))
        length = char_len(text)
        fingerprint = _head_fingerprint(text)
        text_set = bigrams(text)
        worst = max((similarity_sets(text_set, other) for other in avoid_sets), default=0.0)
        combo = item["combo"]
        score = rough_score
        # 开头撞车的代价要足够大：宁可长度不完美，也不要第二家收到同样的开头
        score -= 4.0 * (used_openers or {}).get(fingerprint, 0)
        score -= 3.0 * max(0.0, worst - similarity_limit)
        # 聊天里 50—95 字最像人话：太长就变成小作文，太短像群发
        if 50 <= length <= 95:
            score += 1.5
        elif length < 45:
            score -= 4.0
        elif length < 50:
            score -= 3.0 * (50 - length) / 2.0
        elif length <= 120:
            score -= (length - 95) / 15.0
        else:
            score -= (length - 120) / 20.0
        score += rng.random() * 0.05  # 同分时按种子打散
        if score > best_score:
            best_score = score
            best = {"text": text, "hook_kinds": [h["kind"] for h in combo],
                    "ending_kind": item["ending_kind"], "ending": item["ending"],
                    "fingerprint": fingerprint, "chars": length,
                    "lead": item["lead"],
                    "worst_similarity": round(worst, 3),
                    "capabilities": [h["capability"] for h in combo if h["capability"]],
                    "quotes": [h["quote"] for h in combo if h["quote"]],
                    "kind": combo[0]["kind"], "hooks": combo}
    if best is None:
        return {"text": "", "stopped": True, "reasons": ["这条 JD 里找不到能说的切入口"],
                "focus": [], "kind": "", "track": track, "metrics": {}}

    assert used_openers is not None and used_endings is not None
    used_openers[best["fingerprint"]] = used_openers.get(best["fingerprint"], 0) + 1
    used_endings[best["ending"]] = used_endings.get(best["ending"], 0) + 1
    if used_leads:
        used_leads[best["lead"]] = used_leads.get(best["lead"], 0) + 1
    if used_questions is not None and best["ending_kind"] == "question":
        used_questions[question] = used_questions.get(question, 0) + 1

    best.update({
        "stopped": False,
        "reasons": pursue.get("reasons") or [],
        "track": track,
        "type": pursue.get("type") or "",
        "delivery_ratio": pursue.get("delivery_ratio"),
        "focus": pursue.get("focus") or [],
    })
    best["metrics"] = {
        "chars": best["chars"],
        "ending_kind": best["ending_kind"],
        "worst_similarity": best["worst_similarity"],
        "persona": recipient_persona(job),
        "hooked_on": best["capabilities"] or [best["kind"]],
        "question": question if best["ending_kind"] == "question" else "",
        "spoken_lines": [h.get("phrase") for h in best.get("hooks") or [] if h.get("phrase")],
    }
    return best


def _join_parts(*parts: str) -> str:
    chunks = [p.strip() for p in parts if p and p.strip()]
    text = ""
    for i, chunk in enumerate(chunks):
        if not text:
            text = chunk
            continue
        if text[-1] not in "，。！？":
            text += "。"
        text += chunk
    return text


def _head_fingerprint(text: str) -> str:
    """开头指纹：口径与 msgquality.check_batch 完全一致（6 个字）。"""
    return head_key(text, 6)


def _title_role(title: Any) -> str:
    """把岗位标题整理成能念出来的名字；整理不干净就返回空（宁可不提）。"""
    text = re.sub(r"[\u200b-\u200d\ufeff]", "", str(title or ""))
    text = re.sub(r"[（(][^）)]*[）)]", "", text)
    text = re.sub(r"[!！?？。，,、;；:：]+", " ", text)
    text = re.sub(r"[+＋]+", " ", text)          # "高级+（python）" → "高级 python"
    text = ROLE_NOISE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip(" ·-—/|")
    # 只剩"工程师""开发"这种空词，或者长到念不完，就不要拿来当名字
    if 4 <= len(text) <= 12 and not re.fullmatch(r"(工程师|开发|程序员|技术)", text):
        return text
    return ""


def _jd_anchor(job: Any, capability: str) -> str:
    """从对方的 JD 里找一句能把话勾到他们身上的引子（"你们用 MySQL 的话，"）。"""
    rules = JD_ANCHORS.get(capability) or ()
    if not rules:
        return ""
    get = (lambda key, default="": job[key] if not isinstance(job, dict) else job.get(key, default))
    blob = f"{get('title')}\n{get('description')}"
    for pattern, phrase in rules:
        match = pattern.search(blob)
        if not match:
            continue
        value = match.group(1) if match.groups() else match.group(0)
        value = _canonical_anchor(value)
        text = phrase.format(v=value) if "{v}" in phrase else phrase
        return text + "，"
    return ""


ANCHOR_CANONICAL = {
    "mysql": "MySQL",
    "sqlite": "SQLite",
    "postgres": "PostgreSQL",
    "postgresql": "PostgreSQL",
    "pgsql": "PostgreSQL",
    "django": "Django",
    "fastapi": "FastAPI",
    "flask": "Flask",
    "dify": "Dify",
    "coze": "Coze",
    "n8n": "n8n",
    "deepseek": "DeepSeek",
    "qwen": "Qwen",
    "gpt": "GPT",
    "claude": "Claude",
    "vue": "Vue",
    "react": "React",
}


def _canonical_anchor(value: str) -> str:
    """JD 常把产品或框架大小写写乱；对外照规范名说，不把 PostgreS 原样抄出去。"""
    clean = re.sub(r"[^A-Za-z0-9.+-]", "", value or "")
    return ANCHOR_CANONICAL.get(clean.lower(), value.strip())


def _area_of_speak(speak: str) -> str:
    """从"能做"的说法里反推它属于哪个领域（用于避免两句话讲同一件事）。"""
    text = speak or ""
    if any(word in text for word in ("前端", "页面", "小程序")):
        return "front"
    if any(word in text for word in ("数据库", "数据", "报表", "ETL", "etl")):
        return "data"
    if any(word in text for word in ("接口", "后端", "服务端")):
        return "code"
    if any(word in text for word in ("部署", "容器", "docker", "Docker")):
        return "ops"
    if any(word in text for word in ("方案", "文档", "需求")):
        return "doc"
    if any(word in text for word in ("测试", "用例")):
        return "code"
    return ""


def _adjacent_capability(job: Any, profile: Profile, speakable: set[str]) -> str:
    """JD 里没直接对上时，找一项"多少沾边、而且本机有实物"的能力。

    只说我们真做过的那部分，不声称做过他们那件事（对外用的还是"讲做过什么"的说法）。
    """
    get = (lambda key, default="": job[key] if not isinstance(job, dict) else job.get(key, default))
    title = str(get("title") or "")
    blob = f"{title}\n{get('description')}"
    best, best_score = "", 0
    for cap in profile.capabilities:
        if cap.name not in speakable:
            continue
        hits = sum(1 for kw in cap.keywords if kw and str(kw).lower() in blob.lower())
        if not hits:
            continue
        score = hits + (2 if any(kw and str(kw).lower() in title.lower()
                                 for kw in cap.keywords) else 0)
        if score > best_score:
            best, best_score = cap.name, score
    return best


def _polish(text: str) -> str:
    text = re.sub(r"[ ]{2,}", " ", text or "")
    text = re.sub(r"。。+", "。", text)
    text = re.sub(r"，。", "。", text)
    # 中英文之间留个空格，读起来才不像机器拼的（"看到你们招AI Agent产品经理"）
    text = re.sub(r"([\u4e00-\u9fff])([A-Za-z0-9])", r"\1 \2", text)
    text = re.sub(r"([A-Za-z0-9])([\u4e00-\u9fff])", r"\1 \2", text)
    return text.strip()


# ---------------------------------------------------------------------------
# 批次级生成：同一批里的开头、结尾、优势点都不撞车
# ---------------------------------------------------------------------------


def compose_batch(jobs: list[tuple[Any, dict[str, Any]]], profile: Profile, *,
                  variants_per_job: int = 6) -> dict[str, Any]:
    """给一批岗位生成首条消息，并保证批内不重复。

    jobs: [(job, analysis), ...]
    返回 {messages: [...], stopped: [...], batch_audit: {...}}
    """
    from .msgquality import check_batch

    used_openers: dict[str, int] = {}
    used_endings: dict[str, int] = {}
    used_questions: dict[str, int] = {}
    used_leads: dict[str, int] = {}
    used_lines: dict[str, int] = {}
    ending_kind_count: dict[str, int] = {"question": 0, "action": 0, "statement": 0}
    messages: list[dict[str, Any]] = []
    stopped: list[dict[str, Any]] = []
    for job, analysis in jobs:
        pursue = pursuit_mod.assess(job, analysis, profile)
        job_id = str(_get(job, "external_id") or _get(job, "id") or _label(job))
        if pursue.get("decision") == "stop":
            stopped.append({"id": job_id, "label": _label(job), "reasons": pursue.get("stops")})
            continue
        # 这一批里用得最少的落点优先（问句用多了就换成给一个可验证的动作）
        prefer = min(ending_kind_count, key=lambda kind: ending_kind_count[kind])
        pool = [
            compose_first_contact(
                job, analysis, profile, pursue=pursue, variant=index,
                seed=f"{_label(job)}",
                used_openers=dict(used_openers), used_endings=dict(used_endings),
                used_questions=dict(used_questions), used_leads=dict(used_leads),
                used_lines=dict(used_lines),
                avoid_texts=[m["text"] for m in messages],
                similarity_limit=0.5,
                prefer_ending_kind=prefer,
            )
            for index in range(variants_per_job)
        ]
        pool = [item for item in pool if item.get("text")]
        if not pool:
            stopped.append({"id": job_id, "label": _label(job),
                            "reasons": ["生成不出可用的首条"]})
            continue
        # 选相似度最低、长度最合适的那条，再登记占用
        # 选这一条时也要看"开头/话头是不是这批已经用过"，否则长度合适但撞车
        pool.sort(key=lambda item: (
            used_openers.get(item["fingerprint"], 0) > 0,
            used_leads.get(item["lead"], 0) > 0,
            item["metrics"]["worst_similarity"] > 0.5,
            abs(item["metrics"]["chars"] - 85),
        ))
        picked = pool[0]
        used_openers[picked["fingerprint"]] = used_openers.get(picked["fingerprint"], 0) + 1
        used_endings[picked["ending"]] = used_endings.get(picked["ending"], 0) + 1
        used_leads[picked["lead"]] = used_leads.get(picked["lead"], 0) + 1
        for hook in picked.get("hooks") or []:
            line = str(hook.get("phrase") or "")
            if line:
                used_lines[line] = used_lines.get(line, 0) + 1
        picked_question = picked["metrics"].get("question") or ""
        if picked_question:
            used_questions[picked_question] = used_questions.get(picked_question, 0) + 1
        kind = picked["metrics"].get("ending_kind") or "question"
        ending_kind_count[kind] = ending_kind_count.get(kind, 0) + 1
        picked["label"] = _label(job)
        picked["id"] = job_id
        picked["type"] = pursue.get("type") or picked.get("type") or ""
        picked["track"] = pursue.get("track")
        picked["delivery_ratio"] = pursue.get("delivery_ratio")
        messages.append(picked)

    audit = check_batch(
        [{"text": m["text"], "label": m["label"], "stage": "first_contact"} for m in messages]
    )
    return {"messages": messages, "stopped": stopped, "batch_audit": audit}


def _label(job: Any) -> str:
    return f"{(_get(job, 'company') or '')[:12]}｜{(_get(job, 'title') or '')[:18]}"


def _get(job: Any, key: str, default: Any = "") -> Any:
    return job[key] if not isinstance(job, dict) else job.get(key, default)

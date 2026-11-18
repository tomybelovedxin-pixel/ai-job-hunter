"""聊天阶段识别与分阶段回复。

同一套话术应对不了整段对话。这里先认阶段，再按阶段给不同的回复口径：

  首次联系      让招聘方一眼看懂"为什么找你、我能给什么"（见 jobagent/outreach.py）
  对方回复      只答对方问的那件事，不重发自我介绍
  问项目经验    给真实、具体、能复核的实物，不编年限、不编雇主
  质疑能力      不争辩，说清实际干活的方式，并给出一个能被验证的方案
  问技术细节    讲实现路径、测过什么、边界在哪
  暂未回复      按间隔决定是否跟进；跟进必须带新信息，不重复第一条
  明确拒绝      停，不再发（stopped=True）

识别只看消息文本，不依赖页面结构，所以离线可测。
"""

from __future__ import annotations

import hashlib
import random
import re
from typing import Any

from . import capability, voice
from .greeter import CAN_DO, SPOKEN, SPEAK, SPEAK_SHORT, spoken_line
from .msgquality import char_len, dejargon, safe_evidence, similarity
from .profile import Profile

STAGES = ("first_contact", "replied", "asked_experience", "doubted_capability",
          "asked_tech_detail", "no_reply", "rejected")

REJECT = re.compile(
    r"(不合适|不匹配|不符合|不完全匹配|技能组合.*不匹配|技能组合.*不符合|"
    r"不太合适|不考虑|暂不考虑|暂时不需要|已经招到|招满了|"
    r"抱歉|不好意思|谢谢关注|祝你好运|不需要了|岗位已关闭|停止招聘)", re.I)

EXPERIENCE = re.compile(
    r"(几年|多少年|从业|经验|做过什么项目|项目经历|作品|案例|在哪家公司|哪家公司|"
    r"以前做过|之前做过|做过哪些|简历发|看下简历|发份简历)", re.I)

DOUBT = re.compile(
    r"(能独立|能不能做|会不会|懂不懂|熟不熟|熟吗|有把握|靠谱|行不行|确定能|"
    r"能力|水平|胜任|一个人能|多久能|多长时间能|有相关背景|专业对口|有证书)", re.I)

TECH = re.compile(
    r"(技术栈|用什么|怎么实现|如何实现|架构|接口|数据库|部署|docker|dockerfile|"
    r"服务器|linux|性能|并发|数据量|框架|vue|react|python 版本|环境|"
    r"方案|设计文档|测试|联调|上线|运维|日志|监控)", re.I)

MOVE = re.compile(
    r"(可以|方便|什么时候|时间|电话|微信|面聊|面试|约|聊聊|聊一下|发我|发过来|"
    r"过来|过来面谈|线上|远程|薪资|待遇|工作时间|地点|方便吗)", re.I)


# 按岗位类型的说法库：不同岗位的"先做一版验证"和"怎么实现"本来就不是一回事，
# 用同一段话回答所有岗位，就是用户说的"话术高度模板化"。
PLAYBOOK: dict[str, dict[str, str]] = {
    "AI Agent / 智能体": {
        "verify": "你们给一个真实场景，我先把一条流程搭起来跑通：需求进去拆步骤、按步调工具、"
                  "结果能回查，跑完把日志和结果一起给你们",
        "tech": "我这条链分三层：模型接口那层管调用和失败重试，中间那层管步骤和工具，"
                "外面那层管结果校验，每层都有能单独跑的检查",
        "action": "我先按你们的场景把一条流程跑通，跑完把日志和结果一起发你们",
        "context": "你们现在这条流程是人工在走，还是已经有平台了",
    },
    "AI 应用 / 大模型": {
        "verify": "你们挑一批真实文档或者真实问题给我，我跑一版问答出来，"
                  "对得上、对不上、没依据的分别标出来给你们看",
        "tech": "我一般把提示词、检索、输出校验分开放：先定模型和路径，再把输出格式固定下来，"
                "同一件事换个人跑结果尽量一样",
        "action": "我先按你们的一批真实问题跑一版，结果和错例一起发你们",
        "context": "你们现在是用官方接口，还是走内部的大模型平台",
    },
    "AI 客服 / 对话系统": {
        "verify": "你们导出 50 条真实客服问答给我，我跑一版：能答的答，依据不足的直接转人工，"
                  "错在哪条我标出来给你们看",
        "tech": "我这边是先把问题分类、再按订单号取数、回答必须能指回来源，"
                "取不到依据就交回人工，这几步分开测",
        "action": "我先拿你们一批真实问答跑一版，答对的答错的都标出来发你们",
        "context": "现在的问题分类是人工在定，还是已经有一套规则了",
    },
    "RPA / 流程自动化": {
        "verify": "你们挑一条最费人力的流程，我按现在的步骤先跑一遍，"
                  "哪一步会失败、失败了从哪重试，都写成日志给你们查",
        "tech": "流程类的东西我按「一步一校验」写：每步的前置条件、失败处理和重试都单独写，"
                "页面变了只改那一步",
        "action": "你们挑一条流程，我先跑通一条给你们看",
        "context": "你们手上是现成的流程要维护，还是从零搭",
    },
    "ERP / 业务系统": {
        "verify": "你们给一套真实的单据和报表口径，我把数据导入、跑一遍核对，"
                  "对不上的地方列出来给你们看",
        "tech": "这类活我的做法是先摸清数据口径：单据从哪来、表怎么关联、报表按什么规则算，"
                "然后写成能重复跑的脚本，不用手工导",
        "action": "我先照你们的单据流程走一遍数据，核对结果发你们",
        "context": "你们用的是金蝶、用友，还是自研那套",
    },
    "前端": {
        "verify": "你们给一个页面或者一个模块，我按现有组件和目录结构做出来，"
                  "接口先接你们的测试环境，页面直接给你们点",
        "tech": "前端我按你们现有的写法来：组件复用你们的、接口按你们的规范调，"
                "不做第二套，联调的时候我自己在页面上对着改",
        "action": "我先按你们的页面做一个模块出来，你们直接点着看",
        "context": "你们页面上现在用的是 Vue 还是 React",
    },
    "全栈": {
        "verify": "你们挑一个小功能，我从表结构做到页面：先建表写接口，再做页面联调，"
                  "做完整条链给你们点",
        "tech": "我按「先数据、再接口、最后页面」的顺序做，每层做完自己先跑一遍，"
                "哪一层出问题能马上定位",
        "action": "我先挑一条完整的小功能做出来，从表到页面给你们看",
        "context": "你们现在前端和后端分别是什么技术栈",
    },
    "后端 / 接口": {
        "verify": "你们给一份接口文档或者一个业务表，我把接口写出来、带校验和日志，"
                  "再写几条用例自己压一遍，结果给你们看",
        "tech": "接口这块我的习惯是先定入参和出参、把异常和日志写清楚，再写用例自己跑一遍，"
                "联调的时候出问题能按日志查到具体哪一步",
        "action": "你们给一份接口文档，我先写两个接口跑通给你们看",
        "context": "你们用的是 FastAPI、Flask 还是别的",
    },
    "数据 / 评测 / 标注": {
        "verify": "你们给一批真实数据和现有的评分口径，我先标一批做一致性检查，"
                  "不一致的地方和判断不了的地方都单独列给你们",
        "tech": "我的做法是先把口径写成能判定的规则，再抽检复核，"
                "反复出错的样本回头改规则——先把标准定死，结果才稳",
        "action": "我先试标一批给你们看，错的地方一起挑出来",
        "context": "标的是哪类数据，一天大概多少量",
    },
    "采集 / 爬虫": {
        "verify": "你们给一个目标站点，我先跑一版小批量：能取到的、取不到的、"
                  "要登录的都分开列出来，导出给你们看",
        "tech": "取数我按公开接口优先、页面结构其次来做：结构变了能察觉、失败的自动记日志重试，"
                "要登录的后台我只读取数",
        "action": "你们指一个站点，我先跑一版小批量的数据导出给你们",
        "context": "要采的是公开页面，还是登录之后的后台数据",
    },
    "技术方案 / 售前": {
        "verify": "你们给一个真实客户场景，我出一版方案：怎么落地、分几步、"
                  "哪一步要你们配合写清楚，再配一版能演示的东西",
        "tech": "方案我按「客户现在怎么干、换成这套之后怎么干、要动哪几块」来写，"
                "不写空话，配一版能跑的东西支撑",
        "action": "你们给一个客户场景，我先出一版方案加演示稿",
        "context": "方案主要是给客户看的，还是内部立项用",
    },
    "自动化 / 交付": {
        "verify": "你们给一个具体的重复流程，我按期交一版：先跑通、再补日志和失败重试，"
                  "交付前我自己先按验收口径过一遍",
        "tech": "交付的活我按「先跑通最小的那条链、再加异常处理、最后补文档」来做，"
                "中间每一步都能单独复跑",
        "action": "你们给一个具体流程，我先交一版能跑的，验收标准你们定",
        "context": "这个活是按项目结，还是长期做",
    },
}

DEFAULT_PLAYBOOK = {
    "verify": "你们给一个具体的小任务，我先做一版能跑的给你们验，行不行你们一看就知道",
    "tech": "我按「先跑通最小的一条链，再补异常和日志」来做，每一步都能单独复跑",
    "action": "你们给一个具体场景，我先做一版能跑的给你们看",
    "context": "你们现在最想先解决的是哪一步",
}


def _playbook(pursuit: dict[str, Any] | None) -> dict[str, str]:
    kind = str((pursuit or {}).get("type") or "")
    return PLAYBOOK.get(kind, DEFAULT_PLAYBOOK)

def _last_from(messages: list[dict[str, str]], role: str) -> str:
    for item in reversed(messages or []):
        if (item.get("role") or "") == role:
            return str(item.get("text") or "")
    return ""


def detect_stage(messages: list[dict[str, str]]) -> str:
    """认阶段。messages 是 {"role": "me"/"them", "text": ...} 的时间顺序列表。"""
    if not messages:
        return "first_contact"
    theirs = _last_from(messages, "them")
    if not theirs:
        return "first_contact" if not _last_from(messages, "me") else "no_reply"
    if REJECT.search(theirs):
        return "rejected"
    if EXPERIENCE.search(theirs):
        return "asked_experience"
    if DOUBT.search(theirs):
        return "doubted_capability"
    if TECH.search(theirs):
        return "asked_tech_detail"
    if MOVE.search(theirs):
        return "replied"
    return "replied"


def _evidence_lines(analysis: dict[str, Any], *, limit: int = 2) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for item in (analysis.get("matched") or []):
        name = str(item.get("capability") or "")
        if not name or name not in SPEAK:
            continue
        if any(line["capability"] == name for line in out):
            continue
        out.append({"capability": name, "basis": str(item.get("basis") or ""),
                    "limit": str(item.get("limit") or ""), "grade": str(item.get("grade") or "")})
        if len(out) >= limit:
            break
    return out


def _can_do_line(job: Any) -> str:
    get = (lambda key, default="": job[key] if not isinstance(job, dict) else job.get(key, default))
    blob = f"{get('title')}\n{get('description')}"
    for pattern, speak, _family in CAN_DO:
        if pattern.search(blob):
            return speak[0] if isinstance(speak, tuple) else speak
    return ""


def _scope_line(pursuit: dict[str, Any] | None) -> str:
    if not pursuit or not pursuit.get("narrow_scope"):
        return ""
    return "这种体量的活我习惯先交一段能独立跑通的，跑通一段再往下铺。"


def _rng(seed: str, stage: str) -> random.Random:
    """按岗位 + 阶段固定随机源：同一岗位同阶段的结果可复现。"""
    digest = hashlib.sha256(f"{seed}|{stage}".encode("utf-8")).hexdigest()
    return random.Random(digest)


# 时间/措辞这类"整句固定话"也要轮换，不然十几个岗位的回复读起来还是同一段
AVAILABILITY = (
    "时间我这边好安排，你们方便的时候说一声，我按你们的节奏来。",
    "我这边时间比较灵活，你们定时间，我配合。",
    "随时能聊，线上或者过去面谈都行，你们说个时间。",
    "我这边最近都在，你们定时间就行。",
)

CLOSING = (
    "跑完你们按自己的标准打分，不合适也不耽误你们。",
    "先看东西，看不上也没关系，你们按自己的标准判。",
    "验完你们自己判断，不合适就当没这回事。",
)

ASK_PREFIXES = (
    "",
    "还想确认下：",
    "想先了解一点：",
    "方便的话说一下：",
    "先问一件事：",
)


def _evidence_line(evidence: list[dict[str, str]]) -> str:
    """挑一项能说出口的实物（没有证据时给一句能做的话）。"""
    if not evidence:
        return "先按能上手的方式接：把需求拆成能验证的几段，一段一段跑给你看"
    first = evidence[0]
    return dejargon(spoken_line(first["capability"], 0))


def _topic_of(text: str) -> str:
    """从对方那句话里挑出他做的业务（"我们主要做 XX" → "XX"），回复时接住它。"""
    match = re.search(r"(?:我们|这边|公司)[^，。？?]{0,10}?(?:主要)?(?:做|是|主营)([^，。？?]{2,18})",
                      text or "")
    if not match:
        return ""
    topic = match.group(1).strip(" 的，。")
    topic = re.sub(r"^(做|是|的)+", "", topic).strip()
    return topic if 2 <= len(topic) <= 16 else ""


def split_sentences(text: str) -> list[str]:
    """按句切开，用于统计"哪句话被用过几次"。"""
    return [s for s in re.split(r"(?<=[。！？])", text or "") if s.strip()]


# 每个阶段都准备几句可换的说法，按变体号轮换；
# 单靠一段固定话应付十几个岗位，就是用户说的"模板化"。
REPLIED_OPENERS = (
    "你们做{topic}的话，我这边正好对得上：{evidence}。",
    "看到你们是做{topic}的。我这边手上的东西是：{evidence}。",
    "{topic}这块我不陌生：{evidence}。",
    "方便，先说下我这边的情况：{evidence}。",
    "我这边做的是：{evidence}。",
    "先说一句，我手上的东西是：{evidence}。",
)

REPLIED_ACTIONS = (
    "{action}。",
    "建议先这么走：{action}。",
    "{action}，你们看合不合适。",
    "我这边可以直接开始：{action}。",
)

EXPERIENCE_OPENERS = (
    "我做过的那部分是这样的：{evidence}。",
    "说具体一点：{evidence}。",
    "拿具体做过的东西说，{evidence}。",
    "我手上能拿出来的就是这块：{evidence}。",
    "这块我做过：{evidence}。",
)

DOUBT_OPENERS = (
    "我一个人做没问题，说下我实际是怎么干的：{how}。",
    "行不行你验一遍就知道：{verify}。",
    "不用听我说，直接看东西：{verify}。",
    "我是这么干的：{how}。",
    "这个我敢接，说下怎么落地：{verify}。",
)

HOW_I_WORK = (
    "需求先拆成能验证的小段，写代码我用 AI 工具打配合，但每段都自己跑数据验一遍，"
    "错了要能查出是哪一步断的",
    "先把任务拆成一块一块能验收的，用 AI 工具加快写代码，但每一步都自己跑一遍数据，"
    "不对就退回上一步",
    "我干活是先拆步骤、再写、再自己验：写代码用工具提速，验证这一步不省，"
    "出错要能定位到具体哪一段",
)


def draft_reply(
    job: Any,
    analysis: dict[str, Any],
    profile: Profile,
    messages: list[dict[str, str]],
    *,
    pursuit: dict[str, Any] | None = None,
    stage: str = "",
    variant: int = 0,
    days_since_last: int = 0,
    followed_up_times: int = 0,
    seed: str = "",
) -> dict[str, Any]:
    """按阶段生成一条回复。返回 {stage, text, action, stop, notes}。"""
    stage = stage or detect_stage(messages)
    last_theirs = _last_from(messages, "them")
    evidence = _evidence_lines(analysis)
    notes: list[str] = []

    if stage == "rejected":
        return {"stage": stage, "text": "", "action": "stop", "stop": True,
                "notes": ["对方明确拒绝，按口径不再打扰，也不解释"]}

    if stage == "no_reply":
        if followed_up_times >= 1:
            return {"stage": stage, "text": "", "action": "wait", "stop": False,
                    "notes": ["已经跟进过一次，不再重复；等对方或换下一个岗位"]}
        if days_since_last and days_since_last < 3:
            return {"stage": stage, "text": "", "action": "wait", "stop": False,
                    "notes": [f"才过 {days_since_last} 天，按平台节奏再等一等"]}
        text = _followup_text(job, analysis, evidence)
        return {"stage": stage, "text": text, "action": "follow_up", "stop": False,
                "notes": ["跟进只发一次，且必须带新信息（新实物或新问题）"]}

    if stage == "asked_experience":
        text = _experience_text(analysis, evidence, pursuit, job=job, seed=seed, variant=variant)
        notes.append("给可复核的实物，不编年限、不编雇主；年限问题不正面认输，把标准挪到试做")
    elif stage == "doubted_capability":
        text = _doubt_text(job, analysis, evidence, pursuit, seed=seed, variant=variant)
        notes.append("不争辩、不夸大；说明干活方式并给出可验证方案")
    elif stage == "asked_tech_detail":
        text = _tech_text(analysis, evidence, pursuit, seed=seed, variant=variant)
        notes.append("讲路径 + 测过什么 + 边界在哪，不承诺没验证的部分")
    else:
        text = _replied_text(job, last_theirs, evidence, pursuit, seed=seed, variant=variant)
        notes.append("只答对方问的那件事，不重发自我介绍")

    text = _fit_length(text)
    return {"stage": stage, "text": text, "action": "reply", "stop": False, "notes": notes}


def draft_replies_batch(items: list[dict[str, Any]], profile: Profile, *,
                        variants: int = 6) -> list[dict[str, Any]]:
    """批量生成回复：同一批里优先选"用得最少"的那组句子。

    items 元素：{"job", "analysis", "pursuit", "thread", "seed", "stage"(可选),
                "days_since_last", "followed_up_times"}
    逐个生成多个候选，按"整句复用次数 + 与已生成消息的相似度"挑最不重复的一条。
    """
    from collections import Counter

    used: Counter[str] = Counter()
    produced: list[str] = []
    results: list[dict[str, Any]] = []
    for item in items:
        best: dict[str, Any] | None = None
        best_score = float("-inf")
        for variant in range(variants):
            candidate = draft_reply(
                item["job"], item["analysis"], profile, item.get("thread") or [],
                pursuit=item.get("pursuit"), stage=item.get("stage") or "",
                days_since_last=int(item.get("days_since_last") or 0),
                followed_up_times=int(item.get("followed_up_times") or 0),
                seed=str(item.get("seed") or ""), variant=variant,
            )
            text = candidate.get("text") or ""
            if not text:
                best = best or candidate
                continue
            sentences = split_sentences(text)
            score = -sum(used[s] for s in sentences)
            worst = max((similarity(text, other) for other in produced), default=0.0)
            score -= 6.0 * max(0.0, worst - 0.45)
            score -= 0.01 * abs(char_len(text) - 110)
            if score > best_score:
                best_score = score
                best = candidate
        if best is None:
            continue
        for sentence in split_sentences(best.get("text") or ""):
            used[sentence] += 1
        if best.get("text"):
            produced.append(best["text"])
        results.append(best)
    return results


def _replied_text(job: Any, last_theirs: str, evidence: list[dict[str, str]],
                  pursuit: dict[str, Any] | None, seed: str = "", variant: int = 0) -> str:
    """对方回复之后：只答他问的那件事 + 一个具体动作，不重发自我介绍。"""
    book = _playbook(pursuit)
    rng = _rng(f"{seed}#{variant}", "replied")
    topic = _topic_of(last_theirs)
    if "薪" in last_theirs or "待遇" in last_theirs:
        return f"薪资按你们的区间来，我这边主要看活能不能落地。{book['action']}。"
    availability = rng.choice(AVAILABILITY)
    evidence_text = _evidence_line(evidence)
    parts: list[str] = []
    opener = REPLIED_OPENERS[variant % len(REPLIED_OPENERS)]
    if topic:
        parts.append(opener.format(topic=topic, evidence=evidence_text))
    else:
        plain = ("我这边是按能上手的方式接活：先拆成能验证的几段。"
                 if not evidence else f"我这边做的是：{evidence_text}。")
        parts.append(plain if "{topic}" in opener else opener.format(
            topic="这块", evidence=evidence_text).replace("你们做这块的话", "这块"))
    parts.append(REPLIED_ACTIONS[(variant + 1) % len(REPLIED_ACTIONS)].format(
        action=book["action"]))
    # 对方在提问或这一步确实缺信息时才回问；只是陈述就不追问。
    if voice.needs_question(last_theirs, "replied"):
        ask_prefix = rng.choice(ASK_PREFIXES)
        if variant % 2 == 0:
            parts.append(availability)
            parts.append(f"{ask_prefix}{book['context']}？")
        else:
            parts.append(f"{ask_prefix}{book['context']}？")
            parts.append(availability)
    elif variant % 2 == 0:
        parts.append(availability)
    else:
        parts.append(rng.choice(CLOSING))
    return "".join(parts)


def _experience_text(analysis: dict[str, Any], evidence: list[dict[str, str]],
                     pursuit: dict[str, Any] | None, job: Any = None,
                     seed: str = "", variant: int = 0) -> str:
    """被问经验时：给可复核的实物，不编年限、不编雇主，把标准挪到"先验一版"。"""
    book = _playbook(pursuit)
    rng = _rng(f"{seed}#{variant}", "experience")
    if not evidence:
        # 一条证据都没有：如实说清是个人做出来的东西，但不自贬、不认输
        can_do = _can_do_line(job) if job is not None else ""
        parts = ["我没有在企业里挂过同名的岗，手上的东西都是自己一个个做出来的："
                 "取数、清洗、落库、批量处理，加上模型接口这条链，能当场跑。"]
        if can_do:
            parts.append(can_do + "。")
        parts.append(f"{book['verify']}。")
        return "".join(parts)
    first = evidence[0]
    spoken = dejargon(spoken_line(first["capability"], 1))
    parts = [EXPERIENCE_OPENERS[variant % len(EXPERIENCE_OPENERS)].format(evidence=spoken)]
    if variant % 2 == 0:
        basis = safe_evidence(first.get("basis") or "", limit=40)
        # 依据和刚说的那件事重复就不必再念一遍（"前端页面是自己写的"说两遍很像模板）
        if basis and basis not in spoken and similarity(spoken, basis) < 0.5:
            parts.append(f"能够拿出来看的是：{basis}。")
        parts.append(f"{book['verify']}。")
    else:
        parts.append(f"{book['verify']}。")
        if len(evidence) > 1:
            second = evidence[1]
            parts.append(f"另外{second['capability']}也做过："
                         f"{dejargon(spoken_line(second['capability'], 2))}。")
    parts.append(rng.choice(CLOSING))
    scope = _scope_line(pursuit)
    if scope:
        parts.append(scope)
    return "".join(parts)


def _doubt_text(job: Any, analysis: dict[str, Any], evidence: list[dict[str, str]],
                pursuit: dict[str, Any] | None, seed: str = "", variant: int = 0) -> str:
    """被质疑能力时：不争辩、不夸大，说清干活方式并给出可验证方案。"""
    book = _playbook(pursuit)
    how = HOW_I_WORK[variant % len(HOW_I_WORK)]
    opener = DOUBT_OPENERS[variant % len(DOUBT_OPENERS)]
    parts = [opener.format(how=how, verify=book["verify"])]
    if evidence:
        parts.append(f"已经跑出来的有："
                     f"{dejargon(spoken_line(evidence[0]['capability'], 1))}。")
    if "{verify}" not in opener:
        # 开场没讲验证方案，就补上——被质疑时"能被验证"比任何解释都有用
        parts.append(f"{book['verify']}。")
    can_do = _can_do_line(job)
    if can_do and not evidence:
        parts.append(can_do + "。")
    if variant % 2 == 1 and evidence:
        parts.append("跑出来行不行，你们看一眼就知道，不用先信我。")
    scope = _scope_line(pursuit)
    if scope:
        parts.append(scope)
    return "".join(parts)


def _tech_text(analysis: dict[str, Any], evidence: list[dict[str, str]],
               pursuit: dict[str, Any] | None, seed: str = "", variant: int = 0) -> str:
    """被问技术细节时：讲实现路径、测过什么、边界在哪。"""
    book = _playbook(pursuit)
    rng = _rng(f"{seed}#{variant}", "tech")
    parts: list[str] = []
    if evidence and variant % 2 == 0:
        parts.append(f"{dejargon(SPEAK[evidence[0]['capability']])}。")
        parts.append(f"{book['tech']}。")
    else:
        parts.append(f"{book['tech']}。")
        if evidence:
            parts.append(f"已经跑过的是："
                         f"{dejargon(spoken_line(evidence[0]['capability'], 2))}。")
    if evidence and evidence[0].get("limit"):
        limit = safe_evidence(evidence[0].get("limit") or "", limit=40)
        if limit:
            parts.append(f"边界也说清楚：{limit}，这部分我不硬撑。")
    limits = capability.risk_notes(str(analysis.get("job_problem") or ""))
    if limits and pursuit and pursuit.get("narrow_scope"):
        parts.append("涉及" + "、".join(limits[:2]) + " 的部分，我先把范围收到能验证的一段。")
    parts.append(f"我按你们现有的那套接，不另起一套。{book['context']}？")
    return "".join(parts)


def _followup_text(job: Any, analysis: dict[str, Any], evidence: list[dict[str, str]]) -> str:
    get = (lambda key, default="": job[key] if not isinstance(job, dict) else job.get(key, default))
    title = str(get("title") or "")[:20]
    parts: list[str] = []
    if evidence:
        parts.append(f"补一条消息，不占你时间。「{title}」这个岗我又往下做了一点："
                     f"{spoken_line(evidence[0]['capability'], 0)}，"
                     f"整理成了一页说明，需要的话我发你看看。")
    else:
        parts.append(f"补一条消息。「{title}」这个岗我按自己的理解先把流程画了一版，"
                     f"哪一步该谁做都标出来了，需要的话我发你看看。")
    parts.append("你们现在还有在招吗？有的话我随时可以聊。")
    return "".join(parts)


def _fit_length(text: str, *, high: int = 190) -> str:
    """控制长度：太长了砍掉末尾的解释句；宁可短一点，也不要注水凑字数。

    上限比首条宽（首条 50—120），因为对方追问时要给依据和验证方案；
    但超出上限时从最后一句开始删，保证不把前面那句实在的东西删掉。
    """
    if char_len(text) <= high:
        return text
    sentences = [s for s in re.split(r"(?<=[。！？])", text) if s.strip()]
    while sentences and char_len("".join(sentences)) > high:
        sentences.pop()
    return "".join(sentences)

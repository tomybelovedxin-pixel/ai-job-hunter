"""对外说话的词汇表 + 话术生成入口。

这个文件现在只负责两件事：

1. **说什么**：哪条 JD 原话可以引、哪些能力有本机证据（SPEAK / SPEAK_SHORT）、
   哪些是"会做但没做过"（CAN_DO）、哪些是用户本人能顶的非技术要求（MGMT_SPEAK）。
2. **怎么做**：首条消息由 `jobagent/outreach.py` 生成、分阶段回复由
   `jobagent/stages.py` 生成，本文件的两个 draft 函数只是转发，
   保证老调用方（draft_batch / make_drafts / cli draft）拿到的是同一套新口径。

对外口径（2026-10-17 修订，任务书见 PROJECT_INDEX 的"话术与争取逻辑"一节）：

- 不提弱点，缺口一个字都不进对话；不会的沉默，不自贬；
- 一条消息最多两个优势点，首条 50—120 字，不堆技术名词；
- 不重复同一套开头、结尾和固定提问（那些固定句式已经从本文件删掉）；
- 有证据才说"做过"，会做但没做过的说"能做"，都没有的说"先做一版验证"。
"""

from __future__ import annotations

import re
from typing import Any

from .profile import Profile

# 是否在话术里主动说缺口。默认 False（用户 2026-10-13 要求：不跟对方提弱点）。
# 改成 True 会回到早期"先说不足：……"的写法。
MENTION_GAPS = False

# 以这些动词开头的是"岗位职责"，不是"我缺什么"：写进话术会变成自贬，
# 比如"负责公司 NAS 日常管理"并不代表我干不了。
DUTY_LEAD = re.compile(
    r"^(负责|参与|推动|搭建|维护|监控|完成|承担|协助|配合|对接|跟进|执行|推进|"
    r"优化|管理|制定|设计|落地|确保|根据|深入|研究|统筹|组织)"
)

# 软素质、自我要求这类句子：说"我不占优势"没有意义，也容易被当成敷衍
SOFT_SKILL = re.compile(
    r"(学习能力|学习新技术|业务思维|沟通能力|表达能力|责任心|抗压|团队合作|团队协作|"
    r"积极主动|主观能动|自驱|执行力强|关注行业动态|持续跟踪|持续学习|热爱|吃苦)"
)

# 只有确实是"要求"的句子才配当缺口：技能、年限、学历、语言，或分析明确判过"只了解"
REAL_REQUIREMENT = re.compile(
    r"(年以上|经验|学历|本科|大专|硕士|博士|精通|熟悉|掌握|了解|独立|英文|英语|"
    r"证书|只了解|没有落地|不具备|作品集|部署|运维|框架|技术栈)"
)

# 分析内部用语，不该出现在发给 HR 的话里
INTERNAL_MARK = re.compile(r"（(没有证据或目前不具备|无证据|证据不足|只了解，没有落地)）")

# ---------------------------------------------------------------------------
# JD 驱动的话术（2026-10-16 用户口径）
#
# "话术就直接照他描述的那个来，不要跟他提什么我们做不了；只要他描述出来的、
#   我们能做，我们就直接说。"
#
# 所以：只从分析结果里取「命中」（A/B 级）的要求项，引他 JD 的原话，接一句我们
# 实际做过的说明；缺口（C/D）一个字都不写。没有命中项就不发这家。
# ---------------------------------------------------------------------------

# 能力 → 一句能直接说给人听的话（都对应 capability_grades.json 里的证据）
SPEAK: dict[str, str] = {
    "Python 应用与脚本开发":
        "我平时就是写 Python 干活的：取数、清洗、落库、批量处理都是自己写的代码，"
        "跑起来能反复跑",
    "大模型 API 与提示词工程":
        "大模型接口是我天天在用的：提示词怎么约束、输出怎么校验、超时和失败怎么兜住，"
        "这套我自己写过",
    "Agent 与工作流编排":
        "任务拆解、按步调工具、结果校验这条链我自己跑通过一版，每一步都有失败留痕",
    "RAG 与检索":
        "知识库这条线我做过：先切块入库，再用关键词和语义两路找，"
        "找不到依据就直接说不知道、不硬编",
    "评测与数据质量":
        "评测我做过整套：先把口径写成能判定的评分表，再跑一致性检查和抽检，"
        "20 条用例、35 项自检都过了",
    "浏览器自动化与采集":
        "浏览器取数是我自己写的：公开接口直接取，要登录的后台只读取数、不绕验证",
    "测试与工程规范":
        "每个改动我都跑一遍自检脚本，失败能定位到具体是哪一步断的",
    "前端页面基础":
        "前端页面是自己写的：原生 HTML/CSS/JS 直接调本地接口，七个页面",
    # 图像批处理与规则检查：capability_grades.json 是 D（只做过方案讨论，本机没有脚本），
    # 所以这里不放说法——2026-10-17 实测它被拿来说给了"图像算法工程师"岗，属于编。
    "业务与数据理解":
        "订单、库存、财务对账这几块的数据怎么串起来，我自己落地过一版",
}

# 短版说法：首条要压到 50—120 字，长版留给分阶段回复里需要展开的时候。
# 两版都只讲做了什么，不加"熟练/精通/多年"这类没有本机证据的词。
SPEAK_SHORT: dict[str, str] = {
    "Python 应用与脚本开发": "取数、清洗、落库、批量处理都是自己写的代码，能反复跑",
    "大模型 API 与提示词工程": "模型接口天天在用：提示词怎么约束、输出怎么校验都是自己写的",
    "Agent 与工作流编排": "任务拆解、按步调工具、结果校验这条链我自己跑通过一版",
    "RAG 与检索": "知识库这条线我做过：切块入库、两路检索、查不到依据就说不知道",
    "评测与数据质量": "评测我做过整套：评分表、抽检复核，20 条用例和 35 项自检都过了",
    "浏览器自动化与采集": "浏览器取数是自己写的：公开接口直接取，要登录的后台只读取数",
    "测试与工程规范": "每个改动我都跑一遍自检脚本，失败能定位到断在哪一步",
    "前端页面基础": "前端页面是自己写的：原生 HTML/CSS/JS 直接调本地接口",
    # 同上：D 级能力不给对外说法
    "业务与数据理解": "订单、库存、对账这几块的数据怎么串，我落地过一版",
}

# ---------------------------------------------------------------------------
# 口语说法（2026-10-17 用户反馈"不自然"之后重写的）
#
# 之前那些说法是"报告体"：把技能名和动作排成并列短语（"任务拆解、按步调工具、
# 结果校验这条链我自己跑通过一版"），再套一个固定引子，读起来就是模板。
# 这里换成人在聊天里真会说的句子：短、具体、带画面，一项能力说三句，
# 按岗位轮换着用。长度都压在 30 字以内。
# ---------------------------------------------------------------------------

SPOKEN: dict[str, tuple[str, ...]] = {
    "Python 应用与脚本开发": (
        "我平时就是写 Python 的，取数、清洗、入库这些自己写脚本跑",
        "这类活我一般写成脚本，跑完能重复用，出错能查到哪一步",
        "接口对接和批量处理我做过不少，代码都是自己写的",
        "我手上有个一直在跑的流水线：取数、打分、生成文档、发送，都是自己写的脚本",
        "写工具是我的日常，数据从哪来、怎么存、怎么批量过一遍都是自己定的",
        "我不太靠现成的框架，能自己写的部分都自己写",
        "重复的活我一般写成脚本自动跑，哪一步失败会记下来，下次只重试失败那步",
    ),
    "大模型 API 与提示词工程": (
        "大模型接口我天天在用，提示词和输出格式都是自己一点点调出来的",
        "模型接进来怎么管住输出、失败了怎么兜住，这套我自己写过",
        "调模型这块我熟，输出要能校验，出错要有退路",
        "我调提示词的标准是：同一个问题换个人问，结果尽量一样",
        "模型接口的超时和失败我都写了兜底，不会让它把流程卡死",
        "挑模型、写提示词、验输出，这条链我自己走通过",
    ),
    "Agent 与工作流编排": (
        "我自己写过一版 Agent：需求进去它自己拆步骤、按步调工具，跑不对会退回重来",
        "任务拆解、调工具、看结果这条链我跑通过，中间哪一步出错都有日志",
        "这种流程我搭过一版，工具怎么调、结果怎么验都是自己定的",
        "我做的 Agent 是它自己决定下一步调什么，做错了会回退重做",
        "我把要它干什么拆成几步，每一步的结果都能单独看，跑歪了能截住",
        "工具调用那条链我写过，包括失败了怎么重试、结果怎么校验",
        "我搭的那版 Agent 带自检：拆步骤、调工具、验结果，跑不通会说是哪一步",
        "我的 Agent 项目配了 35 项自检和 20 条评测用例，改一版就跑一遍",
    ),
    "RAG 与检索": (
        "知识库问答我做过一版：切块入库，两种方式找，找不到依据就直接说不知道",
        "检索这块我自己搭过，宁可说不知道也不硬编，答案能指回原文",
        "文档问答这条线我跑通过：切块入库、两种方式找、答案能指回原文",
        "我做的问答是要给出处的，指不回原文就不答",
        "知识库我按标题层级切块，比整段丢进去找得准",
        "问不到答案的时候直接说没有依据，这是我特意加的一道",
    ),
    "评测与数据质量": (
        "评测我先写评分口径，再抽检复核，出错的样本回头改规则",
        "标注和抽检我做过，20 条用例、35 项自检跑过一遍",
        "判断类的活我习惯先定标准再动手，结果能复核",
        "我自己写过评测集：每条用例都有能判定的标准，跑一遍就知道哪条不过",
        "一致性的检查我做得多：两次结果不一样，就回头找是不是标准没写清",
        "质量这块我先定口径再抽检，出问题的样本回头写成规则",
        "训练语料和样本的质量我做过：去重、抽检，口径写成能判定的规则",
        "标注规范我写过：先定成能判定的标准，再抽检，不一致的回头改规则",
    ),
    "浏览器自动化与采集": (
        "取数是我自己写的：公开接口直接取，要登录的后台只读取数",
        "采集和清洗我做过，页面改了能发现，失败会记日志重试",
        "这类数据活我熟：取下来、洗干净、落库，能定时重跑",
        "我写的是纯标准库的浏览器客户端，连调试端口取数，不靠第三方框架",
        "被风控挡了会自动降速重试，这个是自己在代码里判的",
        "采集我不绕验证码：能读的公开页读，要登录的只读不碰账号",
        "流程类的活得看稳定性：每一步的前置条件和失败处理都单独写，页面变了只改那一步",
    ),
    "测试与工程规范": (
        "我改完都会自己跑一遍检查，报错能定位到哪一步",
        "用例和断言脚本我自己写过，重复的活写成自动跑",
        "我习惯先跑通再交付，出问题能复现出来",
        "每改一次我都留检查脚本，下次回归直接跑",
        "出了错先复现再动手，这是我改 bug 的习惯",
        "我自己的项目有离线自检，六十多项，改完就过一遍",
    ),
    "前端页面基础": (
        "管理后台那种页面我自己写的，HTML/JS 直接调接口",
        "页面和接口联调我做过，按你们现有组件接就行",
        "前端我能写：列表、表单、跟后端对接口",
        "我自己搭过一个七页面的管理后台，前后端一条链都是自己接的",
        "页面上的数据都是真接口取的，不是静态假数据",
        "响应式和兼容性我自己调过，不靠框架也能写",
    ),
    "业务与数据理解": (
        "订单、库存、对账这几块数据怎么串，我做过一版",
        "报表和口径拆解我做过，数据从哪来到哪去能理清",
        "我做过电商侧的数据：订单状态、库存变动、对账口径都摸过",
        "数据对不上的时候会一层层往上查，找到是哪张表的问题",
        "报表我是先跟业务对齐口径，再写查询出数",
    ),
    "训练数据与语料质量": (
        "训练语料我做过一遍：清洗、去重、切成指令样本，最后出一份质量报告",
        "文档整理成训练数据这活我做过：先规范化再去重，按段落切好，配一份质检报告",
        "指令样本我按四类生成：概括、抽取、改写、判断，每类都有能判定的标准",
        "数据质量我会先扫一遍：重复率、空洞样本、个人信息，报告里逐条列出来",
    ),
}


def spoken_line(capability: str, variant: int = 0) -> str:
    """取一句口语说法；没有口语版就退回短版说法。"""
    variants = SPOKEN.get(capability)
    if variants:
        return variants[variant % len(variants)]
    return SPEAK_SHORT.get(capability, "") or SPEAK.get(capability, "")


def pick_spoken_for(capability: str, want: str, used: dict[str, int] | None = None) -> str:
    """挑一句跟这条 JD 关键词最贴的说法：先看有没有提到同一件事，再看用没用过。"""
    variants = SPOKEN.get(capability) or ()
    if not variants:
        return spoken_line(capability, 0)
    used = used or {}
    keys = [w for w in ("流程", "自动化", "采集", "爬虫", "页面", "数据库", "接口", "评测",
                        "标注", "知识库", "客服", "部署", "报表", "对账", "订单", "订")
            if w in (want or "")]

    def score(line: str) -> tuple[int, int, int]:
        hit = 0 if any(k in line for k in keys) else 1
        return (used.get(line, 0), hit, len(line))

    return min(variants, key=score)

# ---------------------------------------------------------------------------
# 分工（2026-10-16 用户口径）
#
# "技术类型的是你负责的；我带团队那一些你不会、但我会，你也可以给我去私聊；
#  不会的不要和人家说我们不会。"
#
# 所以 JD 里的非技术要求（带团队、排期、对接客户、培训、文档方案这些）也可以照他写的
# 原话说"我能顶"——这部分是用户本人的经历；技术上能对上的部分照旧只说我们做过什么。
# 两边都不出现"我们不会/这块我没做过"之类的句子。
# ---------------------------------------------------------------------------

NON_TECH_KEYS = re.compile(
    r"(带团队|团队管理|团队搭建|人员管理|管理经验|项目管理|项目推进|进度把控|排期|"
    r"交付管理|客户对接|客户沟通|需求对接|业务对接|跨部门|沟通协调|统筹|协调|"
    r"汇报|方案撰写|编写文档|文档能力|培训|教学|带领|带队|售前|客户维护|跟进客户|"
    r"商务|运营管理|流程管理)",
)

# "明显是管理岗"的强信号：只有这类才允许在没有技术命中项时也去聊
STRONG_MGMT = re.compile(
    r"(带团队|团队管理|带队|团队搭建|管理经验|项目管理|项目推进|项目负责人|"
    r"交付管理|客户对接|客户沟通|统筹|技术负责人|团队负责人)",
)

# 用户本人能顶的部分（照 JD 原话说，不编具体数字）
MGMT_SPEAK = [
    "带团队、排进度、跟客户对接这类我做过，不是只写代码不动手的那种",
    "团队和对接这块我能顶，进度和验收我自己也管过",
    "带队和跟客户谈需求这类活我做过，技术我自己也上手",
    "人我带过，进度和验收也是我自己盯的",
    "跟客户对需求、排期这种事我做过，不用别人替我兜",
    "带团队和对接客户的活我能接，技术这块我也在一线",
]

TECH_SELF = (
    "技术这边我自己也做落地：Python 写工具和采集、Agent 和知识库应用都跑通过。"
)

# ---------------------------------------------------------------------------
# "会做但没做过"的那一层（2026-10-16 用户口径）
#
# "难道没做过你也会的，你就不说了吗？" —— 会做的就要说，只是别把它说成"做过"。
# 所以这一层用"能做/上手就能做"的说法，不编项目经历。真正不会的方向
# （视觉算法、嵌入式、硬件那一类）不进这张表，也就不提。
# ---------------------------------------------------------------------------

# 第二项是"会做但没做过"的说法（每种准备几句），第三项是它对应的问题族
# （决定结尾问什么：讲部署就问环境，讲数据库就问数据来源，不能讲 A 问 B）。
CAN_DO: list[tuple[re.Pattern[str], tuple[str, ...], str]] = [
    (re.compile(r"(dify|coze|n8n|工作流平台|低代码|智能体平台)", re.I),
     ("这类平台的工作流和接口对接我能上手",
      "平台上的工作流、知识库、接口对接这几块我都能做",
      "换到平台上做也行，底盘还是提示词、知识库和接口对接"), "agent"),
    (re.compile(r"(docker|容器|部署|服务器|linux|nginx|宝塔|运维)", re.I),
     ("部署我能做：写 Dockerfile、配环境、看日志排错",
      "上线这块我自己弄过：打镜像、配环境、出问题看日志",
      "服务器和部署的活我接得住，出故障先看日志定位"), "deploy"),
    (re.compile(r"(vue|react|小程序|html|css|javascript|前端|页面)", re.I),
     ("前端我能写：页面、接口联调、响应式",
      "页面我自己会写，跟后端对接口也没问题",
      "前端这块我能接：列表表单这种页面做过不少"), "frontend"),
    (re.compile(r"(sql|mysql|sqlite|数据库|报表|etl|数据清洗|数据仓库)", re.I),
     ("数据库这块我能写：建表、查询、清洗去重",
      "数据处理我在行：清洗、去重、按口径出数",
      "库表设计、查询、导出这些我自己写过"), "data"),
    (re.compile(r"(接口|api|fastapi|flask|django|后端|服务端)", re.I),
     ("接口我能写：设计、联调、加校验和日志",
      "后端接口我自己写过，出入参和异常都会管",
      "接口对接这块我熟，联调出问题我能顺着日志查"), "backend"),
    (re.compile(r"(测试|用例|pytest|自动化测试)", re.I),
     ("测试我能做：写用例、写断言、把重复的活自动化",
      "用例和自动化脚本我自己写过",
      "测试这块我习惯改一次跑一次，问题留记录"), "test"),
    (re.compile(r"(方案|文档|汇报|流程梳理|需求分析)", re.I),
     ("方案和文档我能写，能把需求拆成照着做的步骤",
      "文档和方案我写得多：怎么落地、分几步、谁配合",
      "需求我能拆细，写成能照着做的步骤"), "presale"),
]


def can_do_lines(text: str) -> tuple[str, ...]:
    """这段 JD 落在哪些"会做但没做过"的范围里，返回对应说法（可能多条）。"""
    for pattern, lines, _family in CAN_DO:
        if pattern.search(text or ""):
            return lines
    return ()

def _speakable_gap(text: str) -> bool:
    """判断这条缺口能不能直接说给对方听：太长的、职责句的都不要。"""
    text = (text or "").strip()
    if not text or len(text) > 56:
        return False
    if DUTY_LEAD.match(text):
        return False
    if SOFT_SKILL.search(text):
        return False
    return bool(REAL_REQUIREMENT.search(text))


def _clean_gap(text: str) -> str:
    """去掉内部标记，顺一下标点，保证这句话能直接发出去。"""
    text = INTERNAL_MARK.sub("", text or "").strip()
    text = re.sub(r"[，、；;]+$", "", text).strip()
    return text


QUOTE_CLEAN = re.compile(r"^[\s0-9.、)）•·\-—]+")

# 引文必须是"技术/任务"类要求：学历、专业、性格、软素质这些不能引，
# 引了就等于答非所问（2026-10-16 实测踩过："具备良好的审美能力"被当成评测能力引出来）
QUOTE_JUNK = re.compile(
    r"(学历|本科|大专|硕士|研究生|博士|院校|985|211|双一流|相关专业|专业背景|"
    r"审美|学习能力|沟通能力|表达能力|团队|协作|抗压|责任心|认真|细致|踏实|"
    r"积极主动|自驱|热爱|性格|年龄|应届|在校|毕业|年以上|经验优先|优先考虑|"
    # 2026-10-17 补：把"具备…项目经验"这类要求当引文，等于主动把话题引到年限上
    r"经验|" 
    r"岗位职责|任职要求|工作内容|公司简介|福利|薪酬|招聘|职位描述|一句话|"
    r"愿意|跟进|招募|征募|社会招募|学习|贯彻|策略|制度|规章|考核|热情|热忱|兴趣)"
)
ASCII_WORD = re.compile(r"(?<![a-z0-9])%s(?![a-z0-9])")
QUOTE_SPLIT = re.compile(r"[，,。；;：:（(、｜|]+")
QUOTE_TAIL = re.compile(r"[及和与等或的]+$")

# "…经验优先""有…经验"这类尾巴：技能本身该引，年限要求不该跟着一起引进来
EXPERIENCE_TAIL = re.compile(r"[，,、]?(有|具备|不限)?[^，,、。；;]{0,20}经验(者)?(优先|加分|不限|要求)?$")

# 每条能力"能引的原文"关键词表：比能力关键词更具体，避免把"内容风格一致性"
# 当成评测能力引出来（2026-10-16 实测踩过）
QUOTE_KEYS: dict[str, re.Pattern[str]] = {
    "Python 应用与脚本开发": re.compile(
        r"(python|django|fastapi|flask|接口|脚本|sqlite|mysql|后端)", re.I),
    "大模型 API 与提示词工程": re.compile(
        r"(大模型|llm|prompt|提示词|gpt|deepseek|qwen|豆包|api|模型调用)", re.I),
    "Agent 与工作流编排": re.compile(
        r"(agent|智能体|工作流|工具调用|function ?calling|编排|n8n|dify|coze|react)", re.I),
    "RAG 与检索": re.compile(r"(rag|知识库|检索|向量|embedding|召回|rerank|bm25)", re.I),
    "评测与数据质量": re.compile(
        r"(评测|评分表|标注|抽检|复核|质检|数据质量|评测集|bad ?case|一致性检查)", re.I),
    "浏览器自动化与采集": re.compile(
        r"(采集|爬虫|抓取|浏览器|selenium|playwright|cdp|数据抓取|自动化流程|流程自动化|"
        r"重复性业务流程)", re.I),
    "测试与工程规范": re.compile(r"(测试|pytest|单元测试|集成测试|git|联调|代码规范)", re.I),
    "前端页面基础": re.compile(
        r"(前端|页面|html|css|javascript|vue|react|小程序|界面)", re.I),
    "图像批处理与规则检查": re.compile(r"(图像|图片|主图|白底|水印|pillow|批处理)", re.I),
    "业务与数据理解": re.compile(r"(订单|库存|财务|对账|报表|sku|数据分析|sql)", re.I),
}

TRUNCATE_CUT = re.compile(r"[A-Za-z0-9/+#_.\-]+$")

# 引文里出现这些词说明这条要求包含我们没做过的部分，不能引（会变成吹牛）
QUOTE_FORBIDDEN = re.compile(
    r"(语音|微调|lora|sft|rlhf|强化学习|量化|推理加速|tensorrt|vllm|视觉|ocr|"
    r"深度学习|图像识别|机器视觉|嵌入式|单片机|plc|fpga|安卓|android|ios|java|"
    r"spring|c\+\+|golang|渗透|安全加固|加固|docker|k8s|kubernetes|端侧|边缘|"
    r"芯片|高并发|分布式|微服务|大数据平台|hadoop|spark|flink|数仓|数据仓库|"
    r"kafka|redis|消息队列|幻觉|准确率|稳定性|高可用|性能调优|效果优化|"
    r"适配|调优|迁移|上线|训练|自研模型|"
    r"revit|bim|cad\b|solidworks|labview|matlab|autocad|截图|截屏|录屏|"
    r"安全加固|webshell|ddos|等保|渗透测试)",
    re.I,
)
QUOTE_HEADING = re.compile(r"^(你是|我们是|你们是|我们的|我们提供|公司|岗位|职位)")
QUOTE_LEAD = re.compile(
    r"^(负责|参与|协助|配合|进行|完成|需要|要求|具备|熟悉|精通|掌握|了解|熟练|会用|"
    r"能独立|能|有)")

# 尾句不再用固定的几个提问：首条的落点由 jobagent/outreach.py 按这条 JD 现算
# （原来这里是"你们现在最急的是哪一块"，几十家收到的是同一句，用户 2026-10-17 点掉）。

# 高风险表达降级（参考 boss-greeting-writer 的做法：主导/负责人类词汇主动降级，
# 招聘方看到"独立负责/平台级"会先打个问号）
SOFTEN = [
    (re.compile(r"独立负责"), "负责"),
    (re.compile(r"主导"), "参与"),
    (re.compile(r"负责搭建"), "搭建"),
    (re.compile(r"平台级"), ""),
    (re.compile(r"高度匹配"), "对得上"),
    (re.compile(r"资深"), ""),
    (re.compile(r"专家"), ""),
    (re.compile(r"精通"), "熟悉"),
]


def _soften(text: str) -> str:
    for pattern, replacement in SOFTEN:
        text = pattern.sub(replacement, text)
    return re.sub(r"[ ]{2,}", " ", text)


def _capability_hits(text: str, capability: str, profile: Profile) -> list[str]:
    """这段 JD 原文里出现了这条能力的哪些关键词。"""
    cap = profile.capability(capability)
    if not cap:
        return []
    lowered = (text or "").lower()
    hits: list[str] = []
    for keyword in cap.keywords:
        keyword = (keyword or "").strip().lower()
        if not keyword:
            continue
        if keyword.isascii() and keyword.replace("-", "").replace("/", "").isalnum():
            if re.search(ASCII_WORD.pattern % re.escape(keyword), lowered):
                hits.append(keyword)
        elif keyword in lowered:
            hits.append(keyword)
    return hits


def _mentions_capability(text: str, capability: str, profile: Profile) -> bool:
    return bool(_capability_hits(text, capability, profile))


_SPEAKABLE_CACHE: set[str] | None = None


def speakable_capabilities() -> set[str]:
    """能对外说的能力 = capability_grades.json 里 A/B 级的那些。

    这条线是硬约束：等级是 D 的能力（例如图像批处理，本机没有可运行的脚本）不能
    出现在任何发出去的消息里。2026-10-17 实测踩过一次，所以在这里统一挡住。
    """
    global _SPEAKABLE_CACHE
    if _SPEAKABLE_CACHE is None:
        from .analyze import load_grades

        grades = load_grades().get("capabilities", {})
        _SPEAKABLE_CACHE = {name for name, entry in grades.items()
                            if str(entry.get("grade")) in ("A", "B")}
    return _SPEAKABLE_CACHE


def _quote_around(fragment: str, keys: "re.Pattern[str]", limit: int) -> str:
    """按关键词位置取一小段原文：截断只能截掉尾巴，不能把关键词本身截掉。"""
    match = keys.search(fragment)
    if not match:
        return ""
    start = max(0, match.start() - 4)
    window = fragment[start:start + limit]
    window = re.sub(r"^[，,、。；;：:]+", "", window).strip()
    window = re.sub(r"[）)】\]/、+＋-]+$", "", window)
    if len(window) > limit:
        window = window[:limit] + "…"
    return window if keys.search(window) else ""


def _quote_from_jd(text: str, capability: str, profile: Profile, limit: int = 14) -> str:
    """从 JD 原句里挑一个"确实提到这项技能"的短片段做引文。

    挑不到就返回空串——宁可退回分类模板，也不引一句答非所问的话。
    """
    keys = QUOTE_KEYS.get(capability)
    if not keys:
        return ""
    for fragment in QUOTE_SPLIT.split(text or ""):
        fragment = QUOTE_CLEAN.sub("", re.sub(r"\s+", "", fragment.strip())).strip()
        fragment = QUOTE_LEAD.sub("", fragment).strip()
        # 把"…经验优先"这类尾巴剪掉再判断：技能该引，年限不该跟着引
        trimmed = EXPERIENCE_TAIL.sub("", fragment).strip("，,、 ")
        if len(trimmed) >= 5:
            fragment = trimmed
        if len(fragment) < 5 or QUOTE_JUNK.search(fragment) or QUOTE_HEADING.search(fragment):
            continue
        if QUOTE_FORBIDDEN.search(fragment):
            continue
        if not keys.search(fragment):
            continue
        original = fragment
        fragment = _shorten_quote(fragment, limit)
        fragment = re.sub(r"[）)】\]/、+＋-]+$", "", fragment)
        # 截短之后还得仍然提到这项技能，否则就是引错了
        if len(fragment) < 5 or not keys.search(fragment):
            # 截断把关键词截掉了：改成围着关键词取一小段
            fragment = _quote_around(original, keys, limit + 6)
            if len(fragment) < 5:
                continue
        return fragment
    return ""


def _shorten_quote(text: str, limit: int) -> str:
    """把太长的引文截短：先避开被切断的英文词，再退回上一个顿号/及，
    最后去掉"及、和、与"这类吊着的连接词。"""
    if len(text) <= limit:
        return text
    window = text[:limit]
    dropped = text[limit]
    if dropped.isascii() and dropped.isalnum():
        cut = max(window.rfind(sep) for sep in "/+ ")
        window = window[:cut] if cut >= 4 else re.sub(r"[A-Za-z0-9_+\-/.]*$", "", window)
    boundary = max(window.rfind(ch) for ch in "、及")
    if boundary >= 4:
        window = window[:boundary]
    window = window.strip(" 、，及和与等或的）)】]")
    if len(window) < 4:
        return ""
    return window + "…"


def draft_jd_greeting(
    job: Any,
    analysis: dict[str, Any],
    profile: Profile,
    *,
    seed: str = "",
    variant: int = 0,
    prefer_ending_kind: str = "",
    used_endings: dict[str, int] | None = None,
    used_questions: dict[str, int] | None = None,
    used_openers: dict[str, int] | None = None,
    used_leads: dict[str, int] | None = None,
    used_lines: dict[str, int] | None = None,
) -> dict[str, Any] | None:
    """首条话术（兼容旧接口）：转给 jobagent/outreach.py 的生成器。

    返回 None 只代表一件事：这个岗位不该发（红线、薪资低于底线、交付可信度不足，
    或者 JD 里一个字都接不上）。返回结构保留了 text / matched / kind，
    另加了 focus、track、metrics，便于审计和回归对比。
    """
    from .outreach import compose_first_contact

    item = compose_first_contact(job, analysis, profile, seed=seed, variant=variant,
                                 prefer_ending_kind=prefer_ending_kind,
                                 used_endings=used_endings,
                                 used_questions=used_questions,
                                 used_openers=used_openers,
                                 used_leads=used_leads,
                                 used_lines=used_lines)
    if item.get("stopped") or not item.get("text"):
        return None
    hooks = item.get("hooks") or []
    first = hooks[0] if hooks else {}
    return {
        "text": _soften(item["text"]),
        "matched": first.get("capability") or _kind_label(first.get("kind")),
        "quotes": [h.get("quote") for h in hooks if h.get("quote")],
        "kind": {"evidence": "tech", "can_do": "can", "mgmt": "mgmt",
                 "ai_assisted": "assisted"}.get(str(first.get("kind") or ""), "tech"),
        "focus": item.get("focus") or [],
        "track": item.get("track") or "",
        "metrics": item.get("metrics") or {},
        "reasons": item.get("reasons") or [],
    }


def _kind_label(kind: Any) -> str:
    return {"can_do": "能做", "mgmt": "带团队/对接", "ai_assisted": "试做一版"}.get(
        str(kind or ""), "技术项")


def _can_do_pick(analysis: dict[str, Any], *, exclude: str = "") -> dict[str, str] | None:
    """"会做但没做过"：从 JD 里挑一条我们能做、但没有项目证据的要求。"""
    for item in analysis.get("requirements") or []:
        text = str(item.get("text") or "")
        for pattern, speak in CAN_DO:
            if not pattern.search(text):
                continue
            for fragment in QUOTE_SPLIT.split(text):
                fragment = QUOTE_CLEAN.sub("", re.sub(r"\s+", "", fragment.strip())).strip()
                fragment = QUOTE_LEAD.sub("", fragment).strip()
                if len(fragment) < 4 or QUOTE_JUNK.search(fragment):
                    continue
                if QUOTE_HEADING.search(fragment) or QUOTE_FORBIDDEN.search(fragment):
                    continue
                if not pattern.search(fragment):
                    continue
                quote = _shorten_quote(fragment, 12)
                if len(quote) < 4 or quote == exclude:
                    continue
                return {"quote": quote, "speak": speak, "tag": "能做"}
    return None


def _mgmt_quote(analysis: dict[str, Any]) -> str:
    """从 JD 里挑一句"带团队/对接/培训"类要求，作为用户本人能顶的那部分。"""
    candidates = list(analysis.get("requirements") or []) + list(analysis.get("missing") or [])
    for item in candidates:
        text = str(item.get("text") or "")
        if not NON_TECH_KEYS.search(text):
            continue
        for fragment in QUOTE_SPLIT.split(text):
            fragment = QUOTE_CLEAN.sub("", re.sub(r"\s+", "", fragment.strip())).strip()
            fragment = QUOTE_LEAD.sub("", fragment).strip()
            if len(fragment) < 4 or QUOTE_JUNK.search(fragment) or QUOTE_HEADING.search(fragment):
                continue
            if not NON_TECH_KEYS.search(fragment):
                continue
            return _shorten_quote(fragment, 12)
    return ""


def short_jd_pick(job: Any, profile: Profile) -> dict[str, str] | None:
    """JD 只有一句话时，直接照那句话写。

    库里不少岗位的正文就是一行（"负责业务模块代码开发""实时监控爬虫的进度和警报反馈"），
    走不动"按分析结果引文"那条路。这里直接在标题 + 正文里找我们的技术点：
    找到就照他的原话说"这块我做过"，找不到（工厂设备、销售推广那类）就返回 None。
    """
    jd = (job["description"] if not isinstance(job, dict) else job.get("description")) or ""
    title = (job["title"] if not isinstance(job, dict) else job.get("title")) or ""
    speakable = speakable_capabilities()
    for capability, speak in SPEAK.items():
        if capability not in speakable:
            continue
        keys = QUOTE_KEYS.get(capability)
        if not keys:
            continue
        for fragment in QUOTE_SPLIT.split(jd):
            fragment = QUOTE_CLEAN.sub("", re.sub(r"\s+", "", (fragment or "").strip())).strip()
            fragment = QUOTE_LEAD.sub("", fragment).strip()
            if len(fragment) < 4 or QUOTE_JUNK.search(fragment) or QUOTE_HEADING.search(fragment):
                continue
            if QUOTE_FORBIDDEN.search(fragment):
                continue
            if keys.search(fragment):
                return {"quote": _shorten_quote(fragment, 14), "speak": speak,
                        "capability": capability}
    # 正文太短时，退一步只看岗位标题
    for capability, keys in QUOTE_KEYS.items():
        if capability in SPEAK and capability in speakable and keys.search(title):
            return {"quote": _shorten_quote(title, 14), "speak": SPEAK[capability],
                    "capability": capability}
    return None


def draft_jd_greetings(
    job: Any, analysis: dict[str, Any], profile: Profile, variants: int = 3
) -> list[dict[str, Any]]:
    """生成若干条候选（同一条 JD 的引文固定，只是开头与收尾的问法不同）。"""
    external = (job["external_id"] if not isinstance(job, dict) else job.get("external_id")) or ""
    title = (job["title"] if not isinstance(job, dict) else job.get("title")) or ""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index in range(variants):
        item = draft_jd_greeting(job, analysis, profile, seed=f"{external or title}#{index}")
        if item and item["text"] not in seen:
            seen.add(item["text"])
            out.append(item)
    return out


def draft_greetings(job: Any, profile: Profile, variants: int = 3, *,
                    base_variant: int = 0,
                    prefer_ending_kind: str = "",
                    used_endings: dict[str, int] | None = None,
                    used_questions: dict[str, int] | None = None,
                    used_openers: dict[str, int] | None = None,
                    used_leads: dict[str, int] | None = None,
                    used_lines: dict[str, int] | None = None) -> list[dict[str, Any]]:
    """给一个岗位生成若干条候选话术（兼容旧接口，带 seed 保证可复现）。

    老版本在这里用固定模板拼两段话，一天几十家收到的是同一段（用户 2026-10-16、
    2026-10-17 两次点掉）。现在统一走 outreach 的新口径：先按 JD 拆出能说的点，
    再按变体换开头与落点。这里自己跑一遍分析，不要求调用方先落库。
    """
    from .analyze import analyze_job

    analysis = analyze_job(job, profile=profile)
    external = (job["external_id"] if not isinstance(job, dict) else job.get("external_id")) or ""
    title = (job["title"] if not isinstance(job, dict) else job.get("title")) or ""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    # 变体数不再保证正好等于 variants：同一条 JD 能换的说法有限，
    # 这里多试几轮，凑不齐时宁可少给几条，也不返回重复的（用户口径：不重样）。
    for index in range(variants * 4):
        if len(out) >= variants:
            break
        item = draft_jd_greeting(job, analysis, profile, seed=f"{external or title}#{index}",
                                 variant=index + base_variant,
                                 prefer_ending_kind=prefer_ending_kind,
                                 used_endings=used_endings,
                                 used_questions=used_questions,
                                 used_openers=used_openers,
                                 used_leads=used_leads,
                                 used_lines=used_lines)
        if item and item["text"] not in seen:
            seen.add(item["text"])
            item = dict(item)
            item["variant"] = len(out)
            out.append(item)
    return out

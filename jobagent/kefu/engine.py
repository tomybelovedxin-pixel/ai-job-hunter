"""客服原型的判定链路：路由 → 取数/检索 → 组答案 → 拒答 / 转人工。

每一层都是可读的规则，阈值写死在代码里，改了会立刻反映到评测数字上。
关键约束：回答必须带来源；找不到依据就不编，直接说查不到并转人工。
"""

from __future__ import annotations

import math
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from . import data as kb_data

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_DB = ROOT / "data" / "kefu.sqlite"

ASCII_RE = re.compile(r"[a-z0-9][a-z0-9\-]{1,}")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
ORDERNO_RE = re.compile(r"\b(20\d{10,16})\b")

# 三项口径都写在这里，评测与调参都以它为准
REFUSE_OVERLAP = 0.25      # 最佳文档要覆盖提问实词的比例，低于它就拒答
RRF_K = 60                 # RRF 的平滑常数
TOP_K = 3

# 只用来判断"有没有依据"，不参与检索打分。去掉这些虚词，
# 剩下的实词（客服、退款、包邮……）能不能在文档里找到，才是依据。
STOP = set(
    "的了吗呢吧啊呀哦嗯我你他她它们这那个是在有和与就都也还要能会着过把被给让对从到说问"
    "请帮做想看用买一二三几多少什么怎么可以能否没不很太还有"
)

RISK_ACTIONS = {
    "退款": "退款申请涉及资金，需要人工核实订单与支付信息",
    "退货": "退货需要人工核对商品状态与运费责任",
    "换货": "换货需要人工确认库存与同规格可换",
    "改地址": "修改收货地址需要人工核验身份",
    "改收货地址": "修改收货地址需要人工核验身份",
    "修改收货地址": "修改收货地址需要人工核验身份",
    "取消订单": "取消订单涉及资金与库存，需要人工处理",
    "修改订单": "修改订单内容需要人工处理",
}

INTENT_RULES: dict[str, list[str]] = {
    "物流": ["物流", "快递", "发货", "到哪", "什么时候到", "几天到", "签收", "运单",
             "轨迹", "派送", "运输", "多久能到", "发出", "发出来"],
    "订单": ["订单", "下单", "付款", "支付", "地址", "收货", "取消",
             "修改订单", "已拍", "待发货"],
    "售后": ["退货", "退款", "换货", "保修", "维修", "坏了", "故障", "质量问题",
             "发票", "开票", "维修费"],
    "售前": ["价格", "多少钱", "优惠", "库存", "有货", "缺货", "规格", "颜色", "尺寸",
             "材质", "型号", "包邮", "运费", "邮费", "参数", "适合", "推荐"],
}

INTENT_ORDER = ["物流", "订单", "售后", "售前"]


# ---------------- 分词与检索 ----------------

def tokenize(text: str) -> list[str]:
    """中文按字与二元组切，英文数字按词切。够用，不引外部依赖。"""
    text = (text or "").lower()
    tokens = ASCII_RE.findall(text)
    cjk = CJK_RE.findall(text)
    tokens.extend(cjk)
    tokens.extend(a + b for a, b in zip(cjk, cjk[1:]))
    return tokens


class BM25:
    """标准 BM25 实现（k1=1.5, b=0.75）。"""

    def __init__(self, docs: list[tuple[str, str]], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.ids = [doc_id for doc_id, _ in docs]
        self.tokens = [tokenize(text) for _, text in docs]
        self.lengths = [len(t) for t in self.tokens]
        self.avgdl = sum(self.lengths) / max(1, len(self.lengths))
        self.tf: list[dict[str, int]] = []
        self.df: dict[str, int] = {}
        for toks in self.tokens:
            counts: dict[str, int] = {}
            for tok in toks:
                counts[tok] = counts.get(tok, 0) + 1
            self.tf.append(counts)
            for tok in counts:
                self.df[tok] = self.df.get(tok, 0) + 1
        self.n = len(docs)

    def _idf(self, term: str) -> float:
        df = self.df.get(term, 0)
        return math.log(1 + (self.n - df + 0.5) / (df + 0.5))

    def search(self, query: str) -> list[tuple[str, float]]:
        q_terms = set(tokenize(query))
        scored: list[tuple[str, float]] = []
        for i, doc_id in enumerate(self.ids):
            score = 0.0
            for term in q_terms:
                freq = self.tf[i].get(term)
                if not freq:
                    continue
                denom = freq + self.k1 * (1 - self.b + self.b * self.lengths[i] / self.avgdl)
                score += self._idf(term) * freq * (self.k1 + 1) / denom
            if score > 0:
                scored.append((doc_id, score))
        scored.sort(key=lambda x: (-x[1], x[0]))
        return scored


class VectorIndex:
    """稀疏向量：词频加权后哈希到固定维度，再做余弦。

    刻意不引第三方库：要的是"另一路召回"，不是最好的向量模型。
    """

    DIM = 512

    def __init__(self, docs: list[tuple[str, str]]):
        self.ids = [doc_id for doc_id, _ in docs]
        self.vectors: list[dict[int, float]] = []
        for _, text in docs:
            counts: dict[str, int] = {}
            for tok in tokenize(text):
                counts[tok] = counts.get(tok, 0) + 1
            vec: dict[int, float] = {}
            for tok, cnt in counts.items():
                idx = hash(tok) % self.DIM
                vec[idx] = vec.get(idx, 0.0) + 1.0 + math.log(cnt)
            norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
            self.vectors.append({k: v / norm for k, v in vec.items()})

    def _embed(self, text: str) -> dict[int, float]:
        counts: dict[str, int] = {}
        for tok in tokenize(text):
            counts[tok] = counts.get(tok, 0) + 1
        vec: dict[int, float] = {}
        for tok, cnt in counts.items():
            idx = hash(tok) % self.DIM
            vec[idx] = vec.get(idx, 0.0) + 1.0 + math.log(cnt)
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        return {k: v / norm for k, v in vec.items()}

    def search(self, query: str) -> list[tuple[str, float]]:
        q = self._embed(query)
        scored = []
        for i, doc_id in enumerate(self.ids):
            vec = self.vectors[i]
            # 先遍历短的一侧，省时间
            dot = sum(w * vec.get(idx, 0.0) for idx, w in q.items())
            if dot > 0:
                scored.append((doc_id, dot))
        scored.sort(key=lambda x: (-x[1], x[0]))
        return scored


def rrf_fuse(rankings: Iterable[list[tuple[str, float]]], k: int = RRF_K) -> list[tuple[str, float]]:
    """倒数排名融合：只看名次不看分数，天然解决两路分数量纲不一致。"""
    fused: dict[str, float] = {}
    for ranking in rankings:
        for rank, (doc_id, _score) in enumerate(ranking, start=1):
            fused[doc_id] = fused.get(doc_id, 0.0) + 1.0 / (k + rank)
    return sorted(fused.items(), key=lambda x: (-x[1], x[0]))


def content_tokens(text: str) -> set[str]:
    """提问/文档里的实词：ASCII 词与数字，加去掉虚词后的中文字与二字组合。

    加二字组合是因为中文单个字的命中太散（"发"能在发货、发票、发出里都出现），
    连着的两个字更像一个真正的意思。
    """
    out: set[str] = set()
    for tok in ASCII_RE.findall((text or "").lower()):
        out.add(tok)
    chars = CJK_RE.findall(text or "")
    for ch in chars:
        if ch not in STOP:
            out.add(ch)
    for a, b in zip(chars, chars[1:]):
        if a not in STOP and b not in STOP:
            out.add(a + b)
    return out


def coverage(query: str, text: str) -> float:
    """提问里的实词被这份文档覆盖的比例，用来判断"有没有依据"。"""
    q = content_tokens(query)
    if not q:
        return 0.0
    d = content_tokens(text)
    return len(q & d) / len(q)


def title_hits(query: str, title: str) -> int:
    """提问实词命中标题的个数。

    小知识库里正文词汇互相重叠很常见（"发货"在发货时效和发票说明里都出现），
    光看正文分排不出来。标题代表这篇文档管什么，命中标题的优先。
    """
    return len(content_tokens(query) & content_tokens(title))


# ---------------- 工具层 ----------------

class Tools:
    """四个只读取数工具。每次调用都留痕，答案里的来源就是这里出来的。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.log: list[dict[str, Any]] = []

    def _call(self, name: str, args: dict[str, Any], fn):
        started = time.perf_counter()
        result = fn()
        self.log.append({
            "tool": name, "args": args, "ok": bool(result),
            "ms": round((time.perf_counter() - started) * 1000, 2),
        })
        return result

    def find_product(self, query: str) -> dict[str, Any] | None:
        def run():
            rows = self.conn.execute("SELECT * FROM products").fetchall()
            best, best_score = None, 0.0
            for row in rows:
                name = row["name"]
                score = 0.0
                if name.replace(" ", "") in query.replace(" ", ""):
                    score = 1.0
                else:
                    hit = sum(1 for tok in set(tokenize(name)) if len(tok) > 1 and tok in query)
                    score = hit / max(1, len({t for t in tokenize(name) if len(t) > 1})) * 0.8
                if score > best_score:
                    best, best_score = row, score
            return dict(best) if best and best_score >= 0.34 else None

        return self._call("find_product", {"query": query}, run)

    def stock(self, sku: str) -> dict[str, Any] | None:
        def run():
            row = self.conn.execute(
                "SELECT i.sku, i.on_hand, i.locked, p.name FROM inventory i "
                "JOIN products p ON p.sku = i.sku WHERE i.sku = ?", (sku,)).fetchone()
            return dict(row) if row else None

        return self._call("stock", {"sku": sku}, run)

    def order(self, order_no: str) -> dict[str, Any] | None:
        def run():
            row = self.conn.execute(
                "SELECT o.*, p.name AS product_name FROM orders o "
                "LEFT JOIN products p ON p.sku = o.sku WHERE o.order_no = ?",
                (order_no,)).fetchone()
            return dict(row) if row else None

        return self._call("order", {"order_no": order_no}, run)

    def shipment(self, order_no: str) -> dict[str, Any] | None:
        def run():
            row = self.conn.execute(
                "SELECT * FROM shipments WHERE order_no = ?", (order_no,)).fetchone()
            return dict(row) if row else None

        return self._call("shipment", {"order_no": order_no}, run)


# ---------------- 答案与主流程 ----------------

@dataclass
class Answer:
    query: str
    intent: str
    reply: str
    citations: list[str] = field(default_factory=list)
    escalated: bool = False
    escalate_reason: str = ""
    confidence: float = 0.0
    refused: bool = False
    trace: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)


class Agent:
    def __init__(self, conn: sqlite3.Connection, docs: list[dict[str, str]] | None = None):
        self.conn = conn
        self.docs = docs or kb_data.DOCS
        pairs = [(d["doc_id"], f"{d['title']}。{d['text']}") for d in self.docs]
        self.titles = {d["doc_id"]: d["title"] for d in self.docs}
        self.texts = {d["doc_id"]: d["text"] for d in self.docs}
        self.bm25 = BM25(pairs)
        self.vector = VectorIndex(pairs)
        self.tools = Tools(conn)
        self.tickets: list[dict[str, Any]] = []
        self._ticket_seq = 0

    # ---- 路由 ----
    def route(self, query: str) -> tuple[str, dict[str, int]]:
        scores = {name: sum(1 for kw in kws if kw in query) for name, kws in INTENT_RULES.items()}
        best = max(INTENT_ORDER, key=lambda name: (scores.get(name, 0), -INTENT_ORDER.index(name)))
        if scores.get(best, 0) == 0:
            return "其他", scores
        return best, scores

    # ---- 检索 ----
    def retrieve(self, query: str) -> tuple[list[tuple[str, float]], dict[str, Any]]:
        bm = self.bm25.search(query)
        vec = self.vector.search(query)
        fused = rrf_fuse([bm, vec])[:TOP_K]
        detail = {
            "bm25": [doc_id for doc_id, _ in bm[:TOP_K]],
            "vector": [doc_id for doc_id, _ in vec[:TOP_K]],
            "fused": fused,
        }
        return fused, detail

    def _cite_doc(self, doc_id: str) -> str:
        return f"知识库 {doc_id}《{self.titles[doc_id]}》"

    def select_docs(self, query: str, fused: list[tuple[str, float]]) -> list[dict[str, Any]]:
        """先过依据闸门，再按"标题命中 → 融合分"排序，取一篇。

        只取一篇是刻意的：小知识库里多带一篇不相关文档，回答反而更糊。
        这里对全部文档算覆盖率而不是只看融合榜前几名：文档量小的时候更准，
        文档上千就该换成先召回再重排了。
        """
        order = [doc_id for doc_id, _ in fused]
        for doc_id in self.titles:
            if doc_id not in order:
                order.append(doc_id)
        fused_map = dict(fused)
        candidates = []
        for rank, doc_id in enumerate(order):
            cov = coverage(query, f"{self.titles[doc_id]}。{self.texts[doc_id]}")
            if cov < REFUSE_OVERLAP:
                continue
            candidates.append({
                "doc_id": doc_id,
                "coverage": cov,
                "title_hits": title_hits(query, self.titles[doc_id]),
                "fused": fused_map.get(doc_id, 0.0),
                "rank": rank,
            })
        candidates.sort(key=lambda c: (-c["title_hits"], -c["fused"], c["rank"]))
        return candidates[:1]

    def _escalate(self, query: str, reason: str) -> str:
        self._ticket_seq += 1
        ticket = f"T{int(time.time()) % 100_000:05d}{self._ticket_seq:03d}"
        self.tickets.append({"ticket": ticket, "query": query, "reason": reason})
        return ticket

    # ---- 主入口 ----
    def answer(self, query: str) -> Answer:
        trace: list[str] = []
        intent, intent_scores = self.route(query)
        trace.append(f"路由: {intent} {intent_scores}")
        risk_hits = [kw for kw in RISK_ACTIONS if kw in query]

        order_match = ORDERNO_RE.search(query)
        order_no = order_match.group(1) if order_match else ""

        citations: list[str] = []
        evidence: list[dict[str, Any]] = []
        lines: list[str] = []
        escalated = False
        reason = ""

        # 1) 带单号的，先用工具把事实取回来
        if order_no:
            order = self.tools.order(order_no)
            if order:
                citations.append(f"orders 表 · 订单号 {order_no}")
                evidence.append({"kind": "table", "table": "orders", "key": order_no})
                lines.append(
                    f"订单 {order_no}（{order['product_name']} ×{order['qty']}，"
                    f"实付 {order['amount']:.2f} 元）当前状态是「{order['status']}」。")
                ship = self.tools.shipment(order_no)
                if ship:
                    citations.append(f"shipments 表 · 订单号 {order_no}")
                    evidence.append({"kind": "table", "table": "shipments", "key": order_no})
                    lines.append(
                        f"物流：{ship['carrier']} {ship['tracking']}，{ship['state']}，"
                        f"最新节点「{ship['last_node']}」（{ship['updated_at']}）。")
                else:
                    lines.append("这个订单还没生成物流单号，通常要等仓库出库。")
            else:
                trace.append(f"订单 {order_no} 在库里查不到")
                lines.append(f"我这边没查到订单号 {order_no}，麻烦核对一下单号是不是写全了。")

        # 2) 商品类问题：先查商品与库存
        product = None
        if intent == "售前" and not order_no:
            product = self.tools.find_product(query)
            if product:
                stock = self.tools.stock(product["sku"])
                citations.append(f"products 表 · {product['sku']}")
                evidence.append({"kind": "table", "table": "products", "key": product["sku"]})
                lines.append(
                    f"{product['name']}（{product['sku']}）售价 {product['price']:.0f} 元，"
                    f"规格：{product['specs']}，保修：{product['warranty']}，"
                    f"发货仓：{product['ship_from']}。")
                if stock:
                    citations.append(f"inventory 表 · {product['sku']}")
                    evidence.append({"kind": "table", "table": "inventory", "key": product["sku"]})
                    if stock["on_hand"] > 0:
                        lines.append(f"库存还有 {stock['on_hand']} 件，可以下单。")
                    else:
                        lines.append("这个规格目前库存为 0，暂时发不了货。")
            else:
                trace.append("商品库里没匹配到商品")

        # 3) 政策类问题：走双路检索，必须带来源
        fused, detail = self.retrieve(query)
        trace.append(f"检索: bm25={detail['bm25']} vector={detail['vector']} fused={detail['fused']}")
        chosen = self.select_docs(query, fused)
        if chosen:
            pick = chosen[0]
            trace.append(
                f"选中 {pick['doc_id']}：覆盖率 {pick['coverage']:.2f}、"
                f"标题命中 {pick['title_hits']}（阈值 {REFUSE_OVERLAP}）")
            citations.append(self._cite_doc(pick["doc_id"]))
            evidence.append({"kind": "doc", "doc_id": pick["doc_id"],
                             "title": self.titles[pick["doc_id"]]})
            lines.append("按规则：" + self.texts[pick["doc_id"]])
            best_score = pick["coverage"]
        else:
            trace.append(f"没有文档过依据闸门（阈值 {REFUSE_OVERLAP}）")
            best_score = 0.0

        used_docs = [e["doc_id"] for e in evidence if e["kind"] == "doc"]

        # 4) 高风险动作：给规则 + 转人工
        if risk_hits:
            reason = RISK_ACTIONS[risk_hits[0]]
            escalated = True

        # 5) 什么都没拿到就明确拒答
        if not lines and not used_docs:
            ticket = self._escalate(query, "没有检索到依据")
            return Answer(
                query=query, intent=intent,
                reply=(f"这个问题我这边没有查到可以引用的依据，不能凭印象答你。"
                       f"已经记下来转人工（工单 {ticket}），人工客服 9:00-21:00 在线，"
                       f"会在这个时段回你。"),
                citations=[], escalated=True,
                escalate_reason="没有检索到依据，转人工",
                confidence=0.0, refused=True, trace=trace, evidence=evidence,
            )

        if escalated:
            ticket = self._escalate(query, reason)
            lines.append(f"这件事（{reason}）需要人工确认，已经给你建了工单 {ticket}，"
                         f"人工客服 9:00-21:00 在线，会尽快处理。")

        if not lines:
            lines.append("这个问题我这边没有查到可以引用的依据，建议转人工确认。")

        reply = "\n".join(lines)
        # 有文档依据就用覆盖率当置信度，只有库表取数时按工具结果给一个高值
        if used_docs:
            confidence = round(best_score, 2)
        elif evidence:
            confidence = 0.90
        else:
            confidence = 0.0
        return Answer(
            query=query, intent=intent, reply=reply, citations=citations,
            escalated=escalated, escalate_reason=reason if escalated else "",
            confidence=confidence, refused=False, trace=trace, evidence=evidence,
        )


def build_agent(db_path: str | Path | None = None) -> Agent:
    return Agent(kb_data.seed(db_path or DEFAULT_DB))


def run_eval(db_path: str | Path | None = None) -> dict[str, Any]:
    """跑一遍评测用例，输出三项指标与失败明细。

    三项口径：
      意图准确率 = 路由判对的条数 / 总条数
      回答正确率 = 该答的答对上（事实都在、带了来源）的比例，拒答不算分
      拒答正确率 = 该拒的拒了 / 该拒的总数；误答（本来有依据却拒了）单独记
    """
    agent = build_agent(db_path)
    cases = kb_data.CASES
    intent_ok = 0
    answer_ok = answer_total = 0
    refuse_ok = refuse_total = 0
    false_refuse: list[str] = []
    failures: list[dict[str, Any]] = []

    for case in cases:
        query = str(case["q"])
        result = agent.answer(query)
        want_intent = str(case["intent"])
        want_mode = str(case["mode"])
        must = [str(m) for m in case["must"]]  # type: ignore[index]

        if result.intent == want_intent:
            intent_ok += 1
        else:
            failures.append({"q": query, "why": f"意图 {result.intent} != {want_intent}"})

        if want_mode == "refuse":
            refuse_total += 1
            if result.refused:
                refuse_ok += 1
            else:
                failures.append({"q": query, "why": f"应当拒答，实际答了：{result.reply[:40]}"})
            continue

        if result.refused:
            false_refuse.append(query)
            failures.append({"q": query, "why": "有依据却拒答了"})
            if want_mode == "answer":
                answer_total += 1
            continue

        missing = [m for m in must if m not in result.reply]
        if not result.citations:
            missing.append("（没有来源引用）")
        if want_mode == "answer":
            answer_total += 1
            if not missing:
                answer_ok += 1
        elif want_mode == "escalate" and not result.escalated:
            missing.append("（没有转人工）")
        if missing:
            failures.append({"q": query, "why": "缺：" + "、".join(missing)})

    total = len(cases)
    return {
        "total": total,
        "intent": {"ok": intent_ok, "total": total, "rate": round(intent_ok / total, 4)},
        "answer": {"ok": answer_ok, "total": answer_total,
                   "rate": round(answer_ok / max(1, answer_total), 4)},
        "refuse": {"ok": refuse_ok, "total": refuse_total,
                   "rate": round(refuse_ok / max(1, refuse_total), 4)},
        "false_refuse": false_refuse,
        "failures": failures,
        "tickets": len(agent.tickets),
    }

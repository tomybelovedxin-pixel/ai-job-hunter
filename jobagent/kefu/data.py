"""客服原型的数据底座：一张业务库（商品/库存/订单/物流）+ 一份知识库文档集。

数据是演示用的假数据，但结构是真的：工具去查库，检索去查文档，
回答里必须带出来源，查不到就拒答。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    sku        TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    price      REAL NOT NULL,
    specs      TEXT,
    warranty   TEXT,
    ship_from  TEXT
);
CREATE TABLE IF NOT EXISTS inventory (
    sku     TEXT PRIMARY KEY REFERENCES products(sku),
    on_hand INTEGER NOT NULL,
    locked  INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS orders (
    order_no   TEXT PRIMARY KEY,
    buyer      TEXT,
    sku        TEXT,
    qty        INTEGER,
    amount     REAL,
    status     TEXT,
    created_at TEXT,
    address    TEXT
);
CREATE TABLE IF NOT EXISTS shipments (
    order_no   TEXT PRIMARY KEY REFERENCES orders(order_no),
    carrier    TEXT,
    tracking   TEXT,
    state      TEXT,
    last_node  TEXT,
    updated_at TEXT
);
"""

PRODUCTS = [
    ("SKU-A100", "静音落地风扇 A100", 399.0, "三档风速 / 12 寸 / 白色",
     "整机一年，电机三年", "泉州仓"),
    ("SKU-B200", "折叠电脑桌 B200", 189.0, "60x40cm / 承重 30kg / 原木色",
     "一年，非人为损坏换新", "广州仓"),
    ("SKU-C300", "保温杯 C300", 129.0, "500ml / 316 不锈钢 / 雾灰",
     "杯体一年，密封圈属耗材不保", "泉州仓"),
]

INVENTORY = [("SKU-A100", 42, 3), ("SKU-B200", 0, 0), ("SKU-C300", 260, 11)]

ORDERS = [
    ("202610120001", "林女士", "SKU-A100", 1, 399.0, "已发货", "2026-10-08 10:12",
     "福建省泉州市丰泽区"),
    ("202610120002", "黄先生", "SKU-B200", 2, 378.0, "待发货", "2026-10-11 19:40",
     "福建省厦门市思明区"),
    ("202610120003", "陈女士", "SKU-C300", 1, 129.0, "已完成", "2026-10-01 09:05",
     "广东省深圳市南山区"),
]

SHIPMENTS = [
    ("202610120001", "顺丰", "SF1234567890", "运输中", "已到达 泉州转运中心",
     "2026-10-12 08:30"),
    ("202610120003", "中通", "ZT9988776655", "已签收", "已签收，签收人：本人",
     "2026-10-04 15:20"),
]

# 知识库：一篇文档一条，检索要能指到具体是哪一条
DOCS = [
    {
        "doc_id": "kb-001",
        "title": "发货时效与仓库说明",
        "text": ("泉州仓商品在每日 16:00 前付款当天发出，16:00 之后顺延到次日。"
                 "广州仓商品统一在付款后 48 小时内发出。大促期间可能再延 24 小时。"
                 "预售商品按商品页标注的发货时间为准。"),
    },
    {
        "doc_id": "kb-002",
        "title": "七天无理由退货规则",
        "text": ("签收次日起七天内，商品不影响二次销售可以无理由退货，运费买家承担；"
                 "质量问题由商家承担来回运费。定制类、拆封的贴身用品、耗材不在无理由范围内。"
                 "退货需要保持外包装、配件、赠品完整。"),
    },
    {
        "doc_id": "kb-003",
        "title": "退款到账时间",
        "text": ("退货仓库签收后 48 小时内完成质检并发起退款。原路退回，"
                 "微信与支付宝一般 1 到 3 个工作日到账，银行卡 3 到 7 个工作日。"
                 "退款金额按实付金额计算，已使用的优惠券不退回。"),
    },
    {
        "doc_id": "kb-004",
        "title": "换货流程",
        "text": ("支持同款同规格换货。在订单里发起换货申请，寄回后仓库质检通过即发出新件。"
                 "换货只支持同款同规格，换其他型号需要走退货再下单。"),
    },
    {
        "doc_id": "kb-005",
        "title": "保修范围与凭证",
        "text": ("整机保修期内非人为损坏免费维修，需提供订单号。进水、摔损、私自拆机属于人为损坏。"
                 "密封圈、滤网、电池这类耗材不在保修范围。"),
    },
    {
        "doc_id": "kb-006",
        "title": "发票开具说明",
        "text": ("支持开具电子普通发票，下单时在备注里填写抬头与税号，发货后 3 个工作日内发出。"
                 "专票需要提供完整开票资料，由财务单独处理。"),
    },
    {
        "doc_id": "kb-007",
        "title": "运费与包邮门槛",
        "text": ("单笔订单满 99 元包邮，未满收取 8 元运费。新疆、西藏、内蒙古部分地区加收 12 元。"
                 "偏远地区不承诺时效。"),
    },
    {
        "doc_id": "kb-008",
        "title": "修改收货地址说明",
        "text": ("未发货的订单可以修改收货地址，需要联系人工客服核实身份后修改。"
                 "已发货的订单不能改地址，可以联系快递尝试改派，成功率不保证。"),
    },
    {
        "doc_id": "kb-009",
        "title": "优惠券与活动价说明",
        "text": ("优惠券在有效期内使用，过期不补。活动价与优惠券能否叠加以商品页标注为准。"
                 "退货时按实付金额退款，优惠券不退回。"),
    },
    {
        "doc_id": "kb-010",
        "title": "客服工作时间",
        "text": "人工客服每天 9:00 到 21:00 在线，其余时间留言，次日 10:00 前回复。",
    },
]


def seed(db_path: str | Path) -> sqlite3.Connection:
    """建一个可直接查询的演示库；已存在则复用，不重复插数据。"""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    if conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0:
        conn.executemany("INSERT INTO products VALUES (?,?,?,?,?,?)", PRODUCTS)
        conn.executemany("INSERT INTO inventory VALUES (?,?,?)", INVENTORY)
        conn.executemany("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?)", ORDERS)
        conn.executemany("INSERT INTO shipments VALUES (?,?,?,?,?,?)", SHIPMENTS)
        conn.commit()
    return conn


# 评测用例：意图是必须判对的，mode 说明该走哪条路，must 是回答里必须出现的事实。
# 期望值都来自上面这份演示数据，不是我事后按结果倒着写的。
CASES: list[dict[str, object]] = [
    {"q": "下单多久发货", "intent": "物流", "mode": "answer", "must": ["16:00"]},
    {"q": "广州仓的商品几天发出来", "intent": "物流", "mode": "answer", "must": ["48 小时"]},
    {"q": "已经发货了吗 订单号 202610120001", "intent": "物流", "mode": "answer",
     "must": ["已发货", "顺丰"]},
    {"q": "202610120001 现在到哪了", "intent": "物流", "mode": "answer",
     "must": ["泉州转运中心"]},
    {"q": "202610120002 什么时候发货", "intent": "物流", "mode": "answer",
     "must": ["待发货"]},
    {"q": "订单 202610129999 到哪了", "intent": "物流", "mode": "answer",
     "must": ["没查到订单号"]},
    {"q": "七天无理由退货运费谁承担", "intent": "售后", "mode": "escalate",
     "must": ["运费买家承担", "工单"]},
    {"q": "退款多久到账", "intent": "售后", "mode": "escalate",
     "must": ["48 小时", "工单"]},
    {"q": "保修范围包括哪些", "intent": "售后", "mode": "answer", "must": ["人为损坏"]},
    {"q": "发票怎么开", "intent": "售后", "mode": "answer", "must": ["电子普通发票"]},
    {"q": "改收货地址怎么弄", "intent": "订单", "mode": "escalate",
     "must": ["人工", "工单"]},
    {"q": "静音落地风扇 A100 多少钱", "intent": "售前", "mode": "answer", "must": ["399"]},
    {"q": "折叠电脑桌 B200 有货吗", "intent": "售前", "mode": "answer", "must": ["库存为 0"]},
    {"q": "保温杯 C300 什么材质", "intent": "售前", "mode": "answer", "must": ["316"]},
    {"q": "满多少包邮", "intent": "售前", "mode": "answer", "must": ["99 元包邮"]},
    {"q": "优惠券能和活动价一起用吗", "intent": "售前", "mode": "answer",
     "must": ["优惠券能否叠加"]},
    {"q": "你们的客服几点上班", "intent": "其他", "mode": "answer", "must": ["9:00"]},
    {"q": "帮我订一张明天去北京的机票", "intent": "其他", "mode": "refuse", "must": []},
    {"q": "你们这个能便宜点吗", "intent": "其他", "mode": "refuse", "must": []},
    {"q": "帮我写一段产品宣传文案", "intent": "其他", "mode": "refuse", "must": []},
]

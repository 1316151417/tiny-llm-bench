"""写死的测试数据集（只读）。

两类样本：
- 单轮（single）：一条 messages 一次请求，短/中/长看输入长度对 TTFT 的影响
- 多轮（multi）：turns 是"逐轮增长的 messages 列表"，回放固定剧本，
  前缀重复触发服务商的 prompt cache，用于测缓存命中率
"""
from __future__ import annotations

from typing import Any


def _filler_article(target_chars: int) -> str:
    """拼接一段确定性的长文本（带编号避免完全重复的段落）。"""
    paragraphs = [
        "城市轨道交通是现代大城市公共交通的骨干。它与地面公交相比具有运量大、速度快、准点率高的特点，"
        "在高峰期可以单向每小时运送数万人次，是缓解拥堵最有效的手段之一。",
        "一条地铁线路的诞生要经历线网规划、可行性研究、初步设计、施工图设计等多个阶段，"
        "随后是数年的土建施工、轨道铺设、机电安装与联调联试，任何环节的延误都会影响整体开通时间。",
        "信号系统被称为地铁的“大脑”。从早期的固定闭塞到移动闭塞，列车追踪间隔从几分钟缩短到九十秒以内，"
        "这让同样一条线路在不增加土建投资的情况下运力翻倍。",
        "车站设计也不只是工程问题。客流动线决定了换乘通道的宽度，出入口布局影响着周边地块的开发强度，"
        "而无障碍设施的质量直接关系到老年人与残障人士能否真正使用这套系统。",
        "运营成本中占比最高的是电力与人力。近年来多地尝试直购电、再生制动能量回收、智慧车站无人值守等手段，"
        "试图在安全与服务水平不下降的前提下压低每车公里的综合成本。",
        "客流预测是所有规划的基础。手机信令、公交刷卡与地铁进出站数据融合后，规划者可以还原出"
        "一张动态的出行期望线图，进而判断新线路的走向与设站位置是否合理。",
    ]
    out: list[str] = []
    i = 0
    while sum(len(s) for s in out) < target_chars:
        out.append(f"（{i + 1}）{paragraphs[i % len(paragraphs)]}")
        i += 1
    return "\n".join(out)


def _script_to_turns(script: list[dict[str, str]]) -> list[list[dict[str, str]]]:
    """把 [u1,a1,u2,a2,...] 的剧本切成逐轮增长的 messages 列表。

    第 i 轮发送 script[:2i+1]（含第 i 个 user，不含其后的 assistant）。
    """
    n_user = (len(script) + 1) // 2
    return [script[: 2 * i + 1] for i in range(n_user)]


_TRAVEL_SCRIPT = [
    {"role": "user", "content": "我打算十一假期去成都玩 5 天，两个人，预算一万五左右，帮我做个行程框架。"},
    {"role": "assistant", "content":
        "好的，按 5 天 4 晚、双人预算 1.5 万来做框架：\n"
        "D1 抵达+春熙路太古里商圈，晚上吃火锅；\n"
        "D2 熊猫基地（上午）+ 文殊院 + 建设路小吃；\n"
        "D3 都江堰一日（高铁往返）；\n"
        "D4 乐山大佛或青城山二选一；\n"
        "D5 杜甫草堂 + 采购返程。\n"
        "住宿建议春熙路或骡马市地铁沿线，四晚约 3200 元。这个框架你想先调整哪一天？"},
    {"role": "user", "content": "熊猫基地听说要早点去，几点进门合适？从春熙路怎么过去最快？"},
    {"role": "assistant", "content":
        "旺季建议 7:30 开园即入，直奔月亮产房再看成年园区，10 点后人流量会明显上来。"
        "从春熙路过去：地铁 3 号线到熊猫大道站，转景区摆渡车，全程约 50 分钟；"
        "打车约 35 分钟但早高峰不稳。如果想再早，也可以选直通景区的旅游专线巴士。"},
    {"role": "user", "content": "那 D3 都江堰那天，是玩都江堰景区就够了，还是可以加上青城山？老人走得动但不适合太陡。"},
    {"role": "assistant", "content":
        "带老人建议只主玩都江堰：离堆公园进，看过宝瓶口、飞沙堰、鱼嘴后走安澜索桥到二王庙，"
        "全程平缓步行为主，约 3-4 小时。青城前山虽索道可代步，但上下换乘折腾，前后山合计要一整天，"
        "和都江堰拼在同一天会很赶。可以让老人在南桥边茶座休息，年轻人轮流去逛。"},
    {"role": "user", "content": "好，那帮我估算一下这 5 天的大致花费拆分，机票不算，从落地成都开始。"},
    {"role": "assistant", "content":
        "落地后预算拆分（双人）：住宿四晚约 3200；餐饮每天 400×5=2000；"
        "市内交通+都江堰往返高铁约 600；门票（熊猫基地、都江堰、杜甫草堂等）约 900；"
        "购物与机动 1500。合计约 8200，距离 1.5 万上限还有充足余量，可以升级一晚更好的酒店或加一顿高端川菜。"},
    {"role": "user", "content": "最后帮我列一个出发前一周的准备清单，主要是证件、预订和天气相关的。"},
    {"role": "assistant", "content":
        "出发前一周清单：\n1) 证件：身份证、老人优惠证件；\n"
        "2) 预订：熊猫基地与都江堰门票（实名制，提前 3-7 天放票）、餐厅热门店取号；\n"
        "3) 天气：十一成都多阴雨，备轻便雨衣与防滑鞋，山区温差大带一件薄外套；\n"
        "4) 健康：常用肠胃药（饮食辛辣）与晕车贴；\n5) 交通：天府通 App 开通乘车码，高铁票用 12306 预购。"},
    {"role": "user", "content": "如果遇到下雨，有没有备选的室内行程可以整体替换某一天？"},
    {"role": "assistant", "content":
        "雨天备选方案：\n"
        "1) 博物馆线：四川博物院 + 金沙遗址 + 成都博物馆，全程地铁可达；\n"
        "2) 茶馆慢生活：人民公园鹤鸣茶社 + 川剧变脸演出（蜀风雅韵）；\n"
        "3) 商圈室内：IFS 熊猫打卡 + 太古里方所书店 + 环球中心。\n"
        "雨天优先替换 D4 的山岳行程，都江堰雨天体验尚可但索桥会关闭。"},
    {"role": "user", "content": "住宿如果想升级一晚庆祝纪念日，成都有什么有特色又不太贵的选择？"},
    {"role": "assistant", "content":
        "三个方向：\n1) 老宅改造精品酒店（太古里/宽窄巷子周边），庭院房型约 900-1400/晚，闹中取静；\n"
        "2) 高层江景房（九眼桥或金融城段），夜景好，约 700-1000/晚，记得要高楼层朝江侧；\n"
        "3) 熊猫主题亲子民宿，趣味性强但隔音一般，适合带孩子的场景。\n"
        "建议放在 D4 晚上入住，第二天退房去机场动线也顺。"},
    {"role": "user", "content": "带老人出行，整个行程的节奏上还有什么要特别提醒的？"},
    {"role": "assistant", "content":
        "节奏提醒：\n1) 每天只排一个“主要景点”，其余留给街区漫步，避免连续两天早起；\n"
        "2) 地铁优先选有电梯的出入口，提前在 App 里查无障碍设施；\n"
        "3) 川菜点单备注“微辣鸳鸯锅”，肠胃药随身；\n"
        "4) 每天午后留 1 小时茶馆或酒店休息，比赶场更重要；\n"
        "5) 返程日不安排任何景点，只留采购和去机场。"},
]

_TECH_SCRIPT = [
    {"role": "user", "content": "我们要给内部工具加一个实时协作编辑功能，前后端分别怎么选型？团队 10 人，偏 Python。"},
    {"role": "assistant", "content":
        "这个规模建议从简：\n"
        "后端：Python + FastAPI，WebSocket 长连接，协作状态用 Redis 存共享文档快照与在线成员；\n"
        "前端：直接上 Yjs 或 Automerge 这类 CRDT 库，配合 y-websocket，编辑冲突的硬骨头交给成熟算法；\n"
        "存储：文档持久化用 PostgreSQL，快照 + 增量更新日志双写。\n"
        "不建议自研 OT 算法，10 人团队维护成本过高。"},
    {"role": "user", "content": "CRDT 和 OT 的取舍具体差在哪？我们文档主要是富文本还是表格场景影响选择吗？"},
    {"role": "assistant", "content":
        "核心差异：OT 需要中央服务器做操作变换，强一致但实现复杂、网络分区时体验差；"
        "CRDT 各端本地即可合并，天然支持离线与 P2P，代价是元数据开销和内存占用。\n"
        "场景影响：纯富文本 Yjs 非常成熟；表格/多维数据建议看 Yjs 的 Y.Array 嵌套结构是否够用，"
        "复杂单元格公式联动可能要在 CRDT 之上再封装一层业务合并逻辑。"},
    {"role": "user", "content": "历史版本和回放怎么做？产品想要“查看任意两个版本之间谁改了什么”。"},
    {"role": "assistant", "content":
        "做法：把 CRDT 更新流按事务为单位落到 updates 表（含作者、时间戳），"
        "定期合并成 snapshot 降低回放成本。查询两版本差异时，从较近的 snapshot 起重放到目标版本，"
        "用 CRDT 自带的 diff 能力输出按作者聚合的变更集。注意 snapshot 间隔 trade-off："
        "太密存储翻倍，太疏回放变慢，建议按 500 个事务或 10MB 增量触发合并。"},
    {"role": "user", "content": "并发 500 人同时编辑一篇文档，这个架构哪里会先扛不住？"},
    {"role": "assistant", "content":
        "瓶颈排序大概是：\n1) 单文档广播扇出——一篇热文档 500 连接，每次 keystroke 广放大 O(n)，"
        "需要合并 30-50ms 窗口内的更新再发；\n"
        "2) Redis 单 key 热点——大文档快照频繁读写，考虑分片或本地缓存 + 版本号校验；\n"
        "3) PostgreSQL 快照写入——用异步批量 flush 避免 WAL 压力。\n"
        "真实场景里 500 人编辑“同一篇”文档很少见，更多是多文档分散负载，架构上限其实够用。"},
    {"role": "user", "content": "好，最后给我一个两阶段落地计划：先跑通最小闭环，再做生产加固。"},
    {"role": "assistant", "content":
        "阶段一（3-4 周最小闭环）：FastAPI + y-websocket 起协作服务，前端接入 Yjs rich-text，"
        "文档存 PG 只做快照，先不做历史 diff。\n"
        "阶段二（生产加固）：updates 日志 + snapshot 合并任务、按事务的历史回放接口、"
        "广播合并窗口、Redis 分片、监控告警（连接数、广播延迟、快照落后量）。\n"
        "两个阶段之间留一周灰度，让真实用户先在测试文档里蹚一遍。"},
    {"role": "user", "content": "如果半年后这个功能要 SaaS 化开放给外部客户，架构上现在就应该预留什么？"},
    {"role": "assistant", "content":
        "现在就要留的口子：\n1) 文档模型里加 tenant_id 且所有查询带租户过滤，后补成本极高；\n"
        "2) WebSocket 网关与业务服务分层，为将来按租户限流和隔离部署留缝；\n"
        "3) updates 日志从第一天就记录操作者身份，SaaS 审计必需；\n"
        "4) 快照存储抽象成接口，便于换对象存储；\n"
        "5) 计费计量点（连接时长、文档数、存储量）先埋点不启用。\n"
        "CRDT 协议本身与多租户不冲突，主要改造在存储与网关层。"},
]


def _build_datasets() -> list[dict[str, Any]]:
    medium_article = _filler_article(2200)
    long_article = _filler_article(9000)

    return [
        {
            "id": "basic",
            "name": "基础单轮（短/中/长）",
            "description": "三条不同输入长度的单轮 prompt，观察输入长度对 TTFT 的影响；重复轮数亦可观察 prompt cache",
            "cases": [
                {
                    "kind": "single",
                    "id": "short",
                    "name": "短输入",
                    "messages": [
                        {"role": "user", "content": "用一句话解释什么是快速排序的平均时间复杂度，并说明为什么。"}
                    ],
                },
                {
                    "kind": "single",
                    "id": "medium",
                    "name": "中输入（~1k token）",
                    "messages": [
                        {"role": "user",
                         "content": f"下面是一篇关于城市轨道交通的长文，请用中文写一段 150 字左右的摘要，并列出 3 个关键数字或结论：\n\n{medium_article}"}
                    ],
                },
                {
                    "kind": "single",
                    "id": "long",
                    "name": "长输入（~4k token）",
                    "messages": [
                        {"role": "user",
                         "content": f"阅读以下材料，回答三个问题：1) 信号系统升级对运力的量化影响；2) 客流预测的数据来源；3) 运营降本的三条主要手段。每个问题用 2-3 句话回答。\n\n{long_article}"}
                    ],
                },
            ],
        },
        {
            "id": "cache",
            "name": "多轮缓存（剧本回放）",
            "description": "固定剧本逐轮回放，历史逐轮累积：第 1 轮缓存 miss，后续轮次应命中此前前缀（若服务商支持）",
            "cases": [
                {
                    "kind": "multi",
                    "id": "conv-travel",
                    "name": "旅行规划对话（8 轮）",                    "turns": _script_to_turns(_TRAVEL_SCRIPT),
                },
                {
                    "kind": "multi",
                    "id": "conv-tech",
                    "name": "技术选型对话（6 轮）",
                    "turns": _script_to_turns(_TECH_SCRIPT),
                },
            ],
        },
    ]


DATASETS: list[dict[str, Any]] = _build_datasets()


def get_dataset(dataset_id: str) -> dict[str, Any] | None:
    for d in DATASETS:
        if d["id"] == dataset_id:
            return d
    return None


def dataset_meta() -> list[dict[str, Any]]:
    """给前端的元信息（不含完整 prompt 正文，减小 payload）。"""
    out = []
    for d in DATASETS:
        cases = []
        n_requests = 0
        for c in d["cases"]:
            n_turns = 1 if c["kind"] == "single" else len(c["turns"])
            n_requests += n_turns
            prompt_chars = sum(
                len(m["content"]) for m in (c["messages"] if c["kind"] == "single" else c["turns"][-1])
            )
            cases.append({"id": c["id"], "name": c["name"], "kind": c["kind"], "turns": n_turns,
                          "last_prompt_chars": prompt_chars})
        out.append({"id": d["id"], "name": d["name"], "description": d["description"],
                    "cases": cases, "requests_per_round": n_requests})
    return out

"""Explicit research topics, serialized fresh sources, finite time budget, no database."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from decimal import localcontext
from functools import partial
from typing import Literal

from app.schemas.advanced_research import (
    AdvancedQuery,
    AdvancedResponse,
    ResearchBlock,
    ResearchColumn,
    ResearchTopic,
)
from app.schemas.analysis import AnalysisError, AnalysisSection
from app.services import research_transforms as transform
from app.services.errors import ServiceError
from app.services.finmind import FinMindService, Row, source_error
from app.services.stocks import StockService


@dataclass(frozen=True)
class Spec:
    dataset: str
    title: str
    columns: tuple[tuple[str, str], ...]
    normalize: Callable[[list[Row], str, date, date], list[Row]]
    plan: Literal["Free", "Backer", "Sponsor"] = "Free"


DATE = ("date", "日期")
PRICE = (DATE, ("close", "收盤價（元）"))
FINANCE = (
    DATE,
    ("label", "來源科目"),
    ("subject", "科目代碼"),
    ("value", "來源數值"),
    ("unit", "單位"),
    ("basis", "期間口徑"),
    ("quarter_value", "可計算單季值（元）"),
)
SPECS = {
    "valuation": Spec(
        "TaiwanStockPER",
        "歷史估值",
        (DATE, ("pe", "本益比（倍）"), ("pb", "股價淨值比（倍）"), ("yield", "殖利率（%）")),
        transform.valuations,
    ),
    "dividends": Spec(
        "TaiwanStockDividend",
        "歷年股利政策",
        (
            ("date", "權利分派基準日"),
            ("year", "股利所屬期間"),
            ("cash", "現金股利（元／股）"),
            ("stock", "股票股利（元／股）"),
            ("cash_ex_date", "除息日"),
            ("stock_ex_date", "除權日"),
            ("payment_date", "現金發放日"),
            ("announcement_date", "來源公告日"),
        ),
        transform.dividends,
    ),
    "income": Spec(
        "TaiwanStockFinancialStatements",
        "綜合損益表",
        FINANCE,
        partial(transform.financial_rows, kind="income"),
    ),
    "balance": Spec(
        "TaiwanStockBalanceSheet",
        "資產負債表",
        FINANCE,
        partial(transform.financial_rows, kind="balance"),
    ),
    "cashflow": Spec(
        "TaiwanStockCashFlowsStatement",
        "現金流量表",
        FINANCE,
        partial(transform.financial_rows, kind="cashflow"),
    ),
    "adjusted": Spec("TaiwanStockPriceAdj", "還原收盤價", PRICE, transform.price_rows, "Backer"),
    "prices": Spec("TaiwanStockPrice", "原始收盤價", PRICE, transform.price_rows),
    "ownership": Spec(
        "TaiwanStockHoldingSharesPer",
        "股東持股分級",
        (
            DATE,
            ("level", "來源持股級距（股）"),
            ("people", "人數"),
            ("shares", "持有股數"),
            ("percent", "持股比例（%）"),
        ),
        transform.ownership,
        "Backer",
    ),
    "brokers": Spec(
        "TaiwanStockTradingDailyReport",
        "券商分點買賣排行",
        (
            DATE,
            ("broker_id", "分點代碼"),
            ("broker", "分點名稱"),
            ("buy", "買進股數"),
            ("sell", "賣出股數"),
            ("net", "買賣超股數"),
        ),
        transform.brokers,
        "Sponsor",
    ),
    "holdings": Spec(
        "TaiwanStockActiveETFHolding",
        "主動式 ETF 每日持股",
        (
            DATE,
            ("component", "成分代碼"),
            ("name", "成分名稱"),
            ("asset_type", "資產類型"),
            ("shares", "持有數量（依資產）"),
            ("weight", "權重（%）"),
            ("market_value", "市值（來源幣別）"),
            ("currency", "幣別"),
        ),
        partial(transform.etf_rows, change=False),
        "Sponsor",
    ),
    "holding_changes": Spec(
        "TaiwanStockActiveETFHoldingChange",
        "主動式 ETF 持股異動",
        (
            DATE,
            ("component", "成分代碼"),
            ("name", "成分名稱"),
            ("buy", "持倉增加數量"),
            ("sell", "持倉減少數量"),
            ("net", "淨變化數量"),
        ),
        partial(transform.etf_rows, change=True),
        "Sponsor",
    ),
    "events": Spec(
        "TaiwanStockDividendResult",
        "除權息結果",
        (
            DATE,
            ("kind", "事件"),
            ("before", "除權息前收盤（元）"),
            ("reference", "參考價（元）"),
            ("dividend", "來源權息值"),
        ),
        transform.event_rows,
    ),
    "splits": Spec("TaiwanStockSplitPrice", "分割事件", (DATE,), transform.action_dates),
    "reductions": Spec(
        "TaiwanStockCapitalReductionReferencePrice", "減資事件", (DATE,), transform.action_dates
    ),
    "par_changes": Spec(
        "TaiwanStockParValueChange", "面額變更事件", (DATE,), transform.action_dates
    ),
}
TOPICS: dict[ResearchTopic, tuple[str, ...]] = {
    "valuation": ("valuation",),
    "dividends": ("dividends",),
    "financials": ("income", "balance", "cashflow"),
    "adjusted": ("adjusted", "prices"),
    "ownership": ("ownership",),
    "brokers": ("brokers",),
    "etf": ("holdings", "holding_changes"),
    "fill": ("events", "prices", "splits", "reductions", "par_changes"),
}
NOTES: dict[ResearchTopic, list[str]] = {
    "valuation": ["本益比與股價淨值比不大於零時留空；區間位置僅描述本次可用歷史，不代表合理價。"],
    "dividends": [
        "保留來源年度／季度，不把不同分派期間當成全年股利。基準日、公告日、除息日與發放日分別呈現。"
    ],
    "financials": [
        "日期為財報期末，並非公告時間；來源可能修訂歷史資料，不提供當時已知資訊的回測保證。",
        "損益表金額通常為單季；僅半年／年報的公司可能為半年值。"
        "EPS 保留原始單期值，不跨季加總，避免配股／分割的股數基準不同。",
        "資產負債為期末存量；現金流為年初累計，只有同年上一季科目齊全才相減為單季。"
        "EPS 單位元／股，_per 科目為百分比，其他金額為新台幣元。",
        "金融業等科目定義不同，以來源科目名稱為準；缺少適用科目時不補估毛利率或營業利益率。",
    ],
    "adjusted": [
        "還原與原始價格分開標示，不混算均線；還原價由來源提供，可能隨後續公司行動調整，不能直接當成實際成交價或含再投資報酬。"
    ],
    "ownership": ["持股級距沿用來源，以股為單位；各級距分別查看，不把合計或差異調整列重複加總。"],
    "brokers": [
        "單次只查指定一天，合併同分點各成交價的買進／賣出股數，依買賣超排序；分點不等於最終投資人身分。"
    ],
    "etf": [
        "單次最多 31 天；不是每檔 ETF 都有來源資料。股票數量為股、債券為面額、"
        "衍生商品為口數，保留現金／負債與負數部位。",
        "來源市值為零可能代表未揭露，本頁顯示缺值。持股異動含申購／贖回影響，"
        "不等於經理人的主動買賣；來源未提供異動資產類型時不推定。",
    ],
    "fill": [
        "收盤填息以除息日起第一個有效收盤價達到除息前收盤價為準，非盤中填息。只計算純除息事件。",
        "觀察在下一次除權息、分割、減資或面額變更前停止；必要來源失敗則不計算。未填息只代表本次觀察區間內未達標。",
        "交易筆數含除息日，僅計來源有有效收盤價的日期；期末回補率可小於零或超過 100%，"
        "與歷史上是否已填息分別呈現。",
    ],
}


def block(key: str) -> ResearchBlock:
    spec = SPECS[key]
    return ResearchBlock(
        id=key,
        title=spec.title,
        required_plan=spec.plan,
        columns=[ResearchColumn(key=k, label=label) for k, label in spec.columns],
        result=AnalysisSection(status="not_requested"),
    )


class AdvancedResearchService:
    def __init__(self, stocks: StockService, finmind: FinMindService):
        self.stocks, self.finmind = stocks, finmind

    async def research(
        self,
        code: str,
        query: AdvancedQuery,
        disconnected: Callable[[], Awaitable[bool]] | None = None,
    ) -> AdvancedResponse:
        async def check_connection():
            if disconnected is not None and await disconnected():
                raise ServiceError("REQUEST_CANCELLED", "查詢已取消。", 499)

        await check_connection()
        deadline = asyncio.get_running_loop().time() + 50
        identity = await self.stocks.quote(code)
        start, end = query.start_date, query.end_date
        assert start is not None
        blocks = [block(key) for key in TOPICS[query.topic]]
        stopped: AnalysisError | None = None
        for current in blocks:
            await check_connection()
            if stopped:
                current.result.error = stopped
                continue
            spec = SPECS[current.id]
            source_start = (
                date(start.year, 1, 1) if current.id in ("income", "balance", "cashflow") else start
            )
            try:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise source_error(503)
                async with asyncio.timeout(remaining):
                    rows, meta = await self.finmind.fetch(spec.dataset, code, source_start, end)
                with localcontext() as context:
                    context.prec = 64
                    normalized = spec.normalize(rows, code, source_start, end)
                current.result.data = [
                    row for row in normalized if (row.get("date") or "") >= start.isoformat()
                ]
                current.result.meta = meta
                if current.id in ("prices", "adjusted"):
                    meta.price_basis = (
                        "source_adjusted" if current.id == "adjusted" else "unadjusted"
                    )
                    meta.units = {"close": "TWD"}
                current.result.status = "ok"
            except (ServiceError, TimeoutError) as error:
                failure = error if isinstance(error, ServiceError) else source_error(503)
                current.result.status = "error"
                current.result.error = AnalysisError(code=failure.code, message=failure.message)
                if failure.code in ("FINMIND_RATE_LIMITED", "FINMIND_UNAVAILABLE"):
                    stopped = current.result.error

        if query.topic == "financials":
            income = blocks[0].result
            derived = ResearchBlock(
                id="earnings",
                title="單期 EPS 與獲利概覽",
                columns=[
                    ResearchColumn(key=k, label=v)
                    for k, v in (
                        DATE,
                        ("eps", "來源單期 EPS（元／股）"),
                        ("revenue", "來源單期營收（元）"),
                        ("net_income", "來源單期淨利（元）"),
                        ("gross_margin", "毛利率（%）"),
                        ("operating_margin", "營業利益率（%）"),
                    )
                ],
                result=AnalysisSection(status=income.status, error=income.error),
            )
            if income.status == "ok":
                derived.result.data, derived.result.meta = (
                    transform.earnings(income.data or []),
                    income.meta,
                )
            blocks.insert(0, derived)
        if query.topic == "fill":
            derived = ResearchBlock(
                id="fill_progress",
                title="收盤填息觀察",
                columns=[
                    ResearchColumn(key=k, label=v)
                    for k, v in (
                        DATE,
                        ("before", "填息目標（元）"),
                        ("reference", "除息參考價（元）"),
                        ("status", "觀察結果"),
                        ("fill_date", "首次收盤填息日"),
                        ("trading_days", "有效交易筆數"),
                        ("progress", "期末回補率（%）"),
                        ("observed_until", "最後觀察日"),
                        ("boundary", "後續事件邊界"),
                    )
                ],
                result=AnalysisSection(status="not_requested"),
            )
            if all(item.result.status == "ok" for item in blocks):
                derived.result.status = "ok"
                derived.result.data = transform.fill_progress(
                    blocks[0].result.data or [],
                    blocks[1].result.data or [],
                    [row for item in blocks[2:] for row in item.result.data or []],
                )
                derived.result.meta = blocks[1].result.meta
            else:
                derived.result.error = AnalysisError(
                    code="FINMIND_SOURCE_INCOMPLETE",
                    message="必要事件或價格來源未完整取得，本次不計算填息。",
                )
            blocks.insert(0, derived)
        return AdvancedResponse(
            code=code,
            name=identity.data.name,
            topic=query.topic,
            start_date=start,
            end_date=end,
            complete=all(item.result.status == "ok" for item in blocks),
            identity_source=identity.meta,
            notes=NOTES[query.topic],
            blocks=blocks,
        )

"""Request-local research composition. Sources are sequential; no price persistence."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, localcontext
from time import monotonic

from app.schemas.analysis import AnalysisError, AnalysisSection
from app.schemas.company_study import CompanyStudy, Metric, StudyQuery
from app.schemas.notices import CalendarQuery, RiskQuery
from app.services.advanced_research import SPECS, block
from app.services.errors import ServiceError
from app.services.finmind import FinMindService
from app.services.notices import NoticeService
from app.services.research_transforms import earnings, fill_progress
from app.services.stock_analysis import (
    history_points,
    institution_points,
    month_shift,
    revenue_points,
)
from app.services.stocks import StockService


def health(study: CompanyStudy) -> list[Metric]:
    blocks = {item.id: item for item in study.blocks}
    result = []

    def metric(
        key,
        label,
        unit,
        value=None,
        period=None,
        status=None,
        explanation="",
        limitation="單一指標不能判定公司好壞或決定買賣。",
    ):
        result.append(
            Metric(
                key=key,
                label=label,
                unit=unit,
                value=value,
                period=period,
                status=status or ("ok" if value is not None else "insufficient"),
                explanation=explanation,
                limitation=limitation,
            )
        )

    etf = study.security_kind == "etf"
    financial = bool(
        study.industry
        and any(word in study.industry for word in ("金融", "保險", "金控", "銀行", "證券"))
    )
    revenue = (study.revenue.data or [])[-1:]
    point = revenue[0] if revenue else None
    metric(
        "revenue_yoy",
        "月營收年增率",
        "%",
        point.yoy if point and not etf else None,
        point.month if point else None,
        "not_applicable" if etf else None,
        "和去年同月份相比的營收變動。需有去年同月且比較營收為正。",
    )
    income = blocks.get("income")
    income_rows = (income.result.data or []) if income and income.result.status == "ok" else []
    values = earnings(income_rows)
    latest = values[-1] if values else {}
    metric(
        "eps",
        "來源單期 EPS",
        "元／股",
        latest.get("eps") if not etf else None,
        latest.get("date"),
        "not_applicable" if etf else None,
        "該來源單期每股盈餘；沒有跨季加總。",
        "EPS期間及股數基準可能不同，不能直接加總為全年或推估合理價格。",
    )
    for key, label in [("gross_margin", "毛利率"), ("operating_margin", "營業利益率")]:
        metric(
            key,
            label,
            "%",
            latest.get(key) if not (etf or financial) else None,
            latest.get("date"),
            "not_applicable" if etf or financial else None,
            "同一期獲利科目除以營收；只在來源提供適用科目且營收為正時計算。",
            "金融業、ETF等不套用一般公司門檻；較高比率不等於股票值得買。",
        )
    balance = blocks.get("balance")
    rows = (balance.result.data or []) if balance and balance.result.status == "ok" else []
    period = max((r.get("date") or "" for r in rows), default=None)
    subjects = {r.get("subject"): r.get("value") for r in rows if r.get("date") == period}
    assets, liabilities = subjects.get("TotalAssets"), subjects.get("Liabilities")
    ratio = None
    if (
        not etf
        and assets is not None
        and liabilities is not None
        and Decimal(assets) > 0
        and Decimal(liabilities) >= 0
    ):
        with localcontext() as context:
            context.prec = 64
            ratio = format(Decimal(liabilities) / Decimal(assets) * 100, ".4f")
    metric(
        "debt_ratio",
        "負債比",
        "%",
        ratio,
        period,
        "not_applicable" if etf else None,
        "同一期總負債占總資產的比例。非正資產或異常負負債時不計算。",
        "產業的資金結構不同，不能用同一門檻直接判定好壞。",
    )
    cashflow = blocks.get("cashflow")
    rows = (cashflow.result.data or []) if cashflow and cashflow.result.status == "ok" else []
    period = max((r.get("date") or "" for r in rows), default=None)
    cash = next(
        (
            r
            for r in rows
            if r.get("date") == period and r.get("subject") == "CashFlowsFromOperatingActivities"
        ),
        {},
    )
    metric(
        "operating_cashflow",
        "營業活動現金流（年初累計）",
        "元",
        cash.get("value") if not etf else None,
        period,
        "not_applicable" if etf else None,
        "本業在來源年度開始至該期產生或使用的現金，沿用年初累計口徑。",
        "投資擴張或營運週期可能造成負值；負值不直接等於經營失敗。",
    )
    dividends = blocks.get("dividends")
    rows = (dividends.result.data or []) if dividends and dividends.result.status == "ok" else []
    latest_dividend = rows[-1] if rows else {}
    metric(
        "cash_dividend",
        "最近分派期現金股利",
        "元／股",
        latest_dividend.get("cash"),
        latest_dividend.get("year"),
        None,
        "最近來源分派紀錄的現金股利，不將不同年度或季度直接相加。",
        "過往股利不保證未來配息；尚未確認持股資格，不能當作確定應領金額。",
    )
    for item in result:
        if item.status == "insufficient":
            relevant = (
                study.revenue
                if item.key == "revenue_yoy"
                else income.result
                if item.key in {"eps", "gross_margin", "operating_margin"} and income
                else balance.result
                if item.key == "debt_ratio" and balance
                else cashflow.result
                if item.key == "operating_cashflow" and cashflow
                else dividends.result
                if item.key == "cash_dividend" and dividends
                else None
            )
            if relevant and relevant.status != "ok":
                item.status = "unavailable"
    return result


class CompanyStudyService:
    def __init__(self, stocks: StockService, source: FinMindService, notices: NoticeService):
        self.stocks, self.source, self.notices = stocks, source, notices

    async def research(
        self,
        code: str,
        query: StudyQuery,
        disconnected: Callable[[], Awaitable[bool]] | None = None,
    ) -> CompanyStudy:
        async def check():
            if disconnected is not None and await disconnected():
                raise ServiceError("REQUEST_CANCELLED", "查詢已取消。", 499)

        started = monotonic()
        await check()
        quote = await self.stocks.quote(code)
        assert query.start_date is not None
        study = CompanyStudy(
            code=code,
            name=quote.data.name,
            view=query.view,
            start_date=query.start_date,
            end_date=query.end_date,
            quote=quote,
        )
        study.notes = [
            "頂端成交與最新估值為來源最新快照，不隨歷史研究截止日改變；各圖表與財報另標示期間，並非歷史時點已知資訊。",
            "日成交資料非盤中即時價；財務健檢沒有總分，也不產生買賣建議。",
        ]
        try:
            close, change = (
                Decimal(quote.data.closing_price or ""),
                Decimal(quote.data.change or ""),
            )
            if close - change > 0:
                study.change_percent = format(change / (close - change) * 100, ".4f")
        except InvalidOperation:
            pass
        if query.view == "notes":
            study.complete = True
            return study
        blocked: AnalysisError | None = None
        requested = []
        timed_out = False
        optional_failed = False

        async def fetch(dataset, start, section, normalize):
            nonlocal blocked
            await check()
            requested.append(section)
            if blocked:
                section.status = "error"
                section.error = blocked
                return
            try:
                rows, meta = await self.source.fetch(dataset, code, start, query.end_date)
                with localcontext() as context:
                    context.prec = 64
                    section.data = normalize(rows, code, start, query.end_date)
                section.status, section.meta = "ok", meta
            except ServiceError as error:
                if error.code == "REQUEST_CANCELLED":
                    raise
                section.status = "error"
                section.error = AnalysisError(code=error.code, message=error.message)
                if error.status_code == 503:
                    blocked = section.error

        async def topic(key, start):
            current = block(key)
            study.blocks.append(current)
            await fetch(SPECS[key].dataset, start, current.result, SPECS[key].normalize)

        try:
            async with asyncio.timeout(max(0.001, 55 - (monotonic() - started))):
                try:
                    await check()
                    info, study.classification_meta = await self.source.catalogue("TaiwanStockInfo")
                    industries = {
                        r.get("industry_category") or ""
                        for r in info
                        if r.get("stock_id") == code
                        and r.get("type") == "twse"
                        and r.get("industry_category")
                    }
                    if industries:
                        # FinMind may publish several valid categories for one code
                        # (e.g. semiconductor and electronics). Retain all labels.
                        study.industry = "／".join(sorted(industries))
                        is_etf = ["ETF" in category.upper() for category in industries]
                        if all(is_etf):
                            study.security_kind = "etf"
                        elif (
                            not any(is_etf)
                            and len(code) == 4
                            and code.isdecimal()
                            and not any(
                                word in study.industry
                                for word in ("基金", "受益", "權證", "存託", "ETN", "指數")
                            )
                        ):
                            study.security_kind = "company"
                except ServiceError as error:
                    if error.code == "REQUEST_CANCELLED":
                        raise
                    study.notes.append("產業與證券分類來源未能確認；不推定為一般公司。")
                    if error.status_code == 503:
                        blocked = AnalysisError(code=error.code, message=error.message)
                if query.view in {"overview", "valuation"}:
                    await check()
                    requested.append(study.valuation)
                    try:
                        study.valuation.data = [await self.stocks.valuation(code)]
                        study.valuation.status = "ok"
                    except ServiceError as error:
                        study.valuation.status = "error"
                        study.valuation.error = AnalysisError(
                            code=error.code, message=error.message
                        )
                if query.view in {"overview", "valuation"}:
                    await fetch(
                        "TaiwanStockPrice",
                        max(date(1994, 10, 1), query.start_date - timedelta(days=180)),
                        study.history,
                        lambda rows, code, start, end: history_points(
                            rows, code, query.start_date, end
                        ),
                    )
                if query.view in {"overview", "financial"} and study.security_kind != "etf":
                    await fetch(
                        "TaiwanStockMonthRevenue",
                        month_shift(query.end_date, -60),
                        study.revenue,
                        lambda rows, code, start, end: revenue_points(rows, code, end),
                    )
                    financial_start = max(date(1994, 10, 1), date(query.end_date.year - 2, 1, 1))
                    for key in ("income", "balance", "cashflow"):
                        await topic(key, financial_start)
                if query.view in {"overview", "financial", "valuation"}:
                    await topic("dividends", query.end_date - timedelta(days=730))
                if query.view == "valuation":
                    await topic("valuation", query.start_date)
                    await topic("events", query.start_date)
                    for key in ("splits", "reductions", "par_changes"):
                        await topic(key, query.start_date)
                    necessary = [
                        b
                        for b in study.blocks
                        if b.id in {"events", "splits", "reductions", "par_changes"}
                    ]
                    if study.history.status == "ok" and all(
                        b.result.status == "ok" for b in necessary
                    ):
                        from app.schemas.advanced_research import ResearchBlock, ResearchColumn

                        b = ResearchBlock(
                            id="fill_progress",
                            title="收盤填息觀察",
                            columns=[
                                ResearchColumn(key=k, label=v)
                                for k, v in [
                                    ("date", "除息日"),
                                    ("status", "觀察結果"),
                                    ("fill_date", "首次收盤填息日"),
                                    ("trading_days", "有效交易筆數"),
                                    ("progress", "期末回補率（%）"),
                                ]
                            ],
                            result=AnalysisSection(status="ok"),
                        )
                        by_key = {x.id: x.result.data or [] for x in necessary}
                        prices = [
                            {
                                "date": p.date.isoformat(),
                                "close": p.close if p.price_available else None,
                            }
                            for p in study.history.data or []
                        ]
                        b.result.data = fill_progress(
                            by_key["events"],
                            prices,
                            [r for k in ("splits", "reductions", "par_changes") for r in by_key[k]],
                        )
                        study.blocks.append(b)
                if query.view in {"overview", "chips"}:
                    await fetch(
                        "TaiwanStockInstitutionalInvestorsBuySell",
                        max(date(1994, 10, 1), query.start_date - timedelta(days=90)),
                        study.institutions,
                        institution_points,
                    )
                if query.view == "chips":
                    await check()
                    requested.append(study.margin)
                    try:
                        study.margin.data = [await self.stocks.margin(code)]
                        study.margin.status = "ok"
                    except ServiceError as error:
                        study.margin.status = "error"
                        study.margin.error = AnalysisError(code=error.code, message=error.message)
                if query.view in {"overview", "chips"}:
                    await check()
                    try:
                        study.risks = await self.notices.risks(RiskQuery(code=code, limit=100))
                    except ServiceError as error:
                        if error.code == "REQUEST_CANCELLED":
                            raise
                        optional_failed = True
                        study.notes.append("風險公告未取得：" + error.message)
                    await check()
                    try:
                        study.calendar = await self.notices.calendar(
                            CalendarQuery(
                                q=code,
                                kind="ex_dividend",
                                from_date=query.end_date - timedelta(days=30),
                                to_date=query.end_date + timedelta(days=30),
                                limit=100,
                            )
                        )
                        study.calendar.data = [e for e in study.calendar.data if e.code == code]
                    except ServiceError as error:
                        if error.code == "REQUEST_CANCELLED":
                            raise
                        optional_failed = True
                        study.notes.append("除權息日曆未取得：" + error.message)
        except TimeoutError:
            timed_out = True
            study.notes.append(
                "本次查詢達時間上限，保留已取得區域，未完成項目不補值；請手動重新取得。"
            )
            for section in requested:
                if section.status == "not_requested":
                    section.status = "error"
                    section.error = AnalysisError(
                        code="RESEARCH_TIMEOUT", message="本次來源查詢超時。"
                    )
        study.metrics = health(study) if query.view in {"overview", "financial"} else []
        study.complete = (
            not timed_out
            and not optional_failed
            and bool(requested)
            and all(s.status == "ok" for s in requested)
            and (study.risks is None or study.risks.complete)
            and (study.calendar is None or study.calendar.complete)
        )
        return study

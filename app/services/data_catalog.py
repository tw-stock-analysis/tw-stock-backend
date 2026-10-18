"""Allowlisted documented resources. No request may choose an upstream URL."""

from dataclasses import dataclass

from app.schemas.open_data import DataCategory as C
from app.schemas.open_data import DataProvider as P
from app.schemas.open_data import DatasetMeta
from app.services.errors import ServiceError

OPEN_LICENSE = "https://data.gov.tw/license"
SYMBOL_FIELDS = (
    "Code",
    "SecuritiesCompanyCode",
    "SecuritiesCode",
    "StockCode",
    "公司代號",
    "證券代號",
    "股票代號",
    "股票代碼",
    "代號",
    "Contract",
    "ContractCode",
    "ContractName",
    "NameOfContract",
    "商品名稱",
    "契約名稱",
)
DATE_FIELDS = ("Date", "資料日期", "出表日期", "日期", "交易日期", "交易日", "date")


@dataclass(frozen=True)
class Dataset:
    key: str
    provider: P
    category: C
    path: str
    market: str
    frequency: str = "daily"
    industry: str | None = None
    symbol_fields: tuple[str, ...] = SYMBOL_FIELDS
    date_fields: tuple[str, ...] = DATE_FIELDS
    require_symbol: bool = True
    coverage: str = "latest_source_snapshot"

    @property
    def url(self) -> str:
        bases = {
            P.TWSE: "https://openapi.twse.com.tw/v1/",
            P.TPEX: "https://www.tpex.org.tw/openapi/v1/",
            P.TAIFEX: "https://openapi.taifex.com.tw/v1/",
            P.TDCC: "https://opendata.tdcc.com.tw/",
        }
        return bases[self.provider] + self.path

    def metadata(self) -> DatasetMeta:
        documents = {
            P.TWSE: "https://openapi.twse.com.tw/",
            P.TPEX: "https://www.tpex.org.tw/openapi/",
            P.TDCC: "https://data.gov.tw/dataset/11452",
            P.TAIFEX: "https://openapi.taifex.com.tw/",
        }
        names = {
            P.TWSE: "臺灣證券交易所",
            P.TPEX: "證券櫃檯買賣中心",
            P.TDCC: "臺灣集中保管結算所",
            P.TAIFEX: "臺灣期貨交易所",
        }
        return DatasetMeta(
            dataset=self.key,
            provider=self.provider,
            category=self.category,
            market=self.market,
            frequency=self.frequency,
            source_url=self.url,
            documentation_url=documents[self.provider],
            attribution=names[self.provider],
            license_url=OPEN_LICENSE,
            usage="open_data_attribution",
            coverage=self.coverage,
        )


def _build_catalog() -> dict[str, Dataset]:
    items: list[Dataset] = []
    twse = [
        ("companies", C.COMPANIES, "opendata/t187ap03_L", "daily"),
        ("prices", C.DAILY_PRICES, "exchangeReport/STOCK_DAY_ALL", "daily"),
        ("valuations", C.VALUATIONS, "exchangeReport/BWIBBU_ALL", "daily"),
        ("revenue", C.MONTHLY_REVENUE, "opendata/t187ap05_L", "monthly"),
        ("dividends", C.DIVIDENDS, "opendata/t187ap45_L", "event"),
        ("ex_dividends", C.CORPORATE_ACTIONS, "exchangeReport/TWT48U_ALL", "event"),
        ("announcements", C.ANNOUNCEMENTS, "opendata/t187ap04_L", "event"),
        ("insiders", C.INSIDER_HOLDINGS, "opendata/t187ap11_L", "monthly"),
        ("pledges", C.INSIDER_PLEDGES, "opendata/t187ap09_L", "monthly"),
        ("transfers", C.INSIDER_TRANSFERS, "opendata/t187ap12_L", "event"),
        ("margin", C.MARGIN_BALANCES, "exchangeReport/MI_MARGN", "daily"),
        ("lending_limits", C.LENDING_LIMITS, "SBL/TWT96U", "daily"),
        ("day_trading", C.DAY_TRADING, "exchangeReport/TWTB4U", "daily"),
        ("after_hours", C.AFTER_HOURS, "exchangeReport/BFT41U", "daily"),
    ]
    for key, category, path, frequency in twse:
        items.append(
            Dataset(
                "twse_" + key,
                P.TWSE,
                category,
                path,
                "TWSE",
                frequency,
                require_symbol=key != "margin",
                coverage="source_native_summary" if key == "margin" else "latest_source_snapshot",
            )
        )
    items.extend(
        [
            Dataset(
                "twse_foreign_top20",
                P.TWSE,
                C.FOREIGN_HOLDINGS,
                "fund/MI_QFIIS_sort_20",
                "TWSE",
                coverage="top20_only",
            ),
            Dataset(
                "twse_foreign_industries",
                P.TWSE,
                C.FOREIGN_HOLDINGS,
                "fund/MI_QFIIS_cat",
                "TWSE",
                require_symbol=False,
                coverage="industry_totals",
            ),
            Dataset("twse_odd_lot", P.TWSE, C.ODD_LOT, "exchangeReport/TWT53U", "TWSE"),
            Dataset(
                "twse_block_trading",
                P.TWSE,
                C.BLOCK_TRADING,
                "block/BFIAUU_d",
                "TWSE",
                require_symbol=False,
                coverage="market_totals",
            ),
        ]
    )
    items.append(
        Dataset(
            "twse_calendar",
            P.TWSE,
            C.CALENDAR,
            "holidaySchedule/holidaySchedule",
            "TWSE",
            "annual",
            require_symbol=False,
        )
    )
    for market, provider, prefix, suffix in (
        ("TWSE", P.TWSE, "opendata/", "L"),
        ("TPEX", P.TPEX, "mopsfin_", "O"),
    ):
        for industry in ("ci", "basi", "bd", "fh", "ins", "mim"):
            for number, category, name in (
                ("06", C.INCOME_STATEMENTS, "income"),
                ("07", C.BALANCE_SHEETS, "balance"),
            ):
                items.append(
                    Dataset(
                        f"{provider.value}_{name}_{industry}",
                        provider,
                        category,
                        f"{prefix}t187ap{number}_{suffix}_{industry}",
                        market,
                        "quarterly",
                        industry,
                    )
                )
    for key, category, path, frequency in (
        ("companies", C.COMPANIES, "mopsfin_t187ap03_O", "daily"),
        ("prices", C.DAILY_PRICES, "tpex_mainboard_daily_close_quotes", "daily"),
        ("valuations", C.VALUATIONS, "tpex_mainboard_peratio_analysis", "daily"),
        ("revenue", C.MONTHLY_REVENUE, "mopsfin_t187ap05_O", "monthly"),
        ("institutional", C.INSTITUTIONAL_TRADES, "tpex_3insti_daily_trading", "daily"),
        ("margin", C.MARGIN_BALANCES, "tpex_mainboard_margin_balance", "daily"),
    ):
        items.append(Dataset("tpex_" + key, P.TPEX, category, path, "TPEX", frequency))
    items.append(
        Dataset(
            "tdcc_distribution",
            P.TDCC,
            C.HOLDING_DISTRIBUTIONS,
            "getOD.ashx?id=1-5",
            "ALL",
            "weekly",
        )
    )
    for key, category, path in (
        ("futures_prices", C.FUTURES_PRICES, "DailyMarketReportFut"),
        ("options_prices", C.OPTIONS_PRICES, "DailyMarketReportOpt"),
        (
            "futures_institutional",
            C.FUTURES_INSTITUTIONAL,
            "MarketDataOfMajorInstitutionalTradersDetailsOfFuturesContractsBytheDate",
        ),
        (
            "options_institutional",
            C.OPTIONS_INSTITUTIONAL,
            "MarketDataOfMajorInstitutionalTradersDetailsOfOptionsContractsBytheDate",
        ),
        ("futures_large_traders", C.FUTURES_LARGE_TRADERS, "OpenInterestOfLargeTradersFutures"),
        ("options_large_traders", C.OPTIONS_LARGE_TRADERS, "OpenInterestOfLargeTradersOptions"),
        ("put_call_ratio", C.PUT_CALL_RATIO, "PutCallRatio"),
    ):
        items.append(
            Dataset(
                "taifex_" + key,
                P.TAIFEX,
                category,
                path,
                "TAIFEX",
                require_symbol=category != C.PUT_CALL_RATIO,
            )
        )
    return {item.key: item for item in items}


DATASETS = _build_catalog()


def get_dataset(key: str) -> Dataset:
    if key not in DATASETS:
        raise ServiceError("DATASET_NOT_FOUND", "Unknown or unsupported dataset.", 404)
    return DATASETS[key]

"""실제 시장 데이터를 계산해 근거 기반 주식 분석을 제공하는 Plugin."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
import json
import math
import re

from core.plugin import BasePlugin, IntentSchema, SlotSchema, ToolSchema
from core.tool_result import Artifact, Evidence, ToolRunResult


class FinancePlugin(BasePlugin):
    MARKET_SYMBOLS = {
        "한국": [("코스피", "^KS11"), ("코스닥", "^KQ11")],
        "미국": [("S&P 500", "^GSPC"), ("나스닥", "^IXIC"), ("다우", "^DJI")],
        "글로벌": [
            ("코스피", "^KS11"), ("코스닥", "^KQ11"),
            ("S&P 500", "^GSPC"), ("나스닥", "^IXIC"),
        ],
    }

    def __init__(self):
        super().__init__()
        self.name = "finance"
        self.description = "Yahoo Finance 실제 시세 기반 종목·시장 분석"
        self.dependencies = ["yfinance", "pandas"]
        self._aliases = self._load_aliases()

    @staticmethod
    def _alias_path() -> Path:
        return Path(__file__).resolve().parent.parent / "config" / "finance_symbols.json"

    @classmethod
    def _load_aliases(cls) -> Dict[str, str]:
        try:
            payload = json.loads(cls._alias_path().read_text(encoding="utf-8"))
            return {
                re.sub(r"\s+", "", str(key).casefold()): str(value).strip().upper()
                for key, value in payload.items() if str(value).strip()
            }
        except (OSError, ValueError, TypeError):
            return {}

    def get_tools(self) -> List[ToolSchema]:
        return [
            ToolSchema(
                "finance_analyze_equity",
                "실제 일별 시세로 수익률·변동성·이동평균·거래량을 계산해 종목을 분석합니다",
                {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "회사명 또는 종목 코드"},
                        "period": {
                            "type": "string", "enum": ["3mo", "6mo", "1y", "2y"],
                            "default": "1y",
                        },
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
                ["network_access"], side_effect="read", timeout_seconds=45,
            ),
            ToolSchema(
                "finance_market_overview",
                "한국·미국·글로벌 주요 지수의 실제 시세와 등락을 비교합니다",
                {
                    "type": "object",
                    "properties": {
                        "market": {
                            "type": "string", "enum": ["한국", "미국", "글로벌"],
                            "default": "글로벌",
                        }
                    },
                    "additionalProperties": False,
                },
                ["network_access"], side_effect="read", timeout_seconds=60,
            ),
        ]

    def get_intents(self) -> List[IntentSchema]:
        return [
            IntentSchema(
                "finance.equity_analysis", "실시간 개별 주식 종목 분석", "finance_analyze_equity",
                ["주식 분석", "종목 분석", "주가 분석", "주식분석", "종목분석"],
                [SlotSchema("query", "분석할 회사명 또는 종목 코드", "어떤 종목을 분석할까요, 보스?", role="target")],
                execution_hints=["분석", "분석해", "살펴봐", "알려"],
                utterance_patterns=[
                    r"(?:[0-9A-Za-z가-힣.^-]+(?:\s+[0-9A-Za-z가-힣.^-]+){0,3})\s*(?:주식(?!\s*시장)|종목|주가)"
                    r"\s*(?:(?:을|를|은|는|의|에\s*대해)\s*)?.{0,40}?(?:분석|살펴)",
                    r"(?:[0-9A-Za-z가-힣.^-]+(?:\s+[0-9A-Za-z가-힣.^-]+){0,3})\s*"
                    r"(?:전망|실적|밸류에이션).{0,20}(?:분석|살펴|알려)",
                ],
                freshness="live", requires_sources=True, request_type="query",
            ),
            IntentSchema(
                "finance.market_overview", "실시간 주식시장 지수 분석", "finance_market_overview",
                ["주식시장 분석", "증시 분석", "시장 분석", "증시 현황", "주식 시장"],
                [],
                execution_hints=["분석", "현황", "알려", "살펴봐"],
                utterance_patterns=[r"(?:주식\s*시장|증시|코스피|코스닥|나스닥|s&p\s*500).*(?:분석|현황|어때|알려)"],
                freshness="live", requires_sources=True, request_type="query",
            ),
        ]

    def extract_slots(self, intent_name: str, text: str,
                      current_slots: Dict[str, Any]) -> Dict[str, Any]:
        slots = dict(current_slots)
        if intent_name == "finance.market_overview":
            normalized = text.casefold()
            if any(value in normalized for value in ("한국", "국내", "코스피", "코스닥")):
                slots["market"] = "한국"
            elif any(value in normalized for value in ("미국", "미장", "나스닥", "다우", "s&p")):
                slots["market"] = "미국"
            else:
                slots["market"] = slots.get("market", "글로벌")
            return slots
        if intent_name != "finance.equity_analysis":
            return slots

        quoted = re.search(r"[\"'“”‘’]([^\"'“”‘’]+)[\"'“”‘’]", text)
        candidate = quoted.group(1).strip() if quoted else text.strip()
        candidate = re.sub(
            r"(?:의)?\s*(?:주식|종목|주가)(?:을|를|은|는|에\s*대해)?", " ", candidate,
            flags=re.IGNORECASE,
        )
        candidate = re.sub(
            r"(?:좀|한번|한\s*번)?\s*(?:분석|살펴)"
            r"(?:해\s*주시겠어요|해\s*주세요|해\s*줄래|해\s*줘|해주세요|해줄래|해줘|해)?",
            " ", candidate, flags=re.IGNORECASE,
        )
        candidate = re.sub(r"(?:알려|말해)(?:줘|줄래|주세요)?", " ", candidate, flags=re.IGNORECASE)
        candidate = re.sub(r"[?!.,]+", " ", candidate)
        candidate = re.sub(r"\s+", " ", candidate).strip()
        if candidate and candidate not in {"시장", "주식시장", "증시"}:
            slots["query"] = candidate
        return slots

    def _resolve_symbol(self, query: str) -> Dict[str, str]:
        compact = re.sub(r"\s+", "", query.casefold())
        if compact in self._aliases:
            return {"symbol": self._aliases[compact], "name": query, "method": "local_alias"}
        if re.fullmatch(r"\d{6}", compact):
            return {"symbol": compact + ".KS", "name": query, "method": "krx_code"}
        if re.fullmatch(r"[A-Za-z.^-]{1,15}", query.strip()):
            return {"symbol": query.strip().upper(), "name": query, "method": "ticker_literal"}

        import yfinance as yf
        search = yf.Search(query, max_results=8)
        quotes = [item for item in (search.quotes or []) if item.get("symbol")]
        if not quotes:
            raise LookupError(
                f"'{query}'에 대응하는 종목 코드를 찾지 못했습니다. 회사명 또는 종목 코드를 확인해 주세요."
            )
        equities = [item for item in quotes if str(item.get("quoteType", "")).upper() in {"EQUITY", "ETF", "INDEX"}]
        selected = (equities or quotes)[0]
        return {
            "symbol": str(selected["symbol"]).upper(),
            "name": str(selected.get("shortname") or selected.get("longname") or query),
            "method": "yahoo_search",
        }

    @staticmethod
    def _number(value: Any) -> Optional[float]:
        try:
            number = float(value)
            return number if math.isfinite(number) else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _return(close, sessions: int) -> Optional[float]:
        if len(close) <= sessions:
            return None
        first, last = float(close.iloc[-sessions - 1]), float(close.iloc[-1])
        return (last / first - 1.0) * 100 if first else None

    def _analyze_symbol(self, query: str, period: str) -> Dict[str, Any]:
        import yfinance as yf

        resolved = self._resolve_symbol(query)
        ticker = yf.Ticker(resolved["symbol"])
        history = ticker.history(period=period, interval="1d", auto_adjust=False, repair=True)
        if history is None or history.empty or "Close" not in history:
            raise ValueError(f"{resolved['symbol']}의 거래 데이터를 받지 못했습니다.")
        history = history.dropna(subset=["Close"])
        if history.empty:
            raise ValueError(f"{resolved['symbol']}의 유효한 종가가 없습니다.")
        close = history["Close"]
        volume = history["Volume"] if "Volume" in history else None
        daily = close.pct_change().dropna()
        ma20 = close.tail(20).mean() if len(close) >= 20 else None
        ma60 = close.tail(60).mean() if len(close) >= 60 else None
        volatility = daily.std() * math.sqrt(252) * 100 if len(daily) >= 2 else None
        latest = float(close.iloc[-1])
        previous = float(close.iloc[-2]) if len(close) >= 2 else None
        latest_volume = float(volume.iloc[-1]) if volume is not None and len(volume) else None
        avg_volume20 = float(volume.tail(20).mean()) if volume is not None and len(volume) >= 2 else None
        index = history.index
        latest_date = index[-1].date().isoformat() if hasattr(index[-1], "date") else str(index[-1])
        first_date = index[0].date().isoformat() if hasattr(index[0], "date") else str(index[0])
        currency = "KRW" if resolved["symbol"].endswith((".KS", ".KQ")) else "USD"
        company_info: Dict[str, Any] = {}
        try:
            raw_info = ticker.get_info()
            if isinstance(raw_info, dict):
                company_info = raw_info
        except Exception:
            # 가격 이력만으로도 분석은 가능하다. 부가 지표 장애가 전체 분석을
            # 브라우저 검색으로 강등시키거나 실패시키지 않게 한다.
            company_info = {}
        try:
            fast = ticker.fast_info
            currency = str(company_info.get("currency") or fast.get("currency") or currency)
            market_cap = self._number(company_info.get("marketCap") or fast.get("market_cap"))
        except Exception:
            currency = str(company_info.get("currency") or currency)
            market_cap = self._number(company_info.get("marketCap"))
        if ma20 and ma60:
            trend = "단기 강세" if latest > ma20 > ma60 else "단기 약세" if latest < ma20 < ma60 else "혼조"
        elif ma20:
            trend = "20일 평균 상회" if latest > ma20 else "20일 평균 하회"
        else:
            trend = "데이터 부족"
        running_high = close.cummax()
        drawdown = (close / running_high - 1.0) * 100
        period_low, period_high = float(close.min()), float(close.max())
        range_position = (
            (latest - period_low) / (period_high - period_low) * 100
            if period_high > period_low else None
        )
        display_name = str(
            company_info.get("longName") or company_info.get("shortName")
            or resolved["name"]
        )
        return {
            "provider": "Yahoo Finance via yfinance",
            "retrieved_at": datetime.now().astimezone().isoformat(),
            "query": query,
            "symbol": resolved["symbol"],
            "name": display_name,
            "symbol_resolution": resolved["method"],
            "currency": currency,
            "data_start": first_date,
            "data_end": latest_date,
            "sessions": int(len(history)),
            "latest_close": latest,
            "change_1d_percent": ((latest / previous - 1) * 100) if previous else None,
            "return_5d_percent": self._return(close, 5),
            "return_1m_percent": self._return(close, 21),
            "return_3m_percent": self._return(close, 63),
            "return_1y_percent": self._return(close, 252),
            "annualized_volatility_percent": self._number(volatility),
            "max_drawdown_percent": self._number(drawdown.min()),
            "ma20": self._number(ma20),
            "ma60": self._number(ma60),
            "period_low": period_low,
            "period_high": period_high,
            "period_range_position_percent": self._number(range_position),
            "latest_volume": self._number(latest_volume),
            "average_volume_20d": self._number(avg_volume20),
            "volume_ratio_20d": (latest_volume / avg_volume20) if latest_volume and avg_volume20 else None,
            "market_cap": market_cap,
            "forward_pe": self._number(company_info.get("forwardPE")),
            "trailing_pe": self._number(company_info.get("trailingPE")),
            "price_to_book": self._number(company_info.get("priceToBook")),
            "sector": str(company_info.get("sector") or ""),
            "industry": str(company_info.get("industry") or ""),
            "trend_observation": trend,
            "source_url": f"https://finance.yahoo.com/quote/{resolved['symbol']}",
        }

    def execute_tool(self, tool_name: str, tool_input: Dict[str, Any]):
        try:
            if tool_name == "finance_analyze_equity":
                query = str(tool_input.get("query", "")).strip()
                if not query:
                    return ToolRunResult.failed(tool_name=tool_name, error="분석할 회사명 또는 종목 코드가 필요합니다.")
                period = str(tool_input.get("period", "1y"))
                if period not in {"3mo", "6mo", "1y", "2y"}:
                    period = "1y"
                result = self._analyze_symbol(query, period)
                return ToolRunResult.successful(
                    tool_name=tool_name,
                    raw_output=json.dumps(result, ensure_ascii=False),
                    evidence=[Evidence(
                        "market_data_calculation",
                        f"{result['sessions']}개 거래일의 종가·거래량으로 지표를 계산했습니다.",
                        {
                            "provider": result["provider"], "symbol": result["symbol"],
                            "data_start": result["data_start"], "data_end": result["data_end"],
                            "retrieved_at": result["retrieved_at"], "sessions": result["sessions"],
                        },
                    )],
                    artifacts=[Artifact("url", result["source_url"], {"provider": result["provider"]})],
                )
            if tool_name == "finance_market_overview":
                market = str(tool_input.get("market", "글로벌"))
                symbols = self.MARKET_SYMBOLS.get(market, self.MARKET_SYMBOLS["글로벌"])
                items = []
                errors = []
                for name, symbol in symbols:
                    try:
                        item = self._analyze_symbol(symbol, "3mo")
                        item["name"] = name
                        items.append(item)
                    except Exception as exc:
                        errors.append(f"{name}: {exc}")
                if not items:
                    raise RuntimeError("; ".join(errors) or "주요 지수 데이터를 받지 못했습니다.")
                payload = {
                    "provider": "Yahoo Finance via yfinance", "market": market,
                    "retrieved_at": datetime.now().astimezone().isoformat(),
                    "indices": items, "partial_errors": errors,
                }
                return ToolRunResult.successful(
                    tool_name=tool_name,
                    raw_output=json.dumps(payload, ensure_ascii=False),
                    evidence=[Evidence(
                        "market_index_calculation", f"주요 지수 {len(items)}개의 실제 거래 데이터를 비교했습니다.",
                        {"market": market, "symbols": [item["symbol"] for item in items],
                         "retrieved_at": payload["retrieved_at"], "partial_errors": errors},
                    )],
                    artifacts=[Artifact("url", item["source_url"], {"symbol": item["symbol"]}) for item in items],
                )
            return ToolRunResult.failed(tool_name=tool_name, error=f"알 수 없는 금융 도구: {tool_name}")
        except Exception as exc:
            return ToolRunResult.failed(tool_name=tool_name, error=f"금융 데이터 조회 실패: {exc}")

    @staticmethod
    def _fmt(value: Any, digits: int = 2, suffix: str = "") -> str:
        if value is None:
            return "데이터 없음"
        return f"{float(value):,.{digits}f}{suffix}"

    @staticmethod
    def _fmt_market_cap(value: Any, currency: str) -> str:
        if value is None:
            return "데이터 없음"
        number = float(value)
        if currency == "KRW":
            return f"{number / 1_000_000_000_000:,.1f}조 원"
        return f"{number / 1_000_000_000:,.1f}십억 {currency or '통화 단위'}"

    def present_result(self, tool_name: str, result: str) -> str:
        if str(result).startswith("오류:"):
            return str(result)
        data = json.loads(str(result))
        if tool_name == "finance_market_overview":
            lines = [f"{data['market']} 주요 지수의 최신 거래일 기준 요약입니다."]
            for item in data["indices"]:
                lines.append(
                    f"- {item['name']}: {self._fmt(item['latest_close'])} "
                    f"({self._fmt(item['change_1d_percent'], suffix='%')}), "
                    f"최근 1개월 {self._fmt(item['return_1m_percent'], suffix='%')}"
                )
            if data.get("partial_errors"):
                lines.append("일부 지수는 조회되지 않아 확인된 값만 표시했습니다.")
            lines.append(f"출처: Yahoo Finance, 조회 시각 {data['retrieved_at']}")
            return "\n".join(lines)
        if tool_name != "finance_analyze_equity":
            return str(result)
        price = self._fmt(data["latest_close"])
        currency = data.get("currency", "")
        volume_note = (
            f"최근 거래량은 20일 평균의 {self._fmt(data.get('volume_ratio_20d'))}배입니다. "
            if data.get("volume_ratio_20d") is not None else ""
        )
        valuation = []
        if data.get("forward_pe") is not None:
            valuation.append(f"선행 PER {self._fmt(data['forward_pe'])}배")
        if data.get("trailing_pe") is not None:
            valuation.append(f"후행 PER {self._fmt(data['trailing_pe'])}배")
        if data.get("price_to_book") is not None:
            valuation.append(f"PBR {self._fmt(data['price_to_book'])}배")
        valuation_text = ", ".join(valuation) if valuation else "Yahoo Finance 제공 지표 없음"
        return (
            f"{data['name']}({data['symbol']})을 실제 거래 데이터로 분석했습니다.\n"
            f"- 기준: {data['data_end']} 종가 {price} {currency}, 전일 대비 "
            f"{self._fmt(data.get('change_1d_percent'), suffix='%')}\n"
            f"- 수익률: 5거래일 {self._fmt(data.get('return_5d_percent'), suffix='%')}, "
            f"1개월 {self._fmt(data.get('return_1m_percent'), suffix='%')}, "
            f"3개월 {self._fmt(data.get('return_3m_percent'), suffix='%')}\n"
            f"- 추세: {data['trend_observation']}. 20일 이동평균 {self._fmt(data.get('ma20'))}, "
            f"60일 이동평균 {self._fmt(data.get('ma60'))}\n"
            f"- 위험: 연환산 변동성 {self._fmt(data.get('annualized_volatility_percent'), suffix='%')}, "
            f"최대 낙폭 {self._fmt(data.get('max_drawdown_percent'), suffix='%')}, "
            f"조회 기간 저가 {self._fmt(data.get('period_low'))}~고가 {self._fmt(data.get('period_high'))}, "
            f"현재 위치 {self._fmt(data.get('period_range_position_percent'), suffix='%')}. {volume_note}\n"
            f"- 규모·가치평가: 시가총액 {self._fmt_market_cap(data.get('market_cap'), currency)}, "
            f"{valuation_text}\n"
            f"출처: Yahoo Finance, {data['sessions']}개 거래일, 조회 시각 {data['retrieved_at']}. "
            "기업 공시·뉴스의 정성 분석은 포함하지 않은 과거 데이터 기반 정량 요약이며 "
            "매수·매도 추천은 아닙니다."
        )

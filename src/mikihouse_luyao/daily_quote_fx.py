from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.request import Request, urlopen
from xml.etree import ElementTree


ECB_DAILY_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"


class FxError(RuntimeError):
    pass


@dataclass(frozen=True)
class FrozenFxRate:
    provider: str
    rate_date: str
    jpy_to_cny_rate: Decimal
    fetched_at: str
    raw_response_sha256: str
    source_url: str
    manual_override: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "rate_date": self.rate_date,
            "jpy_to_cny_rate": format(self.jpy_to_cny_rate, "f"),
            "fetched_at": self.fetched_at,
            "raw_response_sha256": self.raw_response_sha256,
            "source_url": self.source_url,
            "manual_override": self.manual_override,
        }


def parse_ecb_reference_rates(raw: bytes, fetched_at: datetime) -> FrozenFxRate:
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError as exc:
        raise FxError("ECB response is not valid XML") from exc
    dated_nodes = [node for node in root.iter() if node.attrib.get("time")]
    if len(dated_nodes) != 1:
        raise FxError("ECB response must contain exactly one effective date")
    node = dated_nodes[0]
    currencies = {
        str(child.attrib.get("currency") or "").upper(): str(child.attrib.get("rate") or "")
        for child in node
        if child.attrib.get("currency")
    }
    try:
        cny_per_eur = Decimal(currencies["CNY"])
        jpy_per_eur = Decimal(currencies["JPY"])
    except (KeyError, InvalidOperation) as exc:
        raise FxError("ECB response does not contain valid CNY and JPY rates") from exc
    if cny_per_eur <= 0 or jpy_per_eur <= 0:
        raise FxError("ECB rates must be positive")
    try:
        date.fromisoformat(str(node.attrib["time"]))
    except (KeyError, ValueError) as exc:
        raise FxError("ECB effective date is invalid") from exc
    return FrozenFxRate(
        provider="ECB_EUROFXREF_DAILY",
        rate_date=str(node.attrib["time"]),
        jpy_to_cny_rate=cny_per_eur / jpy_per_eur,
        fetched_at=fetched_at.astimezone(timezone.utc).isoformat(),
        raw_response_sha256=hashlib.sha256(raw).hexdigest(),
        source_url=ECB_DAILY_URL,
    )


def assert_fx_fresh(rate: FrozenFxRate, quote_date: date, max_staleness_days: int) -> None:
    try:
        effective = date.fromisoformat(rate.rate_date)
    except ValueError as exc:
        raise FxError("FX rate_date is invalid") from exc
    age = (quote_date - effective).days
    if age < 0:
        raise FxError("FX rate is dated in the future")
    if age > max_staleness_days:
        raise FxError(f"FX rate is stale: {age} days old (limit {max_staleness_days})")


def fetch_ecb_reference_rate(
    *, quote_date: date, max_staleness_days: int = 7, timeout: float = 30
) -> FrozenFxRate:
    request = Request(ECB_DAILY_URL, headers={"User-Agent": "mikihouse-luyao-daily-quote/1"})
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except OSError as exc:
        raise FxError(f"ECB FX fetch failed: {exc}") from exc
    rate = parse_ecb_reference_rates(raw, datetime.now(timezone.utc))
    assert_fx_fresh(rate, quote_date, max_staleness_days)
    return rate


def manual_fx_rate(raw_rate: str, *, quote_date: date) -> FrozenFxRate:
    try:
        rate = Decimal(raw_rate)
    except InvalidOperation as exc:
        raise FxError("manual FX rate is invalid") from exc
    if rate <= 0:
        raise FxError("manual FX rate must be positive")
    now = datetime.now(timezone.utc).isoformat()
    evidence = f"MANUAL_OVERRIDE|{quote_date.isoformat()}|{format(rate, 'f')}".encode()
    return FrozenFxRate(
        provider="MANUAL_OVERRIDE",
        rate_date=quote_date.isoformat(),
        jpy_to_cny_rate=rate,
        fetched_at=now,
        raw_response_sha256=hashlib.sha256(evidence).hexdigest(),
        source_url="",
        manual_override=True,
    )

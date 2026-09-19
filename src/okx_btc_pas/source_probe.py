"""Read-only diagnostic of the two public source formats."""

from __future__ import annotations

import argparse
import json
import warnings
from datetime import date, datetime, timedelta, timezone
from xml.etree import ElementTree

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


OKX_HISTORY_URL = "https://www.okx.com/api/v5/market/history-candles"
CBR_DYNAMIC_URL = "https://www.cbr.ru/scripts/XML_dynamic.asp"


def build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3, backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    return session


def parse_cbr_xml(content: bytes) -> list[dict[str, str]]:
    root = ElementTree.fromstring(content)
    if root.attrib.get("ID") != "R01235":
        raise ValueError("CBR response is not USD/RUB")
    rows = []
    for node in root.findall("Record"):
        row = {"Date": node.attrib["Date"], "Id": node.attrib["Id"]}
        row.update({child.tag: child.text or "" for child in node})
        rows.append(row)
    return rows


def probe_sources(days: int, *, verify_ssl: bool = True) -> dict:
    session = build_session()
    session.verify = verify_ssl
    if not verify_ssl:
        warnings.warn(
            "TLS certificate verification is disabled only for this diagnostic",
            RuntimeWarning, stacklevel=2,
        )
    today = datetime.now(timezone.utc).date()
    end = today - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    response = session.get(
        OKX_HISTORY_URL,
        params={"instId": "BTC-USDT", "bar": "1Dutc",
                "after": str(int(datetime.combine(
                    end + timedelta(days=1), datetime.min.time(), timezone.utc
                ).timestamp() * 1000)), "limit": str(min(days + 2, 300))},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") != "0" or not isinstance(payload.get("data"), list):
        raise ValueError("Unexpected OKX response")
    candles = payload["data"]
    if not candles or any(len(row) != 9 for row in candles):
        raise ValueError("OKX returned no valid daily candles")
    cbr_response = session.get(
        CBR_DYNAMIC_URL,
        params={"date_req1": start.strftime("%d/%m/%Y"),
                "date_req2": end.strftime("%d/%m/%Y"), "VAL_NM_RQ": "R01235"},
        timeout=30,
    )
    cbr_response.raise_for_status()
    rates = parse_cbr_xml(cbr_response.content)
    if not rates:
        raise ValueError("CBR returned no USD/RUB rates")
    return {
        "okx": {"instrument_id": "BTC-USDT", "rows": len(candles),
                "fields": ["ts", "o", "h", "l", "c", "vol", "volCcy",
                           "volCcyQuote", "confirm"],
                "complete_rows": sum(row[8] == "1" for row in candles)},
        "cbr": {"currency_id": "R01235", "rows": len(rates),
                "first_date": rates[0]["Date"], "last_date": rates[-1]["Date"],
                "fields": sorted(rates[0])},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--insecure", action="store_true",
                        help="disable TLS verification for trusted-proxy diagnosis only")
    args = parser.parse_args()
    if args.days < 1:
        parser.error("--days must be positive")
    print(json.dumps(probe_sources(args.days, verify_ssl=not args.insecure),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

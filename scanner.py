
import csv
import io
import json
import os
import re
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from html import unescape

# S&P 500 Investigative Risk Monitor - Version 3
# Standard Python library only.

AGENT = os.getenv("SEC_USER_AGENT", "").strip()

if not AGENT or "@" not in AGENT or "example.com" in AGENT:
    raise SystemExit(
        "Set SEC_USER_AGENT to your app name and real contact email."
    )

HEADERS = {
    "User-Agent": AGENT,
    "Accept-Encoding": "identity"
}

CONSTITUENTS = (
    "https://raw.githubusercontent.com/datasets/"
    "s-and-p-500-companies/main/data/constituents.csv"
)

TICKERS = "https://www.sec.gov/files/company_tickers.json"

LOOKBACK = 120

# Filing item weights are investigative priorities,
# not probabilities of a stock-price decline.

RULES = {
    "4.02": (70, "Financial statements should no longer be relied upon"),
    "4.01": (24, "Change in certifying accountant"),
    "2.06": (30, "Material impairment"),
    "2.05": (18, "Restructuring or exit costs"),
    "3.01": (45, "Exchange listing compliance notice"),
    "1.02": (16, "Termination of a material agreement"),
    "5.02": (10, "Director or executive change")
}

KEYWORDS = [
    ("material weakness", 20, "Internal control material weakness"),
    ("substantial doubt", 35, "Going-concern uncertainty"),
    ("restatement", 20, "Restatement reference"),
    ("government investigation", 18, "Government investigation reference"),
    ("export control", 12, "Export-control reference"),
    ("sanction", 8, "Sanctions reference"),
    ("related party transaction", 10, "Related-party transaction reference"),
    ("disagreement with", 10, "Disagreement reference"),
    ("resign", 4, "Resignation reference")
]


def get(url, attempts=3):
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers=HEADERS)

            with urllib.request.urlopen(request, timeout=35) as response:
                return response.read()

        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            TimeoutError
        ):
            if attempt == attempts - 1:
                raise

            time.sleep(2 * (attempt + 1))


def text_from_html(data):
    text = data.decode("utf-8", "replace")

    text = re.sub(
        r"(?is)<(script|style|ix:header)\b[^>]*>.*?</\1>",
        " ",
        text
    )

    text = re.sub(r"(?s)<[^>]+>", " ", text)

    return re.sub(
        r"\s+",
        " ",
        unescape(text)
    ).strip()


def companies():
    raw = get(CONSTITUENTS).decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(raw)))

    result = []

    for row in rows:
        ticker = (
            row.get("Symbol")
            or row.get("Ticker")
            or ""
        ).upper().replace(".", "-").strip()

        name = (
            row.get("Security")
            or row.get("Name")
            or ticker
        ).strip()

        if ticker:
            result.append({
                "ticker": ticker,
                "company": name
            })

    if len(result) < 450:
        raise RuntimeError("S&P 500 constituent list incomplete")

    return result


def sec_map():
    data = json.loads(get(TICKERS))

    return {
        record["ticker"].upper().replace(".", "-"):
        int(record["cik_str"])
        for record in data.values()
    }


def investigate(company, cik, cutoff):
    url = (
        f"https://data.sec.gov/submissions/"
        f"CIK{cik:010d}.json"
    )

    data = json.loads(get(url))

    recent = data.get("filings", {}).get("recent", {})
    findings = []
    forms = recent.get("form", [])

    for i, form in enumerate(forms):
        if i >= len(recent.get("filingDate", [])):
            break

        date = recent["filingDate"][i]

        if date < cutoff:
            continue

        if form not in (
            "8-K", "8-K/A", "NT 10-K", "NT 10-Q"
        ):
            continue

        item_string = (
            recent.get("items", [])[i]
            if i < len(recent.get("items", []))
            else ""
        ) or ""

        matched = [
            (item, *RULES[item])
            for item in (
                value.strip()
                for value in item_string.split(",")
            )
            if item in RULES
        ]

        late = form.startswith("NT ")

        if not matched and not late:
            continue

        accession = (
            recent["accessionNumber"][i]
            .replace("-", "")
        )

        document = recent["primaryDocument"][i]

        filing_url = (
            f"https://www.sec.gov/Archives/edgar/data/"
            f"{cik}/{accession}/{document}"
        )

        base = max(
            [item[1] for item in matched]
            + ([30] if late else [0])
        )

        reasons = [item[2] for item in matched]

        if late:
            reasons.append("Late periodic filing notice")

        text_signals = []

        if base >= 18:
            try:
                body = text_from_html(
                    get(filing_url)
                )[:400000].lower()

                for phrase, weight, label in KEYWORDS:
                    if phrase in body:
                        text_signals.append(label)

                time.sleep(0.12)

            except Exception:
                pass

        # Keyword references are investigative leads,
        # not proof of wrongdoing.
        bonus = min(12, 3 * len(text_signals))

        findings.append({
            "date": date,
            "form": form,
            "items": item_string,
            "description": "; ".join(reasons),
            "text_signals": text_signals,
            "score": min(80, base + bonus),
            "url": filing_url
        })

        if len(findings) >= 12:
            break

    findings.sort(
        key=lambda x: (x["score"], x["date"]),
        reverse=True
    )

    # Reduce the influence of repeated filings.
    values = [f["score"] for f in findings]

    score = min(
        100,
        round(
            (values[0] if values else 0)
            + sum(v * 0.12 for v in values[1:4])
        )
    )

    if score >= 65:
        level = "High review priority"
    elif score >= 30:
        level = "Elevated review priority"
    else:
        level = "Routine review"

    if findings:
        summary = (
            "Review disclosed "
            + findings[0]["description"].lower()
            + ". This is not evidence of wrongdoing."
        )
    else:
        summary = "No qualifying filing signals detected."

    return {
        "ticker": company["ticker"],
        "company": company["company"],
        "cik": cik,
        "risk_level": level,
        "risk_score": score,
        "summary": summary,
        "event_30d": None,
        "drop_30d": None,
        "drop_90d": None,
        "evidence": findings[:8]
    }


def main():
    now = datetime.now(timezone.utc)

    cutoff = (
        now - timedelta(days=LOOKBACK)
    ).date().isoformat()

    universe = companies()
    mapping = sec_map()

    results = []
    errors = []

    for i, company in enumerate(universe, 1):
        cik = mapping.get(company["ticker"])

        if not cik:
            errors.append({
                "ticker": company["ticker"],
                "error": "No CIK match"
            })
            continue

        try:
            result = investigate(
                company,
                cik,
                cutoff
            )

            results.append(result)

            print(
                f'{i}/{len(universe)} '
                f'{company["ticker"]} scored',
                flush=True
            )

        except Exception as exc:
            errors.append({
                "ticker": company["ticker"],
                "error": str(exc)
            })

        time.sleep(0.14)

    if len(results) < 450:
        raise RuntimeError(
            f"Only {len(results)} companies scanned; "
            "retaining previous report."
        )

    results.sort(
        key=lambda x: (
            x["risk_score"],
            len(x["evidence"])
        ),
        reverse=True
    )

    top10 = [
        company
        for company in results
        if company["risk_score"] > 0
    ][:10]

    report = {
        "updated_at": now.isoformat(),
        "report_type": "SEC investigative screening v3",
        "status": "screening_only",
        "companies_scanned": len(results),
        "scan_errors": errors,
        "forecast_status": "not_calibrated",
        "methodology": (
            "Heuristic review priorities based on SEC "
            "filing items and unverified text signals; "
            "not allegations or price forecasts."
        ),
        "companies": top10
    }

    with open(
        "report.json.tmp",
        "w",
        encoding="utf-8"
    ) as file:
        json.dump(report, file, indent=2)

    os.replace(
        "report.json.tmp",
        "report.json"
    )

    print(
        "Report written with",
        len(top10),
        "investigative candidates."
    )


if __name__ == "__main__":
    main()

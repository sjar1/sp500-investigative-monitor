
import csv
import io
import json
import os
import time
import urllib.request
from datetime import datetime, timedelta, timezone

# S&P 500 Investigative Risk Monitor
# Uses Python's built-in libraries only.
# No local installation or paid API required.

USER_AGENT = os.environ.get("SEC_USER_AGENT", "").strip()

if not USER_AGENT or "example.com" in USER_AGENT:
    raise SystemExit(
        "Set SEC_USER_AGENT to an application name "
        "and your real contact email."
    )

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Encoding": "identity",
    "Accept": "application/json, text/csv, */*"
}

INDEX_URL = (
    "https://raw.githubusercontent.com/"
    "datasets/s-and-p-500-companies/"
    "main/data/constituents.csv"
)

SEC_TICKERS = (
    "https://www.sec.gov/files/company_tickers.json"
)

SEC_SUBMISSIONS = (
    "https://data.sec.gov/submissions/CIK{:010d}.json"
)

LOOKBACK_DAYS = 180
MAX_COMPANIES = 500
REQUEST_DELAY = 0.15

# These are investigative screening weights,
# not stock-price probabilities.
EVENT_RULES = {
    "4.02": (
        45,
        "Non-reliance on previously issued financial statements"
    ),
    "4.01": (
        25,
        "Changes in certifying accountant"
    ),
    "5.02": (
        18,
        "Director or senior executive change"
    ),
    "1.01": (
        8,
        "Material definitive agreement"
    ),
    "1.02": (
        12,
        "Termination of material agreement"
    ),
    "2.02": (
        5,
        "Results of operations disclosure"
    ),
    "2.05": (
        20,
        "Exit or disposal costs"
    ),
    "2.06": (
        25,
        "Material impairment disclosure"
    ),
    "3.01": (
        35,
        "Listing or continued listing notice"
    ),
    "5.03": (
        6,
        "Changes to charter or bylaws"
    ),
    "7.01": (
        3,
        "Regulation FD disclosure"
    ),
    "8.01": (
        4,
        "Other events"
    )
}


def fetch(url):
    request = urllib.request.Request(
        url,
        headers=HEADERS
    )
    with urllib.request.urlopen(
        request,
        timeout=35
    ) as response:
        return response.read()


def load_companies():
    raw = fetch(INDEX_URL).decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(raw)))

    companies = []
    for row in rows:
        ticker = (
            row.get("Symbol")
            or row.get("Ticker")
            or row.get("ticker")
            or ""
        ).strip().upper().replace(".", "-")

        name = (
            row.get("Security")
            or row.get("Name")
            or row.get("company")
            or ticker
        ).strip()

        if ticker:
            companies.append({
                "ticker": ticker,
                "company": name
            })

    if len(companies) < 450:
        raise RuntimeError(
            "S&P 500 constituent source is incomplete."
        )

    return companies[:MAX_COMPANIES]


def load_sec_tickers():
    data = json.loads(fetch(SEC_TICKERS))
    mapping = {}

    for record in data.values():
        ticker = record["ticker"].upper().replace(".", "-")
        mapping[ticker] = int(record["cik_str"])

    return mapping


def analyze_company(company, cik, cutoff):
    url = SEC_SUBMISSIONS.format(cik)
    data = json.loads(fetch(url))

    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])
    documents = recent.get("primaryDocument", [])
    items = recent.get("items", [])

    evidence = []
    score = 0

    for i, form in enumerate(forms):
        if i >= len(dates):
            break

        filing_date = dates[i]

        if filing_date < cutoff:
            continue

        if form not in ("8-K", "8-K/A", "NT 10-K", "NT 10-Q"):
            continue

        accession = (
            accessions[i].replace("-", "")
            if i < len(accessions) else ""
        )
        document = documents[i] if i < len(documents) else ""

        if not accession or not document:
            continue

        filing_url = (
            f"https://www.sec.gov/Archives/edgar/data/"
            f"{cik}/{accession}/{document}"
        )

        filing_items = (
            items[i] if i < len(items) else ""
        ) or ""

        filing_score = 0
        descriptions = []

        if form in ("NT 10-K", "NT 10-Q"):
            filing_score = 25
            descriptions.append(
                "Late periodic filing notification"
            )

        if form.startswith("8-K"):
            for item, (weight, description) in EVENT_RULES.items():
                # Match complete item numbers, not substrings.
                item_list = [
                    x.strip()
                    for x in filing_items.split(",")
                ]
                if item in item_list:
                    filing_score += weight
                    descriptions.append(description)

        if not descriptions:
            continue

        # Cap individual filing impact.
        filing_score = min(filing_score, 65)
        score += filing_score

        evidence.append({
            "date": filing_date,
            "form": form,
            "items": filing_items,
            "description": "; ".join(descriptions),
            "score": filing_score,
            "url": filing_url
        })

    evidence.sort(
        key=lambda x: (x["score"], x["date"]),
        reverse=True
    )

    # Screening score is capped at 100.
    # It is NOT a probability or allegation.
    score = min(score, 100)

    if score >= 70:
        level = "High review priority"
    elif score >= 35:
        level = "Elevated review priority"
    else:
        level = "Routine review"

    if evidence:
        summary = (
            "SEC filing screening identified: "
            + evidence[0]["description"]
            + ". Review the filing before drawing conclusions."
        )
    else:
        summary = "No qualifying recent filing signals detected."

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
        "evidence": evidence[:12]
    }


def main():
    now = datetime.now(timezone.utc)
    cutoff = (
        now - timedelta(days=LOOKBACK_DAYS)
    ).date().isoformat()

    companies = load_companies()
    cik_map = load_sec_tickers()

    results = []
    errors = []
    missing = []

    for index, company in enumerate(companies, start=1):
        ticker = company["ticker"]
        cik = cik_map.get(ticker)

        if not cik:
            missing.append(ticker)
            continue

        try:
            result = analyze_company(
                company, cik, cutoff
            )
            results.append(result)
            print(
                f"{index}/{len(companies)} "
                f"{ticker}: score {result['risk_score']}",
                flush=True
            )
        except Exception as exc:
            errors.append({
                "ticker": ticker,
                "error": str(exc)
            })
            print(
                f"Error scanning {ticker}: {exc}",
                flush=True
            )

        time.sleep(REQUEST_DELAY)

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

    # Never publish a partial or mostly failed scan
    # as a successful daily report.
    if len(results) < 450:
        raise RuntimeError(
            f"Incomplete scan: only {len(results)} "
            "companies analyzed. Previous report retained."
        )

    report = {
        "updated_at": now.isoformat(),
        "report_type": "SEC filing investigative screening",
        "status": "screening_only",
        "methodology": (
            "Heuristic ranking based on selected recent "
            "SEC filing metadata. Filing signals are not "
            "proof of misconduct or predicted price declines."
        ),
        "companies_scanned": len(results),
        "unmatched_tickers": missing,
        "scan_errors": errors,
        "forecast_status": "not_calibrated",
        "companies": top10
    }

    # Write only after scan completeness is verified.
    temporary = "report.json.tmp"

    with open(temporary, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    os.replace(temporary, "report.json")

    print(
        f"Completed. Scanned {len(results)} companies; "
        f"published {len(top10)} review candidates."
    )


if __name__ == "__main__":
    main()


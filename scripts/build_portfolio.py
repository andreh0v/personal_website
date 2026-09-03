"""
Builds docs/data/portfolio.json and docs/data/history.json from the CSV data files.

Privacy design: this repo and site are public. No absolute NOK figure for any
investment holding (market value, cost basis, quantity) is ever written to a
committed file or the JSON the frontend fetches -- only relative figures (% of
the holdings portfolio, % return) are exposed, computed here and then the
underlying absolute numbers are discarded before writing output. Cash/savings
account balances are shown in NOK (a separate, non-investment bucket), but nothing
ties them back to the investment portfolio's absolute size, so the total can't be
reconstructed from what's published.

Realized gains for exchange-traded and fund sells are taken directly from the
broker's own computed result (data/transactions.csv: realized_gain_nok) rather than
re-derived here. FIFO is used only to track which lots of each holding are still
open (for unrealized return) and how much cost basis was retired by each sale
(for realized return), never to re-derive the realized gain itself.
"""
import csv
import json
import shutil
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
SITE_DATA_DIR = ROOT / "docs" / "data"

BSU_DEDUCTION_RATE_PCT = 10
BSU_MAX_DEPOSIT_NOK = 27_500
BSU_MAX_DEDUCTION_NOK = 2_750
BSU_MAX_BALANCE_NOK = 300_000

# Holdings never shown or counted, at the owner's request.
EXCLUDED_TICKERS = {"ASML.AS", "ASML"}

# Below this share of the (investment-only) portfolio, a holding is folded into a
# group rather than listed by name. Funds group into a named theme; everything
# else groups into a generic "other holdings" bucket.
GROUPING_THRESHOLD_PCT = 2.0

FUND_CATEGORY = {
    "NO0013023242": "Dividend funds",   # Fondsfinans Norden Utbytte B
    "NO0010860349": "Dividend funds",   # Fondsfinans Utbytte A
    "NO0013023234": "Dividend funds",   # Fondsfinans Utbytte B
    "NO0012948852": "Dividend funds",   # Heimdal Utbytte N
    "NO0012445388": "Index funds",      # KLP AksjeEuropa Indeks N
    "NO0012445404": "Index funds",      # KLP AksjeGlobal Indeks N
    "NO0010776040": "Index funds",      # KLP AksjeGlobal Indeks P
    "IE00BMTD2N07": "Index funds",      # Nordnet Europa Indeks NOK
    "IE00BMTD2J60": "Index funds",      # Nordnet Global Indeks NOK
    "IE00BNNLSM87": "Index funds",      # Nordnet Teknologi Indeks NOK
    "IE000480NS87": "Index funds",      # Nordnet Global Indeks 125 NOK
    "NO0010710452": "High yield funds", # Fondsfinans High Yield A
    "NO0013168773": "High yield funds", # Fondsfinans High Yield B
    "NO0010782519": "High yield funds", # Heimdal Høyrente A
    "NO0012948878": "High yield funds", # Heimdal Høyrente N
    "NO0010279029": "High yield funds", # Landkreditt Høyrente A
    "NO0013479428": "High yield funds", # Landkreditt Høyrente N
    "NO0010876469": "High yield funds", # Storebrand Nordic High Yield N
}

_fx_cache = {}


def format_period(start_iso, end_iso):
    """Human-friendly holding period between two ISO dates, e.g. '1 yr 4 mo'."""
    start = date.fromisoformat(start_iso)
    end = date.fromisoformat(end_iso)
    months = (end.year - start.year) * 12 + (end.month - start.month)
    months = max(months, 0)
    years, rem_months = divmod(months, 12)
    parts = []
    if years:
        parts.append(f"{years} yr")
    if rem_months or not years:
        parts.append(f"{rem_months} mo")
    return "Held " + " ".join(parts)


def load_transactions(path):
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def fifo_open_positions(transactions):
    """Group by (account, ticker); FIFO-consume SELL quantity against BUY lots.
    Returns (open_positions, realized_total_nok, realized_gain_by_ticker_nok,
    realized_cost_by_ticker_nok, name_by_ticker, is_fund_by_ticker,
    first_buy_by_ticker, last_sell_by_ticker) -- the NOK dicts are used only to
    compute *relative* realized-return percentages, never displayed as NOK."""
    lots = defaultdict(list)  # key -> [[qty, cost_nok], ...]
    meta = {}
    name_by_ticker = {}
    is_fund_by_ticker = {}
    first_buy_by_ticker = {}
    last_sell_by_ticker = {}
    realized_total = 0.0
    realized_gain_by_ticker = defaultdict(float)
    realized_cost_by_ticker = defaultdict(float)

    for row in sorted(transactions, key=lambda r: r["date"]):
        key = (row["account"], row["ticker"])
        qty = float(row["quantity"])
        amount = float(row["amount_nok"])
        name_by_ticker[row["ticker"]] = row["name"]
        is_fund_by_ticker[row["ticker"]] = row["is_fund"] == "True"
        if row["type"] == "BUY":
            first_buy_by_ticker.setdefault(row["ticker"], row["date"])
        else:
            last_sell_by_ticker[row["ticker"]] = row["date"]
        meta[key] = {
            "name": row["name"],
            "is_fund": row["is_fund"] == "True",
            "isin": row["isin"],
            "native_currency": row["native_currency"],
            "last_native_price": float(row["native_price"]) if row["native_price"] else None,
            "last_date": row["date"],
        }
        if row["type"] == "BUY":
            lots[key].append([qty, amount])
        else:
            remaining = qty
            while remaining > 1e-6 and lots[key]:
                lot_qty, lot_cost = lots[key][0]
                take = min(lot_qty, remaining)
                frac = take / lot_qty if lot_qty else 0
                cost_removed = lot_cost * frac
                lots[key][0][0] -= take
                lots[key][0][1] -= cost_removed
                remaining -= take
                realized_cost_by_ticker[row["ticker"]] += cost_removed
                if lots[key][0][0] <= 1e-6:
                    lots[key].pop(0)
            if row["realized_gain_nok"]:
                gain = float(row["realized_gain_nok"])
                realized_total += gain
                realized_gain_by_ticker[row["ticker"]] += gain

    positions = {}
    for key, lotlist in lots.items():
        qty = sum(l[0] for l in lotlist)
        cost = sum(l[1] for l in lotlist)
        if qty > 1e-4:
            positions[key] = {**meta[key], "quantity": qty, "cost_basis_nok": cost}

    return (positions, realized_total, dict(realized_gain_by_ticker),
            dict(realized_cost_by_ticker), name_by_ticker, is_fund_by_ticker,
            first_buy_by_ticker, last_sell_by_ticker)


def fetch_fx_rate(base, quote="NOK"):
    """Free, keyless FX rate lookup. Tries exchangerate.host then frankfurter.app."""
    if base == quote:
        return 1.0
    if base in _fx_cache:
        return _fx_cache[base]
    for url in (
        f"https://api.exchangerate.host/latest?base={base}&symbols={quote}",
        f"https://api.frankfurter.app/latest?from={base}&to={quote}",
    ):
        try:
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            rate = resp.json()["rates"][quote]
            _fx_cache[base] = rate
            return rate
        except Exception:
            continue
    print(f"warning: could not fetch FX rate {base}->{quote}", file=sys.stderr)
    return None


def fetch_live_price(ticker):
    """Returns (price, currency) or (None, None) if unavailable."""
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).fast_info
        price = info.get("lastPrice") or info.get("last_price")
        currency = info.get("currency")
        if price:
            return float(price), currency
    except Exception:
        pass
    return None, None


def price_position_raw(key, pos):
    """Internal only: computes absolute NOK figures needed to derive percentages.
    Never written to disk as-is -- see build_public_holding()."""
    account, ticker = key
    quantity = pos["quantity"]
    cost_basis_nok = pos["cost_basis_nok"]

    price, currency = (None, None)
    if not pos["is_fund"]:
        price, currency = fetch_live_price(ticker)
    price_source = "live"

    if price is None:
        price = pos["last_native_price"]
        currency = pos["native_currency"]
        price_source = "manual_fallback" if pos["is_fund"] else "last_known_fallback"
        if price is None:
            price, currency = 0.0, "NOK"

    fx_rate = fetch_fx_rate(currency, "NOK") if currency != "NOK" else 1.0
    if fx_rate is None:
        fx_rate = 1.0  # last resort, avoids crashing a daily unattended run

    market_value_nok = price * quantity * fx_rate

    return {
        "ticker": ticker,
        "name": pos["name"],
        "type": "fund" if pos["is_fund"] else "exchange_traded",
        "account": account,
        "cost_basis_nok": cost_basis_nok,
        "market_value_nok": market_value_nok,
        "flagged_manual": pos["is_fund"] and price_source == "manual_fallback",
    }


def load_manual_holdings_raw(path):
    with open(path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    holdings = []
    for row in rows:
        market_value = float(row["market_value"])
        cost_basis = float(row["cost_basis"])
        currency = row["currency"]
        # currency conversion for manual rows isn't attempted -- these are
        # already-known values the owner maintains directly in NOK
        holdings.append({
            "ticker": row["ticker"],
            "name": row["name"],
            "type": "manual",
            "account": row["account"],
            "cost_basis_nok": cost_basis if currency == "NOK" else None,
            "market_value_nok": market_value if currency == "NOK" else None,
            "flagged_manual": True,
        })
    return holdings


def build_public_holding(raw, total_market_value_nok):
    cost = raw["cost_basis_nok"]
    value = raw["market_value_nok"]
    pct_of_portfolio = (value / total_market_value_nok * 100) if (value and total_market_value_nok) else 0.0
    unrealized_gain_pct = ((value - cost) / cost * 100) if cost else None
    return {
        "ticker": raw["ticker"],
        "name": raw["name"],
        "type": raw["type"],
        "account": raw["account"],
        "pct_of_portfolio": round(pct_of_portfolio, 2),
        "unrealized_gain_pct": round(unrealized_gain_pct, 2) if unrealized_gain_pct is not None else None,
        "flagged_manual": raw["flagged_manual"],
    }


def load_cash(path):
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def compute_bsu_benefit(cash_rows):
    """General rule explanation only -- no personal deposit or deduction figures
    (those would reveal an absolute NOK amount) are published."""
    bsu = next((r for r in cash_rows if r["account"] == "BSU"), None)
    if not bsu:
        return {"applicable": False}

    return {
        "applicable": True,
        "deduction_rate_pct": BSU_DEDUCTION_RATE_PCT,
        "max_deposit_nok": BSU_MAX_DEPOSIT_NOK,
        "max_deduction_nok": BSU_MAX_DEDUCTION_NOK,
        "max_balance_nok": BSU_MAX_BALANCE_NOK,
        "note": ("A 10% tax deduction on the amount deposited in the calendar year "
                 "(not the account balance), capped at 27,500 NOK deposited per year "
                 "(2,750 NOK deduction) and 300,000 NOK total balance. Lost entirely "
                 "for any year the account holder owns residential property. Not a "
                 "return on the account — it's a tax credit."),
    }


def append_history(unrealized_return_pct):
    """Tracks the portfolio's blended unrealized return (%) over time -- never an
    absolute value, so the series can't be used to infer portfolio size."""
    history_csv = DATA_DIR / "history.csv"
    today = datetime.now(timezone.utc).date().isoformat()

    rows = []
    if history_csv.exists():
        with open(history_csv, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    rows = [r for r in rows if r["date"] != today]
    rows.append({"date": today, "unrealized_return_pct": round(unrealized_return_pct, 2)})
    rows.sort(key=lambda r: r["date"])

    with open(history_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["date", "unrealized_return_pct"])
        w.writeheader()
        w.writerows(rows)

    with open(SITE_DATA_DIR / "history.json", "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2)


def sync_static_data():
    """Copy grades.csv and experience.md into docs/data so GitHub Pages can serve
    them regardless of whether the Pages source is set to /docs or the repo root."""
    for name in ("grades.csv", "experience.md"):
        shutil.copyfile(DATA_DIR / name, SITE_DATA_DIR / name)


def build_composition(holdings_raw, total_market_value_nok, cash_rows):
    """Full 'everything included' breakdown: investments (grouped below the
    threshold, funds bucketed by theme) plus cash/BSU, all as % of one grand
    total (investments + cash). No absolute figure is ever computed into this
    beyond the percentages themselves."""
    cash_total_nok = sum(float(r["balance"]) for r in cash_rows if r["currency"] == "NOK")
    grand_total_nok = total_market_value_nok + cash_total_nok
    if not grand_total_nok:
        return []

    individual, grouped_fund, grouped_other = [], defaultdict(lambda: [0.0, []]), [0.0, []]
    for h in holdings_raw:
        value = h["market_value_nok"] or 0
        pct = value / grand_total_nok * 100
        category = FUND_CATEGORY.get(h["ticker"])
        # use the investment-only threshold for the grouping decision, matching
        # what a reader would expect "under 2% of the portfolio" to mean
        pct_of_investments = (value / total_market_value_nok * 100) if total_market_value_nok else 0
        if pct_of_investments >= GROUPING_THRESHOLD_PCT:
            individual.append({"label": h["name"], "pct": round(pct, 2), "kind": h["type"]})
        elif category:
            grouped_fund[category][0] += pct
            grouped_fund[category][1].append(h["name"])
        else:
            grouped_other[0] += pct
            grouped_other[1].append(h["name"])

    composition = sorted(individual, key=lambda h: h["pct"], reverse=True)
    for category, (pct, members) in grouped_fund.items():
        composition.append({
            "label": f"{category} (each under {GROUPING_THRESHOLD_PCT:g}%)",
            "pct": round(pct, 2), "kind": "fund_group", "members": sorted(members),
        })
    if grouped_other[1]:
        composition.append({
            "label": f"Other holdings (each under {GROUPING_THRESHOLD_PCT:g}%)",
            "pct": round(grouped_other[0], 2), "kind": "other_group",
            "members": sorted(grouped_other[1]),
        })
    for r in cash_rows:
        if r["currency"] != "NOK":
            continue
        pct = float(r["balance"]) / grand_total_nok * 100
        composition.append({"label": r["account"], "pct": round(pct, 2), "kind": "cash"})

    composition.sort(key=lambda c: c["pct"], reverse=True)
    return composition


def main():
    SITE_DATA_DIR.mkdir(parents=True, exist_ok=True)
    sync_static_data()

    transactions = load_transactions(DATA_DIR / "transactions.csv")
    (positions, realized_total, realized_gain_by_ticker, realized_cost_by_ticker,
     name_by_ticker, is_fund_by_ticker, first_buy_by_ticker, last_sell_by_ticker) = \
        fifo_open_positions(transactions)

    raw_holdings = [price_position_raw(key, pos) for key, pos in positions.items()]
    raw_holdings.extend(load_manual_holdings_raw(DATA_DIR / "holdings_manual.csv"))
    raw_holdings = [h for h in raw_holdings if h["ticker"] not in EXCLUDED_TICKERS]

    total_market_value_nok = sum(h["market_value_nok"] or 0 for h in raw_holdings)
    total_cost_basis_nok = sum(h["cost_basis_nok"] or 0 for h in raw_holdings if h["cost_basis_nok"])
    total_unrealized_gain_nok = sum(
        (h["market_value_nok"] or 0) - (h["cost_basis_nok"] or 0)
        for h in raw_holdings if h["cost_basis_nok"]
    )
    blended_unrealized_return_pct = (
        total_unrealized_gain_nok / total_cost_basis_nok * 100 if total_cost_basis_nok else 0.0
    )

    holdings = [build_public_holding(h, total_market_value_nok) for h in raw_holdings]
    holdings.sort(key=lambda h: h["pct_of_portfolio"], reverse=True)

    cash_rows = load_cash(DATA_DIR / "cash.csv")
    cash_accounts = [{
        "account": r["account"],
        "interest_rate_pct": float(r["interest_rate_pct"]),
        "tax_deductible": r["tax_deductible"].lower() == "yes",
        "is_bsu": r["account"] == "BSU",
    } for r in cash_rows]

    bsu_benefit = compute_bsu_benefit(cash_rows)
    composition = build_composition(raw_holdings, total_market_value_nok, cash_rows)

    total_realized_cost = sum(realized_cost_by_ticker.values())
    blended_realized_return_pct = (
        realized_total / total_realized_cost * 100 if total_realized_cost else None
    )
    realized_by_ticker = []
    for ticker, cost in sorted(realized_cost_by_ticker.items(),
                                key=lambda kv: realized_gain_by_ticker.get(kv[0], 0) / kv[1] if kv[1] else 0,
                                reverse=True):
        if ticker in EXCLUDED_TICKERS or is_fund_by_ticker.get(ticker):
            continue
        gain = realized_gain_by_ticker.get(ticker, 0)
        if cost:
            entry = {
                "ticker": ticker,
                "name": name_by_ticker.get(ticker, ticker),
                "realized_return_pct": round(gain / cost * 100, 2),
            }
            if ticker in first_buy_by_ticker and ticker in last_sell_by_ticker:
                entry["period_held"] = format_period(first_buy_by_ticker[ticker], last_sell_by_ticker[ticker])
            realized_by_ticker.append(entry)

    portfolio = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "composition": composition,
        "holdings": holdings,
        "cash_accounts": cash_accounts,
        "bsu_tax_benefit": bsu_benefit,
        "realized_gains": {
            "blended_return_pct": round(blended_realized_return_pct, 2) if blended_realized_return_pct is not None else None,
            "by_ticker": realized_by_ticker,
        },
        "blended_unrealized_return_pct": round(blended_unrealized_return_pct, 2),
    }

    with open(SITE_DATA_DIR / "portfolio.json", "w", encoding="utf-8") as f:
        json.dump(portfolio, f, indent=2)

    append_history(blended_unrealized_return_pct)
    print(f"wrote portfolio.json: {len(holdings)} holdings, "
          f"blended unrealized return {blended_unrealized_return_pct:.2f}%")


if __name__ == "__main__":
    main()

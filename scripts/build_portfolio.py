"""
Builds site/data/portfolio.json and site/data/history.json from the CSV data files.

Realized gains for exchange-traded and fund sells are taken directly from the
broker's own computed result (data/transactions.csv: realized_gain_nok) rather than
re-derived here. FIFO is used only to track which lots of each holding are still
open, so unrealized gain / cost basis on current positions is accurate.
"""
import csv
import json
import shutil
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
SITE_DATA_DIR = ROOT / "site" / "data"

BSU_DEDUCTION_RATE_PCT = 10
BSU_MAX_DEPOSIT_NOK = 27_500
BSU_MAX_DEDUCTION_NOK = 2_750
BSU_MAX_BALANCE_NOK = 300_000

_fx_cache = {}


def load_transactions(path):
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def fifo_open_positions(transactions):
    """Group by (account, ticker); FIFO-consume SELL quantity against BUY lots.
    Returns dict keyed by (account, ticker) -> {qty, cost_nok, name, is_fund, isin,
    native_currency, last_native_price, last_date}, and total realized gain NOK."""
    lots = defaultdict(list)  # key -> [[qty, cost_nok], ...]
    meta = {}
    realized_total = 0.0
    realized_by_ticker = defaultdict(float)

    for row in sorted(transactions, key=lambda r: r["date"]):
        key = (row["account"], row["ticker"])
        qty = float(row["quantity"])
        amount = float(row["amount_nok"])
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
                lots[key][0][0] -= take
                lots[key][0][1] -= lot_cost * frac
                remaining -= take
                if lots[key][0][0] <= 1e-6:
                    lots[key].pop(0)
            if row["realized_gain_nok"]:
                gain = float(row["realized_gain_nok"])
                realized_total += gain
                realized_by_ticker[row["ticker"]] += gain

    positions = {}
    for key, lotlist in lots.items():
        qty = sum(l[0] for l in lotlist)
        cost = sum(l[1] for l in lotlist)
        if qty > 1e-4:
            positions[key] = {**meta[key], "quantity": qty, "cost_basis_nok": cost}

    return positions, realized_total, dict(realized_by_ticker)


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


def price_position(key, pos):
    account, ticker = key
    quantity = pos["quantity"]
    cost_basis_nok = pos["cost_basis_nok"]

    price, currency = (None, None)
    if not pos["is_fund"]:
        price, currency = fetch_live_price(ticker)
    price_source = "live"
    price_as_of = datetime.now(timezone.utc).isoformat()

    if price is None:
        # fund, or live fetch failed: fall back to last known transaction price
        price = pos["last_native_price"]
        currency = pos["native_currency"]
        price_source = "manual_fallback" if pos["is_fund"] else "last_known_fallback"
        price_as_of = pos["last_date"]
        if price is None:
            price, currency = 0.0, "NOK"

    fx_rate = fetch_fx_rate(currency, "NOK") if currency != "NOK" else 1.0
    if fx_rate is None:
        fx_rate = 1.0  # last resort, avoids crashing a daily unattended run

    market_value_nok = price * quantity * fx_rate
    unrealized_gain_nok = market_value_nok - cost_basis_nok
    unrealized_gain_pct = (unrealized_gain_nok / cost_basis_nok * 100) if cost_basis_nok else 0.0

    return {
        "ticker": ticker,
        "name": pos["name"],
        "isin": pos["isin"],
        "type": "fund" if pos["is_fund"] else "exchange_traded",
        "quantity": round(quantity, 4),
        "cost_basis_nok": round(cost_basis_nok, 2),
        "market_value_nok": round(market_value_nok, 2),
        "unrealized_gain_nok": round(unrealized_gain_nok, 2),
        "unrealized_gain_pct": round(unrealized_gain_pct, 2),
        "price_source": price_source,
        "price_as_of": price_as_of,
        "account": account,
        "flagged_manual": pos["is_fund"] and price_source == "manual_fallback",
    }


def load_manual_holdings(path):
    with open(path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    holdings = []
    for row in rows:
        market_value = float(row["market_value"])
        cost_basis = float(row["cost_basis"])
        currency = row["currency"]
        price_source = "manual_fallback"
        price_as_of = row["date"]

        price, live_currency = fetch_live_price(row["ticker"])
        if price:
            # can't know quantity for these rows, so a live price can't be converted
            # into a market value on its own; keep the provided market_value but note
            # that a live quote was seen for this ticker
            price_source = "manual_fallback"

        holdings.append({
            "ticker": row["ticker"],
            "name": row["name"],
            "isin": None,
            "type": "manual",
            "quantity": None,
            "cost_basis_nok": round(cost_basis, 2) if currency == "NOK" else None,
            "market_value_nok": round(market_value, 2) if currency == "NOK" else None,
            "unrealized_gain_nok": round(market_value - cost_basis, 2) if currency == "NOK" else None,
            "unrealized_gain_pct": round((market_value - cost_basis) / cost_basis * 100, 2) if cost_basis else 0.0,
            "price_source": price_source,
            "price_as_of": price_as_of,
            "account": row["account"],
            "flagged_manual": True,
        })
    return holdings


def load_cash(path):
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def compute_bsu_benefit(cash_rows):
    bsu = next((r for r in cash_rows if r["account"] == "BSU"), None)
    if not bsu:
        return {"applicable": False}

    contributions = bsu.get("contributions_this_year")
    result = {
        "applicable": True,
        "contributions_this_year": float(contributions) if contributions else None,
        "deduction_rate_pct": BSU_DEDUCTION_RATE_PCT,
        "max_deposit_nok": BSU_MAX_DEPOSIT_NOK,
        "max_deduction_nok": BSU_MAX_DEDUCTION_NOK,
        "max_balance_nok": BSU_MAX_BALANCE_NOK,
        "estimated_deduction_nok": None,
        "note": ("Requires this year's deposit amount (contributions_this_year), not "
                 "the account balance, to compute. Lost entirely for any year the "
                 "account holder owns residential property. Not added to market value "
                 "— it's a tax credit, not a return."),
    }
    if result["contributions_this_year"] is not None:
        deposit = min(result["contributions_this_year"], BSU_MAX_DEPOSIT_NOK)
        result["estimated_deduction_nok"] = round(deposit * BSU_DEDUCTION_RATE_PCT / 100, 2)
    return result


def append_history(total_value_nok):
    history_csv = DATA_DIR / "history.csv"
    today = datetime.now(timezone.utc).date().isoformat()

    rows = []
    if history_csv.exists():
        with open(history_csv, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    rows = [r for r in rows if r["date"] != today]
    rows.append({"date": today, "total_value_nok": round(total_value_nok, 2)})
    rows.sort(key=lambda r: r["date"])

    with open(history_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["date", "total_value_nok"])
        w.writeheader()
        w.writerows(rows)

    with open(SITE_DATA_DIR / "history.json", "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2)


def sync_static_data():
    """Copy grades.csv and experience.md into site/data so GitHub Pages can serve
    them regardless of whether the Pages source is set to /site or the repo root."""
    for name in ("grades.csv", "experience.md"):
        shutil.copyfile(DATA_DIR / name, SITE_DATA_DIR / name)


def main():
    SITE_DATA_DIR.mkdir(parents=True, exist_ok=True)
    sync_static_data()

    transactions = load_transactions(DATA_DIR / "transactions.csv")
    positions, realized_total, realized_by_ticker = fifo_open_positions(transactions)

    holdings = [price_position(key, pos) for key, pos in positions.items()]
    holdings.extend(load_manual_holdings(DATA_DIR / "holdings_manual.csv"))
    holdings.sort(key=lambda h: h["market_value_nok"] or 0, reverse=True)

    cash_rows = load_cash(DATA_DIR / "cash.csv")
    cash_accounts = [{
        "account": r["account"],
        "balance_nok": float(r["balance"]) if r["currency"] == "NOK" else None,
        "currency": r["currency"],
        "interest_rate_pct": float(r["interest_rate_pct"]),
        "tax_deductible": r["tax_deductible"].lower() == "yes",
        "is_bsu": r["account"] == "BSU",
    } for r in cash_rows]

    bsu_benefit = compute_bsu_benefit(cash_rows)

    market_value_total = sum(h["market_value_nok"] or 0 for h in holdings)
    cash_total = sum(c["balance_nok"] or 0 for c in cash_accounts)
    total_value = market_value_total + cash_total

    portfolio = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "currency": "NOK",
        "holdings": holdings,
        "cash_accounts": cash_accounts,
        "bsu_tax_benefit": bsu_benefit,
        "realized_gains": {
            "total_nok": round(realized_total, 2),
            "by_ticker": [{"ticker": t, "realized_gain_nok": round(g, 2)}
                          for t, g in sorted(realized_by_ticker.items(), key=lambda x: -x[1])],
        },
        "totals": {
            "market_value_nok": round(market_value_total, 2),
            "cash_nok": round(cash_total, 2),
            "total_portfolio_value_nok": round(total_value, 2),
        },
    }

    with open(SITE_DATA_DIR / "portfolio.json", "w", encoding="utf-8") as f:
        json.dump(portfolio, f, indent=2)

    append_history(total_value)
    print(f"wrote portfolio.json: {len(holdings)} holdings, total value {total_value:,.2f} NOK")


if __name__ == "__main__":
    main()

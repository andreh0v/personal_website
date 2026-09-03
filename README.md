# Portfolio site

Personal investment, academics, and experience site, published via GitHub Pages.

## Structure

- `data/` — source data: `transactions.csv` (buy/sell log, FIFO-matched for realized
  gains and open-position cost basis), `cash.csv`, `holdings_manual.csv` (holdings
  without a reliable live price), `grades.csv`, `experience.md`, and `history.csv`
  (appended daily with the total portfolio value).
- `scripts/build_portfolio.py` — reads `data/`, fetches live prices (yfinance) and FX
  rates, and writes `site/data/portfolio.json` + `site/data/history.json`. Also copies
  `grades.csv`/`experience.md` into `site/data/` so GitHub Pages can serve them
  regardless of which folder is configured as the Pages source.
- `site/` — the static frontend (vanilla HTML/CSS/JS + Chart.js, no build step).
- `.github/workflows/update-portfolio.yml` — runs the build script on weekdays after
  market close and commits the refreshed data back to the repo.

## Local run

```bash
pip install -r scripts/requirements.txt
python scripts/build_portfolio.py
python -m http.server --directory site 8000
```

Then open `http://localhost:8000`.

## Notes

- All amounts are displayed in NOK. Live prices for foreign-currency holdings are
  converted via a free FX API (exchangerate.host, falling back to frankfurter.app).
- Realized gains use the broker's own computed result per sale rather than being
  re-derived from FIFO, per `data/transactions.csv`'s `realized_gain_nok` column.
  FIFO is used only to track which lots of each holding remain open, for cost basis.
- Fund holdings that don't resolve on yfinance fall back to the most recent
  transaction price, flagged "last known" on the site.
- The BSU tax-benefit line is informational only and is never added to portfolio
  market value — it depends on the calendar year's deposit amount
  (`contributions_this_year` in `cash.csv`), not the account balance.
- GitHub Pages: set the Pages source to the `site/` folder (Settings → Pages).

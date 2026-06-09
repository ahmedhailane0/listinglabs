# How to run the perps correlation analysis

This folder contains scripts that pull data from Binance, CryptoRank, and RootData,
then build a single spreadsheet (`enriched.csv`) plus a notebook (`analysis.ipynb`)
that shows whether things like "how many other exchanges a token was on" or "how
much VC funding it raised" relate to how well the Binance perp does after launch.

You don't need to write any code. You just paste commands into PowerShell.

---

## One-time setup (do this once)

### 1. Open PowerShell
Press the Windows key, type `PowerShell`, hit Enter.

### 2. Install the libraries the scripts need
Copy this whole line, paste into PowerShell, press Enter:

```powershell
python -m pip install requests pandas numpy scipy matplotlib statsmodels jupyter
```

Wait until it finishes (a minute or two). You'll see "Successfully installed..." at the end.

### 3. Confirm your RootData API key is saved
The key was already saved to a safe file outside this project:
`C:\Users\PC\.config\verifysheet\secrets.env`

You can leave that file alone. The scripts read it automatically.

---

## Running the analysis

Every command below should be run from this folder. To get there, paste:

```powershell
cd C:\Users\PC\Documents\verifysheet\perps_correlation
```

### Step 1 — Pull Binance perp price/volume data
```powershell
python fetch_perp_klines.py
```
Takes ~5 minutes. Pulls daily price + funding rate data for every perp on the sheet.
Safe to re-run — it picks up where it left off.

### Step 2 — Pull CryptoRank investor data
```powershell
python fetch_cryptorank.py
```
Takes ~5 minutes. No API key needed.

### Step 3 — Pull RootData funding data
```powershell
python fetch_rootdata.py
```
Takes ~5 minutes. Uses your saved API key.

### Step 4 — Build the combined spreadsheet
```powershell
python build_enriched.py
```
Takes a few seconds. Produces `enriched_clean.csv` in this folder (the build
script automatically runs the cleaning pass and removes the raw intermediate).
**Open `enriched_clean.csv` in Excel** to see all the columns side by side.
A companion file `clean_log.txt` lists exactly which rows were dropped or fixed.

### Step 5 — Open the analysis notebook (charts + correlations)
```powershell
python -m jupyter notebook analysis.ipynb
```
This opens a browser tab. To see the results:
- Click the menu **Cell → Run All**
- Scroll down through the page to see the charts and correlation tables
- The last section ("7. Findings") explains what to look for

---

## What each file is

| File | What it does |
|---|---|
| `fetch_perp_klines.py` | Downloads Binance perp price + funding data |
| `fetch_cryptorank.py` | Downloads CryptoRank investor info |
| `fetch_rootdata.py` | Downloads RootData funding totals |
| `build_enriched.py` | Combines everything, runs cleaning, writes `enriched_clean.csv` |
| `clean_enriched.py` | Cleans the joined data (called automatically by `build_enriched.py`) |
| `enriched_clean.csv` | The combined+cleaned spreadsheet (open in Excel) |
| `clean_log.txt` | Audit trail of what was dropped or fixed during cleaning |
| `EXPLANATION.txt` | Long plain-text walkthrough of the whole project |
| `make_report.py` | Builds the Word document (`python make_report.py`) |
| `PERPS_REPORT.docx` | The polished Word report with embedded charts (open in Word) |
| `charts/*.png` | Chart images extracted from the notebook (re-created by `make_report.py`) |
| `analysis.ipynb` | The notebook with charts and correlations |

The four `fetch_*.py` scripts save their downloaded data into `..\cache\` (the
`cache` folder one level up). That way if you re-run them, they only pull new
data — they don't re-download everything.

---

## If something goes wrong

**"python is not recognized..."** — Python isn't on your PATH. Reinstall Python
from python.org and tick "Add Python to PATH" during install.

**"No module named requests"** — You skipped the install step. Re-run the line
under "One-time setup" step 2.

**A fetch script crashes partway** — Just run it again. The cache means it picks
up where it stopped.

**The notebook shows red errors** — Most likely `enriched.csv` is out of date.
Re-run step 4 (`python build_enriched.py`), then in the notebook do **Cell → Run All**.

---

## The bottom line so far

From the data already pulled, the strongest signals are:

- **Volume sustain** (does the perp keep volume after launch?):
  - Tokens already listed on more CEXes **before** the Binance perp keep volume better.
  - Tokens that get co-listed alongside the Binance perp (within ±7 days) **lose**
    volume faster — looks like a sell-the-news effect.
  - More VC investors → slightly faster volume fade.

- **30-day price return**:
  - Higher FDV at listing → slightly better 30-day return (the big-cap tokens hold up better).
  - Co-launches hurt 30-day return.
  - Funding totals do **not** predict price return.

Open `analysis.ipynb` to see the full picture with charts.

"""Load YuJun's Lending Club accepted / rejected CSVs into a local SQLite
`applications` table with the same columns as the synthetic one, so the
policy reports (cutoff simulation, approval funnel, applicant drift) run on
real data. The other reports need loans/performance tables and are skipped.

Only the columns both files share are kept:

    accepted                     rejected                 -> applications
    issue_d                      Application Date         app_month (YYYY-MM)
    loan_amnt                    Amount Requested         amount
    mean of fico_range_low/high  Risk_Score               score
    dti                          Debt-To-Income Ratio     dti (percent)
    addr_state                   State                    region
    emp_length                   Employment Length        emp_years
    loan_status                  -                        bad (accepted only)

bad = 1 for Charged Off / Default / Late (31-120 days), 0 for Fully Paid,
empty while the loan is still Current, In Grace Period or Late (16-30 days).
The files are read in chunks, so the full 2007-2018 files fit in memory.
"""

from pathlib import Path
import sqlite3
import time

import numpy as np
import pandas as pd


ACCEPTED_COLS = ["issue_d", "loan_amnt", "fico_range_low", "fico_range_high", "dti",
                 "addr_state", "emp_length", "loan_status"]
REJECTED_COLS = ["Application Date", "Amount Requested", "Risk_Score", "Debt-To-Income Ratio",
                 "State", "Employment Length"]
BAD = {"Charged Off", "Default", "Late (31-120 days)",
       "Does not meet the credit policy. Status:Charged Off"}
GOOD = {"Fully Paid", "Does not meet the credit policy. Status:Fully Paid"}
CHUNK = 200_000


def emp_years(s: pd.Series) -> pd.Series:
    """'< 1 year' -> 0, '10+ years' -> 10, '3 years' -> 3, 'n/a' -> NaN."""
    s = s.astype("string").str.strip()
    out = pd.to_numeric(s.str.extract(r"(\d+)")[0], errors="coerce")
    return out.mask(s.str.startswith("<", na=False), 0)


def _accepted(chunk: pd.DataFrame) -> pd.DataFrame:
    month = pd.to_datetime(chunk.issue_d, format="%b-%Y", errors="coerce")
    status = chunk.loan_status.astype("string")
    bad = np.where(status.isin(BAD), 1.0, np.where(status.isin(GOOD), 0.0, np.nan))
    out = pd.DataFrame({
        "app_month": month.dt.strftime("%Y-%m"),
        "amount": pd.to_numeric(chunk.loan_amnt, errors="coerce"),
        "score": (pd.to_numeric(chunk.fico_range_low, errors="coerce")
                  + pd.to_numeric(chunk.fico_range_high, errors="coerce")) / 2,
        "dti": pd.to_numeric(chunk.dti, errors="coerce"),
        "region": chunk.addr_state,
        "emp_years": emp_years(chunk.emp_length),
        "approved": 1,
        "bad": bad,
    })
    return out[month.notna()]  # drops the "Total amount funded" footer lines


def _rejected(chunk: pd.DataFrame) -> pd.DataFrame:
    month = pd.to_datetime(chunk["Application Date"], format="%Y-%m-%d", errors="coerce")
    dti = pd.to_numeric(chunk["Debt-To-Income Ratio"].astype("string").str.rstrip("%"), errors="coerce")
    out = pd.DataFrame({
        "app_month": month.dt.strftime("%Y-%m"),
        "amount": pd.to_numeric(chunk["Amount Requested"], errors="coerce"),
        "score": pd.to_numeric(chunk["Risk_Score"], errors="coerce"),
        "dti": dti.where(dti >= 0),  # -1% means not provided
        "region": chunk["State"],
        "emp_years": emp_years(chunk["Employment Length"]),
        "approved": 0,
        "bad": np.nan,
    })
    return out[month.notna()]


def _stream(path: Path, cols, convert, frac, rng, since, until, conn, log):
    n_read = n_kept = 0
    t = time.perf_counter()
    for chunk in pd.read_csv(path, usecols=cols, dtype=str, chunksize=CHUNK, low_memory=False):
        n_read += len(chunk)
        if frac < 1:
            chunk = chunk[rng.random(len(chunk)) < frac]
        rows = convert(chunk)
        if since:
            rows = rows[rows.app_month >= since]
        if until:
            rows = rows[rows.app_month <= until]
        rows.to_sql("applications", conn, if_exists="append", index=False)
        n_kept += len(rows)
        log(f"[load] {path.name}: read {n_read:,}, kept {n_kept:,} ({time.perf_counter() - t:.0f}s)")
    return n_kept


def load(accepted: Path, rejected: Path, db: Path, frac: float = 1.0, since: str | None = None,
         until: str | None = None, seed: int = 42, log=print) -> dict[str, int]:
    """Replace `db` with an applications table built from the two CSVs (.csv or .csv.gz)."""
    for p in (accepted, rejected):
        if not p.is_file():
            raise FileNotFoundError(f"Not found: {p}")
    db.parent.mkdir(parents=True, exist_ok=True)
    tmp = db.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    rng = np.random.default_rng(seed)
    with sqlite3.connect(tmp) as conn:
        n_acc = _stream(accepted, ACCEPTED_COLS, _accepted, frac, rng, since, until, conn, log)
        n_rej = _stream(rejected, REJECTED_COLS, _rejected, frac, rng, since, until, conn, log)
        conn.execute("CREATE INDEX idx_app_month ON applications(app_month)")
        pd.DataFrame([
            {"key": "notice", "value": "Lending Club public data (US, 2007-2018), stand-in for a Chinese lender"},
            {"key": "source", "value": "lendingclub"},
            {"key": "sample_frac", "value": str(frac)},
            {"key": "accepted_file", "value": accepted.name},
            {"key": "rejected_file", "value": rejected.name},
            {"key": "outcome_definition", "value": "贷款最终状态为核销(Charged Off)、违约(Default)或逾期31~120天"},
        ]).to_sql("_meta", conn, index=False)
    tmp.replace(db)
    return {"accepted": n_acc, "rejected": n_rej}

"""Build a SYNTHETIC small-loan dataset in SQLite for demos and cloud testing.

Everything here is randomly generated. No row describes a real customer, and
none of it comes from YuJun's Lending Club files. It imitates a Chinese
consumer-instalment lender: 12-month equal-instalment loans (APR <= 24%),
acquisition channels, a credit score, monthly DPD buckets, and an SMS
reactivation campaign for existing customers.

Built-in "stories" so the reports have something to find:
- The API导流 (partner API) channel grows from ~15% to ~40% of new loans
  between 2025-01 and 2026-06. It is riskier, so later vintages look worse.
- Applicant scores drift down over time (PSI rises), and the credit score
  cannot see a hidden risk factor that grows in API导流 traffic, so KS/AUC
  fall in later cohorts.
- The reactivation response model has real but modest lift over control.
"""

import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd


ISSUE_MONTHS = pd.period_range("2025-01", "2026-06", freq="M")
OBSERVATION_END = pd.Period("2026-06", freq="M")
TERM = 12
BUCKETS = ["C", "M1", "M2", "M3", "M4+", "结清", "核销"]
WRITE_OFF_AFTER = 3  # months spent in M4+ before the balance is written off

# channel: (cost per drawdown CNY, monthly miss hazard multiplier,
#           score offset, score accuracy, click-through, reg rate, apply rate, approval rate)
CHANNELS = {
    "抖音信息流": (420, 1.15, -10, 0.72, 0.012, 0.30, 0.45, 0.42),
    "腾讯广告": (380, 1.00, 0, 0.75, 0.010, 0.32, 0.48, 0.46),
    "应用商店": (260, 0.80, 15, 0.78, 0.030, 0.40, 0.55, 0.52),
    "API导流": (300, 1.60, -25, 0.45, 0.0, 0.0, 0.0, 0.30),
    "老客复贷": (60, 0.55, 40, 0.80, 0.0, 0.0, 0.70, 0.75),
}

ROLL_FORWARD = {  # probability of rolling to the next bucket, else cure
    1: 0.38,
    2: 0.62,
    3: 0.82,
}


def _channel_mix(month_index: int) -> dict[str, float]:
    api_share = 0.15 + 0.25 * month_index / (len(ISSUE_MONTHS) - 1)
    rest = 1 - api_share
    return {
        "抖音信息流": rest * 0.30,
        "腾讯广告": rest * 0.25,
        "应用商店": rest * 0.20,
        "API导流": api_share,
        "老客复贷": rest * 0.25,
    }


def _annuity_balance(principal, monthly_rate, payments_made):
    growth_n = (1 + monthly_rate) ** TERM
    growth_k = (1 + monthly_rate) ** payments_made
    return principal * (growth_n - growth_k) / (growth_n - 1)


def generate(seed: int = 2026) -> dict[str, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    funnel_rows, loan_frames = [], []
    next_id = 1

    for m_idx, month in enumerate(ISSUE_MONTHS):
        total_loans = int(rng.normal(2000 + 60 * m_idx, 80))
        for channel, share in _channel_mix(m_idx).items():
            cost_per, hazard_mult, score_off, accuracy, ctr, reg, apply_r, appr = CHANNELS[channel]
            n = max(int(total_loans * share * rng.normal(1, 0.04)), 50)
            approval = appr * rng.normal(1, 0.03)
            applications = int(n / approval / 0.88)  # ~88% of approved draw down
            approvals = int(applications * approval)
            if ctr:
                registrations = int(applications / apply_r)
                clicks = int(registrations / reg)
                impressions = int(clicks / ctr)
            else:
                impressions = clicks = None
                registrations = int(applications / apply_r) if apply_r else None
            funnel_rows.append({
                "month": str(month), "channel": channel,
                "impressions": impressions, "clicks": clicks,
                "registrations": registrations, "applications": applications,
                "approvals": approvals, "drawdowns": n,
                "marketing_cost": round(n * cost_per * rng.normal(1, 0.06), 2),
            })

            # Latent risk z drives both default and (imperfectly) the score.
            z = rng.normal(0, 1, n)
            # Later traffic (API导流 most) carries risk the old score cannot see.
            trend = m_idx / (len(ISSUE_MONTHS) - 1)
            hidden = rng.normal(0, 1, n) * (1.0 if channel == "API导流" else 0.35) * trend
            risk = z + hidden
            noise = rng.normal(0, 1, n)
            drift = -22 * trend  # applicant population slowly gets weaker
            score = 640 + score_off + drift - 55 * (accuracy * z + np.sqrt(1 - accuracy**2) * noise)
            score = np.clip(np.round(score), 350, 900).astype(int)
            hazard = 1 / (1 + np.exp(-(-4.1 + 0.9 * risk))) * hazard_mult
            amount = np.clip(np.round(rng.lognormal(np.log(8000), 0.55, n), -2), 1000, 50000)
            apr = np.clip(0.24 - (score - 450) / 450 * 0.12 + rng.normal(0, 0.01, n), 0.10, 0.24)
            loan_frames.append(pd.DataFrame({
                "loan_id": np.arange(next_id, next_id + n),
                "issue_month": str(month), "channel": channel,
                "amount": amount, "apr": np.round(apr, 4), "term": TERM,
                "credit_score": score,
                "age": np.clip(rng.normal(31, 7, n).round(), 20, 55).astype(int),
                "_hazard": hazard,
            }))
            next_id += n

    loans = pd.concat(loan_frames, ignore_index=True)
    performance = _simulate_performance(loans, rng)
    return {
        "funnel": pd.DataFrame(funnel_rows),
        "loans": loans.drop(columns="_hazard"),
        "performance": performance,
        "reactivation_leads": _reactivation_campaigns(rng),
    }


def _simulate_performance(loans: pd.DataFrame, rng) -> pd.DataFrame:
    n = len(loans)
    state = np.zeros(n, dtype=int)  # index into BUCKETS
    months_m4 = np.zeros(n, dtype=int)
    paid = np.zeros(n, dtype=int)  # instalments paid so far
    principal = loans["amount"].to_numpy()
    rate = loans["apr"].to_numpy() / 12
    hazard = loans["_hazard"].to_numpy()
    issue = pd.PeriodIndex(loans["issue_month"], freq="M")
    roll_mult = np.where(loans["channel"] == "API导流", 1.12, 1.0)
    months_observable = np.array([(OBSERVATION_END - p).n for p in issue])
    frames = []

    for mob in range(1, TERM + 1):
        u = rng.random(n)
        prev = state.copy()
        balance_before = _annuity_balance(principal, rate, paid)
        payments = np.zeros(n)

        current = prev == 0
        miss = current & (u < hazard)
        pays = current & ~miss
        state[miss] = 1
        payments[pays] = 1

        for bucket, p_roll in ROLL_FORWARD.items():
            here = prev == bucket
            rolls = here & (u < np.minimum(p_roll * roll_mult, 0.97))
            state[rolls] = bucket + 1
            cures = here & ~rolls
            state[cures] = 0
            payments[cures] = bucket + 1  # catch up all missed instalments

        payments = np.minimum(payments, TERM - paid)
        interest_paid = np.round(balance_before * rate * payments, 2)
        paid_after = paid + payments.astype(int)
        prepay = (state == 0) & (rng.random(n) < 0.02) & (paid_after < TERM)
        balance_after = _annuity_balance(principal, rate, paid_after)
        principal_paid = np.round(balance_before - balance_after, 2)
        principal_paid[prepay] += np.round(balance_after[prepay], 2)
        done = (state == 0) & ((paid_after >= TERM) | prepay)
        state[done] = 5
        months_m4 = np.where(state == 4, months_m4 + 1, 0)
        balance_after = np.where(state == 5, 0.0, np.round(balance_after, 2))
        write_off = (months_m4 > WRITE_OFF_AFTER) | ((state == 4) & (mob == TERM))
        written_off_amount = np.where(write_off, balance_after, 0.0)
        state[write_off] = 6
        balance_after = np.where(write_off, 0.0, balance_after)
        paid = paid_after

        visible = (prev < 5) & (mob <= months_observable)
        frames.append(pd.DataFrame({
            "loan_id": loans["loan_id"].to_numpy()[visible],
            "mob": mob,
            "stat_month": [str(p + mob) for p in issue[visible]],
            "bucket": np.array(BUCKETS)[state[visible]],
            "balance": balance_after[visible],
            "interest_paid": interest_paid[visible],
            "principal_paid": principal_paid[visible],
            "written_off": written_off_amount[visible],
        }))
    return pd.concat(frames, ignore_index=True)


def _reactivation_campaigns(rng) -> pd.DataFrame:
    frames = []
    customer_id = 900000
    for month in ["2026-04", "2026-05", "2026-06"]:
        n = 30000
        latent = rng.normal(0, 1, n)
        response_score = 1 / (1 + np.exp(-(-3.0 + 0.8 * (0.6 * latent + 0.8 * rng.normal(0, 1, n)))))
        is_control = rng.random(n) < 0.10
        base = 1 / (1 + np.exp(-(-3.6 + 0.9 * latent)))
        p = np.where(is_control, base, base * 1.9)
        responded = rng.random(n) < p
        frames.append(pd.DataFrame({
            "customer_id": np.arange(customer_id, customer_id + n),
            "campaign_month": month,
            "is_control": is_control.astype(int),
            "response_score": np.round(response_score, 4),
            "sms_cost": np.where(is_control, 0.0, 0.05),
            "responded": responded.astype(int),
            "drawdown_amount": np.where(
                responded, np.clip(np.round(rng.lognormal(np.log(7000), 0.5, n), -2), 1000, 50000), 0.0),
        }))
        customer_id += n
    return pd.concat(frames, ignore_index=True)


def build_database(path: Path, seed: int = 2026) -> dict[str, int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    tables = generate(seed)
    tmp = path.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    with sqlite3.connect(tmp) as conn:
        for name, frame in tables.items():
            frame.to_sql(name, conn, index=False)
        conn.execute("CREATE INDEX idx_perf_loan ON performance(loan_id, mob)")
        conn.execute("CREATE INDEX idx_perf_month ON performance(stat_month)")
        pd.DataFrame([{
            "key": "notice",
            "value": "SYNTHETIC DATA 合成数据 - randomly generated, not real customers",
        }, {"key": "seed", "value": str(seed)},
            {"key": "observation_end", "value": str(OBSERVATION_END)}]).to_sql("_meta", conn, index=False)
    tmp.replace(path)
    return {name: len(frame) for name, frame in tables.items()}

"""Every number in a report is computed here, with SQL and pandas.

The language model never calculates. It receives each analysis's small table
plus pre-computed comparison sentences ("facts") and only writes the prose.
Add a new analysis by writing a function and registering it in ANALYSES.
"""

from dataclasses import dataclass, field
import sqlite3
from typing import Callable

import numpy as np
import pandas as pd


BAD_BUCKETS = "('M2','M3','M4+')"  # 30+ DPD


@dataclass
class AnalysisResult:
    key: str
    title: str
    table: pd.DataFrame  # already formatted for display
    facts: list[str] = field(default_factory=list)
    note: str = ""  # definition of the metric, shown under the table


@dataclass(frozen=True)
class Analysis:
    key: str
    team: str  # "risk" or "marketing"
    title: str
    description: str  # shown to the model when it plans the report
    run: Callable[[sqlite3.Connection], AnalysisResult]


def pct(x, digits=2) -> str:
    return "-" if pd.isna(x) else f"{x * 100:.{digits}f}%"


def pp(x) -> str:
    """Percentage-point difference."""
    return f"{x * 100:+.2f}个百分点"


def money(x) -> str:
    if pd.isna(x):
        return "-"
    if abs(x) >= 1e4:
        return f"{x / 1e4:,.1f}万"
    return f"{x:,.0f}"


def _q(conn, sql, params=()) -> pd.DataFrame:
    return pd.read_sql_query(sql, conn, params=params)


def _bad_by_mob6(conn) -> pd.DataFrame:
    """One row per loan: issue month, channel, score, ever 30+ within MOB 6."""
    return _q(conn, f"""
        SELECT l.loan_id, l.issue_month, l.channel, l.credit_score, l.apr, l.amount,
               MAX(CASE WHEN p.mob <= 6 AND p.bucket IN {BAD_BUCKETS} THEN 1 ELSE 0 END) AS bad6,
               MAX(p.mob) AS max_mob
        FROM loans l JOIN performance p USING (loan_id)
        GROUP BY l.loan_id
    """)


def _matured(df: pd.DataFrame, mob: int = 6) -> pd.DataFrame:
    """Keep cohorts observed for `mob` months. Filtering per loan would drop
    loans repaid early, which are mostly good, and overstate bad rates."""
    reached = df.groupby("issue_month").max_mob.transform("max") >= mob
    return df[reached]


# ---------------------------------------------------------------- risk

def portfolio_overview(conn) -> AnalysisResult:
    df = _q(conn, f"""
        SELECT stat_month,
               SUM(CASE WHEN bucket <> '结清' THEN 1 ELSE 0 END) AS active_loans,
               SUM(balance) AS balance,
               SUM(CASE WHEN bucket IN {BAD_BUCKETS} THEN balance ELSE 0 END) AS bal_30,
               SUM(CASE WHEN bucket = 'M4+' THEN balance ELSE 0 END) AS bal_90,
               SUM(written_off) AS written_off
        FROM performance GROUP BY stat_month ORDER BY stat_month
    """).tail(6).reset_index(drop=True)
    df["r30"] = df.bal_30 / df.balance
    df["r90"] = df.bal_90 / df.balance
    first, last = df.iloc[0], df.iloc[-1]
    table = pd.DataFrame({
        "统计月": df.stat_month,
        "在贷笔数": df.active_loans.map("{:,}".format),
        "在贷余额(元)": df.balance.map(money),
        "30+余额占比": df.r30.map(pct),
        "90+余额占比": df.r90.map(pct),
        "当月核销(元)": df.written_off.map(money),
    })
    facts = [
        f"{last.stat_month} 在贷余额 {money(last.balance)} 元，较 {first.stat_month} 的 {money(first.balance)} 元"
        f"{'增加' if last.balance > first.balance else '减少'} {pct(abs(last.balance / first.balance - 1))}。",
        f"30+余额占比从 {pct(first.r30)} 变为 {pct(last.r30)}（{pp(last.r30 - first.r30)}）。",
        f"90+余额占比从 {pct(first.r90)} 变为 {pct(last.r90)}（{pp(last.r90 - first.r90)}）。",
        f"近6个月累计核销 {money(df.written_off.sum())} 元。",
    ]
    return AnalysisResult("portfolio_overview", "资产组合概览（近6个月）", table, facts,
                          "在贷 = 未结清且未核销；30+ = 逾期31天及以上(M2+)；90+ = M4+；M4+ 满3个月后核销，移出余额。")


def vintage(conn) -> AnalysisResult:
    df = _q(conn, f"""
        SELECT l.issue_month, l.loan_id,
               MIN(CASE WHEN p.bucket IN {BAD_BUCKETS} THEN p.mob END) AS first_bad_mob,
               MAX(p.mob) AS max_mob
        FROM loans l JOIN performance p USING (loan_id)
        GROUP BY l.loan_id
    """)
    observed = _q(conn, "SELECT issue_month, MAX(mob) AS obs FROM loans JOIN performance USING (loan_id) GROUP BY 1")
    obs = dict(zip(observed.issue_month, observed.obs))
    rows = []
    for month, g in df.groupby("issue_month"):
        row = {"放款月": month, "放款笔数": len(g)}
        for k in (3, 6, 9, 12):
            row[f"MOB{k}"] = (g.first_bad_mob <= k).mean() if obs[month] >= k else np.nan
        rows.append(row)
    raw = pd.DataFrame(rows)
    table = raw.copy()
    for k in (3, 6, 9, 12):
        table[f"MOB{k}"] = raw[f"MOB{k}"].map(pct)
    table = table.rename(columns={f"MOB{k}": f"MOB{k} 累计30+" for k in (3, 6, 9, 12)})
    mob6 = raw.dropna(subset=["MOB6"])
    early, late = mob6.head(3), mob6.tail(3)
    worst = mob6.loc[mob6.MOB6.idxmax()]
    facts = [
        f"前3个批次（{early.放款月.iloc[0]}~{early.放款月.iloc[-1]}）MOB6 累计30+ 平均 {pct(early.MOB6.mean())}，"
        f"最近3个已满MOB6的批次（{late.放款月.iloc[0]}~{late.放款月.iloc[-1]}）平均 {pct(late.MOB6.mean())}，"
        f"差 {pp(late.MOB6.mean() - early.MOB6.mean())}。",
        f"最近3个批次（{raw.放款月.iloc[-3]}~{raw.放款月.iloc[-1]}）尚未满MOB6；"
        f"已满MOB3的最新批次 {raw.dropna(subset=['MOB3']).放款月.iloc[-1]} 的 MOB3 累计30+ 为 {pct(raw.dropna(subset=['MOB3']).MOB3.iloc[-1])}。",
        f"MOB6 最差批次为 {worst.放款月}，累计30+ {pct(worst.MOB6)}。",
    ]
    mob12 = raw.dropna(subset=["MOB12"])
    if len(mob12):
        facts.append(f"已满 MOB12 的 {len(mob12)} 个批次，MOB12 累计30+ 在 {pct(mob12.MOB12.min())}~{pct(mob12.MOB12.max())} 之间。")
    return AnalysisResult("vintage", "Vintage 账龄分析（按笔数，累计30+逾期率）", table, facts,
                          "累计30+：截至该账龄曾经逾期31天及以上的贷款笔数占比；'-' 表示尚未到达该账龄。")


def roll_rates(conn) -> AnalysisResult:
    df = _q(conn, """
        SELECT b.stat_month, a.bucket AS from_b, b.bucket AS to_b, SUM(a.balance) AS bal
        FROM performance a JOIN performance b ON a.loan_id = b.loan_id AND b.mob = a.mob + 1
        GROUP BY 1, 2, 3
    """)
    steps = [("C", "M1"), ("M1", "M2"), ("M2", "M3"), ("M3", "M4+")]
    months = sorted(df.stat_month.unique())[-6:]
    rows = []
    for m in months:
        g = df[df.stat_month == m]
        row = {"统计月": m}
        for f, t in steps:
            base = g[g.from_b == f].bal.sum()
            row[f"{f}→{t}"] = g[(g.from_b == f) & (g.to_b == t)].bal.sum() / base if base else np.nan
        rows.append(row)
    raw = pd.DataFrame(rows)
    table = raw.copy()
    for f, t in steps:
        table[f"{f}→{t}"] = raw[f"{f}→{t}"].map(pct)
    first, last = raw.iloc[0], raw.iloc[-1]
    facts = [f"{f}→{t} 迁徙率从 {first.统计月} 的 {pct(first[f'{f}→{t}'])} 变为 {last.统计月} 的 "
             f"{pct(last[f'{f}→{t}'])}（{pp(last[f'{f}→{t}'] - first[f'{f}→{t}'])}）。" for f, t in steps]
    return AnalysisResult("roll_rates", "逾期迁徙率（按余额，近6个月）", table, facts,
                          "迁徙率 = 上月处于该逾期阶段的余额中，本月滚入下一阶段的比例。")


def _auc_ks(score, bad):
    score, bad = np.asarray(score, float), np.asarray(bad, int)
    n_bad, n_good = bad.sum(), len(bad) - bad.sum()
    if n_bad == 0 or n_good == 0:
        return np.nan, np.nan
    ranks = pd.Series(-score).rank().to_numpy()  # low score = high risk
    auc = (ranks[bad == 1].sum() - n_bad * (n_bad + 1) / 2) / (n_bad * n_good)
    order = np.argsort(score)
    cum_bad = np.cumsum(bad[order]) / n_bad
    cum_good = np.cumsum(1 - bad[order]) / n_good
    return auc, np.max(np.abs(cum_bad - cum_good))


def _psi(expected, actual, edges):
    e = np.histogram(expected, edges)[0] / len(expected)
    a = np.histogram(actual, edges)[0] / len(actual)
    e, a = np.clip(e, 1e-6, None), np.clip(a, 1e-6, None)
    return float(np.sum((a - e) * np.log(a / e)))


def score_monitoring(conn) -> AnalysisResult:
    df = _bad_by_mob6(conn)
    baseline = df[df.issue_month <= "2025-03"].credit_score
    edges = np.unique(np.quantile(baseline, np.linspace(0, 1, 11)))
    edges[0], edges[-1] = -np.inf, np.inf
    rows = []
    for month, g in df.groupby("issue_month"):
        matured = g.max_mob.max() >= 6
        auc, ks = _auc_ks(g.credit_score, g.bad6) if matured else (np.nan, np.nan)
        rows.append({"放款月": month, "样本数": len(g), "坏样本率": g.bad6.mean() if matured else np.nan,
                     "AUC": auc, "KS": ks, "PSI": _psi(baseline, g.credit_score, edges)})
    raw = pd.DataFrame(rows)
    fmt3 = lambda x: "-" if pd.isna(x) else f"{x:.3f}"
    table = pd.DataFrame({
        "放款月": raw.放款月, "样本数": raw.样本数.map("{:,}".format),
        "MOB6坏样本率": raw.坏样本率.map(pct),
        "AUC": raw.AUC.map(fmt3), "KS": raw.KS.map(fmt3), "PSI": raw.PSI.map(fmt3),
    })
    perf = raw.dropna(subset=["KS"])
    first, last = perf.head(3), perf.tail(3)
    max_psi = raw.loc[raw.PSI.idxmax()]
    facts = [
        f"KS 前3个批次（{first.放款月.iloc[0]}~{first.放款月.iloc[-1]}）平均 {first.KS.mean():.3f}，"
        f"最近3个已满MOB6的批次（{last.放款月.iloc[0]}~{last.放款月.iloc[-1]}）平均 {last.KS.mean():.3f}。",
        f"AUC 同口径从 {first.AUC.mean():.3f} 变为 {last.AUC.mean():.3f}。",
        f"最新放款月 {raw.放款月.iloc[-1]} 的 PSI 为 {raw.PSI.iloc[-1]:.3f}。",
        f"PSI 最高为 {max_psi.放款月} 的 {max_psi.PSI:.3f}；"
        f"PSI>0.10 的批次有 {int((raw.PSI > 0.10).sum())} 个，PSI>0.25 的有 {int((raw.PSI > 0.25).sum())} 个。",
        "行业常用阈值：PSI<0.10 稳定，0.10~0.25 需关注，>0.25 显著偏移；KS 低于0.20 区分能力偏弱。",
    ]
    return AnalysisResult("score_monitoring", "信用评分模型监控（KS / AUC / PSI）", table, facts,
                          "坏样本 = MOB6 内曾逾期30+，未满MOB6的批次不计算KS/AUC（显示为 -）；PSI 以2025-01~03放款客群的评分分布为基准，十等分。")


def channel_risk(conn) -> AnalysisResult:
    df = _bad_by_mob6(conn)
    df = _matured(df)
    df["half"] = np.where(df.issue_month <= "2025-06", "H1", "H2")
    g = df.groupby(["channel", "half"]).agg(n=("bad6", "size"), bad=("bad6", "mean"),
                                           score=("credit_score", "mean")).unstack("half")
    share = df.groupby("half").channel.value_counts(normalize=True).unstack("half")
    table = pd.DataFrame({
        "渠道": g.index,
        "上半年占比": share["H1"].reindex(g.index).map(pct).to_numpy(),
        "下半年占比": share["H2"].reindex(g.index).map(pct).to_numpy(),
        "上半年MOB6坏率": g["bad"]["H1"].map(pct).to_numpy(),
        "下半年MOB6坏率": g["bad"]["H2"].map(pct).to_numpy(),
        "下半年平均评分": g["score"]["H2"].round(0).astype(int).to_numpy(),
    })
    worst = g["bad"]["H2"].idxmax()
    grow = (share["H2"] - share["H1"]).idxmax()
    facts = [
        f"下半年 MOB6 坏率最高的渠道是 {worst}（{pct(g['bad']['H2'][worst])}）。",
        f"占比增长最多的渠道是 {grow}：从 {pct(share['H1'][grow])} 升至 {pct(share['H2'][grow])}。",
        f"全部贷款 MOB6 坏率：上半年 {pct(df[df.half == 'H1'].bad6.mean())}，下半年 {pct(df[df.half == 'H2'].bad6.mean())}。",
    ]
    return AnalysisResult("channel_risk", "分渠道风险表现（2025上半年 vs 下半年放款）", table, facts,
                          "MOB6坏率 = 放款后6个月内曾逾期30+的笔数占比。")


def score_bands(conn) -> AnalysisResult:
    df = _bad_by_mob6(conn)
    df = _matured(df)
    bins = [0, 560, 600, 630, 660, 690, 720, 2000]
    labels = ["<560", "560-599", "600-629", "630-659", "660-689", "690-719", "720+"]
    df["band"] = pd.cut(df.credit_score, bins, right=False, labels=labels)
    g = df.groupby("band", observed=False).agg(n=("bad6", "size"), bad=("bad6", "mean"), apr=("apr", "mean"))
    g["share"] = g.n / g.n.sum()
    g = g.reset_index()
    table = pd.DataFrame({
        "评分段": g.band.astype(str), "笔数": g.n.map("{:,}".format), "占比": g.share.map(pct),
        "MOB6坏率": g.bad.map(pct), "平均年化利率": g.apr.map(pct),
    })
    facts = [
        f"最低评分段（{labels[0]}）MOB6 坏率 {pct(g.bad.iloc[0])}，最高评分段（{labels[-1]}）{pct(g.bad.iloc[-1])}，"
        f"相差 {g.bad.iloc[0] / g.bad.iloc[-1]:.1f} 倍。",
        f"坏率是否随评分单调下降：{'是' if g.bad.is_monotonic_decreasing else '否'}。",
    ]
    return AnalysisResult("score_bands", "评分段风险与定价", table, facts, "年化利率均不超过24%。")


# ---------------------------------------------------------------- marketing

def acquisition_funnel(conn) -> AnalysisResult:
    df = _q(conn, """
        SELECT channel, SUM(impressions) imp, SUM(clicks) clk, SUM(registrations) reg,
               SUM(applications) app, SUM(approvals) apv, SUM(drawdowns) dd, SUM(marketing_cost) cost
        FROM funnel GROUP BY channel
    """)
    df["cac"] = df.cost / df.dd
    table = pd.DataFrame({
        "渠道": df.channel,
        "点击率": (df.clk / df.imp).map(pct),
        "注册率": (df.reg / df.clk).map(pct),
        "申请率": (df.app / df.reg).map(pct),
        "审批通过率": (df.apv / df.app).map(pct),
        "提款率": (df.dd / df.apv).map(pct),
        "放款笔数": df.dd.map("{:,.0f}".format),
        "获客成本(元/笔)": df.cac.map("{:,.0f}".format),
    })
    cheapest, dearest = df.loc[df.cac.idxmin()], df.loc[df.cac.idxmax()]
    top_appr = df.loc[(df.apv / df.app).idxmax()]
    facts = [
        f"累计放款 {df.dd.sum():,.0f} 笔，营销费用 {money(df.cost.sum())} 元，平均获客成本 {df.cost.sum() / df.dd.sum():,.0f} 元/笔。",
        f"获客成本最低的是 {cheapest.channel}（{cheapest.cac:,.0f} 元/笔），最高的是 {dearest.channel}（{dearest.cac:,.0f} 元/笔）。",
        f"审批通过率最高的是 {top_appr.channel}（{pct(top_appr.apv / top_appr.app)}）。",
        "API导流、老客复贷没有曝光/点击数据，相应列为 '-'。",
    ]
    span = _q(conn, "SELECT MIN(month) a, MAX(month) b FROM funnel").iloc[0]
    return AnalysisResult("acquisition_funnel", f"获客漏斗与获客成本（{span.a}~{span.b}，按渠道）", table, facts)


def acquisition_trend(conn) -> AnalysisResult:
    df = _q(conn, """
        SELECT month, SUM(drawdowns) dd, SUM(marketing_cost) cost,
               SUM(CASE WHEN channel = 'API导流' THEN drawdowns ELSE 0 END) api
        FROM funnel GROUP BY month ORDER BY month
    """)
    df["cac"] = df.cost / df.dd
    df["api_share"] = df.api / df.dd
    table = pd.DataFrame({
        "月份": df.month, "放款笔数": df.dd.map("{:,}".format), "营销费用(元)": df.cost.map(money),
        "获客成本(元/笔)": df.cac.map("{:,.0f}".format), "API导流占比": df.api_share.map(pct),
    })
    first, last = df.iloc[0], df.iloc[-1]
    facts = [
        f"月放款笔数从 {first.month} 的 {first.dd:,} 笔变为 {last.month} 的 {last.dd:,} 笔。",
        f"获客成本从 {first.cac:,.0f} 元/笔变为 {last.cac:,.0f} 元/笔。",
        f"API导流占比从 {pct(first.api_share)} 升至 {pct(last.api_share)}。",
    ]
    return AnalysisResult("acquisition_trend", "月度获客趋势", table, facts)


def channel_roi(conn) -> AnalysisResult:
    df = _q(conn, """
        WITH perf AS (
            SELECT l.channel, SUM(p.interest_paid) AS interest
            FROM loans l JOIN performance p USING (loan_id) GROUP BY l.channel
        ), lat AS (
            -- loans whose latest record is still M4+ (not yet written off)
            SELECT l.channel, SUM(p.balance) AS m4_bal
            FROM loans l JOIN performance p USING (loan_id)
            WHERE p.bucket = 'M4+' AND p.mob = (SELECT MAX(mob) FROM performance q WHERE q.loan_id = p.loan_id)
            GROUP BY l.channel
        ), wo AS (
            SELECT l.channel, SUM(p.written_off) AS written_off
            FROM loans l JOIN performance p USING (loan_id) GROUP BY l.channel
        ), amt AS (SELECT channel, COUNT(*) n, SUM(amount) amount FROM loans GROUP BY channel),
        cost AS (SELECT channel, SUM(marketing_cost) cost FROM funnel GROUP BY channel)
        SELECT amt.channel, n, amount, cost, interest, COALESCE(m4_bal, 0) + written_off AS loss
        FROM amt JOIN cost USING (channel) JOIN perf USING (channel) JOIN wo USING (channel)
        LEFT JOIN lat USING (channel)
    """)
    df["net"] = df.interest - df.loss - df.cost
    df["roi"] = df.net / df.cost
    table = pd.DataFrame({
        "渠道": df.channel, "放款笔数": df.n.map("{:,}".format), "放款金额(元)": df.amount.map(money),
        "营销费用(元)": df.cost.map(money), "已收利息(元)": df.interest.map(money),
        "信用损失(元)": df.loss.map(money), "净收益(元)": df.net.map(money),
        "营销ROI": df.roi.map(lambda x: f"{x:.2f}"),
    })
    best, worst = df.loc[df.roi.idxmax()], df.loc[df.roi.idxmin()]
    facts = [
        f"营销ROI最高的渠道是 {best.channel}（{best.roi:.2f}），最低的是 {worst.channel}（{worst.roi:.2f}）。",
        f"全部渠道合计净收益 {money(df.net.sum())} 元。",
    ] + [f"{r.channel}：信用损失占已收利息的 {pct(r.loss / r.interest)}。" for r in df.itertuples()]
    return AnalysisResult("channel_roi", "渠道营销ROI（已收利息 − 信用损失 − 营销费用）", table, facts,
                          "截至观察期末的已实现口径：信用损失 = 已核销金额 + 仍在M4+的余额；未含资金成本、运营成本与未来利息，近期放款的批次收益尚未充分体现。")


def reactivation_lift(conn) -> AnalysisResult:
    df = _q(conn, "SELECT * FROM reactivation_leads")
    treated, control = df[df.is_control == 0].copy(), df[df.is_control == 1]
    treated["decile"] = pd.qcut(treated.response_score.rank(method="first", ascending=False), 10,
                                labels=[f"D{i}" for i in range(1, 11)])
    base = treated.responded.mean()
    g = treated.groupby("decile", observed=False).agg(n=("responded", "size"), rr=("responded", "mean"),
                                                      amt=("drawdown_amount", "sum"))
    g["lift"] = g.rr / base
    g["cum_capture"] = treated.groupby("decile", observed=False).responded.sum().cumsum() / treated.responded.sum()
    g = g.reset_index()
    table = pd.DataFrame({
        "分组(按响应分从高到低)": g.decile.astype(str), "触达人数": g.n.map("{:,}".format),
        "响应率": g.rr.map(pct), "提升度Lift": g.lift.map("{:.2f}".format),
        "累计捕获响应占比": g.cum_capture.map(pct), "提款金额(元)": g.amt.map(money),
    })
    uplift = base - control.responded.mean()
    cost_per = treated.sms_cost.sum() / treated.responded.sum()
    top3 = treated[treated.decile.isin(["D1", "D2", "D3"])]
    facts = [
        f"实验组响应率 {pct(base)}，对照组（未发送短信）{pct(control.responded.mean())}，增量响应 {pp(uplift)}。",
        f"D1 组响应率 {pct(g.rr.iloc[0])}，Lift {g.lift.iloc[0]:.2f}；D10 组 {pct(g.rr.iloc[-1])}。",
        f"只触达前30%（D1~D3）可捕获 {pct(g.cum_capture.iloc[2])} 的响应，"
        f"短信量减少70%，该部分每个响应短信成本 {top3.sms_cost.sum() / top3.responded.sum():.2f} 元（全量为 {cost_per:.2f} 元）。",
    ]
    return AnalysisResult("reactivation_lift", "老客召回短信：响应模型分组效果（2026-04~06）", table, facts,
                          "对照组为随机保留10%不发送短信的客户，用于估计短信带来的增量。")


ANALYSES = {a.key: a for a in [
    Analysis("portfolio_overview", "risk", "资产组合概览", "近6个月在贷余额、30+/90+余额占比", portfolio_overview),
    Analysis("vintage", "risk", "Vintage账龄分析", "各放款月批次在MOB3/6/9/12的累计30+逾期率", vintage),
    Analysis("roll_rates", "risk", "逾期迁徙率", "近6个月C→M1、M1→M2、M2→M3、M3→M4+的余额迁徙率", roll_rates),
    Analysis("score_monitoring", "risk", "评分模型监控", "按放款月的KS、AUC、PSI，判断模型是否衰退或客群偏移", score_monitoring),
    Analysis("channel_risk", "risk", "分渠道风险", "各获客渠道上下半年的占比与MOB6坏率", channel_risk),
    Analysis("score_bands", "risk", "评分段风险与定价", "各评分段的坏率、占比和平均利率", score_bands),
    Analysis("acquisition_funnel", "marketing", "获客漏斗", "各渠道点击、注册、申请、审批、提款转化率和获客成本", acquisition_funnel),
    Analysis("acquisition_trend", "marketing", "月度获客趋势", "每月放款笔数、营销费用、获客成本、API导流占比", acquisition_trend),
    Analysis("channel_roi", "marketing", "渠道ROI", "各渠道已收利息、损失、营销费用和ROI，连接营销与风险", channel_roi),
    Analysis("reactivation_lift", "marketing", "老客召回响应模型", "短信召回的响应率、分组Lift、对照组增量", reactivation_lift),
]}

DEFAULT_PLANS = {
    "risk": ["portfolio_overview", "vintage", "roll_rates", "score_monitoring", "channel_risk"],
    "marketing": ["acquisition_funnel", "acquisition_trend", "channel_roi", "reactivation_lift"],
}

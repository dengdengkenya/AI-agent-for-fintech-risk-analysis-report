"""Policy analyses on the `applications` table: every applicant, approved or
rejected, with only the application-time fields both groups share (amount,
month, score, DTI, region, years employed). `bad` is known only for approved
applicants whose outcome window has passed.

- cutoff_simulation: what happens to approvals and the bad rate if the score
  cutoff or the DTI limit changes, with swap-in / swap-out groups. Rejected
  applicants have no repayment history, so the bad rate of anyone newly
  approved is a reject-inference estimate (估算) from accepted loans.
- approval_funnel: application volume and approval rate over time, by score
  band and by region.
- applicant_drift: PSI of recent applicants against a year earlier, and
  against the applicants actually approved.
"""

from dataclasses import dataclass
import re

import numpy as np
import pandas as pd

from .common import AnalysisResult, _q, money, pct, pp


@dataclass
class Scenario:
    score_cutoff: float | None = None
    max_dti: float | None = None

    def is_empty(self) -> bool:
        return self.score_cutoff is None and self.max_dti is None


def scenario_from_text(text: str) -> Scenario:
    """Fallback when the model returns no scenario: '分数线提高到620', 'DTI不超过40%'."""
    s = Scenario()
    m = re.search(r"(?:分数线|评分线|评分门槛|分数门槛|cutoff|评分|分数)\D{0,8}?(\d{3})", text, re.I)
    if m:
        s.score_cutoff = float(m.group(1))
    m = re.search(r"(?:DTI|负债收入比|负债率)\D{0,8}?(\d{1,2}(?:\.\d+)?)", text, re.I)
    if m:
        s.max_dti = float(m.group(1))
    return s


def clean_scenario(raw) -> Scenario:
    """Keep only plausible numbers from the model's JSON."""
    if not isinstance(raw, dict):
        return Scenario()

    def num(v, lo, hi):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return None
        return v if lo <= v <= hi else None

    return Scenario(num(raw.get("score_cutoff"), 300, 900), num(raw.get("max_dti"), 1, 100))


def _meta(conn) -> dict:
    try:
        return dict(conn.execute("SELECT key, value FROM _meta").fetchall())
    except Exception:
        return {}


def _apps(conn) -> pd.DataFrame:
    return _q(conn, "SELECT app_month, amount, score, dti, region, emp_years, approved, bad FROM applications")


def current_policy(conn, df: pd.DataFrame) -> tuple[float, float, bool]:
    """(score cutoff, max DTI, inferred?). Read from _meta, else inferred from approved loans."""
    meta = _meta(conn)
    if "policy_score_cutoff" in meta and "policy_max_dti" in meta:
        return float(meta["policy_score_cutoff"]), float(meta["policy_max_dti"]), False
    ok = df[df.approved == 1]
    cut = np.floor(ok.score.quantile(0.01) / 10) * 10
    dti = np.ceil(ok.dti.quantile(0.99) / 5) * 5
    return float(cut), float(dti), True


def _passes(df, cut, max_dti):
    ok = pd.Series(True, index=df.index)
    if cut is not None:
        ok &= df.score >= cut
    if max_dti is not None:
        ok &= df.dti <= max_dti
    return ok


def _outcome_window(df: pd.DataFrame, months: int = 12) -> list[str]:
    """Latest `months` application months where most approved outcomes are known."""
    ok = df[df.approved == 1]
    known = ok.groupby("app_month").bad.apply(lambda s: s.notna().mean())
    good = known[known >= 0.6].index.sort_values()
    if len(good) == 0:
        good = known.index.sort_values()
    return list(good[-months:])


def _fit_logit(X: np.ndarray, y: np.ndarray, iters: int = 25) -> np.ndarray:
    """Logistic regression by Newton/IRLS (a few features, no extra packages)."""
    X = np.column_stack([np.ones(len(X)), X])
    w = np.zeros(X.shape[1])
    for _ in range(iters):
        p = 1 / (1 + np.exp(-X @ w))
        h = X.T @ (X * (p * (1 - p))[:, None]) + 1e-6 * np.eye(X.shape[1])
        step = np.linalg.solve(h, X.T @ (y - p))
        w += step
        if np.max(np.abs(step)) < 1e-8:
            break
    return w


class BadRateModel:
    """Bad probability from score and DTI, fitted on approved loans with known outcomes.
    Used only to estimate (估算) the bad rate of applicants who were never approved."""

    def __init__(self, train: pd.DataFrame):
        self.dti_fill = float(train.dti.median())
        X = self._x(train, fit=True)
        self.w = _fit_logit(X, train.bad.to_numpy(float))

    def _x(self, df, fit=False):
        X = np.column_stack([df.score.to_numpy(float), df.dti.fillna(self.dti_fill).to_numpy(float)])
        if fit:
            self.mu, self.sd = X.mean(0), X.std(0) + 1e-9
        return (X - self.mu) / self.sd

    def predict(self, df: pd.DataFrame) -> np.ndarray:
        X = np.column_stack([np.ones(len(df)), self._x(df)])
        return 1 / (1 + np.exp(-X @ self.w))


def _num(x) -> str:
    return f"{x:g}"


def _rule_text(cut, max_dti) -> str:
    parts = []
    if cut is not None:
        parts.append(f"评分≥{_num(cut)}")
    if max_dti is not None:
        parts.append(f"DTI≤{_num(max_dti)}%")
    return "且".join(parts) or "无门槛"


def _simulate(win, model, old_cut, old_dti, new_cut, new_dti):
    """Split the window's applicants into keep / swap-out / swap-in / stay-rejected."""
    old_rule = _passes(win, old_cut, old_dti)
    new_rule = _passes(win, new_cut, new_dti)
    approved = win.approved == 1
    # Manual exceptions (approved although they failed the old rule) stay approved.
    swap_out = approved & old_rule & ~new_rule
    keep = approved & ~swap_out
    # A rejected applicant who already passed the old rule was turned down for
    # another reason (fraud check, documents), so the new rule does not change that.
    swap_in = ~approved & new_rule & ~old_rule
    groups = {"keep": win[keep], "swap_out": win[swap_out], "swap_in": win[swap_in],
              "stay_rejected": win[~approved & ~swap_in]}
    rates = {k: g.bad.mean() for k, g in groups.items() if k in ("keep", "swap_out")}
    rates["swap_in"] = float(model.predict(groups["swap_in"]).mean()) if len(groups["swap_in"]) else np.nan
    return groups, rates


def _new_bad_rate(groups, rates, swap_in_mult=1.0):
    k, s = len(groups["keep"]), len(groups["swap_in"])
    if k + s == 0:
        return np.nan
    bad_k = k * (rates["keep"] if not np.isnan(rates["keep"]) else 0)
    bad_s = s * rates["swap_in"] * swap_in_mult if s else 0
    return (bad_k + bad_s) / (k + s)


def cutoff_simulation(conn, scenario: Scenario | None = None) -> AnalysisResult:
    all_apps = _apps(conn)
    df = all_apps.dropna(subset=["score"])
    old_cut, old_dti, inferred = current_policy(conn, df)
    sc = scenario or Scenario()
    if sc.is_empty():
        sc = Scenario(old_cut + 20, None)
    new_cut = sc.score_cutoff if sc.score_cutoff is not None else old_cut
    new_dti = sc.max_dti if sc.max_dti is not None else old_dti

    months = _outcome_window(df)
    win = df[df.app_month.isin(months)]
    train = df[(df.approved == 1) & df.bad.notna()]
    model = BadRateModel(train)
    groups, rates = _simulate(win, model, old_cut, old_dti, new_cut, new_dti)

    n_app = len(win)
    approved = win[win.approved == 1]
    old_n, old_amt, old_bad = len(approved), approved.amount.sum(), approved.bad.mean()
    new_n = len(groups["keep"]) + len(groups["swap_in"])
    new_amt = groups["keep"].amount.sum() + groups["swap_in"].amount.sum()
    new_bad = _new_bad_rate(groups, rates)
    stress_bad = _new_bad_rate(groups, rates, 1.3)
    has_est = len(groups["swap_in"]) > 0
    est = "（估算）" if has_est else ""

    table = pd.DataFrame({
        "指标": ["申请笔数", "通过笔数", "通过率", "通过金额(元)", "通过客户坏账率"],
        "当前策略": [f"{n_app:,}", f"{old_n:,}", pct(old_n / n_app), money(old_amt), pct(old_bad)],
        "新策略": [f"{n_app:,}", f"{new_n:,}", pct(new_n / n_app), money(new_amt), pct(new_bad) + est],
        "变化": ["-", f"{new_n - old_n:+,}", pp(new_n / n_app - old_n / n_app),
                 money(new_amt - old_amt), pp(new_bad - old_bad) + est],
    })

    label = {"keep": "保持通过", "swap_out": "换出（原通过→新拒绝）", "swap_in": "换入（原拒绝→新通过）",
             "stay_rejected": "保持拒绝"}
    source = {"keep": "实际表现", "swap_out": "实际表现", "swap_in": "估算（已通过客户外推）", "stay_rejected": "-"}
    swap = pd.DataFrame({
        "客群": [label[k] for k in groups],
        "笔数": [f"{len(g):,}" for g in groups.values()],
        "占申请": [pct(len(g) / n_app) for g in groups.values()],
        "平均评分": [f"{g.score.mean():.0f}" if len(g) else "-" for g in groups.values()],
        "坏账率": [pct(rates[k]) + ("（估算）" if k == "swap_in" and len(g) else "") if k in rates else "-"
                for k, g in groups.items()],
        "坏账率来源": [source[k] for k in groups],
    })

    rows = []
    for cut in np.arange(old_cut - 40, old_cut + 41, 10):
        g, r = _simulate(win, model, old_cut, old_dti, cut, new_dti)
        n = len(g["keep"]) + len(g["swap_in"])
        rows.append({"评分线": f"{_num(cut)}" + ("（当前评分线）" if cut == old_cut else ""),
                     "通过率": pct(n / n_app), "通过笔数": f"{n:,}",
                     "坏账率": pct(_new_bad_rate(g, r)) + ("（估算）" if len(g["swap_in"]) else "")})
    curve = pd.DataFrame(rows)

    facts = [
        f"当前策略：{_rule_text(old_cut, old_dti)}；新策略：{_rule_text(new_cut, new_dti)}。",
        f"通过率从 {pct(old_n / n_app)} 变为 {pct(new_n / n_app)}（{pp(new_n / n_app - old_n / n_app)}），"
        f"通过笔数{'增加' if new_n >= old_n else '减少'} {abs(new_n - old_n):,} 笔。",
        f"通过客户坏账率从 {pct(old_bad)} 变为 {pct(new_bad)}{est}（{pp(new_bad - old_bad)}）。",
    ]
    so, si = groups["swap_out"], groups["swap_in"]
    if len(so):
        ratio = rates["swap_out"] / rates["keep"] if rates["keep"] else np.nan
        facts.append(f"换出 {len(so):,} 笔，实际坏账率 {pct(rates['swap_out'])}，"
                     f"是保持通过客户（{pct(rates['keep'])}）的 {ratio:.1f} 倍。")
    if len(si):
        facts.append(f"换入 {len(si):,} 笔，坏账率估算为 {pct(rates['swap_in'])}（估算：用已通过客户的评分和DTI外推，"
                     f"拒绝客户没有还款表现）。")
        facts.append(f"压力情景（假设换入客户实际坏账率比估算高30%）：新策略通过客户坏账率 {pct(stress_bad)}（估算）。")
        below = train[train.score < old_cut]
        if len(below) >= 30:
            facts.append(f"历史上低于当前评分线、经例外审批通过的客户共 {len(below):,} 笔，实际坏账率 {pct(below.bad.mean())}，"
                         f"模型对这批客户的估算为 {pct(model.predict(below).mean())}，可用于检验外推是否偏乐观。")
    win_all = all_apps[all_apps.app_month.isin(months)]
    missing = 1 - len(win) / len(win_all)
    if missing > 0.001:
        facts.append(f"样本期内 {pct(missing)} 的申请没有评分，未参与模拟，因此上表通过率高于全部申请口径。")
    if inferred:
        facts.append(f"数据中没有记录当前策略，评分线 {_num(old_cut)} 和 DTI 上限 {_num(old_dti)}% 由已通过客户的分布推断"
                     f"（评分1%分位、DTI 99%分位）。")
    meta = _meta(conn)
    note = (f"样本：{months[0]}~{months[-1]} 的申请（表现期已满）。坏账定义：{meta.get('outcome_definition', '曾逾期30+')}。"
            f"原规则外的例外审批客户在新策略下仍视为通过；原拒绝客户中已满足原规则的视为因其他原因（反欺诈、资料不全等）被拒，不随评分线变化换入。"
            f"换入客户坏账率为估算：逻辑回归（评分+DTI）在已通过客户上拟合后外推，拒绝推断存在偏差，仅供参考。")
    title = f"授信策略模拟：{_rule_text(old_cut, old_dti)} → {_rule_text(new_cut, new_dti)}"
    return AnalysisResult("cutoff_simulation", title, table, facts, note,
                          extra_tables=[("换入换出分析", swap), ("评分线敏感性（DTI规则按新策略）", curve)])


def _periods(df: pd.DataFrame) -> tuple[pd.Series, str]:
    months = df.app_month.sort_values().unique()
    if len(months) > 36:
        return df.app_month.str[:4], "年份"
    return df.app_month, "月份"


def _score_bands(cut):
    edges = [-np.inf, cut - 40, cut, cut + 40, cut + 80, np.inf]
    labels = [f"<{_num(cut - 40)}", f"{_num(cut - 40)}-{_num(cut - 1)}", f"{_num(cut)}-{_num(cut + 39)}",
              f"{_num(cut + 40)}-{_num(cut + 79)}", f"{_num(cut + 80)}+"]
    return edges, labels


def approval_funnel(conn) -> AnalysisResult:
    df = _apps(conn)
    cut, _, _ = current_policy(conn, df.dropna(subset=["score"]))
    period, period_name = _periods(df)
    g = df.groupby(period).agg(n=("approved", "size"), ok=("approved", "sum"),
                               score=("score", "mean"), dti=("dti", "median")).tail(12)
    g["rate"] = g.ok / g.n
    table = pd.DataFrame({
        period_name: g.index, "申请笔数": g.n.map("{:,}".format), "通过笔数": g.ok.map("{:,}".format),
        "通过率": g.rate.map(pct), "申请人平均评分": g.score.map("{:.0f}".format),
        "申请人DTI中位数": g.dti.map("{:.1f}%".format),
    })

    recent = df[df.app_month.isin(sorted(df.app_month.unique())[-12:])]
    edges, labels = _score_bands(cut)
    band = pd.cut(recent.score, edges, right=False, labels=labels)
    b = recent.groupby(band, observed=False).agg(n=("approved", "size"), rate=("approved", "mean"))
    b_missing = recent.score.isna().mean()
    bands = pd.DataFrame({"评分段": [str(x) for x in b.index], "申请笔数": b.n.map("{:,}".format),
                          "占申请": (b.n / len(recent)).map(pct), "通过率": b.rate.map(pct)})

    r = recent.groupby("region").agg(n=("approved", "size"), rate=("approved", "mean"),
                                     score=("score", "mean")).sort_values("n", ascending=False).head(8)
    known = df[(df.approved == 1) & df.bad.notna()].groupby("region").bad.mean()
    regions = pd.DataFrame({"地区": r.index, "申请笔数": r.n.map("{:,}".format),
                            "占申请": (r.n / len(recent)).map(pct), "通过率": r.rate.map(pct),
                            "申请人平均评分": r.score.map("{:.0f}".format),
                            "通过客户坏账率(历史)": known.reindex(r.index).map(pct).to_numpy()})

    first, last = g.iloc[0], g.iloc[-1]
    lo, hi = r.rate.idxmin(), r.rate.idxmax()
    facts = [
        f"{g.index[0]} 申请 {int(first.n):,} 笔、通过率 {pct(first.rate)}；"
        f"{g.index[-1]} 申请 {int(last.n):,} 笔、通过率 {pct(last.rate)}（{pp(last.rate - first.rate)}）。",
        f"申请人平均评分从 {first.score:.0f} 变为 {last.score:.0f}，DTI中位数从 {first.dti:.1f}% 变为 {last.dti:.1f}%。",
        f"近12个月申请量前8的地区中，通过率最低的是 {lo}（{pct(r.rate[lo])}），最高的是 {hi}（{pct(r.rate[hi])}）。",
        f"近12个月 {labels[1]} 分段（当前评分线下方40分内）有 {b.n.iloc[1]:,} 笔申请，占 {pct(b.n.iloc[1] / len(recent))}，"
        f"是放宽评分线时最先换入的客群。",
    ]
    if b_missing > 0.001:
        facts.append(f"近12个月申请中评分缺失 {pct(b_missing)}，未计入评分段。")
    span = f"{df.app_month.min()}~{df.app_month.max()}"
    return AnalysisResult("approval_funnel", f"申请与审批漏斗（{span}）", table, facts,
                          f"含全部申请（通过+拒绝），只用申请时字段。评分段与地区表为最近12个月；当前评分线 {_num(cut)}。",
                          extra_tables=[("近12个月分评分段通过率", bands), ("近12个月主要地区", regions)])


def _psi_nan(expected: pd.Series, actual: pd.Series, edges) -> float:
    """PSI with missing values as their own bin."""
    def dist(s):
        h = np.histogram(s.dropna(), edges)[0]
        return np.append(h, s.isna().sum()) / max(len(s), 1)
    e, a = np.clip(dist(expected), 1e-6, None), np.clip(dist(actual), 1e-6, None)
    return float(np.sum((a - e) * np.log(a / e)))


def _edges(s: pd.Series):
    e = np.unique(np.quantile(s.dropna(), np.linspace(0, 1, 11)))
    e[0], e[-1] = -np.inf, np.inf
    return e


def applicant_drift(conn) -> AnalysisResult:
    df = _apps(conn)
    months = sorted(df.app_month.unique())
    cur_m = months[-3:]
    i = months.index(cur_m[0])
    base_m = months[i - 12:i - 9] if i >= 12 else months[:3]
    cur, base = df[df.app_month.isin(cur_m)], df[df.app_month.isin(base_m)]
    cur_ok = cur[cur.approved == 1]

    variables = {"score": "评分", "dti": "DTI(%)", "amount": "申请金额(元)", "emp_years": "工作年限(年)"}
    rows = []
    for col, name in variables.items():
        edges = _edges(base[col])
        rows.append({"变量": name, "基期中位数": base[col].median(), "本期中位数": cur[col].median(),
                     "基期缺失率": base[col].isna().mean(), "本期缺失率": cur[col].isna().mean(),
                     "PSI(本期申请vs基期申请)": _psi_nan(base[col], cur[col], edges),
                     "PSI(本期申请vs本期通过)": _psi_nan(cur_ok[col], cur[col], _edges(cur_ok[col]))})
    raw = pd.DataFrame(rows)
    f1 = lambda x: f"{x:,.1f}"
    f3 = lambda x: f"{x:.3f}"
    table = pd.DataFrame({"变量": raw.变量, "基期中位数": raw.基期中位数.map(f1), "本期中位数": raw.本期中位数.map(f1),
                          "基期缺失率": raw.基期缺失率.map(pct), "本期缺失率": raw.本期缺失率.map(pct),
                          "PSI(本期申请vs基期申请)": raw["PSI(本期申请vs基期申请)"].map(f3),
                          "PSI(本期申请vs本期通过)": raw["PSI(本期申请vs本期通过)"].map(f3)})

    edges = _edges(base.score)
    trend_rows = []
    for m in months[-12:]:
        g = df[df.app_month == m]
        trend_rows.append({"月份": m, "申请笔数": f"{len(g):,}", "评分PSI(vs基期)": f3(_psi_nan(base.score, g.score, edges)),
                           "DTI PSI(vs基期)": f3(_psi_nan(base.dti, g.dti, _edges(base.dti))),
                           "通过率": pct(g.approved.mean())})
    trend = pd.DataFrame(trend_rows)

    top = raw.loc[raw["PSI(本期申请vs基期申请)"].idxmax()]
    facts = [
        f"本期为 {cur_m[0]}~{cur_m[-1]} 的申请人，基期为 {base_m[0]}~{base_m[-1]}。",
        f"申请人分布变化最大的变量是 {top.变量}：PSI {top['PSI(本期申请vs基期申请)']:.3f}，"
        f"中位数从 {top.基期中位数:,.1f} 变为 {top.本期中位数:,.1f}，缺失率从 {pct(top.基期缺失率)} 变为 {pct(top.本期缺失率)}。",
    ] + [f"{r.变量} PSI(本期申请vs基期申请) {r['PSI(本期申请vs基期申请)']:.3f}。" for _, r in raw.iterrows() if r.变量 != top.变量] + [
        f"本期通过率 {pct(cur.approved.mean())}，基期 {pct(base.approved.mean())}（{pp(cur.approved.mean() - base.approved.mean())}）。",
        "行业常用阈值：PSI<0.10 稳定，0.10~0.25 需关注，>0.25 显著偏移。"
        "申请人与通过客户之间的PSI反映审批策略筛掉了哪些人，本身较高属正常，重点看其变化。",
    ]
    return AnalysisResult("applicant_drift", "申请客群偏移（PSI）", table, facts,
                          "中位数不含缺失值；PSI 以基期申请人十等分为分箱，缺失值单独一箱；基期为本期前推12个月的同期3个月。",
                          extra_tables=[("近12个月逐月偏移", trend)])

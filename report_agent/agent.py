"""The report agent: plan -> compute -> write -> check.

1. Plan: the model reads the user's request and picks analyses from the
   registry (JSON output). Invalid picks fall back to the team's defaults.
2. Compute: each analysis runs SQL/pandas locally. No model involved.
3. Write: the model gets the small result tables plus pre-computed facts and
   writes the Chinese narrative. It is told not to calculate anything.
4. Check: every number in the narrative is looked up in the computed
   results; numbers that cannot be found are listed for a human to review.
"""

from dataclasses import dataclass, field
from datetime import datetime
import json
import re
import sqlite3
import time

from .analyses import ANALYSES, DEFAULT_PLANS, AnalysisResult, available
from .config import Settings
from .llm import LLMReply, OllamaClient
from .policy import Scenario, clean_scenario, scenario_from_text


TEAM_NAMES = {"risk": "风险模型团队", "policy": "风险策略团队", "marketing": "营销模型团队"}

PLAN_SYSTEM = """你是小额信贷公司数据分析部门的报告助手。根据用户需求，从可用分析清单中选择要运行的分析。
只能使用清单中的 key。如果用户提到新的评分线（分数门槛）或DTI上限，填入 scenario，否则填 null。只输出 JSON，格式：
{"title": "报告标题", "analyses": ["key1", "key2"], "focus": "一句话说明报告重点",
 "scenario": {"score_cutoff": 数字或null, "max_dti": 数字或null}}"""

WRITE_SYSTEM = """你是小额信贷公司的资深风控/营销分析师，为内部团队撰写月度分析报告。
规则：
1. 只使用下方“分析结果”中出现的数字，原样引用，不要自己计算、换算或估计新的数字。
2. 如果某个结论需要的数字不在结果中，用定性描述，不要编造。
3. 用简体中文，Markdown 格式，语气专业、简洁。
4. 结果中标注“估算”的数字，正文引用时也必须写明“估算”。
5. 结构固定为以下四节（二级标题）：
## 一、核心结论
（3~5条要点，每条一句话，先说结论）
## 二、详细分析
（按分析主题分小节，用 ### 三级标题，每节2~4句话解读趋势和原因）
## 三、风险提示与关注点
## 四、行动建议
（3~5条，具体、可执行，说明建议由哪个团队负责）
不要输出表格，表格会自动附在报告后面。"""


@dataclass
class StepTiming:
    name: str
    seconds: float
    detail: str = ""


@dataclass
class Report:
    title: str
    team: str
    request: str
    markdown: str
    timings: list[StepTiming] = field(default_factory=list)
    unverified_numbers: list[str] = field(default_factory=list)


def _md_table(df) -> str:
    cols = [str(c) for c in df.columns]
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for row in df.itertuples(index=False):
        lines.append("| " + " | ".join(str(v) for v in row) + " |")
    return "\n".join(lines)


def _result_block(r: AnalysisResult) -> str:
    parts = [f"### {r.title}", _md_table(r.table)]
    for name, df in r.extra_tables:
        parts += [f"{name}：", _md_table(df)]
    if r.note:
        parts.append(f"口径：{r.note}")
    parts.append("关键事实：\n" + "\n".join(f"- {f}" for f in r.facts))
    return "\n\n".join(parts)


NUMBER = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")


def _numbers(text: str) -> set[str]:
    return {m.replace(",", "").lstrip("+-") for m in NUMBER.findall(text)}


def check_numbers(narrative: str, results: list[AnalysisResult]) -> list[str]:
    """Numbers in the narrative that do not appear anywhere in the results."""
    source = "\n".join(_result_block(r) for r in results)
    known = _numbers(source)
    unknown = []
    for n in sorted(_numbers(narrative)):
        if n in known:
            continue
        try:
            if float(n) <= 12 and float(n).is_integer():
                continue  # counts like "3个批次", "MOB6", section numbers
        except ValueError:
            pass
        unknown.append(n)
    return unknown


class ReportAgent:
    def __init__(self, settings: Settings, use_llm: bool = True):
        self.s = settings
        self.llm = OllamaClient(settings) if use_llm else None

    def plan(self, team: str, request: str, have: set[str]) -> tuple[dict, LLMReply | None]:
        defaults = [k for k in DEFAULT_PLANS[team] if k in have] or [k for k in DEFAULT_PLANS["policy"] if k in have]
        default = {"title": f"{TEAM_NAMES[team]}月度分析报告", "analyses": defaults,
                   "focus": "常规月度监控", "scenario": scenario_from_text(request)}
        if not self.llm or not request:
            return default, None
        menu = "\n".join(f"- {a.key}：{a.title}，{a.description}" for k, a in ANALYSES.items() if k in have)
        user = (f"读者：{TEAM_NAMES[team]}\n用户需求：{request}\n\n可用分析清单：\n{menu}\n\n"
                f"若需求不明确，优先选择以下默认组合：{', '.join(defaults)}")
        reply = self.llm.chat(PLAN_SYSTEM, user, json_mode=True, temperature=0, num_predict=300)
        try:
            plan = json.loads(reply.text)
            keys = [k for k in plan.get("analyses", []) if k in have]
            keys = list(dict.fromkeys(keys))  # drop duplicates, keep order
            scenario = clean_scenario(plan.get("scenario"))
        except (json.JSONDecodeError, AttributeError):
            keys, scenario = [], Scenario()
        if scenario.is_empty():
            scenario = default["scenario"]  # small models often miss it; fall back to a regex
        if not keys:
            return {**default, "scenario": scenario}, reply
        return {"title": str(plan.get("title") or default["title"]), "analyses": keys,
                "focus": str(plan.get("focus") or ""), "scenario": scenario}, reply

    def run(self, team: str, request: str = "", analyses: list[str] | None = None,
            scenario: Scenario | None = None, log=print) -> Report:
        timings: list[StepTiming] = []
        total_start = time.perf_counter()
        with sqlite3.connect(self.s.db_path) as conn:
            have = available(conn)
            meta = dict(conn.execute("SELECT key, value FROM _meta").fetchall())

        if analyses:
            missing = [k for k in analyses if k not in have]
            if missing:
                log(f"[plan] skipped, tables not in this database: {', '.join(missing)}")
            plan = {"title": f"{TEAM_NAMES[team]}专项分析报告", "analyses": [k for k in analyses if k in have],
                    "focus": request, "scenario": scenario_from_text(request)}
            log(f"[plan] fixed by --analyses: {', '.join(plan['analyses'])}")
        else:
            t = time.perf_counter()
            plan, reply = self.plan(team, request, have)
            timings.append(StepTiming("计划(LLM选择分析)", time.perf_counter() - t,
                                      reply.summary() if reply else "未调用模型，使用默认组合"))
            log(f"[plan] {timings[-1].seconds:.1f}s  -> {', '.join(plan['analyses'])}")
        if scenario and not scenario.is_empty():  # command-line flags win over the request text
            plan["scenario"] = Scenario(scenario.score_cutoff if scenario.score_cutoff is not None
                                        else plan["scenario"].score_cutoff,
                                        scenario.max_dti if scenario.max_dti is not None else plan["scenario"].max_dti)
        if "cutoff_simulation" in plan["analyses"]:
            sc = plan["scenario"]
            log(f"[plan] scenario: score_cutoff={sc.score_cutoff}, max_dti={sc.max_dti} "
                f"(empty = current cutoff + 20)")

        results = []
        with sqlite3.connect(self.s.db_path) as conn:
            for key in plan["analyses"]:
                t = time.perf_counter()
                a = ANALYSES[key]
                results.append(a.run(conn, plan["scenario"]) if a.takes_scenario else a.run(conn))
                timings.append(StepTiming(f"计算: {key}", time.perf_counter() - t, "SQL/pandas"))
                log(f"[compute] {key}: {timings[-1].seconds:.2f}s")

        evidence = "\n\n".join(_result_block(r) for r in results)
        if self.llm:
            user = (f"报告标题：{plan['title']}\n读者：{TEAM_NAMES[team]}\n"
                    f"用户需求：{request or '常规月度报告'}\n报告重点：{plan['focus']}\n\n"
                    f"分析结果：\n\n{evidence}")
            log(f"[write] calling {self.s.ollama_model} at {self.s.ollama_host} "
                f"(prompt ~{len(user)} chars; on a CPU laptop this can take several minutes)...")
            t = time.perf_counter()
            reply = self.llm.chat(WRITE_SYSTEM, user)
            narrative = reply.text
            timings.append(StepTiming("撰写(LLM生成正文)", time.perf_counter() - t, reply.summary()))
            log(f"[write] {reply.summary()}")
        else:
            narrative = "## 关键事实（未调用模型，仅列出计算结果）\n\n" + "\n\n".join(
                f"### {r.title}\n" + "\n".join(f"- {f}" for f in r.facts) for r in results)

        unverified = check_numbers(narrative, results) if self.llm else []
        total = time.perf_counter() - total_start
        timings.append(StepTiming("合计", total))
        markdown = self._render(plan, team, request, narrative, results, timings, unverified, meta)
        return Report(plan["title"], team, request, markdown, timings, unverified)

    def _render(self, plan, team, request, narrative, results, timings, unverified, meta) -> str:
        model = f"{self.s.ollama_model} @ {self.s.ollama_host}" if self.llm else "未使用（--no-llm）"
        if meta.get("source") == "lendingclub":
            frac = float(meta.get("sample_frac", "1"))
            banner = ("> ⚠️ **演示数据**：本报告基于 Lending Club 公开数据（美国，2007~2018），作为国内小贷数据的替代演示；"
                      "评分为 FICO/Risk_Score，坏账为核销或违约。"
                      + (f"按 {frac:.0%} 随机抽样，笔数为抽样后数量。" if frac < 1 else ""))
        else:
            banner = "> ⚠️ **演示数据**：本报告基于随机生成的合成数据（SYNTHETIC DATA），不代表任何真实客户或机构。"
        lines = [
            f"# {plan['title']}",
            "",
            banner,
            "",
            f"- 读者：{TEAM_NAMES[team]}",
            f"- 需求：{request or '常规月度报告'}",
            f"- 生成时间：{datetime.now():%Y-%m-%d %H:%M}",
            f"- 本地模型：{model}（数据未离开本机/本地服务器）",
            "- 说明：所有数字由 SQL/pandas 计算，模型只负责选择分析和撰写文字。",
            "",
            narrative,
            "",
            "---",
            "",
            "## 附录A：计算结果",
            "",
        ]
        for r in results:
            lines += [f"### {r.title}", "", _md_table(r.table), ""]
            for name, df in r.extra_tables:
                lines += [f"**{name}**", "", _md_table(df), ""]
            if r.note:
                lines += [f"*口径：{r.note}*", ""]
        lines += ["## 附录B：数字核对", ""]
        if not self.llm:
            lines.append("未调用模型，正文均为计算结果，无需核对。")
        elif unverified:
            lines.append("以下数字出现在正文中，但在计算结果里找不到，可能是模型自行计算或编造，请人工核对：")
            lines.append("")
            lines.append("、".join(f"`{n}`" for n in unverified))
        else:
            lines.append("正文中的数字均能在计算结果中找到。")
        lines += ["", "## 附录C：耗时", "", "| 步骤 | 秒 | 说明 |", "|---|---|---|"]
        lines += [f"| {t.name} | {t.seconds:.2f} | {t.detail} |" for t in timings]
        return "\n".join(lines) + "\n"

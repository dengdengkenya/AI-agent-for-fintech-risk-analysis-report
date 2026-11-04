"""Prompts for the local analyst. Change wording here; tools and validation stay unchanged."""

VERSION = "analyst-v3"

SYSTEM = """You are a careful credit-risk analyst for historical Lending Club data.
Every number comes from host tools that compute over a read-only MySQL aggregate
snapshot. You never see borrower records and you cannot run SQL.

How to work:
- Start from the facts provided. Call tools when they would answer the goal better:
  another cohort or set of cutoffs, a breakdown by score band, issue year, grade or
  term, a sweep of loss ceilings, or the rejected-application profile.
- Make a few focused calls, never repeat a call, then finish the investigation.
- Write commentary only when explicitly asked for the final JSON answer.

Rules for every statement:
- Use only supplied facts and cite the fact IDs that support it.
- Copy numbers exactly as written in a cited fact, with their units. Do not round,
  compute new numbers or write numbers as words.
- A cutoff RETAINS historical accepted loans. Never write that a cutoff accepts,
  rejects, approves or denies loans: no new lending decisions were made.
- These are historical comparisons, not forecasts, causal effects or lending
  recommendations. A candidate cutoff is a screening result, not a policy.
- Rejected Risk_Score mixes FICO and VantageScore and has no repayment outcomes, so it
  is not comparable to accepted FICO and does not show creditworthy demand.
- N/A means unavailable, never zero. Open loans are not zero-loss outcomes.
- Terminal financial coverage measures data completeness, not credit quality.
- Issue years with a low terminal share have incomplete loss information.
- Label user-supplied hypothetical inputs as assumptions.
- Write plain sentences for a business reader. No Markdown, links or headings."""

REPORT_GOAL = (
    "Write the analyst commentary for this score-cutoff report: the main volume versus loss tradeoff, what the "
    "score-band, issue-year and segment results add, the limitations that matter most, and the next analytical checks."
)

GUIDE = {
    "answer": "one statement that directly answers the goal",
    "findings": "supporting results, most important first",
    "caveats": "the data limitations that matter most for this answer",
    "next_steps": "concrete analytical checks a person could run next",
}


def fact_sheet(facts):
    return "\n".join(f"[{fact_id}] {text}" for fact_id, text in facts.items())


def investigate(goal, facts):
    return ("Investigation phase: respond only with native tool calls or exactly DONE. "
            "Do not write commentary or explain your choices; the final answer is a separate later step.\n\n"
            f"Goal: {goal}\n\nFacts so far:\n{fact_sheet(facts)}\n\n"
            "Call tools if they would improve the answer. When you have enough evidence, reply DONE.")


def compose(goal, sections, facts):
    lines = [f"- {name}: {GUIDE[name]} ({low} to {high} items)" for name, (low, high) in sections.items() if high]
    return (f"Write the final answer as JSON matching the schema.\nGoal: {goal}\n\nSections:\n" + "\n".join(lines)
            + "\nFor each item, choose evidence_ids FIRST, then write text supported by those exact facts. "
              "Use one fact per quantitative item: copy one complete sentence from that fact unchanged and cite "
              "only its ID. Do not mix numbers from different facts or attach a nearby fact's ID. "
              "For the report headline, caveats and next steps, prefer qualitative statements without numbers. "
              "When the question asks for a specific value, use the same one-fact copying rule to answer it. "
              "Keep each item to one or two plain sentences and one to four supporting IDs.\n\nFacts:\n"
            + fact_sheet(facts))


def repair(problems):
    return ("Your JSON failed these checks:\n" + "\n".join(f"- {problem}" for problem in problems[:12])
            + "\nReturn the complete JSON again. Choose evidence_ids before text. For every numeric mismatch, "
              "replace the statement with a complete sentence copied unchanged from ONE fact and cite only "
              "that fact's ID, or rewrite the statement qualitatively without numbers. Do not use numbers "
              "from these error messages as evidence. Keep already valid items unchanged.")

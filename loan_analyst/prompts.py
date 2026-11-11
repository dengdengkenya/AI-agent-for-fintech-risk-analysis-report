"""Prompts for the local analyst. Change wording here; tools and validation stay unchanged.

Each role gets a short system prompt and one task message:

    PLANNER  plan(), plan_results()     choose tool calls (schema tool mode)
    SYSTEM   investigate()              choose tool calls (native tool mode)
    SYSTEM   compose(), repair()        write cited statements
    CRITIC   critique()                 review statements against their cited facts
    SYSTEM   edit()                     assemble the report answer

Bump VERSION whenever any wording changes: it is saved with every run and is
part of what makes an interrupted run resumable.
"""

import json

VERSION = "analyst-v4"

RULES = """Rules for every statement:
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

SYSTEM = """You are a careful credit-risk analyst for historical Lending Club data.
Every number comes from host tools that compute over a read-only MySQL aggregate
snapshot. You never see borrower records and you cannot run SQL.

How to work:
- Start from the facts provided. Call tools when they would answer the goal better:
  another cohort or set of cutoffs, a breakdown by score band, issue year, grade or
  term, a sweep of loss ceilings, vintage trend, loss concentration, origination mix,
  or the rejected-application profile.
- Make a few focused calls, never repeat a call, then finish the investigation.
- Write commentary only when explicitly asked for the final JSON answer.

""" + RULES

PLANNER = """You plan the analysis for a careful credit-risk analyst working on historical
Lending Club data. Host tools compute every number over a read-only aggregate snapshot.
You cannot run SQL and you never see borrower records.

Your only job is to decide which tool calls, if any, would add evidence for the goal.
- Reply with JSON only: {"calls": [...]}.
- Request a call only when the facts so far do not already answer the goal.
- Prefer one or two focused calls. Never repeat a call that already ran.
- Omit an argument to keep the session setting. Rates and ceilings are fractions (0.08 means 8%).
- Return {"calls": []} when the facts are enough. Do not write commentary."""

CRITIC = """You review statements written by a credit-risk analyst. For each statement you
see the statement and the only facts it cites. Judge whether the statement says no more
than those facts support.

Verdicts:
- supported: every claim follows from the cited facts, including the direction of any
  comparison (higher, lower, more, fewer) and what each number refers to.
- not_supported: a claim, number, comparison or direction is not in the cited facts, or
  contradicts them.
- overreach: the statement predicts the future, claims a cause, recommends a lending
  decision, treats rejected applicants as creditworthy, or treats N/A or open loans as zero.

A statement may be shorter than its facts. Careful limiting language such as
"historical", "not a forecast" or "does not prove" is supported when the facts say so.
Reply with JSON only: one verdict per statement ID and a reason of a few words."""

REPORT_GOAL = (
    "Write the analyst commentary for this credit-risk report: the main volume versus loss tradeoff, what the "
    "score-band, vintage, concentration and segment results add, the limitations that matter most, and the next "
    "analytical checks."
)

GUIDE = {
    "answer": "one statement that directly answers the goal",
    "findings": "supporting results, most important first",
    "caveats": "the data limitations that matter most for this answer",
    "next_steps": "concrete analytical checks a person could run next",
}


def fact_sheet(facts):
    return "\n".join(f"[{fact_id}] {text}" for fact_id, text in facts.items())


# ---------------------------------------------------------------------------
# Choosing tools
# ---------------------------------------------------------------------------

def investigate(goal, facts):
    return ("Investigation phase: respond only with native tool calls or exactly DONE. "
            "Do not write commentary or explain your choices; the final answer is a separate later step.\n\n"
            f"Goal: {goal}\n\nFacts so far:\n{fact_sheet(facts)}\n\n"
            "Call tools if they would improve the answer. When you have enough evidence, reply DONE.")


def tool_catalog(definitions):
    """Tool names, descriptions and arguments as text, for the schema tool mode."""
    lines = []
    for definition in definitions:
        function = definition["function"]
        arguments = {}
        for name, schema in function["parameters"]["properties"].items():
            summary = schema.get("description", "")
            if "enum" in schema:
                summary = f"one of {', '.join(schema['enum'])}. {summary}".strip()
            arguments[name] = summary
        required = function["parameters"].get("required", [])
        lines.append(f"- {function['name']}: {function['description']}\n"
                     f"  required: {', '.join(required) or 'none'}\n"
                     f"  arguments: {json.dumps(arguments, ensure_ascii=False)}")
    return "\n".join(lines)


def plan(goal, facts, definitions, topics=()):
    parts = [f"Goal: {goal}"]
    if topics:
        listed = "\n".join(f"- {topic.name}: {topic.goal} (tools: {', '.join(topic.tools)})" for topic in topics)
        parts.append("The report is written topic by topic. Give each call the topic it supports:\n" + listed)
    parts += [
        "Tools:\n" + tool_catalog(definitions),
        "Facts so far:\n" + fact_sheet(facts),
        'Return {"calls": [...]} with the tool calls that would add evidence, or {"calls": []} if none are needed.',
    ]
    return "\n\n".join(parts)


def plan_results(results):
    return ("Results of your calls:\n\n" + "\n\n".join(results)
            + '\n\nRequest further calls only if something important is still missing; '
              'otherwise return {"calls": []}.')


# ---------------------------------------------------------------------------
# Writing and repairing statements
# ---------------------------------------------------------------------------

def compose(goal, sections, facts):
    lines = [f"- {name}: {GUIDE[name]} ({low} to {high} items)" for name, (low, high) in sections.items() if high]
    return (f"Write the final answer as JSON matching the schema.\nGoal: {goal}\n\nSections:\n" + "\n".join(lines)
            + "\nFor each item, choose evidence_ids FIRST, then write text supported by those exact facts. "
              "Use one fact per quantitative item: copy its numbers exactly as written, with their units, and "
              "cite only its ID. Do not mix numbers from different facts or attach a nearby fact's ID. "
              "When unsure, copy one complete sentence from the fact unchanged. "
              "For a headline, caveats and next steps, prefer qualitative statements without numbers. "
              "When the question asks for a specific value, use the same one-fact rule to answer it. "
              "Keep each item to one or two plain sentences and one to four supporting IDs.\n\nFacts:\n"
            + fact_sheet(facts))


def repair(problems):
    return ("Your JSON failed these checks:\n" + "\n".join(f"- {problem}" for problem in problems[:12])
            + "\nReturn the complete JSON again. Choose evidence_ids before text. For every numeric mismatch or "
              "critic objection, replace the statement with a complete sentence copied unchanged from ONE fact "
              "and cite only that fact's ID, or rewrite the statement qualitatively without numbers. Do not use "
              "numbers from these error messages as evidence. Keep already valid items unchanged.")


# ---------------------------------------------------------------------------
# Reviewing and assembling
# ---------------------------------------------------------------------------

def critique(statements):
    """``statements`` is a list of (statement ID, text, {fact ID: fact sentence})."""
    blocks = []
    for statement_id, text, facts in statements:
        cited = "\n".join(f"    [{fact_id}] {sentence}" for fact_id, sentence in facts.items())
        blocks.append(f"[{statement_id}] {text}\n  cited facts:\n{cited}")
    return ("Review each statement against only its own cited facts.\n\n" + "\n\n".join(blocks)
            + "\n\nReturn one verdict for every statement ID.")


def edit(goal, pools, limits, facts):
    """Ask the editor to select validated statements by ID and write the headline and next steps."""
    parts = [f"Assemble the final report answer as JSON matching the schema.\nGoal: {goal}"]
    for section, pool in pools.items():
        if not pool:
            continue
        low, high = limits[section]
        listed = "\n".join(f"[{statement['id']}] {statement['text']}" for statement in pool)
        parts.append(f"Validated {section} (select {low} to {high} statement IDs, most important first; "
                     f"do not rewrite them):\n{listed}")
    parts.append(
        "Also write:\n"
        f"- answer: {GUIDE['answer']} (1 item). Prefer a qualitative headline without numbers.\n"
        f"- next_steps: {GUIDE['next_steps']} (1 to 3 items), qualitative, citing the facts that motivate them.\n"
        "For these, choose evidence_ids FIRST and follow every rule for statements.")
    parts.append("Facts you may cite:\n" + fact_sheet(facts))
    return "\n\n".join(parts)

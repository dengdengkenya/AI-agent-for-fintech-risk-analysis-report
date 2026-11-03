"""Domain definitions every answer may cite as evidence (fact IDs def.<key>).

Edit wording here to change what the analyst knows about the data; keep each entry a
plain statement of fact, since model text may quote numbers only from cited facts.
"""

DEFINITIONS = {
    "retained": (
        "A score cutoff comparison replays historical accepted loans: a cutoff retains the loans whose original "
        "FICO midpoint is at or above it. It does not measure approvals, rejections or a causal policy effect."
    ),
    "loss_proxy": (
        "Principal-loss proxy: unrecovered principal on financially complete Charged Off loans divided by funded "
        "principal of financially complete Fully Paid or Charged Off loans. Open and other statuses are excluded. "
        "This is historical, not a forecast, annualized default probability, profit or accounting net charge-off rate."
    ),
    "candidate_rule": (
        "A candidate cutoff needs an explicit loss ceiling, enough complete terminal outcomes and 100% terminal "
        "financial coverage; among qualifying tested cutoffs it retains the most historical accepted loans. "
        "It is a historical screening result, not an approval recommendation."
    ),
    "rejected_scores": (
        "Rejected Risk_Score mixes FICO and VantageScore, with unverified scale compatibility. Only numeric "
        "scores in 300-850 are screened. Rejected repayment outcomes are absent; screened counts cannot "
        "establish creditworthiness, approval rates or future losses."
    ),
    "source_limits": (
        "Month filters use issue/application cohorts, not repayment observation dates. Terminal-only selection "
        "and seasoning can bias losses. Missing data are excluded. These reads do not certify import "
        "completeness, deduplication or unique people."
    ),
    "next_checks": (
        "Useful next checks: agree a historical loss ceiling if one is absent; investigate why rejected scores "
        "fail the numeric screen and establish score-system comparability before interpreting demand; review "
        "excluded terminal financial records and loan-age differences before comparing performance."
    ),
}

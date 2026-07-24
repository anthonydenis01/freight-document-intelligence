# Customs Broker Catches $18k Discrepancy Before Filing: RAG for Freight Documents

**The problem.** Every import shipment travels with a small mountain of
paperwork: a bill of lading, a commercial invoice, a packing list, a
certificate of origin, an ISF filing, a draft customs entry, an arrival
notice, a delivery order. Eight documents, prepared by four or five
different parties, all describing the same containers, values and goods —
and none of them guaranteed to agree. A broker's entry team checks what it
can, but under deadline pressure the checking is sampling, not coverage.
The mismatches that slip through are exactly the ones customs authorities
are paid to find: a declared value that doesn't match the invoice, an HTS
code that changed between the invoice and the entry, an origin certificate
that contradicts the paperwork around it. Each one can mean a cargo exam,
penalties, storage charges while the container sits, and a client asking
why nobody caught it.

**The solution.** Freight Document Intelligence is an AI agent that reads
the entire document pack the way a very patient auditor would. It extracts
every field from every PDF, indexes the full text in a local vector store,
and does three things with it. First, it answers plain-English questions —
"What's the declared value versus the invoice total for shipment
MSKU-2847?" — and every answer cites the exact document and page it came
from, with a confidence score. No citation, no answer. Second, it
reconciles the documents against each other: twelve automated checks
covering container numbers, seals, vessels, weights, package counts,
values, tariff codes, origin and consignee data, with every mismatch
graded by severity and priced with a dollar-impact estimate. Third, it
runs a compliance checklist — ISF completeness, tariff-code plausibility,
origin consistency — and assembles an audit-ready report.

Crucially, the engine never files anything and never claims legal
sufficiency — it is decision support. Every report ends in "pending human
review," every decision it makes is written to an append-only audit trail,
and only a named human reviewer can approve a shipment. Dollar figures are
kept honest: exact arithmetic gaps are stated as numbers; everything else
is labeled a potential exposure requiring broker judgment. It runs on
fully synthetic data, works offline, and plugs into whatever your team
already uses — Microsoft Copilot, ChatGPT, Claude or internal tools —
through a standard API.

**The outcome.** On a controlled test set of ten shipments — eighty
machine-generated documents with twelve discrepancies deliberately planted
by an independent generator — the engine scored a perfect 1.000 precision
and recall on both field extraction (870 of 870 labeled fields, zero
false extractions) and discrepancy detection (all twelve planted mismatches
caught, zero false alarms — a system that cries wolf is as useless as one
that misses). That included the headline case: a draft customs entry
declaring $46,800 against a commercial invoice totaling $64,868 — an
$18,068 undervaluation flagged as critical, with both source documents
cited, before anything reached customs. Every answer carried a citation,
and every citation was verified against the stored page it pointed to. A
full one-shipment audit ran in under a second on a laptop. These are
results on clean synthetic documents — an upper bound that proves the
mechanism, stated with that caveat on purpose.

**Why it matters.** An $18k undervaluation caught after filing is a prior
disclosure, a penalty calculation and a difficult client call. Caught
before filing, it's a five-minute correction. The economics are lopsided:
the checking that prevents these losses is exactly the repetitive,
cross-referencing work that computers do better than tired humans at 6pm.
An agent that reads everything, cites everything, and hands the final
decision to a person doesn't replace a compliance team — it gives them
coverage instead of sampling, and receipts for every flag it raises.

*All data in this study is synthetic; metrics are reproducible via
`python eval.py` in the repository.*

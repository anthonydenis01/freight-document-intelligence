# Plan: run the live path on Amazon Bedrock

_Written 2026-10-04 · branch `bedrock-integration` · status: **plan only, no code changed yet**_

This engine already keeps every decision deterministic (parsing, reconciliation, compliance, severity,
audit log) and only lets a model compose answers and summaries through `tools/providers.py`. This plan
adds **Amazon Bedrock** as a fourth provider next to Claude, OpenAI and Azure, then puts **Bedrock
Guardrails** in front of it. It is also the hands-on lab for the AWS Certified AI Practitioner exam
(AIF-C01, 24 Oct 2026), so each step is mapped to the exam domains at the end.

**Ground rules**
- No change to business logic: `tools/reconcile.py`, `compliance.py`, `pdf_extract.py`, `indexer.py`,
  `governance.py`, `reporting.py`, `actions.py`, `rag.py` and `eval.py` stay untouched. The only code that
  changes is in the provider layer and the CLI/API wiring that lists provider names.
- `--offline` remains the default path for tests and demos. Bedrock is used only in live mode.
- No credentials, access keys or AWS account IDs in the repo. `<ACCOUNT_ID>` below is a placeholder.
  Real AWS calls run on the owner's PC with a named AWS CLI profile stored in `~/.aws/`, outside the repo.
- Who writes what: the scaffold (class skeleton, env handling, factory wiring, tests, TODOs) is prepared
  for review, but the owner writes the `converse()` call and the `apply_guardrail()` call. Those are the
  parts worth learning. [ASSUMPTION: "option 1" from the working brief; "option 2" = everything written for you.]

All AWS facts below were checked on 2026-10-04 against the official pages listed in [Sources](#sources).

---

## 1. Decisions

| Decision | Choice | Why |
|---|---|---|
| API | **Converse** on `bedrock-runtime` via `boto3` | One message format for every Bedrock chat model, with client-side tool use and guardrails built in [S1][S2] |
| Model | **Claude Haiku 4.5** | Cheapest current Claude with Converse, tool use and Guardrails support on `bedrock-runtime` [S3] |
| Model ID | `global.anthropic.claude-haiku-4-5-20251001-v1:0` | On `bedrock-runtime`, Haiku 4.5 **requires** a geo or global inference profile; the bare model ID is not supported on demand [S3]. Global is 10 % cheaper than geo (see §5) |
| Region | `us-east-1` [ASSUMPTION] | Matches the study lab. The global profile can route to any commercial Region; acceptable here because the corpus is 100 % synthetic. For EU residency, use `eu.anthropic.claude-haiku-4-5-20251001-v1:0` from an EU Region instead [S3] |
| Auth | IAM user `fdi-bedrock-lab`, programmatic only, used through `AWS_PROFILE` | Least privilege; no API key in `.env` |
| Selection | `--provider bedrock` or `FDI_PROVIDER=bedrock`, model override via existing `FDI_MODEL` | Same switch as the other providers |

---

## 2. Step a (Wed 7 Oct, on your PC): account, $5 budget, least-privilege IAM user

### 2.1 Click path

1. **Account.** Sign up at aws.amazon.com and add a payment method. A valid payment method is required for
   Anthropic models because Bedrock bills them through AWS Marketplace [S4]. Turn on **MFA for the root user**
   right away (IAM console → *Dashboard* → *Add MFA*).
2. **Budget ($5).** Billing and Cost Management console → **Budgets** → **Create budget** → **Use a template
   (simplified)** → **Monthly cost budget** → amount **5.00 USD** → your email → **Create budget** [S5].
   This template alerts when you exceed the amount or are forecast to exceed it [S5]. Then edit the budget
   (*Custom* / advanced) and add an **actual-cost alert at 50 %** so you hear about spend early.
   Budgets and alerts are free [S6]. Leave the budget unfiltered: Claude usage appears in Cost Explorer
   under the **model provider, not under Amazon Bedrock** [S3], so a service filter set to "Bedrock"
   would miss it.
3. **Policy.** IAM console → **Policies** → **Create policy** → **JSON** → paste the policy from §2.2
   (replace `<ACCOUNT_ID>` locally, never in git) → name it `FdiBedrockLabLeastPriv` → **Create policy**.
4. **User.** IAM console → **Users** → **Create user** → name `fdi-bedrock-lab` → do **not** give console
   access → **Next** → **Attach policies directly** → tick `FdiBedrockLabLeastPriv` → **Create user** [S7].
5. **Access key.** Open the user → **Security credentials** tab → **Create access key** → use case
   *Command Line Interface (CLI)* → download it once [S7]. On your PC run
   `aws configure --profile fdi-bedrock-lab` (region `us-east-1`). The key now lives in `~/.aws/credentials`,
   outside every repo. AWS recommends temporary credentials over long-term keys for humans [S7][S8]. That is
   acceptable for a lab if you delete the key at clean-up (Oct 22).
6. **Anthropic first-time-use form.** Bedrock console (us-east-1) → **Model catalog** → *Claude Haiku 4.5* →
   submit the use-case details. Use your GitHub profile as the website: AWS accepts it for individual
   developers. Access is granted immediately after submission [S4].
   [ASSUMPTION: do this one-off console task as root with MFA, or with an IAM Identity Center admin if you
   set one up. The lab user stays API-only.]
7. **First call (opens the Marketplace subscription).**
   ```bash
   aws bedrock-runtime converse --region us-east-1 --profile fdi-bedrock-lab \
     --model-id global.anthropic.claude-haiku-4-5-20251001-v1:0 \
     --messages '[{"role":"user","content":[{"text":"Reply with the word ready."}]}]'
   ```
   The first invocation starts the subscription in the background. It can take up to 15 minutes, and an
   `AccessDeniedException` during that window is expected [S4].
8. **Remove the Marketplace statement.** Once a call succeeds, edit `FdiBedrockLabLeastPriv` and delete the
   `FirstInvocationMarketplaceSubscribe` statement (this saves a new policy version). Marketplace permissions
   are only needed the first time a model is used in an account [S4].

### 2.2 Least-privilege policy (first-day version)

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "InvokeHaiku45ViaGlobalProfile",
      "Effect": "Allow",
      "Action": "bedrock:InvokeModel",
      "Resource": "arn:aws:bedrock:us-east-1:<ACCOUNT_ID>:inference-profile/global.anthropic.claude-haiku-4-5-20251001-v1:0"
    },
    {
      "Sid": "InvokeHaiku45UnderlyingModelOnlyThroughProfile",
      "Effect": "Allow",
      "Action": "bedrock:InvokeModel",
      "Resource": [
        "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0",
        "arn:aws:bedrock:::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0"
      ],
      "Condition": {
        "StringLike": {
          "bedrock:InferenceProfileArn": "arn:aws:bedrock:us-east-1:<ACCOUNT_ID>:inference-profile/global.anthropic.claude-haiku-4-5-20251001-v1:0"
        }
      }
    },
    {
      "Sid": "ApplyLabGuardrails",
      "Effect": "Allow",
      "Action": "bedrock:ApplyGuardrail",
      "Resource": "arn:aws:bedrock:us-east-1:<ACCOUNT_ID>:guardrail/*"
    },
    {
      "Sid": "FirstInvocationMarketplaceSubscribe",
      "Effect": "Allow",
      "Action": "aws-marketplace:Subscribe",
      "Resource": "*",
      "Condition": {
        "ForAnyValue:StringEquals": { "aws-marketplace:ProductId": ["prod-xdkflymybwmvi"] }
      }
    },
    {
      "Sid": "FirstInvocationMarketplaceView",
      "Effect": "Allow",
      "Action": ["aws-marketplace:Unsubscribe", "aws-marketplace:ViewSubscriptions"],
      "Resource": "*"
    }
  ]
}
```

Why each line is there:

| Statement | Reason | Source |
|---|---|---|
| `bedrock:InvokeModel` on the **inference profile** | `Converse` requires `bedrock:InvokeModel`. **There is no `bedrock:Converse` IAM action** | [S2][S9] |
| `bedrock:InvokeModel` on the **foundation model**, conditioned on the profile ARN | A profile call also needs the model allowed in every Region it routes to. For a global profile, AWS's example uses the source-Region ARN plus the Region-less `arn:aws:bedrock:::foundation-model/...` ARN. The condition stops direct model calls that bypass the profile | [S10] |
| `bedrock:ApplyGuardrail` on `guardrail/*` in us-east-1 | Needed to use a guardrail in inference and for the standalone `ApplyGuardrail` API. The guardrail ID doesn't exist yet, so this starts as a wildcard; **narrow it to the real guardrail ARN on Oct 11** | [S11][S9] |
| `aws-marketplace:Subscribe` (conditioned on product `prod-xdkflymybwmvi`, Claude Haiku 4.5), `Unsubscribe`, `ViewSubscriptions` | Needed only for the first invocation in the account. Remove after step 8 | [S4][S3] |
| No `InvokeModelWithResponseStream` | We don't stream. Add it only if `ConverseStream` is ever used | [S9] |
| No `ListFoundationModels`, `bedrock:*`, `iam:*` | Not needed by the code | — |

> **Correction for the study repo:** the table in `aws-ai-practitioner/labs/bedrock-claude/README.md` step a
> lists `bedrock:Converse` / `bedrock:ConverseStream` as IAM permissions. Those actions don't exist in the
> Service Authorization Reference: Converse is authorised by `bedrock:InvokeModel`, and ConverseStream by
> `bedrock:InvokeModelWithResponseStream` [S2][S9].

---

## 3. Step b (Thu 8 Oct): a `bedrock` provider (Converse API)

### 3.1 What changes

| File | Change | Business logic? |
|---|---|---|
| `tools/providers.py` | New `BedrockProvider` class (same 4 methods as the others: `start`, `step`, `add_tool_results`, `add_user`). `DEFAULT_MODELS["bedrock"]`. One new branch in `get_provider()`. Docstring lists the env vars | No: provider layer only |
| `freight_doc_agent.py` | Add `"bedrock"` to `--provider` choices | No: one argparse list |
| `api.py` | `/ask` `provider` field description: add `bedrock` | No: text only |
| `docs/openapi.json` | Refresh the committed copy from the running app (`GET /openapi.json`) so it matches the new description. The repo has no export script, so don't hand-edit it | No |
| `requirements.txt` | `# boto3>=1.40 (--provider bedrock)` under the optional providers comment | No |
| `tests/test_bedrock_provider.py` | **New**: offline unit tests (see §6) | No |
| `.env.example` | **New**: variable names only, no values | No |
| `README.md` | One short "Run on Amazon Bedrock" block + a provider line in *Tech* | No |

`run_tool_loop()`, `complete()`, `rag.answer_live()` and the `trace.jsonl` format don't change. The
provider name `bedrock` simply shows up in `mode: "live/bedrock"` and in `agent.summary_*` trace events.

### 3.2 Environment variables

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `FDI_PROVIDER` | no | `anthropic` | Set to `bedrock` (or pass `--provider bedrock`) |
| `FDI_MODEL` | no | `global.anthropic.claude-haiku-4-5-20251001-v1:0` | Model or inference profile ID override (existing variable, reused) |
| `AWS_REGION` | **yes** for bedrock | — | Region of the `bedrock-runtime` endpoint (boto3 standard; `AWS_DEFAULT_REGION` also accepted). Missing → clean `ProviderError` |
| `AWS_PROFILE` | recommended | — | Named profile holding the lab user's key, e.g. `fdi-bedrock-lab` (boto3 standard credential chain) |
| `FDI_BEDROCK_GUARDRAIL_ID` | step c | — | Guardrail ID. If unset, no guardrail is applied |
| `FDI_BEDROCK_GUARDRAIL_VERSION` | step c | `DRAFT` | Use a numeric version once the guardrail is stable |

No `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` in `.env`: credentials come from the profile.

### 3.3 Format mapping (what your `converse()` call has to translate)

| Normalized (this repo) | Converse API |
|---|---|
| `system: str` | `system=[{"text": system}]` |
| tools `{"name","description","input_schema"}` | `toolConfig={"tools":[{"toolSpec":{"name","description","inputSchema":{"json": schema}}}]}`. **Leave out `toolConfig` when the tool list is empty** (`complete()` passes `[]`) [S12] |
| user text | `{"role":"user","content":[{"text": ...}]}` |
| `max_tokens` | `inferenceConfig={"maxTokens": max_tokens}` |
| response | `resp["output"]["message"]`: append it to history as is. Text = join of `content[].text`; tool calls = `content[].toolUse` → `ToolCall(toolUseId, name, input)`; `stopReason == "tool_use"` [S12] |
| tool results | one **user** message whose content is `[{"toolResult":{"toolUseId": id, "content":[{"text": json.dumps(r, default=str)}]}} ...]`. Text keeps parity with the Anthropic adapter's `json.dumps` [S12] |
| errors | wrap `botocore` `ClientError` / `NoCredentialsError` in `ProviderError` with a hint (`AccessDeniedException` → check FTU form / policy / wait 15 min) |

### 3.4 Scaffold vs. what you write

The scaffold will contain everything except two marked `TODO(you)` bodies:
`BedrockProvider.step()` (the `client.converse(...)` call plus response parsing) and, in step c,
`BedrockProvider._guard_text()` (the `client.apply_guardrail(...)` call). The constructor takes an optional
`client=` argument so the tests can inject a stubbed boto3 client.

---

## 4. Step c (Sun 11 Oct): Bedrock Guardrails on the bedrock provider

### 4.1 Two AWS facts that shape the design

1. **A guardrail in `guardrailConfig` does not evaluate `toolResult` content** (nor tool definitions or
   tool-call arguments). It evaluates only text and `guardContent` blocks: user prompts, guarded system
   prompts and model responses [S13][S14]. In this engine, the document text and party data reach the model
   **through tool results** (`search_documents`, `get_shipment_fields`). Converse's `guardrailConfig` alone
   would therefore never see the party names or addresses.
2. **The `NAME` PII type does not match organisation names.** It covers individuals, and AWS says a
   string like "John Doe Organization" is recognised as an organisation, not a NAME [S14]. Every party in
   this corpus is a company (`SUNBELT TRADING GROUP LLC`, `NINGBO BRIGHTPLAY TOYS CO LTD`, ...), so `NAME`
   would mask almost nothing. `ADDRESS` does apply to the street addresses [S14].

### 4.2 Design: guard all three paths

| Path | Mechanism | Policy that fires |
|---|---|---|
| User question → model | `guardrailConfig` on every `converse()` call (`trace: "enabled"`) | **Denied topic** |
| Tool results → model (party data) | `ApplyGuardrail(source="INPUT")` on each serialized tool result **before** it is appended to history; masked text substituted when `action == "GUARDRAIL_INTERVENED"` [S15] | **PII**: `ADDRESS` (built-in, ANONYMIZE) + custom **regex** for company names (ANONYMIZE) |
| Model answer → user | same `guardrailConfig` (output side) | PII masking + denied topic |

Guardrail definition (console: Bedrock → **Guardrails** → **Create guardrail**) [S14]:
- **Denied topic**: `Customs duty evasion`. Definition: *"Advice on under-declaring value, misclassifying
  HTS codes, misstating origin or otherwise evading customs duties or filing requirements."* Sample
  phrase: *"How can I lower the declared value on MSKU-2847 so we pay less duty?"*
- **Sensitive information**: `ADDRESS` → Mask (input and output). Regex `party_company` → Mask, with a
  pattern such as `[A-Z][A-Z0-9&.,()' -]{2,80} (LLC|INC|CO LTD|PVT LTD|LTD)` (no lookarounds: AWS regex
  filters don't support them [S14]).
- Blocked messages: *"This request is outside what the freight document engine can help with."*

**Demo (one blocked, one masked):**
```bash
export FDI_PROVIDER=bedrock AWS_REGION=us-east-1 AWS_PROFILE=fdi-bedrock-lab
export FDI_BEDROCK_GUARDRAIL_ID=<id>            # in your shell only, not committed
python freight_doc_agent.py ask "How can I lower the declared value on MSKU-2847 so we pay less duty?"
#   → stopReason guardrail_intervened → canned message; trace.jsonl logs guardrail.blocked (topic name)
python freight_doc_agent.py ask "Who is the consignee for OOLU-8571 and where are they located?"
#   → answer cites [bill_of_lading p.1] but shows {ADDRESS} / {party_company}
```

**Expected trade-offs** (document these in the README, not hidden):
- With masking on, the live path can no longer *name* the consignee. That is the point of the demo: the
  deterministic checks (consignee consistency, etc.) still run unmasked on local data, because the
  guardrail sits only between the engine and the model.
- Citations survive: `[doc p.N]` markers are not PII.
- Never write the guardrail `match` field to `trace.jsonl`: it holds the **original** PII value by design
  [S14]. Log only action, policy type, entity type and count.
- Model invocation logging (if ever enabled) stores the unmasked input [S14]. It stays off for this lab.

### 4.3 IAM change on Oct 11

Replace `guardrail/*` in `ApplyLabGuardrails` with the real ARN
`arn:aws:bedrock:us-east-1:<ACCOUNT_ID>:guardrail/<GUARDRAIL_ID>`. Optional extra credit: add a
`bedrock:GuardrailIdentifier` condition (plus a matching Deny) on the InvokeModel statements so the lab user
**cannot** call the model without the guardrail [S16]. That is a strong Domain 5 talking point.

---

## 5. Expected cost

Prices checked 2026-10-04 on the Bedrock pricing page (us-east-1, Standard tier) [S17]:

| Item | Price |
|---|---|
| Claude Haiku 4.5, **global** profile | **1.00 USD / 1M input tokens · 5.00 USD / 1M output tokens** |
| Claude Haiku 4.5, geo / in-Region profile (`us.`, `eu.`) | 1.10 USD / 1M input · 5.50 USD / 1M output |
| Guardrails: denied topics | 0.15 USD / 1,000 text units (1 text unit = up to 1,000 characters) |
| Guardrails: sensitive information filters (PII) | 0.10 USD / 1,000 text units |
| Guardrails: sensitive information filters (regex), word filters | Free |
| AWS Budgets (monitoring + alerts) | Free [S6] |

> **Correction for the study repo:** `docs/why-this-cert.md` §6 lists the standard PII filter at 0 USD.
> The pricing page now shows **0.10 USD per 1,000 text units**; only the regex variant is free [S17].

Estimate for the whole lab (my arithmetic, rounded up):

| Activity | Volume | Cost |
|---|---|---|
| Step a smoke test | 5 calls × ~50 tokens | < 0.01 USD |
| Step b dev + 10-question live run | ~150K input + ~15K output tokens (tool loop resends history, ≤ 6 turns, ≤ 700 output tokens per turn) | ≈ 0.15 + 0.08 = **≈ 0.23 USD** |
| Step b live `audit` executive summary | 1–2 runs × ~40K input + 900 output | ≈ 0.09 USD |
| Step c guardrail runs | ~50 questions × ~10 text units × (0.10 + 0.15) / 1,000 | ≈ **0.13 USD** |
| **Total** | | **≈ 0.50 USD**, well under the 5 USD budget |

Free-plan coverage of Marketplace-billed Claude usage is still **[UNVERIFIED]** (see the study repo,
§6 caveat 1). If Claude is blocked on the Free plan, run steps b–c on Amazon Nova first.

---

## 6. Test strategy (offline, no AWS account needed)

- **Tool:** `botocore.stub.Stubber` on a real `boto3` `bedrock-runtime` client built with dummy credentials
  and `region_name="us-east-1"`. No network call is made, and unlike a bare `MagicMock`, the Stubber
  **validates request parameters and response shapes against the service model**. A malformed Converse
  request fails the test. Checked locally: `converse` and `apply_guardrail` stub cleanly, and an incomplete
  `ApplyGuardrail` usage block is rejected (boto3 1.43).
- `pytest.importorskip("boto3")` keeps the existing suite green on machines without boto3.
- **Baseline before any change:** `python -m pytest tests/ -q` → **44 passed** (2026-10-04).

Planned tests in `tests/test_bedrock_provider.py`:

| # | Test | Asserts |
|---|---|---|
| 1 | `get_provider("bedrock")` without `AWS_REGION` | `ProviderError` (mirrors the existing no-key test) |
| 2 | `start()` + `step()` with tools | Request has `system`, `toolConfig.tools[].toolSpec.inputSchema.json`, `inferenceConfig.maxTokens` (Stubber `expected_params`) |
| 3 | `complete()` (no tools) | Request has **no** `toolConfig` |
| 4 | `toolUse` response | Returns `ToolCall(id, name, arguments)`, assistant message appended verbatim |
| 5 | `add_tool_results()` | Next request carries one user message of `toolResult` blocks with matching `toolUseId`s |
| 6 | `run_tool_loop()` end-to-end (tool call → final cited answer) | Returns the cited text; the citation-retry contract still works |
| 7 | `rag.answer_live(..., provider=BedrockProvider(client=stub))` | `mode == "live/bedrock"`, citations captured from tool results |
| 8 | `ClientError(AccessDeniedException)` | Wrapped in `ProviderError` with the FTU/policy hint |
| 9 (c) | Guardrail configured → `guardrailConfig` present in every `converse` request | |
| 10 (c) | `ApplyGuardrail` returns masked output | The masked text replaces the tool result sent to the model |
| 11 (c) | `stopReason == "guardrail_intervened"` | Canned text returned, `guardrail.blocked` traced |
| 12 (c) | Trace content | No guardrail `match` value appears in `trace.jsonl` |

The live run on your PC is the integration test: `python freight_doc_agent.py --provider bedrock ask ...`
for the 10 `EVAL_QUESTIONS`, then compare with the Claude-API run. `eval.py` itself stays offline and
unchanged.

---

## 7. How each step maps to AIF-C01

Task statements from the official exam guide [S18].

| Step | What you do | Domain / task statement | Exam vocabulary to say out loud |
|---|---|---|---|
| a | Budget, least-privilege IAM user, Marketplace first invocation | **5.1** Explain methods to secure AI systems; **2.3** AWS infrastructure for GenAI apps (cost) | IAM least privilege, IAM user vs role, access keys vs temporary credentials, shared responsibility, AWS Budgets / Cost Explorer, Marketplace billing |
| b | Converse provider, inference profile, tool use | **2.3** AWS infrastructure and technologies for GenAI apps; **3.1** design considerations for FM apps; **2.2** capabilities and limitations | Amazon Bedrock (serverless FM access), Converse vs InvokeModel, cross-Region inference / data residency, token-based pricing, inference parameters (max tokens), agents = model + tools + orchestration |
| b | Model never decides findings; deterministic checks | **2.2** limitations of GenAI (hallucination); **4.2** transparent and explainable models | Hallucination, determinism, explainability, citations / grounding |
| c | Guardrails: denied topic + PII masking | **4.1** responsible AI systems; **5.1** securing AI systems; **5.2** governance and compliance | Bedrock Guardrails, denied topics, sensitive information filters, PII, prompt/response filtering, data privacy |
| c | Tool-result gap, trace never logs PII | **5.1**, **5.2** | Defense in depth, data protection, audit trail (and AWS's split: CloudTrail = API audit, CloudWatch = ops logs) |
| later | Knowledge Base on **S3 Vectors** vs local embeddings | **3.1** (RAG, vector stores) | RAG, embeddings, chunking, vector database, Knowledge Bases |
| later | Bedrock Evaluations (LLM-as-a-judge) vs `eval.py` | **3.4** methods to evaluate FM performance; **1.3** ML lifecycle | Precision, recall, F1, LLM-as-a-judge, human evaluation, ground truth |

---

## 8. Later (optional, after the exam if time is short)

- **Knowledge Base vs local retrieval.** Bedrock Knowledge Bases supports **Amazon S3 Vectors** as a vector
  store [S19]. Use it. **Never** use an OpenSearch Serverless *Classic* collection: it bills at least 2 OCUs
  at 0.24 USD per OCU-hour in us-east-1 [S20], about 11.50 USD/day, which blows the budget in under a day.
  (NextGen collections scale to zero after 10 idle minutes [S20].) Compare chunking, retrieval quality,
  citations and effort against `tools/indexer.py`. Delete the KB the same session.
- **`eval.py` vs Bedrock Evaluations.** Bedrock offers programmatic jobs, human-worker jobs, judge-model
  (LLM-as-a-judge) jobs and RAG evaluations with ground truth [S21]. `eval.py` already reports
  precision/recall on extraction and detection; the comparison is a write-up of what a judge model adds
  (answer faithfulness on the live path, listed in the v1.1 roadmap) and what it can't replace
  (exact-match ground truth).
- **middle-watch-agent.** Docs only, in that repo: map Postgres → Amazon RDS for PostgreSQL / Aurora,
  and restate the 40,000-row benchmark in exam terms (precision, recall, false positives, controlled
  synthetic test set ≠ field accuracy). No code change unless it clearly helps.

---

## Sources

All checked 2026-10-04.

- [S1] Inference using the Converse API — https://docs.aws.amazon.com/bedrock/latest/userguide/conversation-inference.html
- [S2] Converse API reference ("requires permission for the bedrock:InvokeModel action") — https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_Converse.html
- [S3] Claude Haiku 4.5 model card (IDs, inference profiles, Marketplace product ID, billing) — https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-haiku-4-5.html
- [S4] Request access to models (Marketplace actions, FTU form, 15-minute subscription) — https://docs.aws.amazon.com/bedrock/latest/userguide/model-access.html
- [S5] Using a budget template — https://docs.aws.amazon.com/cost-management/latest/userguide/budget-templates.html
- [S6] AWS Budgets pricing — https://aws.amazon.com/aws-cost-management/aws-budgets/pricing/
- [S7] Create an IAM user for workloads (console steps, access key) — https://docs.aws.amazon.com/IAM/latest/UserGuide/getting-started-workloads.html
- [S8] Create an IAM user (temporary-credential recommendation) — https://docs.aws.amazon.com/IAM/latest/UserGuide/id_users_create.html
- [S9] Service Authorization Reference, Amazon Bedrock — https://docs.aws.amazon.com/service-authorization/latest/reference/list_amazonbedrock.html
- [S10] Prerequisites for inference profiles (IAM examples incl. global profile) — https://docs.aws.amazon.com/bedrock/latest/userguide/inference-profiles-prereq.html
- [S11] Set up permissions to use Guardrails — https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails-permissions.html
- [S12] Tool use with the Converse API — https://docs.aws.amazon.com/bedrock/latest/userguide/tool-use-inference-call.html
- [S13] Include a guardrail with the Converse API (fields evaluated / not evaluated) — https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails-use-converse-api.html
- [S14] Sensitive information filters (PII types, NAME vs organisations, regex, trace `match`) — https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails-sensitive-filters.html
- [S15] ApplyGuardrail API — https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails-use-independent-api.html
- [S16] Enforce specific guardrails (`bedrock:GuardrailIdentifier`) — https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails-permissions-id.html
- [S17] Amazon Bedrock pricing (Anthropic global vs geo tables; Guardrails) — https://aws.amazon.com/bedrock/pricing/
- [S18] AIF-C01 exam guide, domains 1–5 — https://docs.aws.amazon.com/aws-certification/latest/ai-practitioner-01/ai-practitioner-01-domain1.html (…-domain2 … -domain5)
- [S19] Knowledge Base vector stores (Amazon S3 Vectors) — https://docs.aws.amazon.com/bedrock/latest/userguide/knowledge-base-setup.html
- [S20] OpenSearch Service pricing (Serverless OCU minimums, NextGen) — https://aws.amazon.com/opensearch-service/pricing/
- [S21] Bedrock evaluations — https://docs.aws.amazon.com/bedrock/latest/userguide/evaluation.html

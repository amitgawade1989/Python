Technical guide · for the team

# Document Comparison Evaluation Pipeline

How we measure an internal LLM's ability to compare two documents: how the documents are chunked, how a verified golden question set is built from them, how the parent-child RAG answers with traceable citations, and how every answer is scored against the LLM Evaluation Framework (Performance P1–P7, Reliability R1–R4, Document Comparison column).

Internal LLM call Local model (runs on the machine) Deterministic (code / formula, no model)

1. Overview
2. [What it does](#overview)
3. [Architecture](#architecture)
4. [Models and roles](#models)
5. Build the test set
6. [0 · Ingest](#p0)
7. [1 · Align](#p1)
8. [2 · Golden set](#p2)
9. [2b · Reliability suite](#p2b)
10. Run and score
11. [3 · RAG answers](#p3)
12. [4 · Performance](#p4)
13. [4b · Reliability](#p4b)
14. [5 · Report](#p5)
15. Reference
16. [Evaluation methods](#evaluation)
17. [Running and recovery](#operations)
18. [Known limits](#limits)

## What the pipeline does

The pipeline takes two documents of the same kind and measures **how well the internal LLM compares them**. To do this objectively, it builds its own test from the documents, so every question has a known correct answer and a known location in both documents.

#### Questions always need both documents

Three types: **differential** (the documents differ), **equivalence** (they say the same thing), **asymmetric** (only one covers it). A question never names a document, region or page.

#### Every answer is traceable

Ground truth and model answers point to exact chunk IDs of the form `D1-P04-C01` (document 1, parent section 4, child chunk 1), so retrieval and citations are scored by ID, not by opinion.

#### Scored against the framework

Each sub-metric uses the method the framework prescribes: deterministic formula, LLM judge, or hybrid. Results land in an Excel scorecard laid out like the framework sheet.

## System architecture

One script per phase. Each phase reads the previous phase's files from disk and writes its own, so any phase can be re-run on its own. The top row builds the test set once per document pair; the bottom row runs and scores one model under test and is repeated for every model evaluated.

Data moves left to right as files. The two document stores never share a collection, so retrieval for one document can never return the other's text. The dashed arrow shows that Phase 4 reads the golden answers and evidence IDs directly; it never sees how they were generated.

## Models and their roles

All model names live in one block of `config.yaml` (`models:`). Swapping a model never needs a code change.

| Role | Kind | Used in | What it does |
| --- | --- | --- | --- |
| **Generator** | Internal LLM | Phases 1, 2, 2b | Extracts facts, confirms section matches, pairs facts, writes and revises questions, builds variants. |
| **Model under test** | Internal LLM | Phase 3 only | Answers the questions from the retrieved context, with chunk-ID citations. This is the model being evaluated. |
| **Judge** | Internal LLM | Phases 2, 2b, 4, 4b | Critic for the golden set; scores the metrics the framework marks as LLM Judge or hybrid. Must differ from the model under test. |
| **Embeddings** · `all-MiniLM-L6-v2` | Local, 384 dimensions | Phases 1, 2b, 3, 4, 4b | Child-chunk and query vectors for retrieval; candidate section matching; cosine pre-filters for hybrid metrics. No API call. Reads at most \~256 word pieces per text. |
| **NLI classifier** · `cross-encoder/nli-deberta-v3-base` | Local | Phase 4 | Labels a (premise, claim) pair as entailment / contradiction / neutral. Used for groundedness (P6) and contradiction pre-checks (P5). |

## 0Ingest: documents into parent and child chunks

No model

Command

`python scripts/run_phase0_ingest.py`

Reads

the documents listed in `config.yaml → documents`

Writes

`data/processed/<doc>/parents.jsonl`, `children.jsonl`, `elements.jsonl`, `structure_review.md`, plus `ingest_manifest.json`

### Method

1. **Layout-aware extraction (PDF).** PyMuPDF `page.get_text("dict")` returns every text span with its font size, bold flag and position. Plain-text extraction would lose this, and heading detection depends on it.
2. **Tables.** `page.find_tables()` finds the grid; each cell's text is rebuilt from the full spans that start inside it (the grid alone clips overflowing text). Tables become markdown with whole rows kept together.
3. **Header/footer removal.** A line in the top or bottom 12% of a page that repeats on at least half the pages is dropped. Digits are ignored when comparing, so running page numbers match.
4. **Heading detection.** Body font size = the most common size weighted by text length. A line is a heading if it is at least 1 pt larger and has at most 20 words. Fallbacks: bold numbered lines ("1.", "2.3", "Section 4"); Word heading styles for DOCX; `#` for Markdown; numbering or ALL CAPS for plain text.
5. **Section level, chosen automatically.** Parents start at the largest heading size that occurs at least twice, so a one-off document title is not mistaken for a section level. Smaller headings stay inside their parent as sub-headings.
6. **Parents.** One per section, up to 1,500 tokens; larger sections split into "(part N)", preferring sub-heading boundaries. Documents with no headings fall back to fixed-size parts.
7. **Children.** Each parent is split with LangChain's recursive character splitter into \~120-token pieces with 20 tokens of overlap. A sub-heading starts a new child; tables split by groups of rows with the header row repeated; pieces under 25 tokens merge into a neighbour.
8. **Stable IDs and checks.** IDs are deterministic (`D1-P04-C02`), each chunk stores a text hash, and the run fails if IDs repeat, a parent has no child, or less than 99% of the document's words land in some chunk.

### Chunk records

| File | Fields |
| --- | --- |
| `parents.jsonl` | `parent_id`, `doc_id`, `doc_label`, `order`, `title`, `pages`, `n_tokens`, `text`, `text_hash` |
| `children.jsonl` | `child_id`, `parent_id`, `doc_id`, `section_title`, `order`, `kind` (text / table), `pages`, `n_tokens`, `text`, `text_hash` |

**Review checkpoint:** read `structure_review.md` before Phase 1. It lists the detected heading mode, removed headers/footers, every parent with its children and the validation results.

| Setting (`ingestion:`) | Default | Purpose |
| --- | --- | --- |
| `heading_min_size_delta` | 1.0 pt | How much larger than body text a heading must be |
| `parent_heading_min_count` | 2 | A heading size must repeat this often to define sections |
| `header_footer_page_ratio` / `margin` | 0.5 / 0.12 | Repeat ratio and page band for header/footer removal |
| `parent_max_tokens` | 1500 | Larger sections are split into parts |
| `child_tokens` / `child_overlap_tokens` / `child_min_tokens` | 120 / 20 / 25 | Child size, overlap, and merge threshold |

## 1Align: matching sections and fact pairs

Internal LLM: generatorMiniLM for candidatesQuote verification

Command

`python scripts/run_phase1_align.py`

Reads

Phase 0 parents and children of both documents

Writes

`data/processed/alignment/`: `facts.jsonl`, `topic_map.json`, `fact_pairs.jsonl`, `alignment_review.md`, plus every raw LLM response

Section headings usually differ between the two documents, so sections are matched by meaning, not by title. The phase produces **fact pairs**: one attribute, each document's value with its chunk IDs and quotes, and how the two relate.

### Method: five steps

| Step | Method | LLM calls |
| --- | --- | --- |
| **1. Fact inventory** | For each section, the generator lists every comparable fact (attribute, value, chunk ID, verbatim quote of ≤30 words). Each quote is checked against the chunk text with a fuzzy match (≥0.85); if it is found in a different chunk of the same section, the chunk ID is repaired; if not found, the fact is flagged. | 1 per section |
| **2. Section matching** | MiniLM ranks the 3 most similar sections of the other document (cosine ≥0.45). The generator confirms which really overlap and marks each match *full* or *partial*. Sections of the second document left unmatched are checked the other way round. | 1 per section of A |
| **3. Topics** | Deterministic grouping of **primary** matches (all full matches, else the single best partial). Other partial overlaps are kept only as cross-references, so unrelated sections cannot chain into one giant topic. | none |
| **4. Fact pairing** | Per topic, the generator groups facts that answer the same question and labels each group SAME, DIFFERENT, PARTIAL or ONE_SIDED. Code then fixes contradictions (e.g. ONE_SIDED with facts on both sides becomes PARTIAL) and adds any fact the LLM omitted as ONE_SIDED. | 1 per topic |
| **5. Absence check** | Every ONE_SIDED fact is searched in the *whole* other document (MiniLM top-4 chunks) and the generator decides whether it is really missing. If found, the pair is upgraded and flagged; if not, the absence is marked verified and the searched sections are recorded. | 1 per one-sided fact |

| Relation | Meaning | Question type in Phase 2 |
| --- | --- | --- |
| `DIFFERENT` | Both documents address it; values or rules differ | differential |
| `SAME` | Both state the same rule (wording may differ) | equivalence |
| `PARTIAL` | Both address it; one has materially more detail or conditions | asymmetric (or differential) |
| `ONE_SIDED` | Only one document addresses it; absence verified in the other | asymmetric |

Pairs flagged as unverifiable (for example a quote that could not be found) are excluded from Phase 2. Pairs that only compare section headings are dropped. **Review checkpoint:** `alignment_review.md` shows the topic map and every pair with each document's value, the relation and any flags.

## 2Golden set: verified comparison questions

Internal LLM: generator (write, revise)Internal LLM: judge (critic)Rule checks, evidence linking

Command

`python scripts/run_phase2_golden.py`

Reads

verified fact pairs and topic map from Phase 1; chunk texts from Phase 0

Writes

`data/golden/golden.jsonl` (the golden dataset), `rejected.jsonl`, `golden_review.md`, `golden_manifest.json`; step results in `work/`

One review round = rule checks + critic on every pending question, then rewriting of the failures. With `max_revisions: 2` there are at most three rounds. Questions that fail the rules skip the critic, which saves judge calls.

### Method

| Step | How it works |
| --- | --- |
| **Generate** generator | One call per topic (default 8 questions) using that topic's verified pairs, with a target mix of differential / equivalence / asymmetric and reasoning styles (multi-hop, conditional scenario, numeric, at most 2 "direct"). Plus cross-topic calls of 2 questions, each given exactly two topics and required to use both. The prompt forbids naming either document. |
| **Evidence linking** code | Each question lists the `pair_ids` it relies on. Code, not the LLM, turns those into evidence: document, parent ID, child ID and the quote that matches each chunk. For one-sided pairs it records the absent side and the sections searched. |
| **Check** rules | Metadata lint (no "Region 1", "R1", "Document A", "first handbook", page/section numbers, chunk IDs, file names, document labels or configured `banned_terms`); type must fit the pair relations; both documents covered; every quote found in its chunk; cross-topic questions really use two topics; required fields present. |
| **Critic** judge | Sees only the question, ground truth and evidence text. Scores answerable, correct and unambiguous (1–5, all must be ≥4) and answers yes/no: needs both documents, type matches, leaks document identity, too trivial. |
| **Revise** generator | Rewrites a rejected question using the exact issues found, keeping its ID; the rewrite goes through check and critic again. |

### Golden record fields

| Field | Content | Used by |
| --- | --- | --- |
| `qid`, `question` | ID and question text (no document names) | Phase 3 input |
| `question_type`, `reasoning`, `difficulty` | differential / equivalence / asymmetric; direct / multi_hop / conditional / numeric; easy / medium / hard | P1 relation match, breakdowns |
| `ground_truth`, `short_answer` | Full reference answer and one-sentence conclusion | P1 semantic correctness |
| `key_points` | 2–6 atomic points a correct answer must contain | P3 completeness |
| `expected_values` | Per cited pair: attribute, relation, each document's values | P1 numeric match |
| `evidence` | List of {doc_id, parent_id, child_id, quote, pair_id} | Retrieval Recall/Precision@k, citation ID match |
| `absent` | For asymmetric questions: the document that does not cover the point, plus the sections searched | Absence detection |
| `revision`, `critic_scores`, `critic_difficulty` | Number of rewrites; critic scores; critic's view of difficulty | Audit |

| Setting (`golden:`) | Default | Purpose |
| --- | --- | --- |
| `questions_per_topic` | 8 | Questions generated per topic |
| `cross_topic_questions` | 8 | Questions combining two topics (2 per call) |
| `max_direct_per_topic` | 2 | Cap on single-attribute questions |
| `max_revisions` | 2 | Rewrite rounds before a question is rejected |
| `critic_min_score` | 4 | Minimum critic score (1–5) for answerable / correct / unambiguous |
| `exclude_pair_flags`, `banned_terms` | list | Pair flags never used as evidence; words that reveal document identity |

## 2bReliability suite: variants of the golden questions

Internal LLM: generatorInternal LLM: judge (answerability)Typos, repeats, instructions

Command

`python scripts/run_phase2b_reliability_suite.py`

Reads

`golden.jsonl`, fact pairs

Writes

`data/reliability/variants.jsonl`, `filler.json`, `suite_review.md`

Each variant copies its parent question's ground truth and evidence, so it is scored exactly like the original. Sampling is stratified by question type and seeded, so the suite is reproducible. Variant IDs take the form `<parent qid>~<variant>`.

| Framework | Default size | How variants are built |
| --- | --- | --- |
| R1 Semantic | 30 questions × 3 paraphrases | generator rewrites with the same meaning; kept only if it passes the metadata lint and MiniLM similarity to the original is ≥0.80 |
| R1 Repeatability | 10 questions × 5 runs | identical copies, answered at temperature 0 with a fixed seed |
| R2 Typo / noise | 30 questions × 3 perturbations | seeded code: character typos on 10% of letters (swap, delete, duplicate, substitute), casing changes on 30% of words, whitespace noise on 10% of word gaps |
| R2 Adversarial | 20 questions × 3 techniques | generator: a false-premise question, a leading question, and a false passage that is planted into one document's context block |
| R2 Long context | 5 questions × 4 lengths × 3 positions | code: the gold sections buried in neutral filler (written once by the generator, 40 paragraphs, no employment-terms content) at 1K / 10K / 50K / 100K tokens, at start / middle / end |
| R3 Format | 6 questions × 4 rules × 2 phrasings | template: word limit 60, exactly 3 bullets, comparison table, no markdown |
| R3 Constraint | 6 questions × 3 rules | template: no recommendation, no law names, stay on topic |
| R3 Multi-step | 10 questions × 2 phrasings | template: answer in three labelled parts: positions, relationship, implication |
| R4 Ambiguity | 12 questions | generator writes deliberately vague comparison questions with their known interpretations |
| R4 Insufficient evidence | 12 questions | generator proposes twice as many questions on topics outside the documents; the judge reads both full documents and keeps only truly unanswerable ones |

## 3RAG answers: the model under test

MiniLM retrievalInternal LLM: under testCitation validation

Commands

`python scripts/run_phase3_rag.py --retrieval-only` (no LLM: retrieval metrics and quality gate)\
`python scripts/run_phase3_rag.py --run-id baseline`\
`python scripts/run_phase3_rag.py --questions data/reliability/variants.jsonl --run-id reliability`

Writes

`outputs/runs/<run_id>/retrieval.jsonl` (saved before any LLM call), `generation.jsonl`, `manifest.json`

Every question is searched in both documents' stores separately, because comparison questions always need both. Each document gets its own labelled context block, and every passage carries its chunk ID so the model can cite exactly where each fact came from.

### Method

1. **Index.** Each document's children are embedded as "section title + chunk text" and stored in their own Chroma collection with cosine distance and metadata (child ID, parent ID, document). Parents are not embedded; they are looked up by ID. A store rebuilds itself if the chunks or the embedding model change.
2. **Retrieve.** The question is embedded once and searched in every store: top 10 children per document, ranked with scores. An isolation check confirms each result came from its own document.
3. **Expand.** The parents of the top 4 children per document (at most 3 sections each) are rendered in full, every child prefixed with its ID.
4. **Answer.** The model under test receives the context and the question and must reply in JSON: relation type, answer, a list of claims each with chunk-ID citations, and anything a document does not address. Format instructions in the question apply to the answer field. The response is streamed so time-to-first-token is measured.
5. **Validate.** Each citation is marked `ok` (in the context), `not_in_context` (real chunk, not retrieved) or `fabricated` (no such chunk), and mapped to its parent and document.

Special modes used by the reliability suite: **long context** skips retrieval and buries the gold sections in filler; **injection** appends a planted false passage to one document's block and marks citations of it as `injected_false_passage`.

### Output records

| File | Fields |
| --- | --- |
| `retrieval.jsonl` | `qid`, `question`, `mode`, `retrieved` {doc: \[rank, child_id, parent_id, score\]}, `context_info` {doc: parent_ids, child_ids}, `context`, `context_tokens`, `isolation_ok` |
| `generation.jsonl` | `qid`, `relation`, `answer`, `claims` \[text, citations {child_id, parent_id, doc_id, status}\], `not_found`, `cited_child_ids`, `cited_parent_ids`, `citation_issues`, `raw`, `usage`, `latency_s`, `ttft_s` |

| Setting (`rag:`) | Default | Purpose |
| --- | --- | --- |
| `k_retrieve` | 10 | Ranked children logged per document |
| `context_k` / `max_parents_per_doc` | 4 / 3 | Children whose parents enter the context; cap on sections per document |
| `ks` | 1, 3, 5, 10 | Cut-offs for retrieval metrics |
| `retrieval_gate` | child recall@5 ≥ 0.8 | Retrieval-only mode fails below this |
| `workers` | 4 | Parallel answer generation |

## 4Performance evaluation (P1–P7)

Internal LLM: judgeNLI + MiniLMFormulas and ID matching

Command

`python scripts/run_phase4_evaluate.py --run-id baseline`

Reads

only `golden.jsonl` and the run's `retrieval.jsonl` / `generation.jsonl` (decoupled from Phases 1–3)

Writes

`outputs/runs/<run_id>/eval/`: `summary.json`, `per_question.jsonl`, `scores.csv`, `judge.jsonl`

Each framework sub-metric that applies to Document Comparison is computed with the implementation the framework names. Hybrid metrics use a cheap deterministic step first; only the uncertain middle band goes to the judge. Judge calls run four at a time and are cached per judge model. A failed judge call is reported as missing, never as 0. The method for every metric is in [Evaluation methods](#evaluation).

## 4bReliability evaluation (R1–R4)

Internal LLM: judgeValidators, deltas, regression

Command

`python scripts/run_phase4b_reliability.py --run-id reliability --baseline-run-id baseline`

Reads

the variant run, plus the clean baseline run and its Phase 4 evaluation (the clean accuracy to compare against)

Writes

`outputs/runs/<run_id>/eval_reliability/`: `summary.json`, `per_variant.jsonl`

Accuracy for every variant uses the same P1 semantic-correctness method as Phase 4, so a drop between a clean question and its variant is caused by the perturbation, not by a change in scoring. Methods per test are in [the reliability table](#eval-rel).

## 5Report: scorecard and HTML

No model

Command

`python scripts/run_phase5_report.py --run-id baseline --reliability-run-id reliability`

Writes

`outputs/reports/<date>_<run>/Evaluation_Scorecard.xlsx` and `report.html`

- **Scorecard sheet:** one row for each sub-metric of the framework workbook, with its Document Comparison applicability (✓ / O / blank), implementation, status, score, direction, threshold, result, n and a data-driven key finding. **Result is an Excel formula**, so changing a threshold re-grades the row; the summary block uses `COUNTIFS`.
- **Rows without a score state why:** "N/A for this capability (framework)", "Not run (optional)", or "Out of scope" for Safety. S5 Fabricated Citations is scored because it comes from the citation checks.
- **Other sheets:** Performance Detail (sub-metrics, relation confusion matrix, breakdowns by type, reasoning and difficulty), Retrieval (every k, per document and combined), Reliability Detail, Per Question, Run Info.
- **report.html:** KPI tiles, scores against thresholds, the full scorecard, retrieval recall@k, the comparison-type confusion heatmap, clean vs perturbed accuracy and the long-context heatmap, with data tables behind each chart.

## Evaluation methods

The three method types follow the framework's definitions:

Deterministic

A formula, algorithm or classifier with no subjective judgement: string and number matching, ID matching, NLI classifier, cosine similarity.

LLM Judge

The internal judge LLM receives the question, response and a rubric and returns a verdict or score where no formula is enough.

Hybrid

A deterministic step first: either a pre-filter that settles clear cases (cosine ≥0.97 pass, ≤0.40 fail) or an evidence-structuring step (key-point matching, NLI flags). The judge decides the rest.

### Retrieval (RAG layer)

| Metric | Method | Formula |
| --- | --- | --- |
| Recall@k | ID match | relevant chunks in top-k ÷ all relevant chunks; per document and combined, at child and parent level, k = 1, 3, 5, 10 |
| Precision@k | ID match | relevant chunks in top-k ÷ chunks retrieved in top-k |
| Hit@k | ID match | 1 if any relevant chunk is in the top-k |
| MRR | ID match | 1 ÷ rank of the first relevant chunk |
| nDCG@k | ID match | rank-discounted gain ÷ ideal gain (binary relevance) |
| Both-sides hit@k | ID match | 1 if each document's evidence is in its own top-k |
| Quality gate | threshold | combined child Recall@5 ≥ 0.80; `--retrieval-only` fails otherwise |

Relevance comes from the golden evidence IDs. A side that does not contain the answer (asymmetric "absent" side) has nothing to retrieve and is excluded from these averages.

### Performance (P1–P7)

| Sub-metric | Method | How it is computed | Pass |
| --- | --- | --- | --- |
| **P1 Exact / Objective** | Deterministic | Mean of (a) relation exact match: predicted comparison type = gold type, and (b) numeric tolerance match: share of the numbers in the ground truth that appear in the answer within 1% relative tolerance. Token-overlap F1 (normalised text) is reported alongside. | ≥ 0.80 |
| **P1 Semantic correctness** | Hybrid | Cosine(answer, ground truth): ≥0.97 auto-pass, ≤0.40 auto-fail; otherwise the judge compares conclusions and explicitly checks polarity/direction flips and attribution swaps. correct = 1, partially correct = 0.5, incorrect = 0. | ≥ 0.80 |
| **P2 Query relevance** | Hybrid | Cosine(question, answer) pre-filter (≥0.90 pass, ≤0.30 fail); otherwise a judge score 1–5 that ignores correctness, mapped to (score − 1) ÷ 4. | ≥ 0.80 |
| **P3 Completeness** | Hybrid | The ground-truth key points form the checklist; each is matched to its most similar answer sentence by cosine; the judge verifies every point (present and correct anywhere in the answer); coverage = points satisfied ÷ N. | ≥ 0.80 |
| P4 Redundancy | — | Blank for Document Comparison in the framework, so not computed. | — |
| **P5 Logical coherence** (O) | Hybrid | NLI checks every ordered pair of answer sentences for contradiction (p ≥ 0.9); pairs that each name a different document are skipped, because "A says X, B says Y" is not a contradiction. The judge then scores 1–5 and must quote the offending sentence below 4. | ≥ 0.80 |
| P5 Linguistic quality (O) | Hybrid | Readability band (Flesch reading ease 30–70) plus a judge clarity score 1–5. Off by default. | ≥ 0.70 |
| **P6 Claim faithfulness** | Deterministic | Atomic claims come from the answer's structured claim list. Each claim is compared by NLI with its 3 most similar context chunks, split into sentences, table rows and adjacent pairs (sentence-level NLI, max entailment). Document-name prefixes are neutralised; "does not mention" claims are excluded and scored as absence handling. Rate = entailed claims ÷ claims (entailment ≥ 0.5). | ≥ 0.85 |
| **P6 Citation accuracy** | Deterministic | NLI on (cited passages, claim) for every cited claim; citations that are fabricated or outside the context count as inaccurate. Also cited IDs vs golden evidence IDs: precision, recall and F1 at child and parent level. | ≥ 0.85 |
| **P7 Latency** | Deterministic | Time to first token and total time from streaming; mean, p50, p95, max; share of answers within SLA (TTFT ≤ 3 s and total ≤ 10 s by default). | ≥ 0.95 |
| **P7 Token usage / cost** | Deterministic | input + output tokens per answer; cost = tokens × configured internal rates per million tokens. | reported |

### Document-comparison checks

| Check | Method | What it catches |
| --- | --- | --- |
| Relation confusion matrix | count | Which comparison types the model confuses (for example equivalence answered as differential). |
| Invented-difference rate | rule | Equivalence questions answered as "different" or "one-sided". |
| Absence detection | rule | For asymmetric questions: the absent document is listed in the answer's `not_found`. |
| Both documents cited | ID match | The answer cites evidence from every document that holds gold evidence. |
| Attribution swaps | judge (within P1 semantic) | A fact from one document credited to the other. |
| S5 Fabricated citations | ID existence | Cited chunk IDs that do not exist anywhere. Lower is better; threshold ≤ 0.01. |

### Reliability (R1–R4)

| Sub-metric | Method | Framework formula as implemented | Pass |
| --- | --- | --- | --- |
| **R1 Semantic consistency** | Hybrid | Headline = expected-match rate: share of paraphrase answers judged fully correct against the ground truth. Also reported: pairwise cosine between all answers to the same question ≥ 0.80 (consistency rate) and relation-label agreement. | ≥ 0.85 |
| **R1 Repeatability** (O) | Deterministic | N identical runs at temperature 0 with a fixed seed: exact-match rate to the most common answer; standard deviation of pairwise similarity (variance attributable to the serving stack). | ≥ 0.80 |
| **R2 Typo / noise** | Hybrid accuracy | Accuracy(clean) − accuracy(perturbed) per perturbation type, flagged if the drop exceeds 5 percentage points. Headline = 1 − overall drop. Retrieval recall of the noisy questions is reported too. | ≥ 0.95 |
| **R2 Adversarial** | Judge + accuracy delta | The judge classifies Held / Partially Shifted / Fully Capitulated given the technique, the normal answer and the adversarial answer; headline = Held rate. Also the accuracy drop per technique and how often the planted passage was cited. | ≥ 0.80 |
| **R2 Long context** | Hybrid accuracy | Accuracy for each length × position cell (heatmap); slope of accuracy against log₁₀(length); lost-in-the-middle gap = mean(start, end) − middle. | ≥ 0.80 |
| **R3 Format** | Deterministic | Validator per rule: word count ≤ 60; exactly 3 bullet lines and nothing else; markdown table with ≥ 3 columns whose header names both documents; no markdown characters. Compliance rate per rule. | ≥ 0.90 |
| **R3 Constraint** | Hybrid | Hard rule by regex (any law / act / regulation name = violation); soft rules (no recommendation, stay on topic) by judge pass/fail. Compliance rate. | ≥ 0.90 |
| **R3 Multi-step** | Hybrid | Five sub-requirements: labelled parts in order, Part 1 names both documents, Part 2 states the relationship, Part 3 is one sentence (all regex/structure checks), Part 3 is a consistent implication (judge). Coverage = requirements met ÷ 5. | ≥ 0.90 |
| **R4 Ambiguity** | Hybrid | Clarification phrases by regex auto-pass; otherwise the judge classifies Clarification Requested / Multiple Interpretations Acknowledged / Silently Assumed One Interpretation. The first two count as appropriate. | ≥ 0.70 |
| **R4 Insufficient evidence** (O) | Hybrid | Abstention phrases plus "not found" for both documents auto-pass; otherwise the judge classifies Correct Abstention / Incorrect Guess. Also reported: false-abstention rate on the answerable golden set. | ≥ 0.80 |

Thresholds are defaults in `config.yaml` (`evaluation.pass_thresholds`, `reliability.pass_thresholds`) and should be set to the team's targets. The cosine pre-filter cut-offs (0.97 / 0.40) and the consistency threshold (0.80) were calibrated for all-MiniLM-L6-v2 against judge verdicts; recalibrate them if the embedding model changes.

## Running and recovery

| Phase | Command | Internal LLM | Needs first |
| --- | --- | --- | --- |
| 0 Ingest | `python scripts/run_phase0_ingest.py` | none | documents |
| Embedding check | `python scripts/check_embeddings.py` | none | Phase 0 |
| 1 Align | `python scripts/run_phase1_align.py` | generator | Phase 0 |
| 2 Golden | `python scripts/run_phase2_golden.py` | generator, judge | Phase 1 |
| 2b Reliability suite | `python scripts/run_phase2b_reliability_suite.py` | generator, judge | Phase 2 |
| 3 Retrieval check | `python scripts/run_phase3_rag.py --retrieval-only` | none | Phase 0 (+2) |
| 3 RAG answers | `python scripts/run_phase3_rag.py --run-id <id> [--questions …]` | model under test | Phase 2 / 2b |
| 4 Performance | `python scripts/run_phase4_evaluate.py --run-id <id>` | judge | Phase 3 |
| 4b Reliability | `python scripts/run_phase4b_reliability.py --run-id <var> --baseline-run-id <clean>` | judge | Phase 3 + Phase 4 on the clean run |
| 5 Report | `python scripts/run_phase5_report.py --run-id <clean> --reliability-run-id <var>` | none | Phase 4 (+4b) |

#### Interruptions

- Each LLM result is appended to its file and flushed to disk as soon as it finishes; a half-written last line is skipped on the next read.
- Running the same command again skips finished items.
- Items that failed after 6 retries with exponential backoff go to `failed_*.jsonl`; `--retry-failed` re-runs them.
- An error matching `llm.quota_error_patterns` stops the run immediately instead of failing every remaining call.

#### Safety rails

- Phases 1–2 refuse to resume if their inputs or models changed (`--fresh` starts over).
- A Phase 3 run id refuses to resume with different settings, questions or answer prompt.
- Judge verdicts are cached per judge model, so a new judge never reuses old verdicts.
- Evaluating another model: change `under_test`, then run Phases 3 → 4 → 4b → 5 with new run ids; the test set is reused unchanged.

## Known limits

| Area | Limit | Effect / mitigation |
| --- | --- | --- |
| PDF input | No OCR; multi-column pages; tables that continue onto the next page | Scanned pages yield no text; column text can interleave; a continued table's first row is treated as a header. Use DOCX/Markdown or OCR the file first. |
| Embeddings | MiniLM reads \~256 word pieces and is weaker than large embedding models | Chunks (\~120 tokens) fit; long answers are truncated for cosine. Raising `rag.context_k` recovers recall at the cost of a longer context. |
| P6 groundedness | Sentence-level NLI cannot combine a long list spread across several sentences | Part of a low P6 score can be the method's limit, not the model's. Per-claim NLI scores are kept in `per_question.jsonl` for review. |
| R3 format | The answer is written inside a JSON field | Line-based formats such as bullets may be harder than in free chat; read format scores in that context. |
| Scale | Validated only on short documents (a few pages) | For long documents: cap topic sizes in Phase 1, check unanswerable questions by retrieval instead of full documents in Phase 2b, and run Phases 1–2b with parallel workers. |
| Prompts | Prompts are worded for HR policy documents | For other document types, adjust the wording in `src/*/prompts.py` and `golden.banned_terms`. |
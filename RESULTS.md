# tokendiet — benchmark results (2026-10-28)

Three measurements, from most real to most modelled. Raw data in `results/*.json`.

## 1. Where tokens actually go (6 projects, 18 real Claude Code sessions, 1,835 API calls; this build's own session omitted below)

`bench/transcripts.py` over `~/.claude/projects`. Usage numbers are the API's own.

| project | sessions | API calls | cache-read tokens | session-start prefill (median) | peak context | exploration share of cost |
|---|---|---|---|---|---|---|
| Project A | 5 | 700 | 275.1M | 17.7k | 963k | 4.2% |
| Project B | 7 | 860 | 320.5M | 44.8k | 965k | 1.8% |
| Project C | 1 | 87 | 13.4M | 60.1k | 295k | 0.2% |
| Project D | 2 | 125 | 19.1M | 45.2k | 263k | 0.9% |
| Project E | 2 | 32 | 2.3M | 52.2k | 106k | 2.0% |

**Finding:** in these sessions (mostly greenfield builds), exploration tool results are a small share of cost.
The cost is the *carry*: every API call re-sends the whole conversation, so a session that drifts to 400k–960k
context pays for it on every turn (Project A: average 390k tokens re-read per call). Session-start prefill
(system prompt, tools, CLAUDE.md) is 18k–60k before any work. Cross-session re-reads were small (49k tokens, Project B).

So a graph that only shrinks exploration would save 2–4% here. The bigger lever is letting the agent start a
fresh session cheaply instead of carrying a huge one — which is what the map + notes are for. Exploration share
will be higher on large existing codebases; the next round of tests (your repos) will show it.

## 2. Early session resets, replayed on the same sessions

`bench/session_sim.py`. Reset when context exceeds T. *Plain*: the agent re-reads its whole working set (files
read or written so far). *tokendiet*: 5k handoff (map + notes + summary) plus targeted re-reads of 25% of the
working set. New tokens per call are replayed as logged. Change in total input-equivalent cost vs actual:

| project | plain @150k | tokendiet @150k | plain @100k | tokendiet @100k |
|---|---|---|---|---|
| Project A | +58.0% | **−51.5%** | +218.6% | −53.2% |
| Project B | +85.8% | **−38.6%** | +187.0% | −37.2% |
| Project D | −17.7% | −18.8% | −23.9% | −25.4% |
| Project C | −6.8% | −9.7% | −6.0% | −10.1% |
| Project E | +5.1% | +5.1% | −5.0% | −1.4% |

Sensitivity (T = 150k, 8k handoff), re-read fraction with tokendiet:

| re-read fraction | Project A | Project B |
|---|---|---|
| 25% | −51.2% | −37.8% |
| 50% | −42.9% | −29.4% |
| 75% | +5.5% | +30.1% |

**Read this as:** resetting long sessions is where the money is, but only if re-orientation is cheap. Without a
map, resets cost more than they save. With one, savings hold as long as the agent re-reads ≤ ~half its working
set. That fraction is an assumption here; `bench/live_ab.py` is what measures it.

## 3. Navigation tasks on 4 public repos (offline A/B)

`bench/offline.py`. 46 tasks per repo, fixed seed. Two baselines:
- **read-files**: Grep (files) → Read whole files. What the transcripts show agents doing most.
- **lean**: Grep with line numbers → ranged reads (±15–30 lines). A careful agent; the conservative comparison.

Recall is checked against text search with comments and strings removed.

| repo | lang | files / source tokens | cold index | refresh (no change) | DB |
|---|---|---|---|---|---|
| flask | Python | 83 / 135k | 1.3 s | 0.09 s | 0.8 MB |
| express | JS | 142 / 142k | 1.2 s | 0.10 s | 0.9 MB |
| gin | Go | 98 / 195k | 1.4 s | 0.08 s | 1.1 MB |
| zod | TS | 522 / 1.0M | 6.5 s | 0.18 s | 5.1 MB |

Median tokens per task (lower is better) and savings vs the **lean** baseline:

| task | flask | express | gin | zod |
|---|---|---|---|---|
| locate symbol (n=20) | 1,104 → 558 (−55%) | 940 → 180 (−81%) | 629 → 158 (−73%) | 1,764 → 216 (−77%) |
| understand symbol + usages (n=15) | 1,892 → 409 (−78%) | 2,619 → 246 (−81%) | 1,422 → 220 (−79%) | 2,759 → 296 (−76%) |
| impact of changing a file (n=10) | 2,774 → 841 (−65%) | 2,594 → 750 (−65%) | 2,174 → 599 (−72%) | 4,331 → 1,785 (−65%) |
| orient at session start | 1,044 → 1,905 (+82%)* | 3,958 → 1,834 (−54%) | 3,730 → 1,974 (−47%) | 5,988 → 1,903 (−68%) |

Against **read-files**: −89% to −99% on every task type.
\* lean "orient" is file list + README only — no code structure — so it is not equivalent information.

Answer quality:

| | flask | express | gin | zod |
|---|---|---|---|---|
| locate: correct definition returned | 100% | 100% | 100% | 95% |
| understand: usage-file recall | 0.97 | 0.70 | 0.95 | 0.87 |
| understand: precision | 0.93 | 0.92 | 0.97 | 0.88 |
| impact: dependent-file recall | 1.00 | 0.92 | 1.00 | 0.82 |

Express recall is lower because it uses lowercase method access (`res.header`) that is neither a call nor
safely attributable by name. Tool schemas cost 507 tokens per turn.

## 4. Five private production Python services (same offline suite)

| repo | files / source tokens | locate vs lean | understand vs lean | impact vs lean | accuracy |
|---|---|---|---|---|---|
| service-1 | 109 / 191k | −27% | −62% | −94% | locate 95%, usage recall 0.88, impact recall 1.0 |
| service-2 | 38 / 48k | −32% | (too few candidates) | −95% | locate 100%, impact recall 1.0 |
| service-3 | 24 / 49k | −52% | (too few candidates) | −93% | locate 100%, impact recall 1.0 |
| service-4, service-5 | 7 / 7k | – | – | – | too small to benefit |

**Where it is worth it:** "what does changing this file affect" saves 93–95% on every private service. Locating a symbol
saves less on small Python repos (a careful grep is already cheap there); returning the body for the best match only
moved service-1 from −3% to −27% and service-2 from −13% to −32%. Repos under ~20k source tokens: don't bother, the agent can read
everything. The orientation map costs more than "file list + README" on small repos, but it carries structure and notes.

## 5. Is the built-in meter honest?

`tokendiet report` estimates, for every logged call, what a careful and a typical agent would have pulled in.
Checked against the measured baselines on 135 tasks across 4 repos:

| | estimate / measured, median | quartiles |
|---|---|---|
| careful (lean) baseline | 0.84 | 0.68 – 1.06 |
| typical (read-files) baseline | 0.65 | 0.59 – 0.82 |

The meter under-counts savings by design (and the reported "conservative" figure also takes the cheaper of the two routes).

## 6. TokenDiet vs a CLAUDE.md / AGENTS.md / skills file

`bench/vs_static_docs.py`. Real human-written agent docs (speechbrain: AGENTS.md + CLAUDE.md; REA: AGENTS.md + 5
skills), and doc rot measured from git history of the private services (structure snapshot N months ago vs HEAD, read-only).
Session cost = 1h cache write (2x) + re-read on each of ~102 calls (0.1x), the median-ish session from section 1.

**What a static doc covers**

| | speechbrain | REA |
|---|---|---|
| always-loaded doc size | 3,617 tok | 2,234 tok |
| cost per session, loaded whether used or not | 44k input-equiv | 27k |
| important symbols (used from 2+ files) the docs mention at all | 16 / 200 (8%) | 12 / 200 (6%) |
| file paths in the docs that still exist | 1 / 1 | 12 / 18 (33% stale) |
| identifiers in the docs that still exist | 27 / 27 | 61 / 61 |
| a static doc with the same structure TokenDiet serves | 63.5k tok, 775k / session | 336k tok, 4.1M / session |
| TokenDiet: 1.5k map + tools on demand | 18.3k / session + calls | 18.3k / session + calls |

**Doc rot (a structure doc written N months ago, checked against today)**

| repo | written | today's symbols missing from the doc | signatures changed | line numbers off by >10 | entries pointing at deleted/renamed/moved code |
|---|---|---|---|---|---|
| service-1 | 3 / 6 / 12 mo | 12% / 47% / 82% | 0 / 0 / 5% | 7 / 5 / 24% | 0 / 0 / 3% |
| service-2 | 3 / 6 / 12 mo | 9% / 15% / 58% | 5 / 5 / 18% | 26 / 23 / 60% | 3 / 4 / 10% |
| service-3 | 3 / 6 mo | 2% / 35% | 0 / 14% | 28 / 39% | 0 / 0% |

**Reading it fairly:** hand-written docs are accurate about what they say and good at what code can't tell you
(conventions, commands, how to run tests, why). They just say very little about the code itself (6–8% of important
symbols), so the agent still greps for everything else. Writing the code structure into a doc instead costs 42–224x
more per session than the map and goes stale fast: half of today's code is missing from a 6-month-old doc.
TokenDiet's index is re-checked on every call. Best setup: a short CLAUDE.md for rules, TokenDiet for structure.

## What is not yet measured

1. Live agent behaviour: does the agent actually use the tools, and how much does it still re-read after a reset?
   (`bench/live_ab.py`, needs your go-ahead to spend tokens.)
2. Large existing codebases and your own repos/sessions.
3. Claude-tokenizer counts for offline numbers (tiktoken proxy used; ratios unaffected).

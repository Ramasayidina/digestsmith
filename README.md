# DigestSmith — a research-digest agent on OpenMinion 0.1

**Cortensor Hackathon #5 entry.** Give DigestSmith a topic; it researches
that topic on two free, key-less sources (Hacker News and arXiv), writes a
short structured digest of what it actually found, and files the digest
plus the raw evidence — the same run, end to end, every time. One useful
workflow, repeated on demand or from cron:

```bash
python digest_agent.py "decentralized inference" --yes
# -> output/decentralized-inference-20261121.md
# -> output/decentralized-inference-20261121.sources.json
```

A real recorded run (gpt-oss-20b, no API key) is in `output/`, with its
console log at `output/pollinations-run.log` and a machine-readable record
in `output/runlog.jsonl`. In that run the model called both research tools
itself (2 tool calls, no code fill-in), the editor's output validated
against the schema on the first attempt, the write went through the
approval gate, and an automated check confirmed **every link in the
digest exists in the collected facts — zero invented sources**.

## Why it is built this way

The design rule is *the model never fetches, and never gets the last
word*:

1. **Researcher agent** (OpenMinion `Agent`, tools `search_hackernews` /
   `search_arxiv`, both `@tool(min_scope="READ_ONLY", idempotent=True)`).
   The tools are plain stdlib fetchers that return facts only — title,
   URL, points, date, snippet. If the model ever skips a tool, code calls
   the same fetcher directly and logs the fill-in in `runlog.jsonl`, so a
   skipped step can never silently become a hallucinated source.
2. **Editor agent** (`output_type=Digest`, a pydantic schema). It has no
   tools and receives only the collected facts plus the JSON schema
   inside the user message. It picks the top items and writes takeaways.
   Its output is validated before anything is rendered; on a validation
   failure it gets one retry with the error fed back.
3. **Publisher agent** (forced tool `save_digest`, marked
   `dangerous=True`). OpenMinion routes the call through an approval
   callback — interactive y/N, or `--yes` in scripts — so nothing is
   written without a human decision. The tool takes only a filename; the
   payload comes from the run store, so the model decides *whether* to
   write while code carries the content (small models mangle large tool
   arguments; they do not mangle a filename).

Every run writes three artefacts: the digest (Markdown, rendered
deterministically from the validated editor output), a `.sources.json`
with the raw facts the tools saw, and one line in `runlog.jsonl` with
per-step timings, tool-call counts, fill-ins and approvals. A reviewer
can check every claim in the digest against the facts file — that check
is scripted in this README's verification section and was run on the
recorded output.

## Setup

Requirements: Python 3.11+, and a model provider. Two are preconfigured;
both are free and neither needs a paid account.

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python configure.py          # builds .openminion-home*/ configs
```

`configure.py` runs OpenMinion's own `config init`/`setup` into
project-local homes (nothing touches your real `~/.openminion`) and
patches the timeouts and background-LLM features described under
*Lessons learned*.

### Provider A (recorded run): Pollinations, key-less

`gpt-oss-20b` served through Pollinations' OpenAI-compatible endpoint on
its anonymous tier — no key, no signup:

```bash
OPENMINION_HOME=$PWD/.openminion-home-pollinations \
OPENAI_COMPATIBLE_API_KEY=anonymous \
.venv/bin/python digest_agent.py "your topic" --yes \
  --config $PWD/.openminion-home-pollinations/.openminion/agents.json
```

Expect a full run to take ~10–25 minutes: the anonymous tier is slow and
the researcher turn is reasoning-heavy. The digest itself is written in
the last minutes.

### Provider B (offline): Ollama, fully local

```bash
ollama serve &                         # any local Ollama install
ollama pull qwen2.5:3b
.venv/bin/python digest_agent.py "your topic" --yes
```

Measured honestly: on the 1.5-shared-CPU VM this was built on, local
generation ran at ~1 token/s and a single agent turn (3,861-token prompt)
did not finish inside 25 minutes (`output/ollama-run.log`). On a normal
laptop or any GPU this path is the nicest one — no network, no keys —
but size the model to your hardware.

### Switching to Cortensor / OpenRouter / a paid key

Nothing in the agent code is provider-specific. Point `configure.py`'s
pattern at another preset (`openminion setup --provider
cortensor-portal`, `--provider openrouter`, …), or edit the `providers`
block in the generated `agents.json`; the agents, tools and prompts stay
identical. That swappability is the point of building on the SDK.

## Tests

The facts layer — the part the agent's honesty depends on — has offline
regression tests (recorded payloads, no network, no model):

```bash
.venv/bin/python -m unittest discover -s tests -v   # 4 tests, all passing
```

## How this maps to the hackathon brief

| The listing asks for | Where it lives here |
| --- | --- |
| A small working agent on the OpenMinion 0.1 Python SDK | `digest_agent.py`: three `Agent` profiles, run-scoped `@tool` functions with scope metadata, `output_type` validation, `forced_tools`, approval callback |
| One useful, repeatable workflow, end to end | Topic in → researched, written, filed digest out; cron-friendly (`--yes`), deterministic artefacts, per-run log |
| A real-model demo of 1–5 minutes | `DEMO.md` script; recorded run artefacts in `output/` (real model: gpt-oss-20b) |
| Building blocks tested, lessons learned | The section below; the facts layer's offline test suite |
| AI assistance allowed | Disclosed below |

## Lessons learned (SDK findings)

- **`NO_PROXY` can crash the SDK's HTTP stack.** This sandbox exports a
  `NO_PROXY` containing bracketed IPv6 literals (`[::1]`, `[fd8b:…::1]`);
  httpx's proxy parser dies on them with `InvalidURL: Invalid port:
  ':1]'` before any request is sent. `digest_agent.py` sanitises
  `NO_PROXY` at startup. If an OpenMinion provider call fails instantly
  with a port error, check your environment before the provider.
- **Turn timeouts are chat-sized by default.** The generated config
  allows 45–90 s per turn; a reasoning model on a free tier needs far
  more. `configure.py` raises provider/turn timeouts to 900–1500 s.
- **Disable the memory auto-extractor for batch agents.** With it on,
  the first request of every turn is a memory-extraction call carrying
  the full system prompt (`"brain": {"auto_fact_extraction":
  {"enabled": false}}` removes it). Memory and self-improvement are
  also off in the shipped config — a digest run has nothing to remember
  between topics, and every extra LLM call is latency a small model
  feels.
- **`min_scope="READ_ONLY"` is what stops confirmation prompts** on
  read-only tools; `idempotent=True` tags alone do not (the decorator's
  `WRITE_SAFE` default wins). Write tools get `dangerous=True` and go
  through the approval callback instead.
- **`output_type` is a validator, not a guarantee.** The schema also
  goes into the user message, and the editor gets one retry with the
  validation error appended. With that, gpt-oss-20b produced valid JSON
  on the first attempt.
- **The model's narration is not evidence.** In the recorded run the
  researcher reported "search_arxiv returned 3 items" while the tool
  had stored 5. The digest is rendered from the run store, never from
  the model's prose, and the run log records tool-call counts from the
  runtime stats — trust those, not the summary sentence.
- **Local small models + a big runtime prompt = patience.** OpenMinion
  sends a substantial system prompt per call (≈3.9k tokens measured).
  At ~1 tok/s (shared-CPU VM) a turn times out; on hosted open-weight
  inference the same code completes. Design so the model is the
  replaceable part — facts in code, schema on the answer, approval in
  front of writes — and the workflow survives the swap.

## AI assistance disclosure

This project was designed and written with the help of an AI coding
assistant (Muse), including the code, tests and this README. The agent
workflow itself runs on OpenMinion with an open-weight model
(gpt-oss-20b) as described above. All claims about the recorded run are
backed by the artefacts in `output/`.

## Files

```
digest_agent.py   the agent (facts layer, tools, 3 agent profiles, CLI)
configure.py      reproducible OpenMinion config builder (timeouts, patches)
tests/            offline tests for the HN/arXiv parsers + slugify
requirements.txt  openminion==0.1.22, pydantic
DEMO.md           the 1–5 minute demo script
output/           recorded run: digest, sources.json, runlog.jsonl, run logs
```

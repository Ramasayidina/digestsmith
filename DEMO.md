# Demo script — DigestSmith (target: 3–4 minutes of video)

One full run, trigger to filed digest. The recorded reference run is in
`output/` (topic: "decentralized inference", model: gpt-oss-20b via the
key-less Pollinations endpoint). A live run takes ~10–25 minutes on the
free tier, almost all of it model thinking time — record the terminal,
then speed the waiting segments 8–16× in the edit; the timestamps printed
by the program keep the sped-up footage honest.

## Before you press record

```bash
cd cortensor
.venv/bin/python configure.py        # if the configs are not built yet
.venv/bin/python -m unittest discover -s tests -v
```

Terminal sized large; `output/` folder open in a file browser or second
pane for the final reveal.

## Shot list

**0:00–0:25 — The problem (voiceover or captions).**
"Every week, the same chore: check Hacker News, check arXiv, decide what
matters, write it up. DigestSmith is a small OpenMinion agent that does
that whole loop for any topic — and shows its evidence."

**0:25–0:45 — It is a real SDK agent.**
Show `digest_agent.py` briefly: the three `@tool` functions
(`search_hackernews`, `search_arxiv` read-only; `save_digest` marked
`dangerous=True`), then the three `Agent` profiles — Researcher, Editor
with `output_type=Digest`, Publisher with `forced_tools`.

**0:45–1:00 — The offline tests.**
Run the unittest command. Four parser tests pass in under a second.
Caption: "The facts layer is tested offline — the agent is only as
honest as this layer."

**1:00–1:20 — Launch the run.**
Type the command (Pollinations variant from the README, or the default
Ollama one if recording on a machine with a GPU):

```bash
.venv/bin/python digest_agent.py "decentralized inference" --yes
```

**1:20–2:20 — The run (sped up 8–16×).**
Let the timestamped log lines carry the story. Call out the three beats
as they appear:
- "Researcher done. Facts collected: {'arXiv': 5, 'Hacker News': 6};
  filled in by code: none" — the model called both tools itself.
- "Editor done: 6 items…" — typed, schema-validated output.
- "Approval for save_digest: APPROVED (--yes flag)" — the write went
  through OpenMinion's approval gate; without `--yes` this is an
  interactive y/N prompt. Show that by pausing on the line.

**2:20–3:10 — The reveal.**
Open `output/decentralized-inference-<date>.md`: headline, summary, six
items with points/authors, themes, follow-ups. Then open the companion
`.sources.json` side by side and click one digest link, then find the
same URL in the facts file. Caption: "Every link in the digest exists in
the raw facts — zero invented sources. That check is automated."

**3:10–3:40 — Repeatability + close.**
Show `output/runlog.jsonl` (one line: per-step seconds, tool calls,
approvals). Final caption/voiceover: "Any topic, same workflow, on cron
if you want it. Facts in code, a schema on the answer, a human in front
of the write — so the model can come from anywhere: a free hosted
endpoint today, Cortensor or a local model tomorrow."

## Fallbacks while recording

- If the free tier is having a slow day, record against the artefacts
  already in `output/` and narrate from `output/pollinations-run.log` —
  it is a genuine recorded run, timestamps included.
- If a source is unreachable, the run still completes on the other
  source and `runlog.jsonl` records the error; that is a feature worth
  one sentence, not a failed take.

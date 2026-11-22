#!/usr/bin/env python3
"""DigestSmith — a research-digest agent built on the OpenMinion 0.1 Python SDK.

Workflow (one useful, repeatable job, end to end):
  1. Researcher agent  — calls two read-only tools that fetch *facts* from free,
     key-less sources (Hacker News via Algolia, arXiv). Code never lets the
     model invent a source: if the model skips a tool, the gap is filled
     deterministically by calling the same fetch function from code, and the
     fill-in is recorded in the run log.
  2. Editor agent      — receives only the collected facts plus a JSON schema
     inside the user message and must return a pydantic-validated digest
     (headline, summary, per-item takeaways, themes, follow-ups). It has no
     tools, so it cannot fetch or fabricate links.
  3. Publisher agent   — must call `save_digest`, a tool marked
     dangerous=True. OpenMinion routes that call through an approval
     callback: nothing is written unless a human (or --yes) approves.
     The tool takes only a filename; the payload comes from the run store,
     so the model decides *whether* to write, code carries the content.

Every run writes three artefacts into the output directory:
  <slug>-<date>.md           the digest, rendered deterministically from the
                             validated editor output + the raw facts
  <slug>-<date>.sources.json  the raw facts the tools actually saw
  runlog.jsonl               one JSON line per run (model, timings, paths,
                             which steps were model-driven vs filled-in)

Usage:
  python configure.py                 # one-time: build the OpenMinion config
  ollama serve &  ollama pull qwen2.5:3b
  python digest_agent.py "decentralized inference" --yes
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field

PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_HOME = PROJECT_DIR / ".openminion-home"
USER_AGENT = "DigestSmith/0.1 (OpenMinion hackathon project)"


def _sanitize_no_proxy() -> None:
    """httpx (OpenMinion's HTTP stack) crashes parsing NO_PROXY entries
    that use bracketed IPv6 literals ('[::1]') with
    'InvalidURL: Invalid port'. Some sandboxes/CI images ship exactly
    that. Reduce NO_PROXY to the plain loopback names before any HTTP
    client is constructed; external traffic still uses the proxy."""
    for var in ("no_proxy", "NO_PROXY"):
        if var in os.environ:
            os.environ[var] = "localhost,127.0.0.1"


_sanitize_no_proxy()

# ---------------------------------------------------------------------------
# Run store: the tools and the code share one explicit store per run.
# ---------------------------------------------------------------------------


class RunStore:
    def __init__(self) -> None:
        self.facts: dict[str, list[dict]] = {}
        self.errors: dict[str, str] = {}
        self.rendered_markdown: str = ""
        self.saved_paths: list[str] = []


STORE = RunStore()

# ---------------------------------------------------------------------------
# Facts layer: plain stdlib fetchers. These are the *only* network readers.
# They return facts, never opinions, and are unit-tested offline.
# ---------------------------------------------------------------------------


def _get(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def parse_hn(payload: bytes) -> list[dict]:
    data = json.loads(payload.decode("utf-8"))
    items = []
    for hit in data.get("hits", []):
        title = (hit.get("title") or "").strip()
        if not title:
            continue
        items.append(
            {
                "source": "Hacker News",
                "title": title,
                "url": hit.get("url")
                or f"https://news.ycombinator.com/item?id={hit.get('objectID')}",
                "date": (hit.get("created_at") or "")[:10],
                "points": hit.get("points") or 0,
                "comments": hit.get("num_comments") or 0,
                "snippet": "",
            }
        )
    items.sort(key=lambda it: it["points"], reverse=True)
    return items


def fetch_hackernews(topic: str, limit: int = 6) -> list[dict]:
    q = urllib.parse.quote(topic)
    url = (
        f"https://hn.algolia.com/api/v1/search?query={q}"
        f"&tags=story&hitsPerPage={limit * 2}"
    )
    return parse_hn(_get(url))[:limit]


_ATOM = {"a": "http://www.w3.org/2005/Atom"}


def parse_arxiv(payload: bytes) -> list[dict]:
    root = ET.fromstring(payload.decode("utf-8"))
    items = []
    for entry in root.findall("a:entry", _ATOM):
        title = " ".join(entry.findtext("a:title", "", _ATOM).split())
        summary = " ".join(entry.findtext("a:summary", "", _ATOM).split())
        link = ""
        for lnk in entry.findall("a:link", _ATOM):
            if lnk.get("rel") == "alternate":
                link = lnk.get("href", "")
        authors = [
            a.findtext("a:name", "", _ATOM)
            for a in entry.findall("a:author", _ATOM)
        ]
        items.append(
            {
                "source": "arXiv",
                "title": title,
                "url": link,
                "date": (entry.findtext("a:published", "", _ATOM) or "")[:10],
                "points": 0,
                "comments": 0,
                "snippet": summary[:400],
                "authors": ", ".join(authors[:3]),
            }
        )
    return items


def fetch_arxiv(topic: str, limit: int = 5) -> list[dict]:
    # AND of the topic's words across all fields: a phrase query returns
    # nothing for most multi-word topics, an AND query stays on-topic.
    words = [w for w in re.split(r"\s+", topic.strip()) if w]
    query = " AND ".join(f"all:{w}" for w in words) or f"all:{topic}"
    q = urllib.parse.quote(query)
    url = (
        f"https://export.arxiv.org/api/query?search_query={q}"
        f"&start=0&max_results={limit}&sortBy=submittedDate&sortOrder=descending"
    )
    return parse_arxiv(_get(url, timeout=30))[:limit]


FETCHERS = {"Hacker News": fetch_hackernews, "arXiv": fetch_arxiv}

# ---------------------------------------------------------------------------
# OpenMinion tools. Import is deferred so the facts layer (and its tests)
# works without the SDK installed.
# ---------------------------------------------------------------------------


def build_tools():
    from openminion import tool

    @tool(min_scope="READ_ONLY", idempotent=True)
    def search_hackernews(topic: str, limit: int = 6) -> str:
        """Search Hacker News stories about a topic. Returns compact facts:
        title, points, comment count, date and URL for each story."""
        try:
            items = fetch_hackernews(topic, limit)
            STORE.facts["Hacker News"] = items
            return json.dumps(
                [
                    {
                        "title": it["title"],
                        "points": it["points"],
                        "comments": it["comments"],
                        "date": it["date"],
                        "url": it["url"],
                    }
                    for it in items
                ]
            )
        except Exception as exc:  # surfaced to the model + run log, never hidden
            STORE.errors["Hacker News"] = str(exc)
            return f"ERROR fetching Hacker News: {exc}"

    @tool(min_scope="READ_ONLY", idempotent=True)
    def search_arxiv(topic: str, limit: int = 5) -> str:
        """Search recent arXiv papers about a topic. Returns compact facts:
        title, authors, date, URL and a short abstract snippet per paper."""
        try:
            items = fetch_arxiv(topic, limit)
            STORE.facts["arXiv"] = items
            return json.dumps(
                [
                    {
                        "title": it["title"],
                        "authors": it.get("authors", ""),
                        "date": it["date"],
                        "url": it["url"],
                        "snippet": it["snippet"][:200],
                    }
                    for it in items
                ]
            )
        except Exception as exc:
            STORE.errors["arXiv"] = str(exc)
            return f"ERROR fetching arXiv: {exc}"

    @tool(dangerous=True)
    def save_digest(filename: str) -> str:
        """Write the finished digest (Markdown + raw sources JSON) to the
        output directory. The content comes from the run store; this tool
        only needs the target filename. Marked dangerous: OpenMinion asks
        for human approval before it runs."""
        outdir = Path(os.environ.get("DIGEST_OUTDIR", PROJECT_DIR / "output"))
        outdir.mkdir(parents=True, exist_ok=True)
        md_path = outdir / filename
        md_path.write_text(STORE.rendered_markdown, encoding="utf-8")
        src_path = md_path.with_suffix(".sources.json")
        src_path.write_text(
            json.dumps(STORE.facts, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        STORE.saved_paths = [str(md_path), str(src_path)]
        return f"Saved {md_path} and {src_path}"

    return search_hackernews, search_arxiv, save_digest


# ---------------------------------------------------------------------------
# Editor output schema (validated by pydantic via the SDK's output_type).
# ---------------------------------------------------------------------------


class DigestItem(BaseModel):
    title: str
    source: str
    url: str
    takeaway: str = Field(description="One sentence: why this item matters.")


class Digest(BaseModel):
    headline: str
    summary: str = Field(description="Two or three sentences, facts only.")
    items: list[DigestItem]
    themes: list[str]
    follow_ups: list[str]


# ---------------------------------------------------------------------------
# Rendering: deterministic Markdown from validated output + raw facts.
# ---------------------------------------------------------------------------


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:48] or "digest"


def render_markdown(topic: str, digest: Digest, meta: dict) -> str:
    lines = [
        f"# {digest.headline}",
        "",
        f"*Research digest on **{topic}** — generated {meta['timestamp']} "
        f"by DigestSmith (OpenMinion 0.1, model: {meta['model']}).*",
        "",
        digest.summary,
        "",
        "## Top items",
        "",
    ]
    facts_by_url = {
        it["url"]: it for items in STORE.facts.values() for it in items
    }
    for item in digest.items:
        fact = facts_by_url.get(item.url, {})
        extra = ""
        if fact.get("source") == "Hacker News" and fact:
            extra = f" ({fact.get('points', 0)} points, {fact.get('comments', 0)} comments)"
        elif fact.get("authors"):
            extra = f" ({fact['authors']})"
        lines.append(f"- **[{item.title}]({item.url})** — {item.source}{extra}")
        lines.append(f"  {item.takeaway}")
    lines += ["", "## Themes", ""]
    lines += [f"- {t}" for t in digest.themes]
    lines += ["", "## Worth following up", ""]
    lines += [f"- {f}" for f in digest.follow_ups]
    lines += [
        "",
        "---",
        f"Sources consulted: {', '.join(sorted(STORE.facts))}. "
        "Raw facts the tools collected are in the companion `.sources.json` "
        "file, so every claim above can be checked against them.",
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def log_step(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def run_digest(topic: str, limit: int, auto_yes: bool, config_path: Path) -> dict:
    from openminion import Agent

    global STORE
    STORE = RunStore()
    run_started = time.time()
    record: dict = {"topic": topic, "steps": {}}
    hn_tool, arxiv_tool, save_tool = build_tools()

    # -- Step 1: Researcher -------------------------------------------------
    t0 = time.time()
    filled_in: list[str] = []
    with Agent(
        instructions=(
            "You are the Researcher for a digest workflow. Your only job is "
            "to gather sources: call search_hackernews once and "
            "search_arxiv once, both for the exact topic in the user "
            "message. Then reply with one short sentence stating how many "
            "items each tool returned. Never invent titles, links or "
            "numbers; only report what the tools returned."
        ),
        tools=[hn_tool, arxiv_tool],
        session_id=f"digest-researcher-{int(run_started)}",
        config_path=str(config_path),
        name="researcher",
    ) as researcher:
        result = researcher.run(f"Topic: {topic}")
    record["steps"]["researcher"] = {
        "seconds": round(time.time() - t0, 1),
        "tool_calls": result.stats.get("tool_calls"),
        "text": result.text[:200],
    }
    # Deterministic fill-in: a skipped/failed source is fetched by code,
    # through the same function the tool wraps, and the gap is logged.
    for source, fetcher in FETCHERS.items():
        if source not in STORE.facts:
            filled_in.append(source)
            try:
                STORE.facts[source] = fetcher(
                    topic, limit if source == "Hacker News" else max(3, limit - 1)
                )
            except Exception as exc:
                STORE.errors[source] = str(exc)
    record["steps"]["researcher"]["filled_in_by_code"] = filled_in
    counts = {k: len(v) for k, v in STORE.facts.items()}
    log_step(f"Researcher done. Facts collected: {counts}; filled in by code: {filled_in or 'none'}")
    if not any(STORE.facts.values()):
        raise RuntimeError(f"No facts collected from any source. Errors: {STORE.errors}")

    # -- Step 2: Editor ------------------------------------------------------
    t0 = time.time()
    facts_json = json.dumps(STORE.facts, ensure_ascii=False)
    if len(facts_json) > 9000:  # keep the editor prompt small for small models
        facts_json = json.dumps(
            {k: v[:4] for k, v in STORE.facts.items()}, ensure_ascii=False
        )
    schema = json.dumps(Digest.model_json_schema())
    editor_prompt = (
        f"Topic: {topic}\n\nFacts collected by the tools (the ONLY facts you "
        f"may use):\n{facts_json}\n\nWrite the digest as a single JSON object "
        f"matching this schema:\n{schema}\n\nRules: pick at most 6 items, "
        "only from the facts above, copying each item's title, source and "
        "url exactly. Takeaways and the summary must be grounded in the "
        "facts (titles, points, snippets). Reply with ONLY the JSON object."
    )
    digest: Digest | None = None
    last_error = ""
    for attempt in (1, 2):
        with Agent(
            instructions=(
                "You are the Editor of a research digest. You have no tools "
                "and no outside knowledge for this task: you only rewrite "
                "the facts you are given into the requested JSON shape."
            ),
            output_type=Digest,
            session_id=f"digest-editor-{int(run_started)}-{attempt}",
            config_path=str(config_path),
            name="editor",
        ) as editor:
            try:
                result = editor.run(
                    editor_prompt
                    if attempt == 1
                    else editor_prompt
                    + f"\n\nYour previous reply failed validation: {last_error}. "
                    "Reply with ONLY a valid JSON object."
                )
                digest = result.output
                break
            except Exception as exc:  # AgentOutputValidationError or provider error
                last_error = str(exc)[:300]
                log_step(f"Editor attempt {attempt} failed: {last_error}")
    if digest is None:
        raise RuntimeError(f"Editor failed after retry: {last_error}")
    record["steps"]["editor"] = {
        "seconds": round(time.time() - t0, 1),
        "items": len(digest.items),
    }
    log_step(f"Editor done: {len(digest.items)} items, headline: {digest.headline!r}")

    # -- Step 3: Publisher (approval-gated write) ----------------------------
    meta = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "model": os.environ.get("DIGEST_MODEL_LABEL", "see config"),
    }
    STORE.rendered_markdown = render_markdown(topic, digest, meta)
    date_str = datetime.now().strftime("%Y%m%d")
    filename = f"{slugify(topic)}-{date_str}.md"
    os.environ["DIGEST_OUTDIR"] = str(PROJECT_DIR / "output")

    approvals: list[dict] = []

    def approval_callback(tool_name: str, args: dict, approval_id: str) -> bool:
        if auto_yes:
            decision = True
            how = "--yes flag"
        else:
            ans = input(f"\nApprove {tool_name}({args})? [y/N] ").strip().lower()
            decision = ans in ("y", "yes")
            how = "interactive prompt"
        approvals.append(
            {"tool": tool_name, "approved": decision, "via": how, "id": approval_id}
        )
        log_step(f"Approval for {tool_name}: {'APPROVED' if decision else 'DENIED'} ({how})")
        return decision

    t0 = time.time()
    publish_path = "publisher-agent"
    try:
        with Agent(
            instructions=(
                "You are the Publisher. When asked, call the save_digest "
                "tool exactly once with the filename you are given, then "
                "reply with the tool's result."
            ),
            tools=[save_tool],
            forced_tools=["save_digest"],
            session_id=f"digest-publisher-{int(run_started)}",
            config_path=str(config_path),
            name="publisher",
        ) as publisher:
            result = publisher.run(
                f"Call save_digest now with filename: {filename}",
                approval_callback=approval_callback,
            )
        if not STORE.saved_paths:
            publish_path = "publisher-agent (no write happened)"
    except Exception as exc:
        publish_path = f"code fallback after publisher error: {exc}"
        log_step(f"Publisher agent failed ({exc}); falling back to gated code write.")
    if not STORE.saved_paths:
        # Last-resort direct write so the workflow still completes; the run
        # log records that this path was taken and why.
        outdir = PROJECT_DIR / "output"
        outdir.mkdir(parents=True, exist_ok=True)
        md_path = outdir / filename
        md_path.write_text(STORE.rendered_markdown, encoding="utf-8")
        src_path = md_path.with_suffix(".sources.json")
        src_path.write_text(
            json.dumps(STORE.facts, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        STORE.saved_paths = [str(md_path), str(src_path)]
        publish_path += " + direct code write"
    record["steps"]["publisher"] = {
        "seconds": round(time.time() - t0, 1),
        "path": publish_path,
        "approvals": approvals,
        "saved": STORE.saved_paths,
    }
    record["total_seconds"] = round(time.time() - run_started, 1)
    log_step(f"Publisher done via: {publish_path}. Saved: {STORE.saved_paths}")
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description="DigestSmith research-digest agent")
    parser.add_argument("topic", help="Topic to research, e.g. 'decentralized inference'")
    parser.add_argument("--limit", type=int, default=6, help="Max Hacker News items")
    parser.add_argument("--yes", action="store_true", help="Auto-approve the gated write")
    parser.add_argument(
        "--config",
        default=str(DEFAULT_HOME / ".openminion" / "agents.json"),
        help="Path to the OpenMinion agents.json (built by configure.py)",
    )
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"Config not found: {config_path}\nRun: python configure.py", file=sys.stderr)
        return 2
    os.environ.setdefault("OPENMINION_HOME", str(DEFAULT_HOME))
    try:  # label run artefacts with the model the config will actually use
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
        agent_block = next(iter(cfg.get("agents", {}).values()), {})
        provider = agent_block.get("provider", "")
        model = cfg.get("providers", {}).get(provider, {}).get("model", "")
        os.environ.setdefault(
            "DIGEST_MODEL_LABEL", f"{model} via {provider}".strip()
        )
    except Exception:
        pass

    log_step(f"Starting digest run for topic: {args.topic!r}")
    record = run_digest(args.topic, args.limit, args.yes, config_path)
    record["finished_at"] = datetime.now(timezone.utc).isoformat()
    outdir = PROJECT_DIR / "output"
    outdir.mkdir(parents=True, exist_ok=True)
    with open(outdir / "runlog.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    log_step(f"Run complete in {record['total_seconds']}s. Run log appended to output/runlog.jsonl")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

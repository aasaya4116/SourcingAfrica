"""
Sourcing Africa Analyst — Local CLI Agent
Reads the Ledger, applies the ADE Framework via Claude, and produces:
  1. ADE-tagged articles (written back to ledger)
  2. The Friday Brief (3 mobile-optimised investable signals)
  3. Podcast Hooks for The Africast
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import anthropic

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
DATA_DIR = ROOT / "data"
OUTPUTS_DIR = ROOT / "outputs"
OUTPUTS_DIR.mkdir(exist_ok=True)


# ── Config / Ledger I/O ───────────────────────────────────────────────────────

def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)


def load_ledger(path: Path) -> dict:
    if not path.exists():
        print(f"[ERROR] Ledger not found at {path}. Run the ingestor first.")
        sys.exit(1)
    with open(path) as f:
        return json.load(f)


def save_ledger(ledger: dict, path: Path):
    with open(path, "w") as f:
        json.dump(ledger, f, indent=2, ensure_ascii=False)


# ── Claude helpers ────────────────────────────────────────────────────────────

def get_client() -> anthropic.Anthropic:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("[ERROR] ANTHROPIC_API_KEY environment variable not set.")
        sys.exit(1)
    return anthropic.Anthropic(api_key=api_key)


def call_claude(client: anthropic.Anthropic, model: str, system: str, user: str) -> str:
    msg = client.messages.create(
        model=model,
        max_tokens=2048,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return msg.content[0].text.strip()


# ── ADE Tagging ───────────────────────────────────────────────────────────────

ADE_SYSTEM = """You are an African tech investment analyst.
Your job is to classify news articles using the ADE Framework:

- AUTOMATION: Efficiency plays — AI adoption, process automation, cost-cutting tech,
  restructuring via technology (e.g. AI-driven logistics, fintech automation).
- DISCOVERY: New startups, funding rounds, new market entrants, product launches,
  partnerships creating new value (e.g. seed rounds, Series A, new verticals).
- EMERGENCE: Macro-level shifts — policy changes, infrastructure buildout, currency
  moves, demographic trends, regulatory shifts, new asset classes
  (e.g. AI data centers, debt-vs-equity trend shifts, currency consolidation).

Respond with ONLY a JSON array. Each element must have:
  "url": <string>,
  "ade_tag": <"AUTOMATION" | "DISCOVERY" | "EMERGENCE">,
  "signal": <one-sentence investable insight, ≤ 20 words>

No markdown, no explanation, just the JSON array."""


def tag_articles(client: anthropic.Anthropic, model: str, articles: list[dict]) -> list[dict]:
    """Batch-tag untagged articles with ADE categories."""
    untagged = [a for a in articles if not a.get("ade_tag")]
    if not untagged:
        print("All articles already tagged.")
        return articles

    print(f"Tagging {len(untagged)} article(s) with ADE framework...")

    # Send in batches of 20 to stay within token limits
    batch_size = 20
    tag_map: dict[str, dict] = {}

    for i in range(0, len(untagged), batch_size):
        batch = untagged[i:i + batch_size]
        article_list = "\n".join(
            f"URL: {a['url']}\nTitle: {a['title']}\nSummary: {a['summary']}"
            for a in batch
        )
        response = call_claude(
            client, model,
            ADE_SYSTEM,
            f"Classify these articles:\n\n{article_list}"
        )
        try:
            tags = json.loads(response)
            for t in tags:
                tag_map[t["url"]] = t
        except json.JSONDecodeError as exc:
            print(f"[WARN] Could not parse ADE tags for batch {i // batch_size + 1}: {exc}")

    # Apply tags back to articles
    for article in articles:
        if article["url"] in tag_map:
            article["ade_tag"] = tag_map[article["url"]]["ade_tag"]
            article["signal"] = tag_map[article["url"]].get("signal", "")

    return articles


# ── Friday Brief ─────────────────────────────────────────────────────────────

BRIEF_SYSTEM = """You are a mobile-first investment analyst covering African tech.
Write concisely — this will be read on a phone screen.

Produce a "Friday Brief" with exactly 3 bullet points labeled "Investable Signals".
Each bullet must:
  • Start with a bold ADE category tag: **AUTOMATION**, **DISCOVERY**, or **EMERGENCE**
  • Describe the signal in ≤ 25 words
  • End with a concrete "so what" for an investor (deal sourcing, market entry, thesis validation)

Format:
## Friday Brief — Investable Signals

• **[ADE]** Signal. _So what: ..._
• **[ADE]** Signal. _So what: ..._
• **[ADE]** Signal. _So what: ..._

---
_Sourcing Africa | {date}_"""


def generate_friday_brief(client: anthropic.Anthropic, model: str, articles: list[dict]) -> str:
    today = datetime.now(timezone.utc).strftime("%d %b %Y")
    tagged = [a for a in articles if a.get("ade_tag")][:50]

    if not tagged:
        return "No tagged articles available. Run `analyst.py tag` first."

    article_list = "\n".join(
        f"[{a['ade_tag']}] {a['title']} — {a.get('signal', a['summary'][:100])}"
        for a in tagged
    )
    prompt = f"Today is {today}. Here are the top signals from the past week:\n\n{article_list}\n\nWrite the Friday Brief."
    system = BRIEF_SYSTEM.replace("{date}", today)
    return call_claude(client, model, system, prompt)


# ── Podcast Hooks ─────────────────────────────────────────────────────────────

HOOKS_SYSTEM = """You are a podcast producer for "The Africast", a show about African tech investing.
Your job: turn raw news signals into punchy episode hooks that make listeners lean in.

Produce 5 hooks in this format:
## Africast Hooks — {date}

1. **Hook title** (≤ 8 words, provocative)
   _The angle_: One sentence explaining the narrative tension or trend.
   _The question_: The big "so what" question to explore on air.

Keep language direct and edgy. Avoid jargon."""


def generate_podcast_hooks(client: anthropic.Anthropic, model: str, articles: list[dict]) -> str:
    today = datetime.now(timezone.utc).strftime("%d %b %Y")
    tagged = [a for a in articles if a.get("ade_tag")][:50]

    if not tagged:
        return "No tagged articles available. Run `analyst.py tag` first."

    article_list = "\n".join(
        f"[{a['ade_tag']}] {a['title']} — {a.get('signal', a['summary'][:100])}"
        for a in tagged
    )
    system = HOOKS_SYSTEM.replace("{date}", today)
    return call_claude(client, model, system, f"Generate Africast hooks from:\n\n{article_list}")


# ── Output writers ────────────────────────────────────────────────────────────

def write_output(content: str, filename: str) -> Path:
    path = OUTPUTS_DIR / filename
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"Saved → {path}")
    return path


def format_ledger_report(articles: list[dict]) -> str:
    lines = ["# ADE-Tagged Ledger\n"]
    for tag in ("AUTOMATION", "DISCOVERY", "EMERGENCE"):
        matching = [a for a in articles if a.get("ade_tag") == tag]
        if not matching:
            continue
        lines.append(f"\n## {tag} ({len(matching)})\n")
        for a in matching:
            lines.append(f"- **{a['title']}** ({a['source']}, {a['date'][:10]})")
            if a.get("signal"):
                lines.append(f"  > {a['signal']}")
            lines.append(f"  {a['url']}\n")
    return "\n".join(lines)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Sourcing Africa Analyst — ADE Framework CLI"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # tag: apply ADE tags to untagged articles
    subparsers.add_parser("tag", help="ADE-tag all untagged articles in the ledger")

    # brief: generate Friday Brief
    subparsers.add_parser("brief", help="Generate this week's Friday Brief")

    # hooks: generate Africast podcast hooks
    subparsers.add_parser("hooks", help="Generate Africast podcast hooks")

    # full: run tag + brief + hooks in one shot
    subparsers.add_parser("full", help="Run tag → brief → hooks (weekly pipeline)")

    args = parser.parse_args()

    cfg = load_config()
    model = cfg["analyst"]["model"]
    ledger_path = ROOT / cfg["ledger"]["local_path"]
    ledger = load_ledger(ledger_path)
    articles = ledger.get("articles", [])
    client = get_client()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if args.command in ("tag", "full"):
        articles = tag_articles(client, model, articles)
        ledger["articles"] = articles
        save_ledger(ledger, ledger_path)
        report = format_ledger_report(articles)
        write_output(report, f"ade_ledger_{today}.md")

    if args.command in ("brief", "full"):
        brief = generate_friday_brief(client, model, articles)
        print("\n" + brief + "\n")
        write_output(brief, f"friday_brief_{today}.md")
        write_output(brief, f"friday_brief_{today}.txt")

    if args.command in ("hooks", "full"):
        hooks = generate_podcast_hooks(client, model, articles)
        print("\n" + hooks + "\n")
        write_output(hooks, f"africast_hooks_{today}.md")
        write_output(hooks, f"africast_hooks_{today}.txt")


if __name__ == "__main__":
    main()

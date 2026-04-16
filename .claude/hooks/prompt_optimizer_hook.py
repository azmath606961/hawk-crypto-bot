"""
HAWK Crypto Bot — Prompt Optimizer Hook
Fires on every UserPromptSubmit via .claude/settings.json.

What it does:
1. Reads context.md for current project state
2. Classifies the user's intent into one of 7 categories
3. Injects the relevant hard-rule constraints into the prompt context
4. Outputs a structured header that Claude Code prepends to the model's context
"""

import json
import os
import sys
from pathlib import Path


# ─── HARD RULES ────────────────────────────────────────────────────────────────
# These are the invariants of the HAWK crypto bot. Any violation causes real damage.

HARD_RULES = {
    "R1":  "CRITICAL: qty = risk_usdt / sl_dist — NEVER multiply qty by leverage. Violating this = instant blowup at 10x+",
    "R2":  "Always verify sl_dist > 0 before computing qty",
    "R3":  "Paper trader reads PREVIOUS closed candle (iloc[-2]) — never the current open candle (no look-ahead)",
    "R4":  "30% drawdown gate is hardcoded — never override without explicit user instruction",
    "R5":  "If trades = 0 → check CSV column names first (must be lowercase: open/high/low/close/volume)",
    "R6":  "No API keys in code — Binance public REST only for paper mode (no auth needed)",
    "R7":  "Never recommend going live without 30+ paper trades with positive EV confirmed",
    "R8":  "50x leverage is in backtester for research only — do not recommend for actual trading (liq distance = 1.5%)",
    "R9":  "Always run backtest before changing strategy parameters",
    "R10": "Max 3 concurrent positions, 60% total margin cap — never override",
    "R11": "No trailing stops, partial exits, or speculative features unless explicitly requested",
    "R12": "leveraged_engine.py constants (fees=0.0005, funding=0.0001, GBP_TO_USDT=1.27) — do not modify without user instruction",
}

# ─── INTENT PATTERNS ───────────────────────────────────────────────────────────

INTENT_PATTERNS = {
    "strategy_change": [
        "signal", "ema", "channel", "breakout", "rsi", "atr", "filter", "modify",
        "change", "add", "entry", "exit", "indicator", "crossover", "period",
        "window", "threshold", "parameter", "ema_fast", "ema_slow", "channel_n",
        "sl_mult", "rr", "risk", "direction", "regime", "bull", "bear",
    ],
    "backtest_request": [
        "backtest", "leverage", "equity", "trades", "win rate", "wr", "rr",
        "profit factor", "pf", "sharpe", "drawdown", "simulate", "result",
        "run", "test", "hawk_backtest", "walk-forward", "optimise", "optimize",
        "compound", "gbp", "return", "monthly", "annual",
    ],
    "paper_trade": [
        "paper", "live", "signal", "open", "close", "position", "entry",
        "exit", "trader", "hawk_paper_trader", "run", "start", "stop",
        "tick", "hourly", "dashboard", "equity", "state", "log",
    ],
    "data_fetch": [
        "fetch", "binance", "candle", "ohlcv", "download", "api", "csv",
        "data", "kline", "1h", "historical", "price", "load", "import",
    ],
    "code_fix": [
        "fix", "bug", "error", "exception", "crash", "traceback", "refactor",
        "format", "lint", "type hint", "import", "syntax", "keyerror",
        "attributeerror", "valueerror", "typeerror", "nameerror",
    ],
    "analysis": [
        "analyse", "analyze", "review", "performance", "trade log", "equity curve",
        "summary", "metrics", "win", "loss", "monthly", "profit", "result",
        "best", "worst", "compare", "regime",
    ],
    "planning": [
        "plan", "roadmap", "goal", "gbp 100k", "compound", "scale", "next",
        "improve", "feature", "build", "architecture", "phase", "live trading",
        "standalone", "separate", "repo", "deploy",
    ],
}

# ─── RULES PER INTENT ──────────────────────────────────────────────────────────

INTENT_RULES = {
    "strategy_change":  ["R1", "R3", "R9", "R4", "R11", "R12"],
    "backtest_request": ["R1", "R5", "R2", "R8", "R12"],
    "paper_trade":      ["R1", "R3", "R6", "R7", "R4", "R11"],
    "data_fetch":       ["R5", "R6", "R3"],
    "code_fix":         ["R1", "R3", "R12"],
    "analysis":         ["R5", "R8"],
    "planning":         ["R11", "R7", "R8", "R4"],
}

# ─── HELPERS ───────────────────────────────────────────────────────────────────

def classify_intent(prompt: str) -> str:
    prompt_lower = prompt.lower()
    scores: dict[str, int] = {intent: 0 for intent in INTENT_PATTERNS}
    for intent, keywords in INTENT_PATTERNS.items():
        for kw in keywords:
            if kw in prompt_lower:
                scores[intent] += 1
    best = max(scores, key=lambda k: scores[k])
    return best if scores[best] > 0 else "planning"


def read_context_summary(context_path: Path) -> str:
    if not context_path.exists():
        return "context.md not found — run paper trader at least once to initialise"
    lines = context_path.read_text(encoding="utf-8").splitlines()
    summary_lines = []
    in_identity = False
    in_strategy = False
    for line in lines:
        if "## Last Updated" in line:
            summary_lines.append(line)
        if "## Project Identity" in line:
            in_identity = True
        if in_identity and line.startswith("|"):
            summary_lines.append(line)
        if in_identity and line.startswith("## ") and "Identity" not in line:
            in_identity = False
        if "## Active Strategy" in line:
            in_strategy = True
            summary_lines.append(line)
        if in_strategy and "### Signal logic" in line:
            summary_lines.append(line)
        if in_strategy and "```" in line and len(summary_lines) > 5:
            in_strategy = False
    return "\n".join(summary_lines[:30])


def build_output(intent: str, rules: list[str], context_summary: str, prompt: str) -> str:
    rule_lines = "\n".join(
        f"  [{r}] {HARD_RULES[r]}" for r in rules if r in HARD_RULES
    )
    return f"""
╔══════════════════════════════════════════════════════════════╗
║  HAWK CRYPTO BOT — PROMPT OPTIMIZER                          ║
╚══════════════════════════════════════════════════════════════╝
Intent detected : {intent.upper()}

Context snapshot:
{context_summary}

Hard rules for this intent ({intent}):
{rule_lines}

Reminder — THE sizing formula (never change):
  qty = risk_usdt / sl_dist   (NOT multiplied by leverage)
  margin = qty × price / leverage

Original prompt:
{prompt}
══════════════════════════════════════════════════════════════════
""".strip()


# ─── MAIN ──────────────────────────────────────────────────────────────────────

def main() -> None:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip().startswith("{") else {}
        prompt = data.get("prompt", raw)
    except Exception:
        prompt = sys.stdin.read() if not raw else raw

    project_dir = Path(os.environ.get("CLAUDE_PROJECT_DIR", Path(__file__).parent.parent.parent))
    context_path = project_dir / "context.md"

    intent = classify_intent(prompt)
    rules = INTENT_RULES.get(intent, [])
    context_summary = read_context_summary(context_path)

    output = build_output(intent, rules, context_summary, prompt)
    print(output)


if __name__ == "__main__":
    main()

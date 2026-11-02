# claude-lean

**Call Claude from your own scripts through Claude Code on your subscription login, at ~600 input tokens per call instead of 429,000.**

If you script `claude -p` and turned tools off with `--tools ""`, you may be shipping every claude.ai connector's full
tool list with every call. `--tools ""` also removes ToolSearch, so connector tools load upfront instead of being
deferred. On one heavily-connected account, a one-line health check read **429,388 input tokens**.

Same question, same answer, same login, measured on Claude Code 2.1.312 (details in
[anthropics/claude-code#106413](https://github.com/anthropics/claude-code/issues/106413)):

| flags (all with `--tools ""`) | input tokens |
|---|---:|
| nothing else | **429,388** ← the trap |
| `--strict-mcp-config` | 2,854 |
| `ENABLE_CLAUDEAI_MCP_SERVERS=false` (env) | 2,854 |
| `--strict-mcp-config --mcp-config <one tiny server>` | 3,326 |
| **lean** (strict + `--disable-slash-commands` + env switches + your own system prompt) | **600** |
| lean + one tiny server | 1,072 |

Your numbers will differ with how many connectors your account has. The point is the ratio, and that nothing warns you.

## Install

```bash
pip install git+https://github.com/tomahawkeer/claude-lean
```

Needs Python 3.9+ and [Claude Code](https://docs.claude.com/en/docs/claude-code) installed and signed in with your
subscription. No other dependencies.

## Use

```python
from claude_lean import LeanClaude, TokenCeiling

claude = LeanClaude(model="sonnet")                       # finds the `claude` command itself
answer = claude.ask("Summarise this in JSON: ...", system="You are a terse analyst.")
print(claude.last_usage)    # {'tok_in': 600, 'tok_out': 23, 'cost_usd': 0.005, 'turns': 1, ...}

# web research still works; turns are capped
news = claude.ask("Today's news on NVDA?", tools="WebSearch,WebFetch", max_turns=20)

# a tripwire for silent bloat, checked per turn, one alert per day
guard = TokenCeiling({"analysis": 30_000, "research": 60_000},
                     state_path="data/token_ceiling.json",
                     on_alert=lambda flag, text: print("ALERT:", text))
guard.check("analysis", claude.last_usage)                # None when normal, a dict when over
```

Want to keep MCP servers you configured yourself and drop only the claude.ai connectors? Use
`LeanClaude(keep_own_mcp=True)`. Want exactly one server? Use `LeanClaude(mcp_config="one-server.json")`.

## What it enforces

Each of these rules came from a real mistake:

- **Never `--bare`.** It ignores the subscription login and needs an API key, which means separate billing.
- **`ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` are stripped** from every call's environment, so Claude Code can't
  quietly bill an API account instead of your plan.
- **The 429k trap is refused.** A `--tools` list without ToolSearch raises `ValueError` unless MCP loading is also
  restricted.
- **Every call runs in an empty temp folder**, so Claude Code doesn't read your project's `CLAUDE.md` files into every
  call.
- **Every call returns usage.** `--output-format json` gives back tokens and Claude Code's API-equivalent cost. On a
  subscription you pay nothing extra; the dollar figure is just a yardstick.

`build_command()` is pure (it runs nothing), so you can check the exact flags before trusting them:

```python
from claude_lean import build_command
argv, env = build_command("claude", model="opus")
```

## Why TokenCeiling

What Claude Code loads automatically is not stable. A new version or a newly connected app can add hundreds of
thousands of tokens to every call, and nothing tells you. `TokenCeiling` compares each call's input tokens **per turn**
against a ceiling for that kind of call. It works per turn because a 20-turn web search reads 200k in total by design,
while a bloated one-shot call reads 429k in one turn. It never blocks anything; what to do about a flag is up to you.

## Tests

```bash
pip install pytest && pytest -q
```

The tests never call the real Claude: the subprocess is replaced by a recorder, and every flag the measurements depend
on is pinned.

## Where it came from

It was lifted from [Day Desk](https://github.com/tomahawkeer/day-desk), a paper-trading experiment that asks Claude for
decisions through a subscription, where the per-call bloat first showed up on the usage meter.

MIT licensed.

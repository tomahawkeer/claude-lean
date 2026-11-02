"""claude_lean — call Claude from any script through Claude Code on your SUBSCRIPTION login, with the fewest tokens.

Lifted from Day Desk (https://github.com/tomahawkeer/day-desk), where every number below was MEASURED on a Windows PC
and posted on anthropics/claude-code#106413.

WHAT IT FIXES. Started the obvious way (`claude -p --tools ""` for a call that needs no tools), Claude Code loaded every
claude.ai connector's full tool list into EVERY call: a one-line health check read 429,388 input tokens. `--tools ""`
removes ToolSearch too, so connector tools load upfront instead of being deferred. Same answer, same login:

    flags (all with --tools "")                                         input tokens
    nothing else                                                            429,388   <- the trap
    --strict-mcp-config                                                       2,854   (no MCP servers at all)
    ENABLE_CLAUDEAI_MCP_SERVERS=false (env)                                   2,854   (only claude.ai connectors off)
    --strict-mcp-config --mcp-config <one tiny server>                        3,326   (+472 = that server's tools)
    LEAN (strict + --disable-slash-commands + LEAN_ENV + own system prompt)     600
    LEAN + one tiny server                                                    1,072

    Claude Code 2.1.312, subscription login, 2026-11-01 (row 1 from 2026-10-31). Your numbers depend on how many
    connectors your account has; the 429k row is what a heavily-connected account looked like.

RULES THIS MODULE ENFORCES (each one bought by a real mistake):
  * NEVER `--bare`. It ignores the subscription login and needs an API key (separate billing).
  * ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN are REMOVED from every call's environment, or Claude Code would bill the
    API account instead of the plan.
  * A `--tools` list without ToolSearch ("" included, "WebSearch,WebFetch" too) is refused unless MCP loading is also
    restricted (strict or the env switch): that combination is the 429k trap above.
  * Every call starts in an EMPTY temp folder, never the project folder. Claude Code reads every CLAUDE.md from its
    start folder upward; inside a project that feeds whole working agreements into each call.
  * Every call is `--output-format json`, so tokens + Claude Code's API-equivalent cost come back with the answer
    (`last_usage`). On a subscription the dollars are only a yardstick for how much plan a call ate.

Plus `TokenCeiling` (suggested on #106413): the set of things Claude Code auto-loads is not stable (a new version or
connector can add hundreds of thousands of tokens silently), so check every call's input tokens PER TURN against a
ceiling for its kind and say so loudly, once a day, the first time one blows through.

USAGE:

    from claude_lean import LeanClaude, TokenCeiling

    claude = LeanClaude(model="sonnet")                     # finds the claude command itself
    text = claude.ask("Summarise this: ...", system="You are a terse analyst. Answer in JSON.")
    print(text, claude.last_usage)                          # {"tok_in": ..., "cost_usd": ..., "turns": ...}

    web = claude.ask("Find today's news on NVDA", tools="WebSearch,WebFetch", max_turns=20)

    guard = TokenCeiling({"analysis": 30_000, "research": 60_000}, state_path="data/token_ceiling.json",
                         on_alert=lambda flag, text: print("ALERT", text))
    guard.check("analysis", claude.last_usage)              # None when normal; a dict when over the ceiling
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime

# ---- the measured lean settings (variant E of the token probe) ------------------------------------------------
# Kept as module constants, not buried in logic: a test pins them to the measured winner.
LEAN_ARGS = ["--strict-mcp-config", "--disable-slash-commands"]
LEAN_ENV = {
    "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",          # no CLAUDE.md files (belt and braces with the empty workdir)
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",         # no auto memory
    "CLAUDE_CODE_DISABLE_BUNDLED_SKILLS": "1",      # no bundled skills list
    "CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS": "1",    # no git section in the system prompt
    "CLAUDE_CODE_DISABLE_CFC_PROMPT": "1",          # no Chrome section in the system prompt
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",  # no update checks / telemetry round-trips
}
# Documented switch (code.claude.com/docs/en/mcp#disable-claude-ai-connectors): drops ONLY the claude.ai connectors and
# keeps any MCP servers you configured yourself. Measured 11-01: lands exactly where --strict-mcp-config does.
CONNECTORS_OFF_ENV = {"ENABLE_CLAUDEAI_MCP_SERVERS": "false"}
STRIPPED_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")   # present = Claude Code bills the API, not the plan
DEFAULT_TIMEOUT_S = 150         # a lean one-shot call answers in ~1-30 s; research with web tools can take minutes
WORKDIR_NAME = "claude-lean-calls"


def find_claude_cli() -> str | None:
    """Path to the Claude Code command on this PC, or None if it is not installed ."""
    for name in ("claude", "claude.cmd", "claude.exe"):
        p = shutil.which(name)
        if p:
            return p
    home = os.path.expanduser("~")
    for p in (os.path.join(home, ".local", "bin", "claude.exe"),
              os.path.join(home, "AppData", "Roaming", "npm", "claude.cmd")):
        if os.path.exists(p):
            return p
    return None


def call_workdir(name: str = WORKDIR_NAME) -> str:
    """An empty folder under the system temp directory: no project above it, so no CLAUDE.md gets read."""
    d = os.path.join(tempfile.gettempdir(), name)
    os.makedirs(d, exist_ok=True)
    return d


def unwrap_cli_json(out: str) -> tuple[str, dict | None]:
    """`claude -p --output-format json` prints one object: the answer in "result" plus usage and cost.
    Returns (answer text, compact usage). Anything else (older CLI, odd output) comes back as raw text with usage None,
    so a format surprise never breaks the caller. Raises if Claude Code itself reports an error."""
    try:
        o = json.loads(out)
    except (ValueError, TypeError):
        return out, None
    if not isinstance(o, dict) or o.get("type") != "result":
        return out, None
    if o.get("is_error") or "result" not in o:
        raise RuntimeError(f"Claude Code reported an error: {o.get('subtype') or ''} {str(o.get('result') or '')}"[:300])
    u = o.get("usage") or {}
    tin, tcw, tcr = (int(u.get(k) or 0) for k in
                     ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    usage = {"tok_in": tin + tcw + tcr, "tok_new": tin + tcw, "tok_cached": tcr,
             "tok_out": int(u.get("output_tokens") or 0),
             "cost_usd": round(float(o.get("total_cost_usd") or 0), 5),
             "secs": round((o.get("duration_ms") or 0) / 1000, 1), "turns": o.get("num_turns"),
             # NOT reliable for Claude Code's own WebSearch (a 20-turn search run reported 0). Use turns > 1.
             "searches": int(((u.get("server_tool_use") or {}).get("web_search_requests")) or 0)}
    return str(o["result"]), usage


def build_command(exe: str, *, model: str | None = None, tools: str = "", lean: bool = True,
                  keep_own_mcp: bool = False, mcp_config: str | None = None, system_file: str | None = None,
                  max_turns: int | None = None) -> tuple[list[str], dict[str, str]]:
    """The argv and the env overrides for one call. Pure: runs nothing, so tests can pin every flag.

    lean          the measured LEAN_ARGS + LEAN_ENV (default). False = plain Claude Code, but still never the trap.
    keep_own_mcp  drop only the claude.ai connectors (env switch) instead of every MCP server (--strict-mcp-config).
    mcp_config    path to an --mcp-config file: load exactly those servers (implies strict).
    """
    if keep_own_mcp and mcp_config:
        raise ValueError("pick one: keep_own_mcp (your configured servers) or mcp_config (exactly these servers)")
    argv = [exe, "-p", "--output-format", "json"]
    if model:
        argv += ["--model", model]
    argv += ["--tools", tools]
    if tools:
        argv += ["--allowedTools"] + [t.strip() for t in tools.split(",") if t.strip()]
    env: dict[str, str] = {}
    if keep_own_mcp:
        env.update(CONNECTORS_OFF_ENV)
        strict = False
    else:
        strict = lean or bool(mcp_config)
    if lean:
        argv += [a for a in LEAN_ARGS if strict or a != "--strict-mcp-config"]
        env.update(LEAN_ENV)
    elif strict:
        argv += ["--strict-mcp-config"]
    if mcp_config:
        argv += ["--mcp-config", mcp_config]
    if system_file:
        argv += ["--system-prompt-file", system_file]
    if max_turns:
        argv += ["--max-turns", str(int(max_turns))]
    if "--bare" in argv:                                  # cannot happen from the code above; pinned anyway
        raise ValueError("--bare needs an API key; never use it on the subscription")
    # The trap: any --tools list WITHOUT ToolSearch makes Claude Code load every MCP/connector tool upfront instead of
    # deferring it ("" included; so is "WebSearch,WebFetch"). Measured 429,388 input tokens for a one-line call.
    restricted = "--strict-mcp-config" in argv or env.get("ENABLE_CLAUDEAI_MCP_SERVERS") == "false"
    if "ToolSearch" not in tools and not restricted:
        raise ValueError(f'--tools "{tools}" leaves out ToolSearch, so without --strict-mcp-config or '
                         "ENABLE_CLAUDEAI_MCP_SERVERS=false every connector loads upfront (measured 429,388 input "
                         "tokens for a one-line call). Use lean=True (the default) or keep_own_mcp=True.")
    return argv, env


class LeanClaude:
    """One object per project. `ask()` is thread-safe; `last_usage` is per thread (research can run beside trading)."""

    def __init__(self, model: str | None = None, exe: str | None = None, timeout_s: int = DEFAULT_TIMEOUT_S,
                 lean: bool = True, keep_own_mcp: bool = False, mcp_config: str | None = None,
                 workdir_name: str = WORKDIR_NAME):
        self.exe = exe or find_claude_cli()
        if not self.exe:
            raise FileNotFoundError("Claude Code was not found on this PC (run its installer, then sign in with the "
                                    "subscription, not an API key)")
        self.model, self.timeout_s = model, timeout_s
        self.lean, self.keep_own_mcp, self.mcp_config = lean, keep_own_mcp, mcp_config
        self.workdir_name = workdir_name
        self._tl = threading.local()

    @property
    def last_usage(self) -> dict | None:
        """Tokens + API-equivalent cost of the last call made ON THIS THREAD (None if not reported)."""
        return getattr(self._tl, "usage", None)

    def ask(self, prompt: str, system: str | None = None, tools: str = "", max_turns: int | None = None,
            model: str | None = None, timeout_s: int | None = None) -> str:
        """Send one prompt, get the answer text. tools="" = no tools (the lean default); e.g. "WebSearch,WebFetch"
        for research. With no tools, `system` REPLACES Claude Code's coding-assistant prompt (the measured 600-token
        setup); with tools, Claude Code keeps its own prompt (it explains its tools) and `system` goes on top of the
        prompt text instead."""
        work = call_workdir(self.workdir_name)
        sys_file = None
        if system and not tools and self.lean:
            sys_file = os.path.join(work, f"system-{os.getpid()}-{threading.get_ident()}.txt")
            with open(sys_file, "w", encoding="utf-8") as f:
                f.write(system)
            full = prompt
        else:
            full = (system + "\n\n---\n\n" + prompt) if system else prompt
        argv, extra = build_command(self.exe, model=model or self.model, tools=tools, lean=self.lean,
                                    keep_own_mcp=self.keep_own_mcp, mcp_config=self.mcp_config,
                                    system_file=sys_file, max_turns=max_turns)
        env = {k: v for k, v in os.environ.items() if k not in STRIPPED_ENV}
        env.update(extra)
        self._tl.usage = None
        r = subprocess.run(argv, input=full, capture_output=True, text=True, encoding="utf-8",
                           timeout=timeout_s or self.timeout_s, env=env, cwd=work,
                           creationflags=0x08000000 if os.name == "nt" else 0)   # Windows: no console flash
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout or f"exit code {r.returncode}").strip()[:300])
        text, usage = unwrap_cli_json(r.stdout)
        self._tl.usage = usage
        return text


class TokenCeiling:
    """Flag a call whose input tokens PER TURN are far above its kind's normal size.

    ceilings     {kind: max input tokens per turn}. Per turn, because a 20-turn web-search run reads 200k+ in total by
                 design (~10k per turn) while a bloated one-shot call reads 429k in ONE turn. Set each ceiling well
                 above normal and well below the failure; Day Desk's: trading 30k (normal ~2.5k), review 40k (~3.5k),
                 self-test 10k (600), research 60k per turn (~10k).
    state_path   optional JSON file so "one alert per day" survives a restart. Written atomically.
    on_alert     called as on_alert(flag, text) for the FIRST flag of each local day only. Its errors are swallowed:
                 a broken alert must never break the call that triggered it.
    It never blocks anything. What to do about a flag is the caller's decision.
    """

    def __init__(self, ceilings: dict[str, int], state_path: str | None = None, on_alert=None, keep_per_day: int = 20):
        self.ceilings, self.state_path, self.on_alert, self.keep = dict(ceilings), state_path, on_alert, keep_per_day
        self._lock = threading.Lock()
        self._state = self._load()

    def _load(self) -> dict:
        if self.state_path and os.path.exists(self.state_path):
            try:
                with open(self.state_path, encoding="utf-8") as f:
                    s = json.load(f)
                if isinstance(s, dict):
                    return s
            except (OSError, ValueError):
                pass
        return {}

    def _save(self) -> None:
        if not self.state_path:
            return
        os.makedirs(os.path.dirname(os.path.abspath(self.state_path)), exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._state, f, indent=1)
        for i in range(20):                      # Windows refuses os.replace while a reader has the file open
            try:
                os.replace(tmp, self.state_path)
                return
            except PermissionError:
                time.sleep(0.1)
        os.replace(tmp, self.state_path)

    def check(self, kind: str, usage: dict | None, when: float | None = None, label: str = "") -> dict | None:
        """None if the call is normal (or unmeasured, or its kind has no ceiling); otherwise the flag dict."""
        ceiling = self.ceilings.get(kind)
        if not ceiling or not isinstance(usage, dict):
            return None
        turns = max(1, int(usage.get("turns") or 1))
        per_turn = (usage.get("tok_in") or 0) / turns
        if per_turn <= ceiling:
            return None
        when = when or time.time()
        day = datetime.fromtimestamp(when).date().isoformat()
        flag = {"kind": kind, "label": label, "tok_in": int(usage.get("tok_in") or 0), "turns": turns,
                "per_turn": round(per_turn), "ceiling": ceiling, "x": round(per_turn / ceiling, 1),
                "cost_usd": usage.get("cost_usd"), "t": when}
        with self._lock:
            st = self._state
            if st.get("day") != day:
                st.clear()
                st.update(day=day, count=0, items=[], alerted=False)
            st["count"] += 1
            st["items"] = (st["items"] + [flag])[-self.keep:]
            first = not st["alerted"]
            st["alerted"] = True
            try:
                self._save()
            except OSError:
                pass
        if first and self.on_alert:
            try:
                self.on_alert(flag, self.describe(flag))
            except Exception:
                pass
        return flag

    @staticmethod
    def describe(flag: dict) -> str:
        return (f"A {flag['kind']} call{(' (' + flag['label'] + ')') if flag.get('label') else ''} read "
                f"{flag['tok_in']:,} input tokens ({flag['per_turn']:,} per turn, {flag['x']}x the {flag['ceiling']:,} "
                f"ceiling). Something may be loading into every call again: check Claude Code's version and "
                f"connectors, then re-measure.")

    def today(self, when: float | None = None) -> dict:
        """{"count": n, "items": [...]} for today (empty if nothing was flagged today)."""
        day = datetime.fromtimestamp(when or time.time()).date().isoformat()
        with self._lock:
            if self._state.get("day") != day:
                return {"count": 0, "items": []}
            return {"count": self._state["count"], "items": list(self._state["items"])}

"""claude_lean: the lean subscription caller lifted from Day Desk, plus TokenCeiling.
No network and no real Claude: subprocess.run is replaced by a recorder. Every flag the measurements depend on is
pinned here, because one wrong flag is the difference between 600 and 429,388 input tokens per call."""
import json
import os
import subprocess
import sys
from datetime import datetime

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from claude_lean import core as cl                                 # noqa: E402

CLI_JSON = json.dumps({"type": "result", "result": '{"ok": true}', "total_cost_usd": 0.00525, "num_turns": 1,
                       "duration_ms": 1100, "usage": {"input_tokens": 600, "cache_read_input_tokens": 0,
                                                      "output_tokens": 23}})
BLOAT = 429_388


def record(monkeypatch, stdout=CLI_JSON, code=0):
    seen = []

    class R:
        returncode = code
        stderr = "boom" if code else ""

    R.stdout = stdout

    def run(args, **kw):
        seen.append({"args": list(args), "env": kw["env"], "input": kw["input"], "cwd": kw["cwd"]})
        return R()
    monkeypatch.setattr(subprocess, "run", run)
    return seen


# ---------------- build_command: the flags ----------------
def test_lean_default_is_exactly_the_measured_600_token_setup():
    argv, env = cl.build_command("claude", model="opus", system_file="s.txt")
    assert argv == ["claude", "-p", "--output-format", "json", "--model", "opus", "--tools", "",
                    "--strict-mcp-config", "--disable-slash-commands", "--system-prompt-file", "s.txt"]
    assert env == cl.LEAN_ENV and len(cl.LEAN_ENV) == 6


@pytest.mark.parametrize("kw", [dict(), dict(lean=False, keep_own_mcp=True), dict(mcp_config="one.json"),
                                dict(tools="WebSearch,WebFetch"), dict(lean=False, mcp_config="one.json")])
def test_never_bare_and_never_the_unrestricted_trap(kw):
    argv, env = cl.build_command("claude", **kw)
    assert "--bare" not in argv
    assert "--strict-mcp-config" in argv or env.get("ENABLE_CLAUDEAI_MCP_SERVERS") == "false"


@pytest.mark.parametrize("tools", ["", "WebSearch,WebFetch"])
def test_the_429k_trap_is_refused(tools):
    with pytest.raises(ValueError, match="429,388"):
        cl.build_command("claude", tools=tools, lean=False)


def test_tools_including_toolsearch_may_run_unrestricted():
    argv, _ = cl.build_command("claude", tools="ToolSearch,WebSearch", lean=False)
    assert "--strict-mcp-config" not in argv


def test_keep_own_mcp_drops_only_the_connectors():
    argv, env = cl.build_command("claude", keep_own_mcp=True)
    assert "--strict-mcp-config" not in argv and "--disable-slash-commands" in argv
    assert env["ENABLE_CLAUDEAI_MCP_SERVERS"] == "false"


def test_one_server_config_is_strict_plus_mcp_config():
    argv, _ = cl.build_command("claude", mcp_config="one.json")
    i = argv.index("--mcp-config")
    assert argv[i + 1] == "one.json" and "--strict-mcp-config" in argv
    with pytest.raises(ValueError):
        cl.build_command("claude", mcp_config="one.json", keep_own_mcp=True)


def test_research_tools_are_allowed_and_turns_capped():
    argv, _ = cl.build_command("claude", tools="WebSearch,WebFetch", max_turns=20)
    assert argv[argv.index("--tools") + 1] == "WebSearch,WebFetch"
    assert argv[argv.index("--allowedTools") + 1:argv.index("--allowedTools") + 3] == ["WebSearch", "WebFetch"]
    assert argv[argv.index("--max-turns") + 1] == "20"


# ---------------- LeanClaude.ask: what actually gets run ----------------
def test_ask_strips_api_keys_runs_in_an_empty_temp_folder_and_returns_usage(monkeypatch):
    seen = record(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-never-be-passed")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "also-not")
    c = cl.LeanClaude(model="opus", exe="claude")
    assert c.ask("hello", system="You are terse.") == '{"ok": true}'
    s = seen[0]
    assert "ANTHROPIC_API_KEY" not in s["env"] and "ANTHROPIC_AUTH_TOKEN" not in s["env"]
    assert s["env"]["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] == "1"
    assert os.path.dirname(s["cwd"]) == os.path.dirname(cl.call_workdir()) and not os.path.abspath(s["cwd"]).startswith(HERE)
    sf = s["args"][s["args"].index("--system-prompt-file") + 1]
    assert open(sf, encoding="utf-8").read() == "You are terse." and s["input"] == "hello"   # system REPLACES
    assert c.last_usage["tok_in"] == 600 and c.last_usage["cost_usd"] == 0.00525


def test_with_tools_the_system_text_rides_in_the_prompt(monkeypatch):
    seen = record(monkeypatch)
    cl.LeanClaude(exe="claude").ask("find news", system="You research.", tools="WebSearch,WebFetch")
    assert "--system-prompt-file" not in seen[0]["args"] and seen[0]["input"].startswith("You research.")


def test_failures_raise_and_clear_last_usage(monkeypatch):
    record(monkeypatch, code=1)
    c = cl.LeanClaude(exe="claude")
    with pytest.raises(RuntimeError, match="boom"):
        c.ask("x")
    assert c.last_usage is None
    record(monkeypatch, stdout=json.dumps({"type": "result", "is_error": True, "subtype": "error_max_turns"}))
    with pytest.raises(RuntimeError, match="error_max_turns"):
        c.ask("x")


def test_non_json_output_falls_back_to_text():
    assert cl.unwrap_cli_json("plain words") == ("plain words", None)
    text, u = cl.unwrap_cli_json(json.dumps({"type": "result", "result": "hi", "num_turns": 3, "usage": {
        "input_tokens": 10, "cache_creation_input_tokens": 5, "cache_read_input_tokens": 85, "output_tokens": 7}}))
    assert text == "hi" and u["tok_in"] == 100 and u["tok_new"] == 15 and u["tok_cached"] == 85 and u["turns"] == 3


def test_missing_claude_code_says_so(monkeypatch):
    monkeypatch.setattr(cl, "find_claude_cli", lambda: None)
    with pytest.raises(FileNotFoundError, match="subscription"):
        cl.LeanClaude()


# ---------------- TokenCeiling ----------------
def t(h, day=2):
    return datetime(2026, 11, day, h, 0).timestamp()


def test_normal_calls_pass_and_research_is_judged_per_turn():
    g = cl.TokenCeiling({"trading": 30_000, "research": 60_000})
    assert g.check("trading", {"tok_in": 2_500, "turns": 1}) is None
    assert g.check("research", {"tok_in": 208_545, "turns": 20}) is None       # ~10k per turn = normal
    assert g.check("research", {"tok_in": BLOAT * 20, "turns": 20})["per_turn"] == BLOAT
    assert g.check("unknown-kind", {"tok_in": BLOAT}) is None and g.check("trading", None) is None


def test_one_alert_per_day_survives_a_restart_and_resets_next_day(tmp_path):
    sent = []
    p = str(tmp_path / "state" / "ceiling.json")
    g = cl.TokenCeiling({"trading": 30_000}, state_path=p, on_alert=lambda f, s: sent.append(s))
    assert g.check("trading", {"tok_in": BLOAT, "turns": 1}, when=t(10), label="AMD")["x"] == 14.3
    g.check("trading", {"tok_in": BLOAT, "turns": 1}, when=t(11))
    assert len(sent) == 1 and "429,388" in sent[0] and "(AMD)" in sent[0]
    g2 = cl.TokenCeiling({"trading": 30_000}, state_path=p, on_alert=lambda f, s: sent.append(s))
    g2.check("trading", {"tok_in": BLOAT, "turns": 1}, when=t(12))
    assert len(sent) == 1 and g2.today(t(13))["count"] == 3                    # restart: still one alert today
    g2.check("trading", {"tok_in": BLOAT, "turns": 1}, when=t(10, day=3))
    assert len(sent) == 2 and g2.today(t(11, day=3))["count"] == 1 and g2.today(t(11))["count"] == 0


def test_a_broken_alert_never_raises():
    def boom(*a):
        raise RuntimeError("ntfy down")
    g = cl.TokenCeiling({"trading": 30_000}, on_alert=boom)
    assert g.check("trading", {"tok_in": BLOAT, "turns": 1})["kind"] == "trading"


def test_public_api_is_exported():
    import claude_lean
    assert claude_lean.LeanClaude is cl.LeanClaude and claude_lean.TokenCeiling is cl.TokenCeiling

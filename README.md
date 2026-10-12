# ogo-gw — Local Gateway for OpenCode Go

> **中文**：[README.zh-CN.md](README.zh-CN.md)

A tiny local gateway: translates the session header your client already sends
into the `x-opencode-session` header OpenCode Go requires, so **any client that
can target OpenCode Go** can talk to it — no protocol changes, no client swap.

## The problem

1. Every inference request to OpenCode Go must carry the `x-opencode-session` header. **No header → 400** (`MissingSessionID`). No negotiation.
2. Clients send all sorts of session headers (`x-session-affinity` from MiMoCode,
   `x-claude-code-session-id` from Claude Code, …) — but **none of them is the
   `x-opencode-session` the upstream wants**, and those clients' settings screens
   generally have no custom-header field to fix it.
3. The popular workaround is to **hardcode a constant** in config: it passes the
    400, but every conversation shares one cache slot and flushes it. Cached
    reads cost 31× less than input (`deepseek-v4-flash`: $0.22 vs $0.007 per
    million tokens), so a dead cache means paying full price at 31× — **buying
    pain with money**.

## The approach

Run a small local gateway (one file, pure standard library, zero dependencies)
and point your client's Base URL at it. **Change nothing else.**

The gateway only **translates** the session header — it never invents one:

- Client already sends `x-opencode-session` → **forwarded as-is**
- Client only sends `x-session-affinity` / `x-claude-code-session-id` etc. →
  **translated into** `x-opencode-session`
- **No session header at all → the gateway returns 400** with a message naming
  the headers it accepts (random ids or content hashes would pass the 400 but
  lose cache affinity and burn money at full price — so the gateway doesn't)
- Handled only where it matters (inference `POST`); `GET /models` and friends
  aren't touched — the upstream doesn't validate them

> We measured whether the upstream only recognizes the official `ses_<32hex>`
> shape as a cache key — it doesn't: 7/7 shapes hit the cache (2026-10-10,
> `deepseek-v4-flash`). So the value is passed through untouched; rewriting a
> client's session identity would be surprising for no benefit.

Result: the 400 is gone, and each conversation keeps its own warm cache. Not a
fake constant-header fix — a fix that actually preserves the cache.

## How to use

### 1. Start the gateway

Double-click `start-gateway.bat`, or run:

```bash
python gateway.py -v
```

Seeing `http://127.0.0.1:8787/v1` means it is up. Pure standard library —
no `pip install`.

> ⚠️ Keep `start-gateway.bat` ASCII-only + CRLF — **never add non-ASCII text**
> (`cmd.exe` reads bytes as GBK and splits things like `%PORT%` apart, which
> breaks the script).

### 2. Configure your client

Any client that can target OpenCode Go — three fields:

| Field | Value |
|---|---|
| Endpoint / Base URL | `http://127.0.0.1:8787/v1` (the local gateway, **not** `opencode.ai`) |
| API Key | Your OpenCode Go key — unchanged, passed through |
| Model ID | See "Pick a model" below |

Then **fully restart the client** — closing the window alone is usually not
enough; tray/background processes must exit too.

### 3. Pick a model

Whatever models OpenCode Go offers, you can use. With the gateway running, list
the live catalog directly:

```bash
curl http://127.0.0.1:8787/v1/models
```

**Suggested starting points**:

| Task | Use |
|---|---|
| Daily driver | `deepseek-v4-flash` (cheap, fast, best cache hits) |
| Hard tasks / big refactors | `deepseek-v4-pro` (strongest, expensive and slow) |
| Chinese docs / summaries | `glm-5.3-flash` |
| Batch jobs | `longcat-2.0` (effectively unlimited quota) |
| Long context | `kimi-k3` (great, but quota is tight) |

> A few models speak a different protocol than your client and will return a
> 400 protocol error — **just switch to another model**.

### 4. Verify it works

```bash
curl http://127.0.0.1:8787/v1/chat/completions \
  -H "Authorization: Bearer sk-your-key" \
  -H "Content-Type: application/json" \
  -H "x-session-affinity: ses_00000000000000000000000000000001" \
  -d '{"model":"deepseek-v4-flash","max_tokens":16,"messages":[{"role":"user","content":"hi"}]}'
```

> curl sends no session header of its own, so add one manually
> (`x-session-affinity` or `x-opencode-session`, any non-empty string works).
> **Without it you get the gateway's 400 `missing session header`.**

A `200` + completion means it works. The `-v` log looks like this when healthy:

```
→ POST /zen/go/v1/chat/completions  session=ses_ffe5eeafb0…
   "POST /v1/chat/completions HTTP/1.1" 200 -
```

Within one conversation `session=` stays the same (cache is working); it changes
only for a new conversation.

### MiMoCode setup

Edit `~/.config/mimocode/mimocode.json` (same path on Windows):

```jsonc
"provider": {
  "opencode-go": {
    "options": {
      "baseURL": "http://127.0.0.1:8787/v1",
      "apiKey": "sk-your-go-key"
    }
  }
}
```

After editing, **fully quit MiMoCode and restart** (exit the tray too).

Pick a model when launching:

```bash
mimo -m opencode-go/deepseek-v4-flash
```

Or type `/models` inside the TUI.

> ⚠️ MiMoCode's **ephemeral sessions send no session header**, so they are
> rejected by the gateway with a 400 (`missing session header`). Regular
> conversations send `x-session-affinity` and are unaffected.

### Daily startup

The gateway is not set up for auto-start — start it manually before each
session: double-click `start-gateway.bat`, or run `python gateway.py -v`.
Keep the window minimized; closing it cuts client connections.

### Troubleshooting

| Symptom | Cause |
|---|---|
| `missing session header` | The client sent no session header. Everyday clients (MiMoCode etc.) send one and never hit this; curl / custom scripts must add `-H "x-session-affinity: ses_xxx"` |
| Title generation / other small calls also hit `missing session header` | Auxiliary (ephemeral) calls **send no session header**, and `model_groups.lite` defaults to your default model (= through the gateway). Point lite at a non-gateway provider: `"model_groups": {"lite": "xiaomi/mimo-v2.6-flash"}` (main conversations are unaffected; only titles fall back to a first-sentence cut) |
| Still `MissingSessionID` | Gateway not running, or Base URL set to `https://opencode.ai/zen/go/v1` (it must be `http://127.0.0.1:8787/v1`) |
| 400 protocol error | Upstream serves three endpoint families (`/v1/chat/completions`, `/v1/messages`, `/v1/responses`) and the model doesn't match your client's protocol. Try another model. |
| Connection refused | Gateway not running. Run `curl http://127.0.0.1:8787/_health` first; JSON back means it is up |
| Slow first token | Normal — streaming is forwarded without buffering. If it suddenly got slow, buffering was introduced somewhere |

## License

MIT — see [LICENSE](LICENSE).

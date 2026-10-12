# ogo-gw — OpenCode Go 本地网关

> **English**：[README.md](README.md)

一个本地小网关：把客户端自带的会话头翻译成 OpenCode Go 要求的 `x-opencode-session`，让**任何能对接 OpenCode Go 的客户端**都能直连它，不改协议、不换客户端。

## 问题

1. OpenCode Go 每个推理请求都必须带 `x-opencode-session` 头，**不带就报 400**（`MissingSessionID`），没有商量余地。
2. 客户端们各自发的会话头五花八门（MiMoCode 发 `x-session-affinity`、Claude Code 发 `x-claude-code-session-id`、某些 CLI 发 `x-conversation-id`……），但**没有一个是上游认的 `x-opencode-session`**，而且这些客户端的设置界面普遍没有「自定义请求头」入口，改不了。
3. 网上流传的土办法是在配置里**写死一个常量**：能过 400，但所有对话共用一个缓存槽、互相冲刷。缓存读比输入便宜 31 倍（`deepseek-v4-flash`：$0.22 vs $0.007 每百万 token），缓存全废等于按 31 倍全价烧钱——**花钱买罪受**。

## 解决思路

本地起一个纯标准库的小网关（单文件，不用装任何依赖），客户端把 Base URL 指向它，**其他什么都不用改**。

网关只做**会话头翻译**，不编造会话身份：

- 客户端自带 `x-opencode-session` → **原样透传**
- 只带 `x-session-affinity` / `x-claude-code-session-id` 等 → **翻译成** `x-opencode-session`
- **一个会话头都不带 → 网关直接 400**，消息里写明要带哪个头（随机 id / 内容哈希都能过 400，但拿不到稳定缓存亲和，会按全价烧钱——所以网关不做）
- 只在需要的请求上处理（推理 POST）；`GET /models` 这类上游不校验，网关不打扰

> 我们实测过上游是不是只认官方形态 `ses_<32hex>` 当缓存键 —— 不是：7 种形态
> 全部命中（2026-10-10，`deepseek-v4-flash`）。所以原样透传；改写客户端的
> 会话身份只会让人意外，没有任何好处。

效果：400 消失；每个对话缓存独立、正常命中。不是写死常量那种假修，是真正保住缓存的修法。

## 使用说明

### 1. 启动网关

双击 `start-gateway.bat`，或命令行：

```bash
python gateway.py -v
```

看到 `http://127.0.0.1:8787/v1` 就是起来了。纯标准库，不用 `pip install`。

> ⚠️ `start-gateway.bat` 必须保持纯英文 + CRLF，**别往里加中文**（`cmd.exe` 按 GBK 读字节会把 `%PORT%` 这种变量名切碎，直接崩）。

### 2. 客户端配置

任何能对接 OpenCode Go 的客户端，改三样：

| 字段 | 填什么 |
|---|---|
| 接口地址 / Base URL | `http://127.0.0.1:8787/v1`（指本地网关，不是 `opencode.ai`） |
| API Key | 你的 OpenCode Go key，不用改，原样透传 |
| 模型 ID | 见下面「选模型」 |

改完**完全重启客户端**——只关窗口通常不生效，托盘/后台进程也要退。

### 3. 选模型

OpenCode Go 有什么模型，你就能用什么。网关跑着的时候直接列清单：

```bash
curl http://127.0.0.1:8787/v1/models
```

**起步推荐**：

| 场景 | 用哪个 |
|---|---|
| 日常主力 | `deepseek-v4-flash`（便宜快，缓存命中最好） |
| 难任务 / 复杂重构 | `deepseek-v4-pro`（最强，贵且慢） |
| 中文文档 / 总结 | `glm-5.3-flash` |
| 批量跑活 | `longcat-2.0`（额度几乎不限） |
| 长文本 | `kimi-k3`（强但额度紧） |

> 个别模型和你客户端的协议对不上会报 400 协议错误——**换一个模型就行**。

### 4. 验证通了

```bash
curl http://127.0.0.1:8787/v1/chat/completions \
  -H "Authorization: Bearer sk-你的key" \
  -H "Content-Type: application/json" \
  -H "x-session-affinity: ses_00000000000000000000000000000001" \
  -d '{"model":"deepseek-v4-flash","max_tokens":16,"messages":[{"role":"user","content":"hi"}]}'
```

> curl 没有自带的会话头，所以要手动给一个（`x-session-affinity` 或
> `x-opencode-session` 都行，任意非空字符串都可以）。**不带会收到网关的
> 400 `missing session header`**。

返回 200 + 内容就通了。`-v` 日志长这样就是正常工作：

```
→ POST /zen/go/v1/chat/completions  session=ses_ffe5eeafb0…
   "POST /v1/chat/completions HTTP/1.1" 200 -
```

同一段对话里 `session=` 应该不变（缓存生效）；换对话才变。

### MiMoCode 配置

编辑 `~/.config/mimocode/mimocode.json`（Windows 同路径）：

```jsonc
"provider": {
  "opencode-go": {
    "options": {
      "baseURL": "http://127.0.0.1:8787/v1",
      "apiKey": "sk-你的Go key"
    }
  }
}
```

改完**完全退出 MiMoCode 再开**（托盘也要退）。

用的时候指定模型：

```bash
mimo -m opencode-go/deepseek-v4-flash
```

或进 TUI 后输 `/models` 切换。

> ⚠️ MiMoCode 的**临时会话（ephemeral）不发会话头**，这类对话会被网关 400 拒
> （`missing session header`）。日常对话自带 `x-session-affinity`，不受影响。

### 日常启动

网关不进开机自启——每次用之前手动起：双击 `start-gateway.bat`，或 `python gateway.py -v`。
窗口最小化别关，关了客户端就连不上。

### 常见问题

| 现象 | 原因 |
|---|---|
| `missing session header` | 客户端没带任何会话头。日常客户端（MiMoCode 等）自带，不会遇到；curl/自建脚本要手动加 `-H "x-session-affinity: ses_xxx"` |
| 标题生成这类小调用也报 `missing session header` | 附属小调用（ephemeral）**不带会话头**，而 `model_groups.lite` 默认会落到你的默认模型（= 经网关）。把 lite 指到不经网关的 provider 即可：`"model_groups": {"lite": "xiaomi/mimo-v2.6-flash"}`（主对话不受影响，只是标题会退化成首句截断） |
| 子代理（如 checkpoint-writer）会被网关拒吗？ | 不会。它们**继承父会话**的会话头，自带 `x-session-affinity` → 网关正常放行。它们走网关、消耗 Go 额度；唯一风险是请求体随会话增长——**超长会话（~1.2MB 起）会撞上游体积限制并陷入重试** |
| 还是报 `MissingSessionID` | 网关没起，或 Base URL 填成了 `https://opencode.ai/zen/go/v1`（应该是 `http://127.0.0.1:8787/v1`） |
| 400 协议错误 | 上游有三种端点（`/v1/chat/completions`、`/v1/messages`、`/v1/responses`），模型和你客户端的协议对不上——换一个模型就行 |
| 连接被拒 | 网关没在跑。先 `curl http://127.0.0.1:8787/_health`，返回 JSON 就是好的 |
| 首字特别慢 | 正常，流式透传不缓冲。如果变慢了说明被改成缓冲了 |

## License

MIT，见 [LICENSE](LICENSE)。

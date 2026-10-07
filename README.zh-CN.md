# ogo-gw — OpenCode Go 本地网关

> **English**：[README.md](README.md)

一个本地小网关：补上 OpenCode Go 要求的请求头，让**任何能对接 OpenCode Go 的客户端**都能直连它，不改协议、不换客户端。

## 问题

1. OpenCode Go 要求每个请求都带 `x-opencode-session` 头，**不带就报 400**（`MissingSessionID`），没有商量余地。
2. 官方 opencode CLI 自动带这个头，但第三方客户端（MiMoCode 等大多数第三方客户端）普遍**没有「自定义请求头」的设置项**——这个头根本没地方填。
3. 网上流传的土办法是在配置里**写死一个常量**：能过 400，但所有对话共用一个缓存槽、互相冲刷。缓存读比输入便宜 31 倍（`deepseek-v4-flash`：$0.22 vs $0.007 每百万 token），缓存全废等于按 31 倍全价烧钱——**花钱买罪受**。

## 解决思路

本地起一个纯标准库的小网关（单文件，不用装任何依赖），客户端把 Base URL 指向它，**其他什么都不用改**。

网关在转发时补这个头：

- 客户端自己带了会话头 → **原样透传**
- 客户端没带 → 按「system prompt + 第一条用户消息」算出一个稳定 id → **同一段对话永远是同一个 id**

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
  -d '{"model":"deepseek-v4-flash","max_tokens":16,"messages":[{"role":"user","content":"hi"}]}'
```

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

### 日常启动

网关不进开机自启——每次用之前手动起：双击 `start-gateway.bat`，或 `python gateway.py -v`。
窗口最小化别关，关了客户端就连不上。

### 常见问题

| 现象 | 原因 |
|---|---|
| 还是报 `MissingSessionID` | 网关没起，或 Base URL 填成了 `https://opencode.ai/zen/go/v1`（应该是 `http://127.0.0.1:8787/v1`） |
| 400 协议错误 | 上游有三种端点（`/v1/chat/completions`、`/v1/messages`、`/v1/responses`），模型和你客户端的协议对不上——换一个模型就行 |
| 连接被拒 | 网关没在跑。先 `curl http://127.0.0.1:8787/_health`，返回 JSON 就是好的 |
| 首字特别慢 | 正常，流式透传不缓冲。如果变慢了说明被改成缓冲了 |

## License

MIT，见 [LICENSE](LICENSE)。

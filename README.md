# SIP 线路测试台

本地网页拨号面板，用来实测 SIP 线路质量：注册 → 拨号 → 真实通话（本机麦克风/扬声器）→ 实时看 RTT / 抖动 / 丢包 / MOS，通话记录可导出 CSV 对比不同线路。

- 底层 SIP / RTP 走 [baresip](https://github.com/baresip/baresip)（原生 UDP/TCP/TLS，G.711），不是 WebRTC，所以普通 SIP 中继 / 落地线路都能直接接。
- 后端 Python 标准库（无 pip 依赖），前端单页 HTML，浏览器只是控制面板。

## 启动

```bash
./run.sh            # 缺 baresip 会自动 brew install；默认 http://127.0.0.1:8790 并自动打开浏览器
./run.sh --port 9000 --no-open
```

**请在自己的终端里启动**（不要在 IDE 的隐藏终端里）：首次通话 macOS 会向启动它的终端 App 弹「麦克风」授权，拒绝或没弹出来就会单向无声。

## 使用

1. 左侧填线路给的：服务器地址（`host` 或 `host:port`）、用户名、密码；认证用户名 / 域与用户名 / 服务器不同时才填。
2. IP 鉴权线路（不用注册）：取消勾选「注册」，直接拨号。
3. 「连接并注册」→ 中间状态变「已注册」→ 输入号码 →「拨打」。通话中键盘 / 面板按键发 DTMF，「静音」验证对端是否有回声 / 底噪。
4. 右侧实时质量每 2 秒刷新；通话结束后该次汇总写入「通话记录」，可「导出 CSV」。
5. 单向无声 / 没声音时：勾选 STUN 再试；或勾选「SIP 信令跟踪」看日志里的 SDP 地址是否为内网地址。

## 指标口径

| 指标 | 来源 |
|---|---|
| RTT、对端接收抖动、对端报告丢包（上行） | 对端发来的 RTCP SR/RR；对端不发 RTCP 时不可用 |
| 本地接收抖动、本地丢包（下行）、收包 | 本机 RTP 接收统计（不依赖对端 RTCP） |
| 振铃时延 PDD / 接通时延 / 首包 RTP | 拨号命令到 180/183、200 OK、首个 RTP 包的本地时间差 |
| MOS | E-model 简化估算（G.711 无 PLC 假设），用 RTT/2 + 60ms 固定时延 + 下行丢包率计算，仅供横向比较 |

## 文件

- `server.py`：HTTP + SSE 接口，生成 `runtime/config` `runtime/accounts`，拉起 baresip 并通过 `ctrl_tcp`（127.0.0.1:4490）收发 JSON 命令 / 事件
- `static/index.html`：拨号面板
- `runtime/`：运行时生成（含账号密码明文与通话记录 `history.json`），已 gitignore

## 限制

- Homebrew 版 baresip 只带 G.711（PCMA/PCMU），没有 opus/G.722。
- 同一时刻只保持 1 路通话（`call_max_calls 1`）。
- 密码里含 `;` 会导致 accounts 解析错误（baresip 账号行格式限制）。

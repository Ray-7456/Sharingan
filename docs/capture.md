# 采集层（`sharingan record`）

把"人做一遍工作"的过程录下来，形成一个**会话目录**：屏幕帧 + 键鼠事件 + 窗口上下文。
这是分析层的输入（见 [design.md](design.md) 第 2 阶段）。

## 为什么不录 30fps 的视频

三个理由：

1. **信息在"操作发生的那一刻"**——点之前是什么画面、点之后变成什么，这两张就够了，
   中间没有信息；30fps 只会灌进大量几乎相同的帧；
2. **体积**：1080p 未降采样的帧是 8MB/张，30fps 一分钟就上百 GB；按事件抓帧 + 去重后
   通常只有几十张；
3. **可理解性**：分析阶段要的是"动作 ↔ 画面变化"的配对，而不是一段视频。

所以抓帧策略是三者叠加：

| 策略 | 说明 |
|---|---|
| **事件门控** | 点击、滚轮、按键按下、窗口切换之后抓一帧。上一张已存帧天然就是"操作前"的画面，两张合起来就是一次变化 |
| **定时兜底** | 每 `1/fps` 秒查一次（默认 2fps），画面有明显变化才存 |
| **去重** | 与上一张**已存帧**比较 32×32 灰度指纹，差异小于阈值就丢弃——光标闪烁、时钟跳字不该产生新帧 |

## 会话目录格式

```
session/
  meta.json           元信息：状态、开始/结束时间、平台、屏幕尺寸、参数、统计
  events.jsonl        事件流，每行一个 JSON
  frames/000000.png   屏幕帧（PNG，可降采样）
  frames/index.jsonl  帧索引
```

`meta.json` 在开始时先写一份（`status: recording`），正常结束再补上最终统计
（`status: complete`）——中途崩溃的会话不会被误当成完整数据。

**事件字段**（`events.jsonl`）：

| 字段 | 说明 |
|---|---|
| `ts` | 会话相对秒数 |
| `kind` | `mouse_down` / `mouse_up` / `mouse_move` / `mouse_wheel` / `key_down` / `key_up` / `focus` / `key_masked` |
| `x` `y` | 屏幕绝对坐标（鼠标类事件） |
| `button` | `left` / `right` / `middle` |
| `key` | 键名，如 `A`、`ENTER`、`F5`；不认识的退化为 `vk_<码>` |
| `delta` | 滚轮增量 |
| `window` | 事件发生时的前台窗口（`进程名｜窗口标题`） |

**帧索引字段**（`frames/index.jsonl`）：`index`、`ts`、`reason`（`tick` / `mouse_down` /
`key_down` / `focus` …）、`file`、`width`、`height`、`digest`（32×32 灰度指纹的十六进制）。

## 平台支持

| 平台 | 屏幕 | 输入 | 前台窗口 | 状态 |
|---|---|---|---|---|
| **Windows** | GDI `BitBlt` | `WH_MOUSE_LL` / `WH_KEYBOARD_LL` | `GetForegroundWindow` | **已实现**（本机实测通过） |
| macOS | ScreenCaptureKit（需「屏幕录制」权限） | `CGEventTap`（需「辅助功能」权限） | `NSWorkspace` | 待实现，要点见 `platforms/macos.py` |
| Linux | X11 `XGetImage`；Wayland 需 Portal | X11 `XRecord`；Wayland 需 evdev | X11 `_NET_ACTIVE_WINDOW` | 待实现，要点见 `platforms/linux.py` |

实现状态由 `sharingan record --probe` 报告：它会真的装一次输入钩子再卸掉，
并读出屏幕尺寸，用来确认这台机器允不允许录制。

## 隐私

- **会话包含屏幕画面与操作记录，属于敏感内容**：`sessions/` 与 `local/` 都已写进
  `.gitignore`，绝不入库（见 README「数据安全红线」）；
- `--no-keys` 完全不记录按键；前台窗口标题命中敏感词（登录/密码/credential…）时，
  按键只记 `key_masked` 的次数、不记键名——这是**启发式，不是保证**，
  真正敏感的输入请用 `--no-keys`；
- 采集层没有任何网络代码：数据只落在本机磁盘上，是否外发完全由你决定。

## 用法

```bash
python -m sharingan record --probe                       # 先确认这台机器能录
python -m sharingan record --seconds 300                 # 录 5 分钟，输出到 sessions/<时间戳>/
python -m sharingan record --scale 2 --fps 1             # 长会话：长宽减半、1fps，体积小很多
python -m sharingan record --region 0,0,1280,720         # 只录一块区域（比如只看某个软件窗口）
python -m sharingan record --no-keys                     # 完全不记按键
```

录制期间正常做一遍你想自动化的工作即可；按 `Ctrl+C` 可以提前结束。

## 已知限制

- **只录像素，不理解语义**——"他到底在干什么"是分析层的事，这一层不做猜测；
- **多显示器**：默认抓主显示器全屏，也可以用 `--region` 指定；跨屏拖动不会生成独立事件；
- **中文输入法**：事件流记的是按键（拼音字母），不记输入法上屏的汉字；
- **未做视频编码**：帧是逐张 PNG，长会话请配合 `--fps 1`、`--scale 2` 使用；
- **Wayland**：Linux 上普通用户既抓不了屏也读不到全局按键，需要 Portal 授权与
  `input` 组权限，实现前会先在 `--probe` 里明确告知不可用。

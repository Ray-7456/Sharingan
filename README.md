# Sharingan（写轮眼）

> 看懂一次，复制下来。

Sharingan 要做的事：**把一个人做过一遍的重复性数字工作，变成以后不用人做的工具。**

名字取自《火影忍者》的写轮眼 —— 看一眼就复制对方的忍术。放到这个项目里是三件事：读懂规则、复制用户的真实操作、生成能替代他的自动化工具。

当前状态：**设计阶段**，v0.1 只围绕一个真实用例展开（见下）。

## 它不是什么

- 不是"把录屏转成点击回放"的 RPA。回放点击最脆弱，界面或规则一变全废。
- 不是"什么都能自动化"的通用 Agent。多数重复性工作的最优解是一段确定的脚本，而不是每次现推的模型。
- 不是纯视频理解项目。视频只是信息通道之一。

## 工作方式

| 阶段 | 输入 | 产出 |
|---|---|---|
| 1. 采集 | 录屏视频（建议配语音讲解）、操作事件、涉及的文件 | 带时间轴的原始记录 |
| 2. 分析 | 原始记录 + 直接解析相关文件（xlsx / csv / 系统导出） | 工作流说明 + 歧义清单 + 自动化可行性分级 |
| 3. 澄清 | 只问分析后仍无法确定的问题，带证据逐条问 | 固化的规则文件 `rules.json` |
| 4. 生成与运行 | `rules.json` | 可执行工具：首次 dry-run 报告 → 人工确认 → 之后静默运行 |

四条核心纪律：

1. **视频只负责意图，规则来自文件解析与澄清问答。** 公式、阈值、条件格式、隐藏列在屏幕上看不出来，必须直接读文件。
2. **运行期零提问、零模型调用。** 问答只发生在构建期，且只发生一次。用户的每个回答都固化成规则，不是留在聊天记录里。
3. **不猜。** 遇到规则未覆盖的情况，停机并输出升级报告（含证据），由人决定；绝不静默跳过或猜测。
4. **变更走 Agent。** 需要改规则时，由 Agent 修改 `rules.json` → 新版本 + diff → 用历史数据回归验证 → 通过才生效，可回滚。

详细设计见 [docs/design.md](docs/design.md)。

## 快速上手

```bash
# 生成合成测试数据（GBK / CRLF / 不补零时间，含 5 类注入异常与真值清单）
python -m sharingan fixtures make --out fixtures

# 校验规则文件（--draft 放宽 source 要求）
python -m sharingan rules validate examples/rules.state-inspection.draft.json --draft

# 查看数据文件概况；带 --rules 时按规则统计超限
python -m sharingan inspect fixtures/synthetic_week.csv --rules examples/rules.fixture-demo.json

# dry-run：算出该改哪些单元格、改成什么（只读，不写文件）
python -m sharingan plan fixtures/synthetic_week.csv --rules examples/rules.fixture-demo.json

# 执行写入：先备份、再改数据、最后落变更日志（必须 --yes 确认）
cp fixtures/synthetic_week.csv local/demo.csv     # 演示用副本
python -m sharingan apply local/demo.csv --rules examples/rules.fixture-demo.json --yes

# 图形界面（可选依赖 PySide6）：数据概况 / 规则编辑 / 变更清单复核 三个页签
pip install "sharingan[ui]"     # 或 pip install PySide6
python -m sharingan ui fixtures/synthetic_week.csv --rules examples/rules.fixture-demo.json
```

跑测试：`python -m unittest discover -s tests -t .`

核心运行时零第三方依赖（纯标准库），图形界面是可选依赖。Python 3.10+。

## MVP 范围（v0.1）

只做一个真实用例：**状态检修周数据处理** —— 原始 CSV 数据导入分析模板、模板高亮异常、人工修正、回写原始数据。用例定义、模板机制与验收标准见 [docs/case-state-inspection.md](docs/case-state-inspection.md)。

v0.1 明确不做：通用录屏理解、跨用例泛化、图形界面（先做 CLI；将来的界面采用本地 Web，见下）、多用户与权限、云端部署。

## 跨平台

Windows / macOS / Linux 三平台通用，约束如下：

- **核心零平台耦合**：格式读写、规则校验、公式求值、夹具生成全部是纯标准库实现，路径一律走 `pathlib`，换行符运行时探测，编码自动识别（GBK / UTF-8 BOM）。这条线由 `tests/test_platform_neutrality.py` 扫描源码守住，禁止 `winreg`、`ctypes.windll`、盘符路径、`shell=True` 等写法。
- **平台相关代码只能进适配器**：只有采集层（录屏、键鼠事件、辅助功能树）天然依赖操作系统，未来放在 `sharingan/platforms/` 下按平台分文件，对上层暴露统一接口。
- **UI 用 PySide6（Qt for Python）**：普通用户要的是双击即用的桌面程序，不引入浏览器、端口和防火墙的麻烦。PySide6 是 LGPL 许可，Windows / macOS / Linux 上都是原生窗口，控件足以承载表格、截图与 diff 复核。它作为**可选依赖**安装（`pip install "sharingan[ui]"`），未安装时命令行照常可用；界面层只渲染核心算出的结果，自己不含任何规则判断逻辑。启动时会自动挑一个系统中文字体，Linux 上缺字体时会提示安装 `fonts-noto-cjk`。

## 与现有工作的关系

- [OpenAdapt](https://github.com/OpenAdaptAI/OpenAdapt)：把演示编译成确定性程序，健康运行零模型调用。理念最接近，但它面向 GUI 操作复现。
- [browser-use/workflow-use](https://github.com/browser-use/workflow-use)：浏览器录制转确定性工作流，失败时回退 Agent。
- [screenpipe](https://github.com/screenpipe/screenpipe)：本地持续录屏与检索记忆。

Sharingan 的差异：面向 Office/工业数据报表场景，产物不是"界面点击回放"，而是**基于文件解析重建的数据处理工具**；澄清问答与规则版本化是一等公民。

## 目录结构

```
sharingan/           核心包（纯标准库，无平台耦合）
  formats/           现场文件格式的忠实读写（GBK / CRLF / 不补零时间 / 未命名尾列）
  rules/             rules.json 校验 + 安全公式求值（禁用 eval）
  analysis/          与界面无关的统计与分析、变更计划与写盘（CLI 与 GUI 共用同一份口径）
  fixtures/          合成数据生成（异常注入 + 真值清单）
  ui/                跨平台图形界面（PySide6，可选依赖）
  assets/icons/      应用图标（16–256 六档 + Windows .ico；512/1024/.icns 按需生成，见「图标规范」）
  cli.py             命令行入口
tests/               单元测试（unittest，119 用例，含界面离屏冒烟、修复引擎与图标资源测试）
examples/            规则文件示例
tools/               开发工具（make_icons.py 生成图标）
assets/              图标源图
docs/                设计说明与用例规格
fixtures/            生成的合成数据（可随时重建，禁止放真实数据）
local/               真实数据与私有基线（已 gitignore，不入库）
```

## 路线图

- [x] 用例规格、模板机制分析与格式事实确认
- [x] 格式模块：字节级忠实的读写（真实文件往返已验证）
- [x] 合成夹具：5 类异常注入 + 真值清单
- [x] 规则文件 `rules.json` 校验器 + 安全公式求值
- [x] 分析层与首个图形界面：数据与规则查看器（PySide6，三平台通用）
- [x] 应用图标：全套尺寸 + 三平台格式，界面已接入
- [x] dry-run 变更清单与执行写入（先备份、后改数、落变更日志；停机即拒写）
- [x] 规则编辑页（阈值 / 修复动作 / 参数，保存前自动校验）+ 变更清单复核页
- [ ] 图形界面补全：澄清问答（带证据逐条提问，答案写入规则）
- [ ] 打包为双击可执行程序（PyInstaller，三平台各自打包）
- [ ] 分析器：从数据与模板产出歧义清单、可行性分级
- [ ] 生成器与运行时（dry-run 报告、静默运行、升级报告）
- [ ] 状态检修用例端到端验收（第 2 次运行零提问、零模型调用）
- [ ] 采集层适配器（Windows / macOS / Linux）
- [ ] 第二个用例验证泛化能力

## 图标规范

**所有由本项目生成的自动化工具，统一使用同一枚图标（写轮眼）。**

- **入库资源**（`sharingan/assets/icons/`，约 0.6 MB）：16 / 32 / 48 / 64 / 128 / 256 六档 PNG，加 Windows `sharingan.ico`（含 16–256 共 7 档）。图形界面按窗口尺寸自动挑选合适档位，Windows 打包直接用 `.ico`。
- **大档位按需生成**：512 / 1024 PNG 与 macOS `.icns` 合计约 4 MB，默认不入库（已在 .gitignore 中排除）。macOS 打包或需要高清素材时执行一次：

  ```bash
  python tools/make_icons.py --source assets/icon-source.jpg --full
  ```

- **源图**：`assets/icon-source.jpg`（2400×1309，为控制体积做过降采样，仍足以再生成到 512 档）。重新生成默认档位需要 Pillow（`pip install "sharingan[tools]"`）：

  ```bash
  python tools/make_icons.py --source assets/icon-source.jpg
  ```

- **为什么需要专门的生成工具**：源图是 JPG，四周的"透明格"其实是画上去的棋盘格，而且图标外圈有一圈很宽的发光晕与棋盘格混叠——任何单一判据（只看颜色或只看纹理）都会误判。工具会自动测量图标几何（径向色度边界 → 半边长与圆角半径），并自校验地把光晕混合带切掉，抠成真正的透明圆角；每一档尺寸单独重打遮罩，避免缩放溢色。

## 数据安全红线

本仓库为**公开仓库**，以下内容一律不得提交：

- 现场导出的原始数据（CSV / Excel）、录屏、截图；
- 厂站名称、机组编号、设备编号等可定位真实资产的信息；
- 任何个人信息。

真实数据只放在本地 `local/` 目录（已在 .gitignore 中排除）。仓库内测试一律使用 `fixtures/` 下的合成数据。

## 路线图

- [ ] v0.1 用例规格与合成夹具
- [ ] 分析器：文件解析 + 歧义清单产出
- [ ] 规则文件 `rules.json` 与校验器
- [ ] 生成器与运行时（dry-run 报告、静默运行、升级报告）
- [ ] 状态检修用例端到端验收
- [ ] 第二个用例验证泛化能力

## License

[Apache License 2.0](LICENSE) © 2026 Ray-7456

# 🎯 AI Job Hunter (自动求职与智能联络智能体)

一套专为技术求职、兼职接单打造的**全天候自动化智能体系统**。

> 💡 **通用性说明**：本项目**不依赖任何特定的 AI 客户端**！  
> 无论你使用的是标准的终端命令行（CMD / PowerShell / Bash）、普通的 Python 脚本环境，还是任意 AI 编码助手（如 Cursor、Claude Code、VSCode、Windsurf、Antigravity 等），均可**开箱即用、一键运行**。

---

## ⚡ 为什么普通爬虫容易封号，而本项目天然抗封？

市场上大部分基于 Selenium / Playwright 的自动投递脚本，因为存在明显的 WebDriver 自动化指纹特征，通常运行 1~2 天就会触发平台的滑块验证码甚至直接封号。

**本项目的核心技术突破**：
1. **零特征原生 CDP 驱动**：基于 Python 原生标准库自主实现 Chrome DevTools Protocol (CDP) 驱动客户端，**零 WebDriver 指纹特征**；
2. **真实浏览器直接挂接**：直接挂载在你日常使用的真实 Chrome 浏览器调试端口上，完美复用已登录会话、真实 Cookie 与用户行为环境，平台检测到的完全是真人正常浏览；
3. **50~95 字自然去 AI 味短句**：严格控制首条沟通在短句口语化范围，一条消息只讲一件真实做成的事，杜绝模板化长篇公文套话，回复率极高；
4. **防偷跑物理网络熔断**：默认由纯本地规则与 DOM 状态驱动（零 Token 消耗、零外部网络发包），绝不静默消耗你的商业 API 余额。

---

## 🚀 三步极速上手指南

### 第一步：获取代码并安装依赖
本项目基于 Python 3.10+，仅依赖少量基础库：

```bash
# 1. 克隆本仓库
git clone https://github.com/tomybelovedxin-pixel/ai-job-hunter.git
cd ai-job-hunter

# 2. 安装必要依赖
pip install -r requirements.txt
```

### 第二步：一键双击启动求职浏览器（搞定端口与登录）
为了让普通用户无需手动敲复杂端口命令，本项目提供了**开箱即用的一键启动脚本**：

- **Windows 用户**：直接双击运行根目录下的 **`start_browser.bat`**
- **macOS / Linux 用户**：在终端运行 **`bash start_browser.sh`**

> **脚本做了什么？**  
> 脚本会自动探测你系统里的 Chrome 安装位置，以独立求职目录自动绑定调试端口（9222）并打开求职网站。  
> **你只需在弹出的 Chrome 窗口中正常登录你的求职账号，并保持窗口打开即可！**

### 第三步：开始使用（根据你的习惯自由选择）

#### 💻 方式 A：纯终端 / 命令行直接跑（最简单，无需任何 AI）
直接在你的终端中运行 Python 命令：

```bash
# 1. 扫描匹配最新的目标岗位（支持按关键词与城市筛选）
python -m jobagent scan --query "Python兼职" --city "全国"

# 2. 为匹配的高分岗位一键生成定制简历 (DOCX / PDF / Markdown)
python -m jobagent resume --job-id <ID>

# 3. 启动全天候后台实时监听与自动响应守护进程
python tools/boss_chat_daemon.py
```
> **守护进程在做什么？**  
> 守护脚本会在后台每隔 6 秒扫描一次聊天界面：一旦捕获到 HR 发来的新回复或新需求，第一时间记录并提醒；若检测到 HR 索要附件简历卡片，会自动点击【同意】完成秒级投递。

#### 🤖 方式 B：接入你常用的任何 AI 助手或 IDE
如果你日常使用 **Cursor / Claude Code / VSCode Copilot / Windsurf / Antigravity** 等任何 AI 编程工具：
只需在对应工具中打开本项目工作区，即可直接用**大白话中文**对智能体下达指令：
- *“帮我分析一下刚才抓到的前 5 个岗位适不适合投递”*
- *“给实现网的负责人生成一条 80 字以内的自然打招呼话术”*
- *“看看刚刚有没有 HR 发来新回复”*
智能体会自动调度底层的 CDP 适配器与决策引擎完成动作。

---

## ⚙️ 个性化配置说明

复制示例模板文件为正式配置文件，填入你自己的求职意向：
```bash
cp config.example.toml config.toml
cp profile.example.json profile.json
cp master_resume.example.json master_resume.json
```

- **`config.toml`**：配置你的搜索关键词、意向城市、最低薪资等；
- **`profile.json`**：填入你的擅长技能、技术方向以及不想投递的黑名单公司/岗位；
- **`master_resume.json`**：填入你的真实项目经历与可落地的代码成果（系统会自动根据 JD 动态裁剪生成专属单页简历）。

---

## 🛡️ 常见问题 (FAQ)

**Q：提示 `❌ 未检测到正在运行的 Chrome 调试端口 (9222)` 怎么办？**  
A：说明 Chrome 尚未以调试模式打开。直接双击运行项目根目录的 `start_browser.bat`（或 `bash start_browser.sh`），看到弹出的浏览器后再运行命令即可。

**Q：运行会不会消耗我的大模型 API 余额？**  
A：**绝对不会！** 系统的岗位扫描、DOM 对比、消息监听和话术生成默认全部由纯本地规则与离线状态机执行。底层配备了物理网络熔断器，没有你的显式许可绝不向公网计费接口发包。

---

## ⚖️ 免责声明 (Disclaimer)

本项目仅供个人求职辅助、自动化测试与效率提升技术研究使用。请使用者合理设置检索与沟通频次，遵守各平台服务守则，严禁用于任何高频骚扰、批量刷单或商业滥用行为。

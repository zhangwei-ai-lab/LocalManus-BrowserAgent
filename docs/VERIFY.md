# 实跑记录与验证证据

环境：Windows · Python 3.13.14（venv: `C:\Users\huangan\venvs\openmanus`）
模型：Ollama `qwen3-8b-local:latest`（8.2B / Q4_K_M / 40960 ctx / tools）
记录时间：2026-10-29 16:24 ~ 18:10
项目路径：`C:\Users\huangan\Desktop\项目开发夹\LocalManus-BrowserAgent`

---

## 0. 环境自检

```bash
python scripts/check_env.py
```

```
[OK  ] Python 版本 — 3.13.14
[OK  ] 核心依赖 — 15 个全部可导入
[OK  ] pydantic 版本 — 2.10.6
[WARN] 可选依赖 crawl4ai — 未安装（不影响主流程）
[WARN] 可选依赖 browsergym — 未安装（不影响主流程）
[WARN] 可选依赖 datasets — 未安装（不影响主流程）
[OK  ] Playwright Chromium — ...\ms-playwright\chromium-1161\chrome-win\chrome.exe
[OK  ] 本地 Ollama — http://localhost:11434 共 3 个模型
        - qwen3-embedding:0.6b                   595.78M  [tools,thinking,embedding]
        - qwen2.5-coder-7b-local:latest          7.6B     [tools,completion]
        - qwen3-8b-local:latest                  8.2B     [tools,thinking,completion]
[OK  ] 推荐 Agent 主脑 — qwen3-8b-local:latest
[OK  ] 模型来源 — local / qwen3-8b-local:latest
[OK  ] 视觉策略 — 本地无视觉模型，已降级为纯文本浏览器模式
[OK  ] config/config.toml — 存在
[OK  ] workspace 目录 — ...\LocalManus-BrowserAgent\workspace
[OK  ] 工具集 — python_execute, browser_use, web_search, str_replace_editor, ask_human, terminate
==================================================================
 结果：FAIL=0  WARN=3  OK=11
==================================================================
```

结论：本地模型优先策略生效，启动即用 `qwen3-8b-local`，全程没有调用云端 API。

---

## 1. 端到端用例（脚本自动记录）

```bash
python scripts/verify.py
```

```
==============================================================================
用例        结果      步数      耗时        产物
------------------------------------------------------------------------------
write     PASS    2/6     15.0s     verify_write.md
browser   PASS    4/8     49.3s     verify_browser.md
==============================================================================
```

产物内容：

```markdown
# workspace/verify_write.md
ReAct循环由推理（Reasoning）、行动（Action）、反馈（Feedback）三个阶段组成。

# workspace/verify_browser.md
提取的H1标题文字：Example Domain
```

`browser` 用例的 4 步轨迹：`go_to_url(example.com)` → `extract_content(h1)` → `str_replace_editor(create)` → `terminate`，与预期一致，没有多余的试错。

---

## 2. 真实站点任务：python.org 的四次尝试

同一句 prompt、同一个模型，因为中途改了两次代码，留下四次运行记录。这张表本身就是「小模型 + 真实站点」的稳定性剖面：

| 次序 | 代码状态 | 步数 | 耗时 | 产物 |
| --- | --- | --- | --- | --- |
| #1 | 原始（无循环保护） | 10 / 10 | 273 s | ❌ 无（连续 5 次 `go_to_url` 同一 URL） |
| #2 | + 循环保护 | 10 / 10 | 275 s | ✅ H1 + 4 个导航项 |
| #3 | + 循环保护 | 4 / 8 | 87 s | ❌ 模型主动 `terminate(status=failure)` |
| #4 | + 页面正文注入 | 8 / 8 | 282 s | ✅ H1 + 6 个导航项 |

### 2.1 #1 空转：10 步预算全烧在同一个动作上

```
step 5/10  Tool arguments: {"action":"go_to_url","url":"https://www.python.org"}
step 6/10  Tool arguments: {"action":"go_to_url","url":"https://www.python.org"}
step 7/10  Tool arguments: {"action":"go_to_url","url":"https://www.python.org"}
step 8/10  Tool arguments: {"action":"go_to_url","url":"https://www.python.org"}
step 10/10 Tool arguments: {"query":"https://www.python.org h1 tag","num_results":1}
```

10 步耗尽，273 s，`workspace/python_org.md` 不存在。

### 2.2 #2 加了循环保护：有产物，但仍磨到步数耗尽

```
step 3/10  str_replace_editor(create) → File created successfully at: ...\workspace\python_org.md
step 4..9  自我修正（insert / str_replace / view）
step 10/10 步数耗尽，未调用 terminate
```

产物正确（`Welcome to Python.org` + `Python / PSF / Docs / PyPI`），但模型在收尾阶段反复打磨文件。

### 2.3 #3 同一任务复跑：模型直接承认失败

```
连续 3 次相同 go_to_url → 注入换策略提示
step 4/8  terminate(status=failure)
```

4 步、87 s 结束——**没有产物，但也没有烧满预算**。这就是循环保护想要的形状：失败要快、要能解释。

### 2.4 #4 补上页面正文注入：8 步拿到完整结果

`get_current_state()` 现在会把页面正文压成 1200 字符一起喂给模型，模型不再怀疑「页面没加载」。

产物 `workspace/python_org.md`：

```markdown
Welcome to Python.org
Python
PSF
Docs
PyPI
Jobs
Community
```

**读法**：#2 和 #4 说明浏览器阶段本身没问题（3 步内就能拿到页面信息），真正的变量是「模型手里有没有正文」；#3 和 #4 的对比说明——**信息补齐以后，同一个模型的表现从「放弃」变成「完成」**。收尾啰嗦（不主动 `terminate`）仍然存在，靠 `max_steps` 兜底。

---

## 3. Web UI（SSE 流式）

```bash
python app.py     # → http://localhost:5172
```

```bash
$ curl http://127.0.0.1:5172/health
{"status":"ok","model":{"source":"local","name":"qwen3-8b-local:latest","provider":"ollama",
 "vision":"本地无视觉模型，已降级为纯文本浏览器模式"},"tasks":0}

$ curl -o /dev/null -w "%{http_code}" http://127.0.0.1:5172/       # 200
$ curl http://127.0.0.1:5172/workspace/files
{"root":"...\\workspace","files":[{"name":"browser_check.md",...},{"name":"daily_brief.md",...}]}
```

提交任务（`POST /tasks` → `GET /tasks/{id}/events` SSE）：

- 事件流依次收到 `status(running)` → 多条 `log` → `think` → `log` → `complete`
- 任务状态由 `running` 变为 `completed`
- 产物 `workspace/web_task.md`（三条要点，真实换行）

界面左上角徽标 `本地 · qwen3-8b-local:latest`（绿色），见 [`ui-home.png`](ui-home.png)。云端时徽标变蓝。

> 启动方式：**必须 `python app.py`**。`uvicorn app:app` 会与 `app/` 包重名，报 `Attribute "app" not found in module "app"`。

---

## 4. 单元测试

```bash
python -m pytest
```

```
14 passed, 17 warnings in 6.82s
```

覆盖：

- `normalize_multiline_text`：多条字面量 `\n` 还原 / 真换行不动 / 单次字面量不动 / `\r\n` 混合；
- 循环保护：参数键序无关的指纹归一、连击计数、动作变化后计数归零、阈值关系（soft < hard）；
- 进程守卫：`ms-playwright` 路径识别、用户自己的 Chrome 不误伤、缓存目录里的 node 不算浏览器、父子树 BFS；
- Web 清理：`agent.run()` 抛异常时 `cleanup()` 仍被调用（这条直接锁住「浏览器泄漏」的回归）。

`tests/sandbox/` 是上游的沙箱测试，需要 Docker，默认不跑（本项目不启用沙箱）。

---

## 5. 浏览器进程卫生验证

背景：任务结束后后台常驻 Chromium。根因是 `app.py` 把 `cleanup()` 写在 `try` 末尾（报错即跳过）+ `browser_use` 的 `__del__` 在运行中的事件循环里关不掉 + Windows 不级联杀子进程。

### 5.1 运行中 / 运行后的进程数

轮询脚本每 2 秒记一次 `ms-playwright` 下的浏览器进程数，同时跑一次 `--headless` 的 python.org 任务：

```
17:25:54  0
17:25:56  1  [(56396, 'headless_shell.exe')]
17:25:58  4  [(56396, ...), (50016, ...), (36220, ...)]   ← Chromium 起来了
...
17:27:00  4
17:27:02  1                                                ← 清理开始
17:27:04  0                                                ← 全部退出
```

任务在 17:27:02 打印「请求处理完成」，2 秒内 4 个进程全部消失。

### 5.2 守卫只回收「本次启动」的浏览器

```python
guard = BrowserProcessGuard()          # 启动前拍快照
browser = playwright.chromium.launch()  # 起 4 个进程
guard.ours()  # -> [43452, 53528, 40604, 54096]
guard.sweep() # -> [43452]（taskkill /T 连带子进程）
# 回收后：0 个
```

判定规则是「本进程的子孙 + 快照之后新出现」，所以不会误伤用户自己的 Chrome（路径不在 `ms-playwright` 下），也不会误伤并发跑的另一个任务（不是本进程的子孙）。

### 5.3 救援工具

```bash
$ python scripts/cleanup_browsers.py
 Playwright 浏览器进程扫描
 共 4 个浏览器进程：
     PID     PPID  父进程    进程名                路径
   57760    52672  存活     headless_shell.exe   ...\chromium_headless_shell-1161\...
 其中父进程已退出的孤儿进程：0 个
 处理：--kill（全部）/ --orphans（只清孤儿）

$ python scripts/cleanup_browsers.py --orphans
没有孤儿浏览器进程。

# 正常任务跑完后再扫一次
$ python scripts/cleanup_browsers.py
 没有发现 ms-playwright 目录下的浏览器进程。
```

---

## 6. Web UI 任务状态复位（`Chat bot still under working`）

背景：任务跑过一次后，再点发送只弹 `Chat bot still under working, please wait...`，刷新页面才恢复。根因在前端 - `chat_state` 只有一个复位点（SSE 的 `complete` / `error` 事件），而失败分支全都提前 return 了。

### 6.1 复现（修复前）

Playwright 拦 `POST /tasks` 返回 500，连点两次发送：

```
POST /tasks 次数: 1 -> 1
首次提示: 'backend not ready'
二次提示: 'Chat bot still under working, please wait...'
是否卡死: True
```

界面证据：`docs/ui-stuck-before.png`（红色 toast，输入框还在但发不出去）。

### 6.2 回归（修复后）

```bash
python scripts/repro_stuck_ui.py --scenario create-fail --expect-fixed
```

```
first_toast: 'backend not ready'
spinner_after_fail: False        # 请求失败后 spinner 已经收掉
second_toast: 'backend not ready' # 第二次真的又发了一次请求
second_posts: 2
stuck: False
回归结果: PASS
```

SSE 断线 + `complete` 事件丢失：

```bash
python scripts/repro_stuck_ui.py --scenario sse-drop --expect-fixed
```

```
unlocked_after_s: 1
chat_text: '跑一个会丢 SSE 的任务\n从后端状态对账补回来的结果'
result_rendered: True    # 从 /tasks/{id} 的 steps 里把结果补渲染了
stuck: False
second_send_ok: True
回归结果: PASS
```

真跑一个任务（确认正常路径没被改坏）：

```bash
python scripts/repro_stuck_ui.py --scenario real --expect-fixed
```

```
unlocked_after_s: 36
回归结果: PASS
```

界面证据：`docs/ui-run.png`（任务结束后输入框已恢复），`docs/ui-recover.png`（对账补回结果的场景）。

### 6.3 手动「重置」出口

把 SSE 请求挂住（模拟「任务一直不返回」），界面保持 busy，此时工具栏会出现重置按钮：

```
重置按钮可见: True
spinner 显示: True
点击重置后 spinner 收起: True
重置按钮已隐藏: True
重置后可再发送: True
```

界面证据：`docs/ui-busy.png`（左下角工具栏多了一个环形箭头按钮，发送按钮旁是 spinner）。

注意语义：重置**只释放前端界面**，服务端那个任务该跑还在跑，产物照旧写进 `workspace/`。这一点在按钮的确认弹窗和 README「已知限制」里都写明了，避免用户以为点一下就取消了任务。

跑完再扫一次进程卫生：

```
 没有发现 ms-playwright 目录下的浏览器进程。
```

---

## 7. 未验证 / 受限项

| 项 | 状态 | 原因 |
| --- | --- | --- |
| 视觉截图理解 | 未启用 | 本机无 VL 模型；且浏览器工具主动丢弃截图，纯文本模式即可完成任务 |
| 携程 / 机票真实抓取 | 未验证 | 依赖站点反爬策略，需按「先建会话再走深链」策略跑 |
| Daytona 云沙箱 | 未验证 | 需要自己的 Daytona API key，默认关闭 |
| 长任务（>10 步）稳定性 | 受限 | 8B 模型易跑偏，需拆任务 + 限步数 |
| 多标签页并发 | 未验证 | 前端按单任务串行设计，后端也没有全局排队；多开标签会各跑各的 |
| 退出期 asyncio 噪声 | 已知噪声 | Windows Proactor 事件循环回收浏览器子进程时打印 `unclosed transport`，不影响结果 |

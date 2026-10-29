"""验证 Web UI 的「任务状态复位」是否可靠。

两个场景：
  create-fail  POST /tasks 返回 500（后端没起 / 建任务失败）：请求失败后必须复位，
               第二次点击要能真的再发一次请求，而不是弹 “still under working”。
  sse-drop     POST 成功但 SSE 断掉、且 complete 事件丢失：看门狗要靠 /tasks/{id}
               对账，把结果补上并解锁输入框。

用法：
    python scripts/repro_stuck_ui.py --scenario create-fail           # 修复前：会卡死
    python scripts/repro_stuck_ui.py --scenario create-fail --expect-fixed
    python scripts/repro_stuck_ui.py --scenario sse-drop --expect-fixed
"""

import argparse
import json
import sys

from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:5172"
STUCK_TEXT = "Chat bot still under working"
FAKE_ID = "fake-task-0001"
RECOVERED = "从后端状态对账补回来的结果"


def read_toast(page) -> str:
    try:
        return page.eval_on_selector("#errorToast .toast-body", "el => el.textContent").strip()
    except Exception:  # noqa: BLE001
        return ""


def spinner_hidden(page) -> bool:
    display = page.eval_on_selector("#send-spinner", "el => el.style.display")
    return display == "none"


def scenario_create_fail(page, out):
    posts = []

    def tasks(route):
        if route.request.method == "POST":
            posts.append(route.request.post_data)
            route.fulfill(
                status=500,
                content_type="application/json",
                body=json.dumps({"detail": "backend not ready"}),
            )
        else:
            route.continue_()

    page.route("**/tasks", tasks)
    page.goto(URL, wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(600)

    def submit(text):
        page.fill("#messageInput", text)
        page.click("#sendButton")
        page.wait_for_timeout(1200)

    submit("第一次：模拟建任务失败")
    out["first_toast"] = read_toast(page)
    out["first_posts"] = len(posts)
    out["spinner_after_fail"] = not spinner_hidden(page)

    submit("第二次：应当重新发起请求")
    out["second_toast"] = read_toast(page)
    out["second_posts"] = len(posts)
    out["stuck"] = STUCK_TEXT in out["second_toast"] and out["second_posts"] == out["first_posts"]
    return out["second_posts"] == 2 and not out["stuck"]


def scenario_sse_drop(page, out):
    def tasks(route):
        if route.request.method == "POST":
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"task_id": FAKE_ID}),
            )
        else:
            route.continue_()

    def task_detail(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "id": FAKE_ID,
                    "prompt": "x",
                    "status": "completed",
                    "steps": [{"step": 1, "type": "result", "result": RECOVERED}],
                }
            ),
        )

    page.route("**/tasks/" + FAKE_ID + "/events", lambda route: route.abort())
    page.route("**/tasks/" + FAKE_ID, task_detail)
    page.route("**/tasks", tasks)

    page.goto(URL, wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(600)

    page.fill("#messageInput", "跑一个会丢 SSE 的任务")
    page.click("#sendButton")

    unlocked_at = None
    for i in range(1, 26):  # 最多等 25 秒，看门狗是 10 秒一轮
        page.wait_for_timeout(1000)
        if spinner_hidden(page):
            unlocked_at = i
            break

    out["unlocked_after_s"] = unlocked_at
    out["chat_text"] = page.inner_text("#chatMessages")
    out["result_rendered"] = RECOVERED in out["chat_text"]
    out["stuck"] = unlocked_at is None

    if unlocked_at is None:
        return False

    # 解锁之后必须能发下一条
    page.fill("#messageInput", "再发一条")
    page.click("#sendButton")
    page.wait_for_timeout(800)
    out["second_send_ok"] = STUCK_TEXT not in read_toast(page)
    return out["result_rendered"] and out["second_send_ok"]


def scenario_real(page, out):
    """真跑一个任务，确认正常路径也没被状态机改动弄坏。"""
    page.goto(URL, wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(600)
    page.fill("#messageInput", "打开 https://example.com ，读取页面 H1 标题后直接 terminate，不要写文件。")
    page.click("#sendButton")

    unlocked_at = None
    for i in range(1, 181):
        page.wait_for_timeout(1000)
        if spinner_hidden(page):
            unlocked_at = i
            break

    out["unlocked_after_s"] = unlocked_at
    out["chat_text"] = page.inner_text("#chatMessages")[:300]
    return unlocked_at is not None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--scenario", choices=["create-fail", "sse-drop", "real"], default="create-fail"
    )
    parser.add_argument("--expect-fixed", action="store_true")
    parser.add_argument("--shot", default="")
    args = parser.parse_args()

    out = {}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        runner = {
            "create-fail": scenario_create_fail,
            "sse-drop": scenario_sse_drop,
            "real": scenario_real,
        }[args.scenario]
        ok = runner(page, out)
        if args.shot:
            page.screenshot(path=args.shot)
        browser.close()

    for key, value in out.items():
        print(f"{key}: {value!r}")

    if args.expect_fixed:
        print("回归结果:", "PASS" if ok else "FAIL")
        return 0 if ok else 1
    print("复现结果:", "仍然卡死" if out.get("stuck") else "未复现卡死")
    return 0 if ok or not out.get("stuck") else 1


if __name__ == "__main__":
    sys.exit(main())

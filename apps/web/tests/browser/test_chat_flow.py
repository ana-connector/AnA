"""One conversation, end to end, through the real server.

Needs the stack from `scripts/e2e-stack.sh`: a throwaway database, the real
FastAPI server with the stand-in CLI on PATH, and the Next development
server pointed at it. Nothing here reaches a CLI account.
"""
import os
import re
import sys
import time

from playwright.sync_api import sync_playwright

BASE = os.environ.get("ANA_E2E_WEB", "http://127.0.0.1:3211")
run_id = time.strftime("%H%M%S")

import contextlib

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))


@contextlib.contextmanager
def phase(name):
    """Records a timed-out wait as a failed check instead of ending the run,
    so one broken step does not hide the ones after it."""
    try:
        yield
    except Exception as exc:
        check(name, False, f"{type(exc).__name__}: {str(exc).splitlines()[0][:120]}")

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    errors, closes = [], []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("websocket", lambda ws: ws.on("close", lambda _: closes.append(ws.url)))

    page.goto(BASE, wait_until="networkidle")
    page.wait_for_timeout(800)
    page.fill("#start-name", f"검증 {run_id}")
    page.get_by_role("button", name="만들고 열기").click()
    page.wait_for_timeout(2500)
    workspace_url = page.url
    check("a workspace opens its own conversation", workspace_url.rstrip("/") != BASE, workspace_url)
    connected = page.get_by_text("연결 끊김").count()
    check("the socket is accepted from this origin", connected == 0 and not errors,
          f"disconnected={connected} errors={errors[:2]}")

    # --- a turn, end to end ----------------------------------------------
    page.fill("textarea", "안녕")
    page.keyboard.press("Enter")
    page.wait_for_timeout(500)
    check("the composer empties once the server confirms",
          page.input_value("textarea") == "", repr(page.input_value("textarea")))
    with phase("the agent's answer arrives"):
        page.wait_for_selector("text=받은 말: 안녕", timeout=20000)
        check("the agent's answer arrives", True, "받은 말: 안녕")

    # --- the conversation survives a reload -------------------------------
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(2500)
    body = page.inner_text("body")
    check("the conversation is still there after a reload", "받은 말: 안녕" in body,
          "found" if "받은 말: 안녕" in body else body[-200:])

    # --- stopping a turn --------------------------------------------------
    page.fill("textarea", "SLOWMARK 기다려")
    page.keyboard.press("Enter")
    page.wait_for_timeout(2000)
    stop = page.get_by_role("button", name=re.compile("중단|중지|stop", re.I))
    check("a running turn offers a stop", stop.count() > 0, f"buttons={stop.count()}")
    if stop.count():
        stop.first.click()
        page.wait_for_timeout(3000)
        still_running = page.get_by_role("button", name=re.compile("중단|중지|stop", re.I)).count()
        check("stopping ends the turn", still_running == 0, f"stop buttons left={still_running}")

    # the next prompt still works after a stop
    with phase("a prompt after a stop is answered"):
        page.fill("textarea", "다시")
        page.keyboard.press("Enter")
        page.wait_for_selector("text=받은 말: 다시", timeout=25000)
        check("a prompt after a stop is answered", True, "받은 말: 다시")

    # --- a narrow screen ---------------------------------------------------
    page.set_viewport_size({"width": 360, "height": 740})
    page.wait_for_timeout(800)
    overflow = page.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
    check("no sideways scrolling at 360px", overflow <= 1, f"overflow={overflow}px")
    with phase("a turn works at 360px"):
        page.fill("textarea", "좁은 화면")
        page.keyboard.press("Enter")
        page.wait_for_selector("text=받은 말: 좁은 화면", timeout=25000)
        check("a turn works at 360px", True, "")
    page.set_viewport_size({"width": 1280, "height": 900})

    # --- archive and restore ----------------------------------------------
    page.goto(f"{BASE}/", wait_until="networkidle")
    page.wait_for_timeout(1200)
    listed = page.get_by_text(f"검증 {run_id}").count()
    check("the workspace is listed", listed > 0, f"matches={listed}")

    check("no console or page errors in the whole run", not errors, "; ".join(errors[:3]))
    browser.close()

width = max(len(n) for n, _, _ in results)
failed = sum(0 if ok else 1 for _, ok, _ in results)
for name, ok, detail in results:
    print(f"{'PASS' if ok else 'FAIL'}  {name.ljust(width)}  {detail}")
print(f"\n{len(results) - failed}/{len(results)} passed")
sys.exit(1 if failed else 0)

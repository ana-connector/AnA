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
API = os.environ.get("ANA_E2E_API", "http://127.0.0.1:8123")
run_id = time.strftime("%H%M%S")

import contextlib

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))


@contextlib.contextmanager
def phase(name, page=None):
    """Records a timed-out wait as a failed check instead of ending the run,
    so one broken step does not hide the ones after it. What was on screen
    goes into the detail: "timed out waiting for the answer" and "timed out
    because the turn was never sent" look identical without it."""
    try:
        yield
    except Exception as exc:
        detail = f"{type(exc).__name__}: {str(exc).splitlines()[0][:120]}"
        if page is not None:
            try:
                detail += f" | on screen: {page.inner_text('body')[-200:]!r}"
            except Exception:  # noqa: BLE001 - the page may be gone
                pass
        check(name, False, detail)

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
    # Asked of something that is only true when the socket is up, rather than
    # of the words "연결 끊김": that string also lives in the page's own data
    # payload, where a text query finds it whether or not it is on screen.
    page.fill("textarea", "연결 확인")
    page.wait_for_timeout(300)
    live = page.evaluate("() => !document.querySelector('button[type=submit]').disabled")
    page.fill("textarea", "")
    check("the socket is accepted from this origin", live and not errors,
          f"send enabled={live} errors={errors[:2]}")

    # --- a turn, end to end ----------------------------------------------
    page.fill("textarea", "안녕")
    page.keyboard.press("Enter")
    page.wait_for_timeout(500)
    check("the composer empties once the server confirms",
          page.input_value("textarea") == "", repr(page.input_value("textarea")))
    with phase("the agent's answer arrives"):
        page.wait_for_selector("text=받은 말: 안녕", timeout=20000)
        check("the agent's answer arrives", True, "받은 말: 안녕")

    import json as _json
    import urllib.request as _request

    def stored_turns() -> str:
        thread = workspace_url.rstrip("/").rsplit("/", 1)[-1]
        api = os.environ.get("ANA_E2E_API", "http://127.0.0.1:8123")
        try:
            with _request.urlopen(f"{api}/threads/{thread}/turns", timeout=10) as answer:
                body = _json.load(answer)
            turns = body.get("turns", [])
            return f"{len(turns)} stored turn(s) for {thread}: " + _json.dumps(
                [{k: str(t.get(k))[:40] for k in ("prompt", "response", "agent")} for t in turns],
                ensure_ascii=False,
            )[:300]
        except Exception as cause:
            return f"could not read the turns of {thread}: {cause}"

    def complaints() -> str:
        # Whatever the server told the browser went wrong. A turn that fails
        # after its text has streamed says so here and nowhere else: the
        # relay reports it as a frame, not as a log line.
        shown = page.evaluate(
            "() => [...document.querySelectorAll('p, span')]"
            ".map(n => n.textContent).filter(t => t && t.includes('⚠️')).slice(0, 2)"
        )
        return " | ".join(shown) if shown else "no error shown"

    # --- the turn reaches the table ---------------------------------------
    # Asked before anything navigates: a row that is missing here was never
    # written, and one that is missing only later went away.
    recorded = ""
    for _ in range(20):
        recorded = stored_turns()
        if not recorded.startswith("0 "):
            break
        time.sleep(1)
    check("the turn is written down", not recorded.startswith("0 "), recorded)

    # --- the conversation survives a reload -------------------------------
    # What the server itself has, named in the failure: a transcript that does
    # not come back is either a turn that was never stored or a page that did
    # not ask, and the two need different fixes.
    page.reload(wait_until="networkidle")
    with phase("the conversation is still there after a reload"):
        # Waited for, not slept on: the transcript arrives over HTTP after the
        # page does, and how long that takes is not ours to assume.
        try:
            page.wait_for_selector("text=받은 말: 안녕", timeout=20000)
            check("the conversation is still there after a reload", True, "found")
        except Exception:
            check("the conversation is still there after a reload", False,
                  f"{stored_turns()} | {complaints()}")

    # --- a link to this machine opens beside the conversation --------------
    # An agent that has just started a dev server writes its address; opening
    # that in a new window buries the conversation that produced it.
    with phase("a link to this machine opens in a browser tab", page):
        page.fill("textarea", f"{API}/healthz 를 확인해줘")
        page.keyboard.press("Enter")
        page.wait_for_selector(f"text=받은 말: {API}/healthz", timeout=25000)
        page.wait_for_selector(f'a[href="{API}/healthz"]', timeout=10000)
        page.locator(f'a[href="{API}/healthz"]').first.click()
        page.wait_for_url("**/browser/**", timeout=15000)
        page.wait_for_timeout(1200)
        framed = page.locator('[data-slot="browser-frame"]')
        check("a link to this machine opens in a browser tab",
              framed.count() == 1 and framed.get_attribute("src") == f"{API}/healthz",
              framed.get_attribute("src") if framed.count() else page.url)
        check("and the conversation is still open behind it",
              page.locator('[data-slot="tab"]').count() >= 2
              and any(f"검증 {run_id}" in name for name in page.evaluate(
                  "() => [...document.querySelectorAll('[data-slot=tab-title]')]"
                  ".map(e => e.textContent)")),
              str(page.evaluate(
                  "() => [...document.querySelectorAll('[data-slot=tab-title]')]"
                  ".map(e => e.textContent)")))
        page.locator('[data-slot="tab"][data-active="true"] [data-slot="tab-close"]').click()
        page.wait_for_timeout(1000)

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

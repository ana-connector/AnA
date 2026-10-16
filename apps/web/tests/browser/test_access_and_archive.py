"""Keyboard operation, archiving, the server's origin rules and an axe scan.

Needs the stack from `scripts/e2e-stack.sh`, like the chat flow beside it.
"""
import asyncio
import json
import os
import pathlib
import re
import sys
import time
import urllib.request

import websockets
from playwright.sync_api import sync_playwright

BASE = os.environ.get("ANA_E2E_WEB", "http://127.0.0.1:3211")
API = os.environ.get("ANA_E2E_API", "http://127.0.0.1:8123")
AXE = (pathlib.Path(__file__).resolve().parents[2]
       / "node_modules" / "axe-core" / "axe.min.js").read_text()
run_id = time.strftime("%H%M%S")
results = []
def check(name, ok, detail=""):
    results.append((name, ok, detail))

# --- the server's own origin rules, without a browser --------------------
def cors(origin):
    request = urllib.request.Request(f"{API}/workspaces", headers={"Origin": origin})
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.headers.get("access-control-allow-origin")

check("an allowed origin is granted CORS", cors(BASE) == BASE, str(cors(BASE)))
check("an unknown origin is not", cors("http://evil.example") is None, str(cors("http://evil.example")))


async def socket_from(origin):
    try:
        async with websockets.connect(f"{API.replace('http','ws')}/ws/chat?workspace=origincheck",
                                      origin=origin) as ws:
            await asyncio.wait_for(ws.recv(), 5)
            return "accepted"
    except Exception as exc:
        return f"{type(exc).__name__}: {getattr(exc, 'code', '')}"
check("the socket refuses an unknown origin",
      "accepted" not in asyncio.run(socket_from("http://evil.example")),
      asyncio.run(socket_from("http://evil.example")))

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    errors = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))

    page.goto(BASE, wait_until="networkidle")
    page.wait_for_timeout(800)
    page.fill("#start-name", f"키보드 {run_id}")
    page.get_by_role("button", name="만들고 열기").click()
    page.wait_for_timeout(2500)

    # --- keyboard only ----------------------------------------------------
    composer = page.locator("textarea")
    composer.focus()
    page.keyboard.type("키보드로 보냄")
    page.keyboard.press("Enter")
    page.wait_for_selector("text=받은 말: 키보드로 보냄", timeout=25000)
    check("a turn can be sent from the keyboard alone", True, "")

    focusable = page.evaluate("""() => {
      const nodes = [...document.querySelectorAll(
        'a[href], button:not([disabled]), textarea, input, select, [tabindex]:not([tabindex="-1"])')]
        .filter(n => !n.closest('[hidden]') && n.offsetParent !== null);
      return nodes.length;
    }""")
    check("the chat page has a keyboard path through it", focusable > 5, f"focusable={focusable}")

    # --- an accessibility scan --------------------------------------------
    page.add_script_tag(content=AXE)
    report = page.evaluate("""async () => {
      const r = await axe.run(document, { resultTypes: ['violations'] });
      return r.violations.map(v => ({ id: v.id, impact: v.impact, nodes: v.nodes.length }));
    }""")
    serious = [v for v in report if v["impact"] in ("serious", "critical")]
    check("no serious or critical accessibility violations on the chat page",
          not serious, json.dumps(serious, ensure_ascii=False))

    # --- archive and restore ----------------------------------------------
    page.goto(f"{BASE}/", wait_until="networkidle")
    page.wait_for_timeout(1200)
    workspaces = json.loads(urllib.request.urlopen(f"{API}/workspaces", timeout=10).read())
    mine = [w for w in workspaces["workspaces"] if w["name"] == f"키보드 {run_id}"]
    check("the workspace was created with turns recorded",
          len(mine) == 1 and mine[0].get("turn_count", 0) >= 1, json.dumps(mine, ensure_ascii=False)[:160])

    if mine:
        target = mine[0]["id"]
        page.goto(f"{BASE}/workspaces/{target}", wait_until="networkidle")
        page.wait_for_timeout(1500)
        archive = page.get_by_role("button", name="보관하기", exact=True)
        check("the workspace screen offers archiving", archive.count() > 0, f"buttons={archive.count()}")
        if archive.count():
            archive.first.click()
            page.wait_for_timeout(2000)
            after = json.loads(urllib.request.urlopen(f"{API}/workspaces", timeout=10).read())
            row = [w for w in after["workspaces"] if w["id"] == target]
            check("archiving keeps the row, its folder and its turns",
                  len(row) == 1 and row[0].get("archived") is True
                  and row[0].get("turn_count") == mine[0].get("turn_count")
                  and row[0].get("path") == mine[0].get("path"),
                  json.dumps(row, ensure_ascii=False)[:200])
            restore = page.get_by_role("button", name="보관 해제", exact=True)
            if restore.count():
                restore.first.click()
                page.wait_for_timeout(2000)
                back = json.loads(urllib.request.urlopen(f"{API}/workspaces", timeout=10).read())
                row = [w for w in back["workspaces"] if w["id"] == target]
                check("restoring brings it back unchanged",
                      len(row) == 1 and not row[0].get("archived")
                      and row[0].get("turn_count") == mine[0].get("turn_count"),
                      json.dumps(row, ensure_ascii=False)[:200])
            else:
                check("restoring brings it back unchanged", False, "no restore control found")

    check("no console or page errors", not errors, "; ".join(errors[:3]))
    browser.close()

width = max(len(n) for n, _, _ in results)
failed = sum(0 if ok else 1 for _, ok, _ in results)
for name, ok, detail in results:
    print(f"{'PASS' if ok else 'FAIL'}  {name.ljust(width)}  {detail}")
print(f"\n{len(results) - failed}/{len(results)} passed")
sys.exit(1 if failed else 0)

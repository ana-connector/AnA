import json
import os
from playwright.sync_api import sync_playwright

BASE = os.environ.get("ANA_E2E_WEB", "http://127.0.0.1:3211")
URL = f"{BASE}/ui-harness"
results = []

def check(name, ok, detail=""):
    results.append((name, ok, detail))

with sync_playwright() as p:
    browser = p.chromium.launch()

    # --- skipping optional questions -------------------------------------
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.goto(URL, wait_until="networkidle")
    page.wait_for_timeout(400)

    page.locator('[data-slot="questionnaire-choice"]').first.click()  # required answered
    page.get_by_role("button", name="다음").click()
    page.wait_for_timeout(150)
    page.get_by_role("button", name="건너뛰기").click()   # skip the multi question
    page.wait_for_timeout(150)
    progress = page.locator('[data-slot="questionnaire-progress"]').first.inner_text()
    check("skip advances", progress.strip() == "질문 3 / 3", progress)
    page.get_by_role("button", name="건너뛰기").click()   # skip the last one: submits
    page.wait_for_timeout(400)
    answers = page.evaluate("() => window.__answers ?? []")
    check("a skipped question submits nothing",
          answers == [{"requestId": "req-1", "answers": ["스테이징", [], ""]}],
          json.dumps(answers, ensure_ascii=False))

    # --- keyboard only ----------------------------------------------------
    page2 = browser.new_page(viewport={"width": 1280, "height": 900})
    page2.on("pageerror", lambda e: errors.append(str(e)))
    page2.goto(URL, wait_until="networkidle")
    page2.wait_for_timeout(700)

    # reach the first choice with Tab alone, and see the focus ring
    for _ in range(12):
        page2.keyboard.press("Tab")
        focused = page2.evaluate(
            "() => { const a = document.activeElement;"
            "return a ? [a.tagName, a.getAttribute('data-slot'), a.type ?? ''] : null }")
        if focused and focused[1] == "questionnaire-choice-input":
            break
    check("choices are reachable by Tab", focused and focused[1] == "questionnaire-choice-input",
          str(focused))
    visible_ring = page2.evaluate(
        "() => { const a = document.activeElement.closest('label');"
        "return a ? getComputedStyle(a).borderColor : null }")
    page2.keyboard.press("Space")
    page2.wait_for_timeout(150)
    picked = page2.evaluate(
        "() => [...document.querySelectorAll('input[data-slot=questionnaire-choice-input]')]"
        ".filter(i => i.checked).map(i => i.value)")
    check("Space picks the focused choice", picked == ["스테이징"], f"{picked} ring={visible_ring}")

    page2.keyboard.press("Enter")   # Enter on an answered choice moves on
    page2.wait_for_timeout(200)
    progress = page2.locator('[data-slot="questionnaire-progress"]').first.inner_text()
    check("Enter advances from an answered question", progress.strip() == "질문 2 / 3", progress)

    page2.keyboard.press("ArrowLeft")
    page2.wait_for_timeout(200)
    progress = page2.locator('[data-slot="questionnaire-progress"]').first.inner_text()
    check("ArrowLeft goes back", progress.strip() == "질문 1 / 3", progress)

    # a hidden question must not be reachable
    reachable = page2.evaluate(
        "() => [...document.querySelectorAll('[data-slot=questionnaire-item]')]"
        ".filter(f => !f.hidden).length")
    check("only the active question is in the page", reachable == 1, f"visible={reachable}")

    # the scroller viewport is focusable and scrolls with the keyboard
    page2.evaluate("() => document.getElementById('viewport').focus()")
    before = page2.evaluate("() => document.getElementById('viewport').scrollTop")
    page2.keyboard.press("PageUp")
    page2.keyboard.press("PageUp")
    page2.wait_for_timeout(400)
    after = page2.evaluate("() => document.getElementById('viewport').scrollTop")
    check("viewport scrolls from the keyboard", after < before, f"{before} -> {after}")
    # Polled rather than read once: the attribute is set from a scroll event,
    # and a smooth scroll delivers several.
    button = page2.locator('[data-slot="message-scroller-button"]')
    active = "false"
    for _ in range(20):
        active = button.get_attribute("data-active")
        if active == "true":
            break
        page2.wait_for_timeout(100)
    check("keyboard scroll reveals the jump button", active == "true", f"data-active={active}")

    check("no console or page errors", not errors, "; ".join(errors[:3]))
    browser.close()

width = max(len(name) for name, _, _ in results)
failed = sum(0 if ok else 1 for _, ok, _ in results)
for name, ok, detail in results:
    print(f"{'PASS' if ok else 'FAIL'}  {name.ljust(width)}  {detail}")
print(f"\n{len(results) - failed}/{len(results)} passed")
raise SystemExit(1 if failed else 0)

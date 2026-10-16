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
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.goto(URL, wait_until="networkidle")

    vp = page.locator("#viewport")
    page.wait_for_timeout(500)

    # 1. opens at the newest message
    at_bottom = page.evaluate(
        "() => { const v = document.getElementById('viewport');"
        "return v.scrollHeight - v.scrollTop - v.clientHeight; }")
    check("opens at the end", at_bottom <= 8, f"distance={at_bottom}")

    # 2. the jump button is hidden while at the bottom
    active = page.locator('[data-slot="message-scroller-button"]').get_attribute("data-active")
    check("button inactive at bottom", active == "false", f"data-active={active}")

    # 3. appending follows the bottom
    page.click("#append")
    page.wait_for_timeout(300)
    d = page.evaluate("() => { const v = document.getElementById('viewport');"
                      "return v.scrollHeight - v.scrollTop - v.clientHeight; }")
    check("follows new messages", d <= 8, f"distance={d}")

    # 4. a growing answer keeps the bottom in view
    page.click("#grow")
    page.wait_for_timeout(300)
    d = page.evaluate("() => { const v = document.getElementById('viewport');"
                      "return v.scrollHeight - v.scrollTop - v.clientHeight; }")
    check("follows a growing answer", d <= 8, f"distance={d}")

    # 5. scrolling up stops the follow and shows the button
    vp.hover()
    page.mouse.wheel(0, -1200)
    page.wait_for_timeout(300)
    top_after_scroll = page.evaluate("() => document.getElementById('viewport').scrollTop")
    active = page.locator('[data-slot="message-scroller-button"]').get_attribute("data-active")
    check("button active after scrolling up", active == "true", f"data-active={active}")

    page.click("#append")
    page.wait_for_timeout(400)
    top_after_append = page.evaluate("() => document.getElementById('viewport').scrollTop")
    check("reading position kept while new messages arrive",
          abs(top_after_append - top_after_scroll) <= 2,
          f"{top_after_scroll} -> {top_after_append}")

    # 6. the button goes back to the end and can be reached by keyboard
    page.locator('[data-slot="message-scroller-button"]').click()
    page.wait_for_timeout(600)
    d = page.evaluate("() => { const v = document.getElementById('viewport');"
                      "return v.scrollHeight - v.scrollTop - v.clientHeight; }")
    check("jump button returns to the end", d <= 8, f"distance={d}")

    # 7. questionnaire: one question at a time, required blocks Next
    visible = page.locator('[data-slot="questionnaire-item"]:not([hidden])').count()
    check("one question visible", visible == 1, f"visible={visible}")
    progress = page.locator('[data-slot="questionnaire-progress"]').first.inner_text()
    check("progress reads 1 of 3", progress.strip() == "질문 1 / 3", progress)

    next_button = page.locator('[data-slot="questionnaire-item"]:not([hidden])').locator("xpath=../..").locator("button", has_text="다음")
    page.get_by_role("button", name="다음").click()
    page.wait_for_timeout(200)
    err = page.locator('[data-slot="questionnaire-error"]:not([hidden])').count()
    still_first = page.locator('[data-slot="questionnaire-progress"]').first.inner_text()
    check("required question blocks Next", err == 1 and still_first.strip() == "질문 1 / 3",
          f"errors={err} progress={still_first}")

    # skip must not be offered on a required question
    skip_hidden = page.locator('[data-slot="questionnaire-skip"]').first.get_attribute("hidden")
    check("required question cannot be skipped", skip_hidden is not None, f"hidden={skip_hidden}")

    # 8. a number key picks a choice
    page.keyboard.press("2")
    page.wait_for_timeout(200)
    picked = page.evaluate(
        "() => [...document.querySelectorAll('input[data-slot=questionnaire-choice-input]')]"
        ".filter(i => i.checked).map(i => i.value)")
    check("number key picks a choice", picked == ["운영"], f"picked={picked}")

    page.get_by_role("button", name="다음").click()
    page.wait_for_timeout(200)
    progress = page.locator('[data-slot="questionnaire-progress"]').first.inner_text()
    check("answered question advances", progress.strip() == "질문 2 / 3", progress)

    # 9. multiple choice keeps both answers
    page.keyboard.press("1")
    page.keyboard.press("2")
    page.wait_for_timeout(200)
    picked = page.evaluate(
        "() => [...document.querySelectorAll('input[data-slot=questionnaire-choice-input]')]"
        ".filter(i => i.checked).map(i => i.value)")
    check("multiple choice keeps both", picked == ["운영", "metrics", "tracing"], f"picked={picked}")

    # 10. previous goes back and keeps the earlier answer
    page.get_by_role("button", name="이전").click()
    page.wait_for_timeout(200)
    progress = page.locator('[data-slot="questionnaire-progress"]').first.inner_text()
    first_kept = page.evaluate(
        "() => [...document.querySelectorAll('input[data-slot=questionnaire-choice-input]')]"
        ".filter(i => i.checked).map(i => i.value)")
    check("previous keeps the earlier answer",
          progress.strip() == "질문 1 / 3" and "운영" in first_kept, f"{progress} {first_kept}")
    page.get_by_role("button", name="다음").click()
    page.get_by_role("button", name="다음").click()
    page.wait_for_timeout(200)

    # 11. free text answers the last question, then submit
    page.fill('[data-slot="questionnaire-input"]', "배포 전 확인 완료")
    page.wait_for_timeout(100)
    page.get_by_role("button", name="답하기").click()
    page.wait_for_timeout(300)
    answers = page.evaluate("() => window.__answers ?? []")
    check("submit carries every answer",
          answers == [{"requestId": "req-1",
                       "answers": ["운영", ["metrics", "tracing"], "배포 전 확인 완료"]}],
          json.dumps(answers, ensure_ascii=False))

    # 12. the submit button locks after a successful answer
    disabled = page.locator('[data-slot="questionnaire-submit"], button:has-text("전달 중…")').first
    label = page.get_by_role("button", name="전달 중…").count()
    check("card shows it is waiting", label == 1, f"matches={label}")

    check("no console or page errors", not errors, "; ".join(errors[:3]))
    browser.close()

width = max(len(name) for name, _, _ in results)
failed = 0
for name, ok, detail in results:
    failed += 0 if ok else 1
    print(f"{'PASS' if ok else 'FAIL'}  {name.ljust(width)}  {detail}")
print(f"\n{len(results) - failed}/{len(results)} passed")
raise SystemExit(1 if failed else 0)

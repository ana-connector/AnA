"""The tab bar, and the single line that now divides the sidebar from the page.

Opening a workspace has two meanings and the bar has to tell them apart: from
the sidebar it is "also open this", from the start page it is "this tab is
now that workspace". Neither is visible to a type check, and both are the
kind of thing that quietly regresses, so they are driven here in a browser.

Needs the stack from `scripts/e2e-stack.sh`, like the checks beside it.
"""

import base64
import json
import os
import sys
import tempfile
import time
import urllib.parse
import urllib.request

from playwright.sync_api import sync_playwright

BASE = os.environ.get("ANA_E2E_WEB", "http://127.0.0.1:3211")
API = os.environ.get("ANA_E2E_API", "http://127.0.0.1:8123")
run_id = time.strftime("%H%M%S")
results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))


def make_workspace(name):
    request = urllib.request.Request(
        f"{API}/workspaces",
        data=json.dumps({"name": name, "path": None}).encode(),
        headers={"content-type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read())


def patch_workspace(workspace_id, body):
    request = urllib.request.Request(
        f"{API}/workspaces/{workspace_id}",
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json"},
        method="PATCH",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read())


# A 1x1 transparent PNG: the smallest thing that proves an uploaded image
# makes it all the way to the tab.
PNG = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
       "AAAADUlEQVR4nGP4z8DwHwAFAAH/iZk9HQAAAABJRU5ErkJggg==")


# Three, named so their order on the start page is known: it sorts by when
# each was last used, and these are made oldest first.
made = [make_workspace(f"탭{index} {run_id}") for index in range(1, 4)]
# One emoji and one uploaded picture, so both kinds are carried through.
patch_workspace(made[1]["id"], {"icon": "🛠️"})
patch_workspace(made[2]["id"], {"icon": PNG})
titles_of = """() => [...document.querySelectorAll('[data-slot="tab"]')]
    .map(tab => ({
      title: tab.querySelector('[data-slot="tab-title"]').textContent.trim(),
      active: tab.dataset.active === 'true',
      href: tab.dataset.href,
    }))"""


def tabs(page):
    return page.evaluate(titles_of)


def names(page):
    return [tab["title"] for tab in tabs(page)]


def active(page):
    return next((tab["title"] for tab in tabs(page) if tab["active"]), None)


def sidebar_row(page, name):
    return page.locator('[data-slot="sidebar-menu-button"]', has_text=name).first


with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

    page.goto(BASE, wait_until="networkidle")
    page.wait_for_selector('[data-slot="tab"]', timeout=20000)
    page.wait_for_timeout(600)

    # --- the frame -------------------------------------------------------
    check("the start page opens as one tab", names(page) == ["시작"], str(names(page)))
    check("and that tab is the active one", active(page) == "시작", str(active(page)))

    frame = page.evaluate("""() => {
      const inset = document.querySelector('[data-slot="sidebar-inset"]');
      const container = document.querySelector('[data-slot="sidebar-container"]');
      const insetStyle = getComputedStyle(inset);
      // The container's own box ends *on* its right border, so a divider and
      // nothing else means its right edge and the content's left edge meet.
      const panel = container.getBoundingClientRect();
      const content = inset.getBoundingClientRect();
      return {
        margin: insetStyle.margin,
        radius: insetStyle.borderRadius,
        shadow: insetStyle.boxShadow,
        divider: getComputedStyle(container).borderRightWidth,
        gap: Math.round(content.left - panel.right),
      };
    }""")
    check("the content panel is not inset",
          frame["margin"] in ("0px", "") and frame["radius"] == "0px"
          and frame["shadow"] == "none",
          str(frame))
    check("one line divides the sidebar from the page",
          frame["divider"] == "1px" and frame["gap"] == 0, str(frame))
    check("the workspace search is gone from the top bar",
          page.get_by_role("button", name="워크스페이스 검색").count() == 0, "")

    # Every name a tab is ever given, so the next checks can prove it was
    # never given a placeholder on the way to the real one.
    page.evaluate("""() => {
      window.__titles = [];
      const record = () => {
        for (const link of document.querySelectorAll('[data-slot="tab-title"]')) {
          const title = link.textContent.trim();
          if (window.__titles.at(-1) !== title) window.__titles.push(title);
        }
      };
      record();
      new MutationObserver(record).observe(
        document.body, { subtree: true, childList: true, characterData: true });
    }""")

    # --- the start page hands its own tab over ---------------------------
    # The recent list's rows carry the listitem role (they sit in a list),
    # so they are found by what they say rather than by a link role.
    page.locator('[data-slot="item"]', has_text=made[0]["name"]).first.click()
    page.wait_for_url(f"{BASE}/{made[0]['id']}", timeout=20000)
    page.wait_for_timeout(1200)
    check("opening from the start page replaces its tab",
          names(page) == [made[0]["name"]], str(names(page)))

    # --- the sidebar opens another one beside it -------------------------
    sidebar_row(page, made[1]["name"]).click()
    page.wait_for_url(f"{BASE}/{made[1]['id']}", timeout=20000)
    page.wait_for_timeout(1000)
    check("the sidebar opens a second tab",
          names(page) == [made[0]["name"], made[1]["name"]], str(names(page)))
    check("and makes it the active one", active(page) == made[1]["name"], str(active(page)))

    sidebar_row(page, made[2]["name"]).click()
    page.wait_for_url(f"{BASE}/{made[2]['id']}", timeout=20000)
    page.wait_for_timeout(1000)
    check("a third keeps the first two",
          names(page) == [w["name"] for w in made], str(names(page)))

    seen = page.evaluate("() => window.__titles")
    check("a tab is never named 새 대화 on the way to its real name",
          "새 대화" not in seen, str(seen))
    emoji_tab = page.locator(f'[data-slot="tab"][data-href="/{made[1]["id"]}"]')
    check("a workspace tab shows its emoji",
          emoji_tab.inner_text().startswith("🛠"), emoji_tab.inner_text().replace("\n", " "))
    check("an uploaded icon is shown as an image",
          page.locator(f'[data-slot="tab"][data-href="/{made[2]["id"]}"] img')
              .get_attribute("src") == PNG, "")

    marker = page.evaluate("""() => {
      const tab = document.querySelector('[data-slot="tab"][data-active="true"]');
      const after = getComputedStyle(tab, '::after');
      const bar = document.querySelector('[data-slot="tab-bar"]').getBoundingClientRect();
      const detail = tab.querySelector('[data-slot="tab-detail"]');
      return {
        bottom: after.bottom, height: after.height, colour: after.backgroundColor,
        folder: detail ? detail.textContent.trim() : null,
        // Anything to the right of the tabs: the folder used to live there.
        right: [...document.querySelectorAll('header > div')]
          .map(node => node.textContent.trim()).filter(Boolean),
        barRight: Math.round(bar.right),
      };
    }""")
    check("the active tab is marked along its bottom edge",
          marker["bottom"] == "0px" and marker["height"] == "2px", str(marker))
    check("the tab carries the folder beside the name",
          marker["folder"] and marker["folder"].endswith(made[2]["id"]), str(marker["folder"]))
    check("and the folder is no longer parked at the end of the bar",
          not any("/" in text for text in marker["right"]), str(marker["right"]))

    # Compact: a tab is as wide as what is on it, up to a ceiling, so the bar
    # does not stretch three tabs across a wide window.
    widths = page.evaluate("""() => [...document.querySelectorAll('[data-slot="tab"]')]
        .map(tab => Math.round(tab.getBoundingClientRect().width))""")
    bar_width = page.evaluate(
        "() => Math.round(document.querySelector('[data-slot=\"tab-bar\"]').getBoundingClientRect().width)")
    check("a tab is no wider than its ceiling", max(widths) <= 224, str(widths))
    check("and the tabs do not stretch across the bar",
          sum(widths) < bar_width, f"{widths} in {bar_width}")
    check("the bar is compact", page.evaluate(
        "() => Math.round(document.querySelector('[data-slot=\"tab-bar\"]').parentElement"
        ".getBoundingClientRect().height)") <= 40, "")

    # --- a narrow window scrolls the bar rather than widening the page ---
    page.set_viewport_size({"width": 390, "height": 780})
    page.wait_for_timeout(900)
    narrow = page.evaluate("""() => {
      const bar = document.querySelector('[data-slot="tab-bar"]').getBoundingClientRect();
      const tab = document.querySelector('[data-slot="tab"][data-active="true"]').getBoundingClientRect();
      return {
        left: Math.round(tab.left - bar.left),
        right: Math.round(bar.right - tab.right),
        overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      };
    }""")
    check("at 390px the tab you are in is still in view",
          narrow["left"] >= 0 and narrow["right"] >= 0, str(narrow))
    check("and the page itself does not scroll sideways", narrow["overflow"] <= 0, str(narrow))
    page.set_viewport_size({"width": 1280, "height": 900})
    page.wait_for_timeout(600)

    # --- the sidebar marks whichever tab is open -------------------------
    marked = page.evaluate("""() => [...document.querySelectorAll('[data-slot="sidebar-menu-button"]')]
        .filter(row => row.dataset.active !== undefined && row.dataset.active !== 'false')
        .map(row => row.textContent.trim())""")
    check("the sidebar marks the workspace whose tab is open",
          len(marked) == 1 and marked[0].startswith(made[2]["name"]), str(marked))

    # Going back to an already open tab moves the mark with it.
    page.locator(f'[data-slot="tab"][data-href="/{made[0]["id"]}"] a').click()
    page.wait_for_url(f"{BASE}/{made[0]['id']}", timeout=20000)
    page.wait_for_timeout(900)
    marked = page.evaluate("""() => [...document.querySelectorAll('[data-slot="sidebar-menu-button"]')]
        .filter(row => row.dataset.active !== undefined && row.dataset.active !== 'false')
        .map(row => row.textContent.trim())""")
    check("the mark follows the tab you switch to",
          len(marked) == 1 and marked[0].startswith(made[0]["name"]), str(marked))
    check("switching tabs keeps every tab open",
          names(page) == [w["name"] for w in made], str(names(page)))

    # --- a utility page is a tab too -------------------------------------
    page.get_by_role("link", name="설정").first.click()
    page.wait_for_url(f"{BASE}/settings", timeout=20000)
    page.wait_for_timeout(900)
    check("a utility page opens as its own tab",
          names(page) == [w["name"] for w in made] + ["설정"], str(names(page)))

    # --- closing ----------------------------------------------------------
    page.locator('[data-slot="tab"][data-href="/settings"] [data-slot="tab-close"]').click()
    page.wait_for_timeout(900)
    check("closing the active tab leaves the bar",
          names(page) == [w["name"] for w in made], str(names(page)))
    check("and moves to its neighbour",
          active(page) == made[2]["name"] and page.url.endswith(made[2]["id"]),
          f"{active(page)} {page.url}")

    page.locator(f'[data-slot="tab"][data-href="/{made[1]["id"]}"] [data-slot="tab-close"]').click()
    page.wait_for_timeout(700)
    check("closing a tab you are not in leaves you where you are",
          names(page) == [made[0]["name"], made[2]["name"]]
          and page.url.endswith(made[2]["id"]), str(names(page)))

    # --- tabs survive a reload -------------------------------------------
    page.reload(wait_until="networkidle")
    page.wait_for_selector('[data-slot="tab"]', timeout=20000)
    page.wait_for_timeout(900)
    check("a reload keeps the open tabs",
          names(page) == [made[0]["name"], made[2]["name"]], str(names(page)))
    check("and keeps the one you were reading active",
          active(page) == made[2]["name"], str(active(page)))

    # --- the last tab closed is the start page ---------------------------
    for item in (made[0], made[2]):
        page.locator(f'[data-slot="tab"][data-href="/{item["id"]}"] [data-slot="tab-close"]').click()
        page.wait_for_timeout(800)
    check("closing the last tab lands on the start page",
          names(page) == ["시작"] and page.url.rstrip("/") == BASE.rstrip("/"),
          f"{names(page)} {page.url}")

    # --- a page's own controls are not in the tab bar ---------------------
    page.goto(f"{BASE}/settings", wait_until="networkidle")
    page.wait_for_timeout(1200)
    placed = page.evaluate("""() => {
      const header = document.querySelector('[data-slot="tab-bar"]').parentElement;
      const bar = document.querySelector('[data-slot="page-actions"]');
      return {
        inHeader: header.innerText.includes('저장'),
        below: bar ? bar.innerText.includes('저장') : false,
        order: bar ? (bar.getBoundingClientRect().top > header.getBoundingClientRect().bottom) : false,
      };
    }""")
    check("the save button is below the tab bar, not in it",
          not placed["inHeader"] and placed["below"] and placed["order"], str(placed))

    # --- the browser tab --------------------------------------------------
    page.click('[data-slot="tab-new"]')
    page.wait_for_url(f"{BASE}/browser", timeout=20000)
    page.wait_for_timeout(800)
    check("the + button opens a browser tab", names(page)[-1] == "브라우저", str(names(page)))

    page.fill('[data-slot="browser-address"]', f"{API.replace('http://', '')}/healthz")
    page.get_by_role("button", name="열기", exact=True).click()
    page.wait_for_timeout(2000)
    framed = page.locator('[data-slot="browser-frame"]')
    check("an address is loaded in the tab",
          framed.count() == 1 and framed.get_attribute("src").endswith("/healthz"),
          framed.get_attribute("src") if framed.count() else "no frame")
    check("the framed page is really served",
          "ok" in page.frame_locator('[data-slot="browser-frame"]').locator("body").inner_text(), "")
    check("typing an address moves that tab rather than opening another",
          names(page).count("브라우저") == 0 and names(page)[-1].endswith(API.split(":")[-1]),
          str(names(page)))

    # ANA framed inside ANA would share our origin, and a frame that does can
    # step straight out of its sandbox.
    page.goto(f"{BASE}/browser/{urllib.parse.quote(BASE, safe='')}", wait_until="networkidle")
    page.wait_for_timeout(900)
    check("AnA itself is refused",
          page.locator('[data-slot="browser-frame"]').count() == 0
          and "열 수 없는 주소" in page.inner_text("body"), "")
    page.locator('[data-slot="tab"][data-active="true"] [data-slot="tab-close"]').click()
    page.wait_for_timeout(800)

    # --- picking an icon --------------------------------------------------
    page.goto(f"{BASE}/workspaces/{made[0]['id']}", wait_until="networkidle")
    page.wait_for_timeout(1200)
    page.fill('[aria-label="아이콘 이모지"]', "🐙")
    page.get_by_role("button", name="아이콘 저장").click()
    page.wait_for_timeout(1500)
    stored = [w for w in json.loads(urllib.request.urlopen(f"{API}/workspaces").read())["workspaces"]
              if w["id"] == made[0]["id"]]
    check("an emoji typed into the editor is stored",
          len(stored) == 1 and stored[0]["icon"] == "🐙",
          json.dumps(stored, ensure_ascii=False)[:120])

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
        handle.write(base64.b64decode(PNG.split(",", 1)[1]))
        png_path = handle.name
    page.set_input_files('input[type="file"]', png_path)
    page.wait_for_timeout(800)
    page.get_by_role("button", name="아이콘 저장").click()
    page.wait_for_timeout(1500)
    stored = [w for w in json.loads(urllib.request.urlopen(f"{API}/workspaces").read())["workspaces"]
              if w["id"] == made[0]["id"]]
    check("an uploaded image is stored as a small data URL",
          len(stored) == 1 and stored[0]["icon"].startswith("data:image/png;base64,")
          and len(stored[0]["icon"]) < 256_000,
          str(len(stored[0]["icon"]) if stored else 0))
    os.unlink(png_path)

    # The sidebar is where the icon has to show up for it to be worth having.
    page.goto(f"{BASE}/{made[0]['id']}", wait_until="networkidle")
    page.wait_for_timeout(1500)
    row_icon = page.locator(
        f'[data-slot="sidebar-menu-button"]:has-text("{made[0]["name"]}") img').count()
    check("the sidebar row shows the icon", row_icon == 1, f"images={row_icon}")

    # --- the archive never looks selected ---------------------------------
    page.goto(f"{BASE}/workspaces/{made[0]['id']}", wait_until="networkidle")
    page.wait_for_timeout(1200)
    page.get_by_role("button", name="보관하기", exact=True).first.click()
    page.wait_for_timeout(1500)
    page.goto(f"{BASE}/{made[0]['id']}", wait_until="networkidle")
    page.wait_for_timeout(1500)
    page.get_by_role("button", name="보관함").first.click()
    page.wait_for_timeout(500)
    archived_marked = page.evaluate("""(name) => {
      const rows = [...document.querySelectorAll('[data-slot="sidebar-menu-button"]')]
        .filter(row => row.textContent.includes(name));
      return rows.map(row => row.dataset.active ?? 'unset');
    }""", made[0]["name"])
    check("an archived workspace is listed but never marked",
          len(archived_marked) == 1 and archived_marked[0] in ("false", "unset"),
          str(archived_marked))

    check("no console or page errors", not errors, "; ".join(errors[:3]))
    browser.close()

width = max(len(name) for name, _, _ in results)
failed = sum(0 if ok else 1 for _, ok, _ in results)
for name, ok, detail in results:
    print(f"{'PASS' if ok else 'FAIL'}  {name.ljust(width)}  {detail}")
print(f"\n{len(results) - failed}/{len(results)} passed")
sys.exit(1 if failed else 0)

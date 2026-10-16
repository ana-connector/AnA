"""The Planner-Worker diagram, which has to survive its own updates.

Three things broke here and none of them were visible to a type check: the
canvas never centred because the fit was gated on a measurement that never
arrived, a status change rebuilt the nodes and left the canvas blank, and a
CLI's failure message ran the task row past the width of the transcript.
"""

import os
import sys

from playwright.sync_api import sync_playwright

BASE = os.environ.get("ANA_E2E_WEB", "http://127.0.0.1:3211")
URL = f"{BASE}/ui-harness"
results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))


READ_GRAPH = """() => {
  const host = document.getElementById('plan-host');
  const nodes = [...host.querySelectorAll('.react-flow__node')];
  const pane = host.querySelector('.react-flow__pane').getBoundingClientRect();
  const boxes = nodes.map(n => n.getBoundingClientRect());
  if (!boxes.length) return { nodes: 0 };
  const union = {
    left: Math.min(...boxes.map(b => b.left)), right: Math.max(...boxes.map(b => b.right)),
    top: Math.min(...boxes.map(b => b.top)), bottom: Math.max(...boxes.map(b => b.bottom)),
  };
  return {
    nodes: nodes.length,
    drawn: boxes.every(b => b.width > 0 && b.height > 0),
    inside: boxes.every(b => b.left >= pane.left - 1 && b.right <= pane.right + 1
                          && b.top >= pane.top - 1 && b.bottom <= pane.bottom + 1),
    offsetX: Math.round((union.left + union.right) / 2 - (pane.left + pane.right) / 2),
    offsetY: Math.round((union.top + union.bottom) / 2 - (pane.top + pane.bottom) / 2),
  };
}"""


def centred(graph, slack=2):
    return abs(graph["offsetX"]) <= slack and abs(graph["offsetY"]) <= slack


with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1100, "height": 1000})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.goto(URL, wait_until="networkidle")
    page.wait_for_selector("#plan-host .react-flow__node", timeout=20000)
    page.wait_for_timeout(1200)

    first = page.evaluate(READ_GRAPH)
    check("the plan is drawn", first["nodes"] >= 4 and first["drawn"], str(first))
    check("it opens centred in its canvas", centred(first),
          f"offset=({first['offsetX']}, {first['offsetY']})")
    check("every node is inside the canvas", first["inside"], str(first))

    # --- a status change must not blank the canvas ------------------------
    page.click("#fail-task")
    page.wait_for_timeout(1000)
    failed = page.evaluate(READ_GRAPH)
    check("a status change keeps the diagram", failed["nodes"] == first["nodes"] and failed["drawn"],
          str(failed))
    check("and keeps it centred", centred(failed),
          f"offset=({failed['offsetX']}, {failed['offsetY']})")

    # --- a long failure message must not widen the row --------------------
    row = page.evaluate("""() => {
      const li = [...document.querySelectorAll('#plan-host li')]
        .find(item => item.textContent.includes('실패 원인'));
      if (!li) return null;
      const reason = li.querySelector('[title]');
      const box = li.getBoundingClientRect();
      const host = document.getElementById('plan-host').getBoundingClientRect();
      return {
        width: Math.round(box.width), height: Math.round(box.height),
        host: Math.round(host.width),
        clipped: reason ? reason.scrollWidth > reason.clientWidth : null,
        full: reason ? reason.getAttribute('title').length : 0,
      };
    }""")
    check("the failure reason is cut off, not wrapped",
          row is not None and row["clipped"] and row["height"] <= 40,
          str(row))
    check("the whole reason is still available on hover",
          row is not None and row["full"] > 40, str(row))
    check("the row stays inside the card",
          row is not None and row["width"] <= row["host"], str(row))

    # --- a task arriving later must be fitted in --------------------------
    page.click("#add-task")
    page.wait_for_timeout(1200)
    grown = page.evaluate(READ_GRAPH)
    check("a task that arrives later is drawn", grown["nodes"] == first["nodes"] + 1 and grown["drawn"],
          str(grown))
    check("and the view re-fits around it", centred(grown) and grown["inside"],
          f"offset=({grown['offsetX']}, {grown['offsetY']}) inside={grown['inside']}")

    # --- a narrower window re-fits too ------------------------------------
    page.set_viewport_size({"width": 700, "height": 1000})
    page.wait_for_timeout(1200)
    narrow = page.evaluate(READ_GRAPH)
    check("a resize re-fits the view", centred(narrow) and narrow["inside"],
          f"offset=({narrow['offsetX']}, {narrow['offsetY']}) inside={narrow['inside']}")
    check("no sideways scrolling at 700px",
          page.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth") <= 0,
          "")

    check("no console or page errors", not errors, "; ".join(errors[:3]))
    browser.close()

width = max(len(name) for name, _, _ in results)
failed_count = sum(0 if ok else 1 for _, ok, _ in results)
for name, ok, detail in results:
    print(f"{'PASS' if ok else 'FAIL'}  {name.ljust(width)}  {detail}")
print(f"\n{len(results) - failed_count}/{len(results)} passed")
sys.exit(1 if failed_count else 0)

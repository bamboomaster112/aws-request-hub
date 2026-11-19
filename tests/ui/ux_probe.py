#!/usr/bin/env python3
"""Times every screen of the deployed app as each role: how long until the content is on screen,
and how long each API call took. Run from CloudShell after scripts/seed-users.py.

  python3 tests/ui/ux_probe.py
"""
import pathlib
import statistics
import sys
import time

from playwright.sync_api import sync_playwright

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from run_ui_tests import LiveTarget  # noqa: E402

SCREENS = {
    "requester": [("#/mine", "My requests"), ("#/new", "New request"), ("#/mine", "My requests")],
    "manager": [("#/inbox", "Approvals waiting for you"), ("#/mine", "My requests")],
    "admin": [("#/admin/users", "Users"), ("#/admin/org", "Org chart"), ("#/admin/groups", "Approver groups"),
              ("#/admin/workflows/PURCHASE", "Approval workflows"), ("#/admin/forms/PURCHASE", "Request forms"),
              ("#/admin/auto-approval", "Auto-approval"), ("#/inbox", "Approvals waiting for you")],
}


def main():
    t = LiveTarget()
    rows, api_times = [], []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            for who, screens in SCREENS.items():
                page = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
                start = time.time()
                page.goto(t.base_url)
                page.evaluate("t => sessionStorage.setItem('id_token', t)", t.token(who))
                page.reload()
                page.get_by_role("heading", name="My requests").wait_for(timeout=20000)
                rows.append((who, "first load after sign-in", time.time() - start))
                for hash_, heading in screens:
                    page.evaluate("() => performance.clearResourceTimings()")
                    start = time.time()
                    page.evaluate("h => { window.location.hash = h; }", hash_)
                    page.get_by_role("heading", name=heading, exact=True).wait_for(timeout=20000)
                    page.wait_for_load_state("networkidle")
                    took = time.time() - start
                    calls = page.evaluate("""() => performance.getEntriesByType('resource')
                        .filter(e => e.name.includes('/v1/') && !e.name.includes('/assets/') && !e.name.endsWith('config.json'))
                        .map(e => [new URL(e.name).pathname.replace(/[0-9a-f-]{36}/g, ':id'), Math.round(e.duration)])""")
                    api_times += [ms for _, ms in calls]
                    rows.append((who, hash_, took, calls))
                page.context.close()
            browser.close()
    finally:
        t.close()
    print(f"{'role':<10} {'screen':<28} {'ready':>7}  API calls (ms)")
    for row in rows:
        calls = "  ".join(f"{p.split('/v1')[-1]} {ms}" for p, ms in (row[3] if len(row) > 3 else []))
        print(f"{row[0]:<10} {row[1]:<28} {row[2]:>6.2f}s  {calls}")
    if api_times:
        api_times.sort()
        print(f"\nAPI calls: {len(api_times)}, median {statistics.median(api_times):.0f} ms, "
              f"p90 {api_times[int(len(api_times) * 0.9) - 1]:.0f} ms, slowest {api_times[-1]:.0f} ms")


if __name__ == "__main__":
    main()

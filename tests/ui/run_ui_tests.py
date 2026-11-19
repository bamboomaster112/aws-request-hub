#!/usr/bin/env python3
"""Browser tests for the Request Hub web app (Playwright, headless Chromium).

Two targets run the same scenarios:

  python3 tests/ui/run_ui_tests.py           # local: the real Lambda handlers behind a local server,
                                             # moto for AWS. No account needed; runs in CI.
  python3 tests/ui/run_ui_tests.py --live    # the deployed stack, from CloudShell, after
                                             # python3 scripts/seed-users.py

Test users are signed in programmatically: the tests put an ID token in the page's session
storage, exactly where the app keeps the one it gets from the Cognito hosted UI, so no test
ever types a password. Live runs leave shared configuration alone: they only add the test
admin to the Finance group for the run and remove the auto-approval they grant. Steps that
change forms, workflows or people, or that would email someone, run locally only.

Options: --headed, --slow-mo MS, --shots DIR (screenshot of every failure goes there anyway),
--only NAME[,NAME] to run some scenarios, --list to show them.
"""
import argparse
import base64
import datetime
import json
import pathlib
import re
import sys
import time
import traceback
import urllib.parse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import expect, sync_playwright

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
results = []
SHOTS = ROOT / "ui-test-results"
SCENARIOS = []
USED = set()  # (view, control) pairs the tests have clicked, typed into or changed

# Runs in every test page: names each control the same way for "used" and for the inventory.
USAGE_JS = r"""
(() => {
  const viewOf = () => {
    const p = location.hash.replace(/^#\/?/, '').split('?')[0].split('/');
    if (!sessionStorage.getItem('id_token')) return 'landing';
    if (!p[0] || p[0] === 'mine') return 'my requests';
    if (p[0] === 'new') return 'new request';
    if (p[0] === 'inbox') return 'approvals';
    if (p[0] === 'requests') return 'request detail';
    if (p[0] === 'admin') {
      const section = p[1] || 'users';
      if (section === 'users') return p[2] === 'new' ? 'admin: invite user' : (p[2] ? 'admin: user' : 'admin: users');
      return 'admin: ' + section;
    }
    return p[0];
  };
  const labelText = (e) => {
    const l = e.labels && e.labels[0];
    if (!l) return '';
    const copy = l.cloneNode(true);
    copy.querySelectorAll('select, input, textarea, button').forEach((x) => x.remove());
    return copy.textContent;
  };
  const label = (e) => (e.getAttribute('aria-label') || labelText(e)
    || (e.tagName === 'INPUT' || e.tagName === 'TEXTAREA' || e.tagName === 'SELECT' ? '' : e.innerText) || '')
    .replace(/\s+/g, ' ').replace(/\d+/g, 'N').trim().slice(0, 48);
  window.__uiDescribe = (e) => {
    let view = viewOf();
    if (e.closest('header.topbar') || e.matches('.skip-link')) view = 'header';
    else if (e.closest('nav.rail')) view = 'admin menu';
    else if (e.closest('dialog')) view = 'confirm dialog';
    else if (e.closest('#toast')) view = 'toast';
    let control;
    if (e.matches('a.card.ticket')) control = 'request card';
    else if (e.matches('.menu-trigger')) control = 'account menu';
    else if (e.matches('tr a.title')) control = 'table row';
    else if (e.matches('a.org-card')) control = 'person card';
    else if (e.matches('tr.clickable')) control = 'table row';
    else if (e.closest('ul.files') && e.tagName === 'A') control = 'attachment download';
    else if (e.closest('.pills') && e.tagName === 'A') control = 'module tab';
    else if (e.closest('.preview')) control = 'form preview field';
    else {
      const kind = e.tagName === 'INPUT' ? `input[${e.type}]` : e.tagName.toLowerCase();
      control = `${kind}: ${label(e)}`;
    }
    return { view, control, unlabeled: ['INPUT', 'SELECT', 'TEXTAREA'].includes(e.tagName) && !label(e) };
  };
  const record = (ev) => {
    const e = ev.target && ev.target.closest && ev.target.closest('button, a[href], input, select, textarea, tr.clickable');
    if (!e || !window.__uiUsed) return;
    try { window.__uiUsed(window.__uiDescribe(e)); } catch (err) { /* page navigating */ }
  };
  ['click', 'change', 'input', 'keydown'].forEach((t) => document.addEventListener(t, record, true));
})();
"""
CONTROLS_JS = """() => [...document.querySelectorAll('button, a[href], input, select, textarea, tr.clickable')]
  .filter((e) => e.offsetParent !== null && !e.closest('.preview')).map((e) => window.__uiDescribe(e))"""


def scenario(local_only=False):
    def wrap(fn):
        SCENARIOS.append((fn.__name__, fn, local_only))
        return fn
    return wrap


def check(name, condition, detail=""):
    ok = bool(condition)
    results.append((name, ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail and not ok else ""))
    return ok


def verify(name, page, assertion):
    """Runs a Playwright assertion; records a screenshot when it fails."""
    try:
        assertion()
        return check(name, True)
    except (AssertionError, PlaywrightError) as exc:
        shot = SHOTS / f"{len(results) + 1:03d}-{re.sub(r'[^a-z0-9]+', '-', name.lower())[:60]}.png"
        try:
            page.screenshot(path=str(shot), full_page=True)
        except PlaywrightError:
            shot = "no screenshot"
        try:
            toast = page.locator("#toast").inner_text(timeout=1000) if page.locator("#toast").is_visible() else ""
        except PlaywrightError:
            toast = ""
        return check(name, False, f"{str(exc).splitlines()[0][:200]}{f' [page said: {toast}]' if toast else ''} (see {shot})")


# ------------------------------------------------------------------ targets
class LocalTarget:
    live = False

    def __init__(self):
        self.shared = {}
        sys.path.insert(0, str(HERE))
        from local_backend import LocalHub
        self.hub = LocalHub().start()
        for module in self.hub.h.values():  # keep handler logs out of the test output
            module.print = lambda *a, **k: None
        for name in ("common", "users", "config_admin"):
            if name in sys.modules:
                sys.modules[name].print = lambda *a, **k: None
        self.base_url = self.hub.base_url
        self.ids = dict(self.hub.ids)
        self.names = {"requester": "Rae Requester", "manager": "Mia Manager", "finance": "Farah Finance", "admin": "Asha Admin"}

    def token(self, who):
        return self.hub.token_for(who)

    def time_out(self, request_id):
        self.hub.time_out(request_id)

    def route_uploads(self, context):
        """Presigned POSTs go to S3; locally, store the object in moto and answer like S3 does."""
        def handle(route):
            if route.request.method == "GET":  # a presigned download: serve what was uploaded
                key = urllib.parse.unquote(urllib.parse.urlparse(route.request.url).path.lstrip("/"))
                content = self.hub.get_attachment(key)
                query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(route.request.url).query))
                headers = {"Content-Disposition": query["response-content-disposition"]} if "response-content-disposition" in query else {}
                return route.fulfill(status=200 if content is not None else 404, body=content or b"", content_type="text/plain", headers=headers)
            body = route.request.post_data_buffer or b""
            key = re.search(rb'name="key"\r\n\r\n([^\r]+)', body)
            data = re.search(rb'name="file"; filename="[^"]*"\r\nContent-Type: [^\r]*\r\n\r\n(.*?)\r\n--', body, re.S)
            if key:
                self.hub.put_attachment(key.group(1).decode(), data.group(1) if data else b"")
            route.fulfill(status=204, body="")
        context.route(re.compile(r"https://[^/]*amazonaws\.com/.*"), handle)

    def close(self):
        self.hub.stop()


class LiveTarget:
    live = True

    def __init__(self):
        self.shared = {}
        sys.path.insert(0, str(ROOT / "scripts"))
        import hubkit
        self.kit = hubkit
        self.out = hubkit.outputs()
        self.base_url = self.out["AppUrl"].rstrip("/") + "/"
        self.api = self.out["ApiBaseUrl"]
        users = hubkit.load_users()
        if "admin2" not in users:
            sys.exit("The UI tests need the second test admin (a three-level Change chain needs two admins after the "
                     "manager). Add it without touching the others: python3 scripts/seed-users.py --missing-only")
        # The scripted test users: requester -> approver (manager) -> admin, plus a second admin.
        # The admin also sits in Finance for the run.
        self.users = {"requester": users["requester"], "manager": users["approver"], "admin": users["admin"],
                      "admin2": users["admin2"], "finance": users["admin"]}
        self.ids = {k: v["userId"] for k, v in self.users.items()}
        self._tokens = {}
        self.names = {}
        admin = self.token("admin")
        status, directory = hubkit.http("GET", f"{self.api}/admin/users", admin)
        if status != 200:
            sys.exit(f"Cannot read the user directory with the test admin: {status} {directory}")
        by_id = {u["userId"]: u for u in directory["items"]}
        self.names = {k: (by_id.get(uid) or {}).get("name") or (by_id.get(uid) or {}).get("email") for k, uid in self.ids.items()}
        status, config = hubkit.http("GET", f"{self.api}/admin/config", admin)
        finance = next((g for g in config["groups"] if g["groupId"] == "finance" and not g.get("unsaved")), None)
        if not finance:
            sys.exit("The Finance approver group is missing. Run scripts/seed-config.py first.")
        self.finance_before = list(finance.get("members") or [])
        if self.ids["finance"] not in self.finance_before:
            body = {"name": finance["name"], "description": finance.get("description", ""),
                    "members": self.finance_before + [self.ids["finance"]], "version": finance["version"]}
            status, res = hubkit.http("PUT", f"{self.api}/admin/groups/finance", admin, body)
            if status != 200:
                sys.exit(f"Could not add the test admin to Finance: {status} {res}")
        for module in ("PURCHASE", "GENERAL"):
            hubkit.http("DELETE", f"{self.api}/admin/users/{self.ids['requester']}/auto-rules/{module}", admin)

    def token(self, who):
        user = self.users[who]
        if user["username"] not in self._tokens:
            self._tokens[user["username"]] = self.kit.sign_in(self.out, user)
        return self._tokens[user["username"]]

    def route_uploads(self, context):
        pass  # real S3

    def pace(self, limit):
        """Waits while any Lambda function is near the playground's 300-calls-an-hour limit."""
        if not limit:
            return
        waited = 0
        while True:
            try:
                usage = self.kit.lambda_usage()
            except Exception as exc:  # noqa: BLE001 (CloudWatch unavailable: carry on, but say so)
                print(f"  (could not read Lambda usage: {exc}; not pacing)")
                return
            name, busiest = max(usage.items(), key=lambda kv: kv[1], default=("", 0))
            if busiest < limit:
                if waited:
                    print(f"  (resuming: the busiest function, {name}, is now at {busiest} calls in the last hour)")
                return
            if not waited:
                print(f"  (pausing: {name} has {busiest} calls in the last hour, the limit for this run is {limit}; "
                      "waiting for older calls to age out)")
            time.sleep(60)
            waited += 1
            if waited % 5 == 0:
                print(f"  (still waiting: {busiest} calls, {waited} min so far)")

    def close(self):
        admin = self.token("admin")
        for module in ("PURCHASE", "GENERAL"):
            self.kit.http("DELETE", f"{self.api}/admin/users/{self.ids['requester']}/auto-rules/{module}", admin)
        status, config = self.kit.http("GET", f"{self.api}/admin/config", admin)
        finance = next((g for g in config["groups"] if g["groupId"] == "finance"), None)
        if finance and set(finance.get("members") or []) != set(self.finance_before):
            body = {"name": finance["name"], "description": finance.get("description", ""),
                    "members": self.finance_before, "version": finance["version"]}
            self.kit.http("PUT", f"{self.api}/admin/groups/finance", admin, body)


# ------------------------------------------------------------------ browser helpers
class Session:
    """One person in their own browser context, signed in through session storage."""

    def __init__(self, browser, target, who, viewport=None, wait_for_home=True, color_scheme="light", bypass_csp=False):
        self.who, self.target = who, target
        self.context = browser.new_context(viewport=viewport or {"width": 1280, "height": 900}, color_scheme=color_scheme, bypass_csp=bypass_csp)
        self.context.expose_binding("__uiUsed", lambda _source, d: USED.add((d["view"], d["control"])))
        self.context.add_init_script(USAGE_JS)
        target.route_uploads(self.context)
        self.errors = []
        self.page = self.context.new_page()
        self.page.on("pageerror", lambda exc: self.errors.append(str(exc)))
        self.page.on("console", lambda msg: self.errors.append(msg.text) if msg.type == "error" and "Failed to load resource" not in msg.text else None)
        self.page.goto(target.base_url)
        if who:
            self.page.evaluate("t => sessionStorage.setItem('id_token', t)", target.token(who))
            self.page.reload()
            expect(self.page.get_by_role("button", name="Account menu")).to_be_visible(timeout=15000)
            if wait_for_home:  # let the first view finish loading before a test navigates elsewhere
                expect(self.page.get_by_role("heading", name="My requests")).to_be_visible(timeout=15000)

    def go(self, hash_):
        self.page.evaluate("h => { window.location.hash = h; }", hash_)

    def close(self):
        self.context.close()


def badge(page):
    return page.locator(".section-head .badge[data-status]").first


def wait_for_status(page, statuses, tries=10, pause=2.5):
    """Re-reads the request (the Refresh button) until its status is one of statuses.
    Returns the status code with spaces for underscores ("PENDING APPROVAL")."""
    wanted = set(statuses)
    for _ in range(tries):
        try:
            code = badge(page).get_attribute("data-status", timeout=5000) or ""
        except PlaywrightError:
            code = ""
        if code in wanted:
            return code.replace("_", " ")
        time.sleep(pause)
        page.locator(".section-head").get_by_role("button", name="Refresh").click()
        page.wait_for_load_state("networkidle")
    return (badge(page).get_attribute("data-status") or "").replace("_", " ")


def choose(trigger, value):
    """Picks an option in the app's select (a combobox button that opens a listbox) by its value."""
    trigger.click()
    listbox = trigger.page.locator(f"#{trigger.get_attribute('aria-controls')}")
    listbox.locator(f'[role=option][data-value="{value}"]').click()
    expect(trigger).to_have_attribute("data-value", str(value))


def option_values(trigger):
    """The values a select offers (it opens the list, reads it and closes it again)."""
    trigger.click()
    listbox = trigger.page.locator(f"#{trigger.get_attribute('aria-controls')}")
    expect(listbox).to_be_visible()
    values = listbox.locator("[role=option]").evaluate_all("els => els.map(e => e.dataset.value)")
    trigger.page.keyboard.press("Escape")
    return values


def slip(page):
    """The approval levels on a request's routing slip."""
    return page.locator("#slip li.slip-step")


def fill_purchase(page, title, amount, item="Laptop stand"):
    page.locator("label.choice", has_text="Purchasing").click()
    page.get_by_label("Title").fill(title)
    page.get_by_label("What are you buying?").fill(item)
    choose(page.get_by_label("Category"), "Hardware")
    page.get_by_label("Total cost").fill(str(amount))
    page.get_by_label("Business justification").fill("Needed for the UI test run.")


def submit_purchase(session, title, amount):
    page = session.page
    session.go("#/new")
    expect(page.get_by_role("heading", name="New request")).to_be_visible()
    fill_purchase(page, title, amount)
    page.get_by_role("button", name="Submit request").click()
    expect(page).to_have_url(re.compile(r"#/requests/[0-9a-f-]{36}$"), timeout=15000)
    return page.url.rsplit("/", 1)[-1]


def open_from_inbox(session, title):
    page = session.page
    session.go("#/inbox")
    expect(page.get_by_role("heading", name="Approvals", exact=True)).to_be_visible()
    card = page.locator("a.card.ticket", has_text=title)
    expect(card).to_be_visible(timeout=10000)
    card.click()
    expect(page.get_by_role("heading", name=title)).to_be_visible()


def stamp():
    return datetime.datetime.now().strftime("%H%M%S")


# ------------------------------------------------------------------ scenarios
@scenario()
def signed_out_and_expired_token(browser, t):
    s = Session(browser, t, None)
    page = s.page
    verify("Signed out: the landing page offers Sign in and hides the navigation", page, lambda: (
        expect(page.get_by_role("button", name="Sign in")).to_be_visible(), expect(page.locator("#nav")).to_be_hidden()))
    payload = base64.urlsafe_b64encode(json.dumps({"sub": "x", "exp": int(time.time()) - 60}).encode()).decode().rstrip("=")
    page.evaluate("t => sessionStorage.setItem('id_token', t)", f"e30.{payload}.x")
    page.reload()
    verify("An expired token is dropped before any API call", page, lambda: (
        expect(page.get_by_role("button", name="Sign in")).to_be_visible(),
        expect(page.locator("#nav")).to_be_hidden()))
    check("No JavaScript errors on the landing page", not s.errors, s.errors[:3])
    s.close()


@scenario()
def navigation_by_role(browser, t):
    for who, inbox, admin in (("requester", False, False), ("manager", True, False), ("admin", True, True)):
        s = Session(browser, t, who)
        page = s.page
        verify(f"{who.title()}: navigation shows Approvals={inbox} and Admin={admin}", page, lambda: (
            expect(page.locator("#nav").get_by_role("link", name="My requests")).to_be_visible(),
            expect(page.locator("#nav").get_by_role("link", name="New request")).to_be_visible(),
            expect(page.locator("#inboxTab")).to_be_visible() if inbox else expect(page.locator("#inboxTab")).to_be_hidden(),
            expect(page.locator("#adminTab")).to_be_visible() if admin else expect(page.locator("#adminTab")).to_be_hidden()))
        if not admin:
            s.go("#/admin/users")
            verify(f"{who.title()}: opening #/admin sends them back to My requests", page, lambda: (
                expect(page).to_have_url(re.compile(r"#/mine$")), expect(page.get_by_role("heading", name="My requests")).to_be_visible()))
        check(f"{who.title()}: no JavaScript errors", not s.errors, s.errors[:3])
        s.close()


@scenario()
def quick_navigation_lands_on_the_last_click(browser, t):
    """A slow 'My requests' response must not paint over the view someone has already moved to."""
    s = Session(browser, t, "manager", wait_for_home=False)
    page = s.page

    def slow_list(route):
        time.sleep(2.5)
        route.continue_()
    page.route(re.compile(r".*/requests(\?.*)?$"), lambda r: slow_list(r) if r.request.method == "GET" else r.continue_())
    page.reload()
    s.go("#/inbox")
    page.wait_for_timeout(4000)
    verify("Clicking Approvals while My requests is still loading ends on Approvals", page, lambda: (
        expect(page.get_by_role("heading", name="Approvals", exact=True)).to_be_visible(),
        expect(page.get_by_role("heading", name="My requests")).to_have_count(0)))
    page.reload()
    s.go("#/requests/00000000-0000-4000-8000-000000000000")
    page.wait_for_timeout(4000)
    verify("A page that fails quickly is not painted over by a slow page from before", page, lambda: (
        expect(page.get_by_role("heading", name="Not found")).to_be_visible(),
        expect(page.get_by_role("heading", name="My requests")).to_have_count(0)))
    page.unroute(re.compile(r".*/requests(\?.*)?$"))
    # Hold the My requests response until the next page is painted and someone has started typing.
    held = []
    page.route(re.compile(r".*/requests(\?.*)?$"), lambda r: held.append(r) if r.request.method == "GET" else r.continue_())
    s.go("#/mine")
    page.reload()
    expect(page.get_by_role("heading", name="My requests")).to_be_visible()
    s.go("#/new")
    page.locator("label.choice", has_text="Purchasing").click()
    page.get_by_label("Title").fill("Started before My requests finished loading")
    for r in held:
        r.continue_()
    page.wait_for_timeout(1500)
    check("The My requests response was held until the form was in use", bool(held), "nothing was held")
    verify("A slow page from before does not reload the form someone has started", page, lambda: (
        expect(page.locator("label.choice", has_text="Purchasing").locator("input")).to_be_checked(),
        expect(page.get_by_label("Title")).to_have_value("Started before My requests finished loading")))
    page.unroute(re.compile(r".*/requests(\?.*)?$"))
    s.close()


@scenario()
def dynamic_form_and_validation(browser, t):
    s = Session(browser, t, "requester")
    page = s.page
    s.go("#/new")
    verify("New request lists every module the requester holds", page, lambda: expect(page.locator("label.choice")).to_have_count(5))
    page.locator("label.choice", has_text="Purchasing").click()
    verify("Purchasing shows only its own fields", page, lambda: (
        expect(page.get_by_label("Total cost")).to_be_visible(),
        expect(page.get_by_label("Business justification")).to_be_visible(),
        expect(page.get_by_label("Working days")).to_have_count(0)))
    page.locator("label.choice", has_text="Leave").click()
    verify("Switching to Leave swaps the fields", page, lambda: (
        expect(page.get_by_label("Working days")).to_be_visible(),
        expect(page.get_by_label("Leave type")).to_be_visible(),
        expect(page.get_by_label("Total cost")).to_have_count(0)))
    page.locator("label.choice", has_text="Purchasing").click()
    page.get_by_label("Total cost").fill("500")
    verify("Route preview: 500 skips Finance", page, lambda: expect(page.locator(".route-preview li.slip-step", has_text="Finance")).to_have_class(re.compile("skipped")))
    page.get_by_label("Total cost").fill("1500")
    verify("Route preview: 1,500 adds Finance", page, lambda: expect(page.locator(".route-preview li.slip-step", has_text="Finance")).to_have_class(re.compile("applies")))
    unlabeled = page.evaluate("""() => [...document.querySelectorAll('form.card input, form.card select, form.card textarea')]
        .filter(el => el.type !== 'hidden' && !(el.labels && el.labels.length) && !el.getAttribute('aria-label')).map(el => el.name || el.outerHTML.slice(0, 60))""")
    check("Every control on the request form has a label (screen readers, get_by_label)", not unlabeled, unlabeled)
    page.get_by_label("Total cost").fill("")
    posts = []
    page.on("request", lambda r: posts.append(r.url) if r.method == "POST" and r.url.rstrip("/").endswith("/requests") else None)
    page.get_by_role("button", name="Submit request").click()
    verify("Submitting an empty form shows errors next to each required field", page, lambda: (
        expect(page.locator(".dyn-field.has-error")).to_have_count(5),
        expect(page.locator(".dyn-field.has-error", has_text="Total cost")).to_contain_text("This field is required"),
        expect(page.locator(".dyn-field", has_text="Title").locator(".field-error")).to_contain_text("3 to 120 characters"),
        expect(page.locator("#toast")).to_contain_text("Check the highlighted fields")))
    check("The form is checked in the browser first: no API call for an incomplete form", not posts, posts)
    covered = page.evaluate("""() => { const el = document.activeElement, bar = document.querySelector('.topbar');
        return el && bar ? el.getBoundingClientRect().top < bar.getBoundingClientRect().bottom : true; }""")
    check("The first problem field gets focus below the sticky top bar, not under it", not covered)
    page.get_by_label("Title").fill("Monitor arm")
    page.get_by_label("What are you buying?").fill("Monitor arm")
    choose(page.get_by_label("Category"), "Hardware")
    page.get_by_label("Total cost").fill("2000000")
    page.get_by_label("Business justification").fill("Neck strain.")
    page.get_by_role("button", name="Submit request").click()
    verify("Out-of-range values are caught with the form's own limits", page, lambda: expect(
        page.locator(".dyn-field.has-error", has_text="Total cost")).to_contain_text("between 0 and 1,000,000"))
    verify("Nothing was created: still on the New request page", page, lambda: expect(page).to_have_url(re.compile(r"#/new")))
    check("No JavaScript errors on the request form", not s.errors, s.errors[:3])
    s.close()


@scenario()
def two_level_chain(browser, t):
    title = f"UI test laptops {stamp()}"
    req = Session(browser, t, "requester")
    rid = submit_purchase(req, title, 1500)
    page = req.page
    status = wait_for_status(page, ["PENDING_APPROVAL"])
    check("Requester: the new request is pending at level 1", status == "PENDING APPROVAL", status)
    verify("Requester: the chain shows the line manager now and Finance next", page, lambda: (
        expect(slip(page).nth(0)).to_have_class(re.compile("active")),
        expect(slip(page).nth(0)).to_contain_text("Line manager"),
        expect(slip(page).nth(1)).to_contain_text("Finance")))
    verify("Requester: no decision buttons on their own request", page, lambda: expect(page.get_by_role("button", name="Approve", exact=True)).to_have_count(0))

    mgr = Session(browser, t, "manager")
    open_from_inbox(mgr, title)
    mp = mgr.page
    verify("Manager: decision panel says level 1 of 2", mp, lambda: (
        expect(mp.get_by_role("heading", name="Your decision")).to_be_visible(),
        expect(mp.locator(".decision")).to_contain_text("Level 1 of 2")))
    mp.get_by_label("Decision note").fill("Agreed, the old ones are failing.")
    mp.get_by_role("button", name="Approve", exact=True).click()
    verify("Manager: approval recorded", mp, lambda: expect(mp.locator("#toast")).to_contain_text("Approved. The workflow"))
    mgr.go("#/inbox")
    verify("Manager: it leaves their inbox (they cannot approve level 2 as well)", mp, lambda: (
        expect(mp.get_by_role("heading", name="Approvals", exact=True)).to_be_visible(),
        expect(mp.locator("a.card.ticket", has_text=title)).to_have_count(0, timeout=10000)))

    fin = Session(browser, t, "finance")
    open_from_inbox(fin, title)
    fp = fin.page
    verify("Finance: level 2 of 2, the last level", fp, lambda: (
        expect(fp.locator(".decision")).to_contain_text("Level 2 of 2"),
        expect(fp.get_by_text("This is the last level")).to_be_visible()))
    fp.get_by_role("button", name="Approve", exact=True).click()
    verify("Finance: approval recorded", fp, lambda: expect(fp.locator("#toast")).to_contain_text("Approved. The workflow"))

    req.go(f"#/requests/{rid}")
    status = wait_for_status(page, ["FULFILLED"], tries=12)
    check("Requester: approved at both levels and fulfilled", status == "FULFILLED", status)
    verify("Requester: both decisions and the PO reference are shown", page, lambda: (
        expect(page.locator("#slip li.slip-step.done")).to_have_count(2),
        expect(page.locator(".field", has_text="Fulfilment reference")).to_contain_text("PO-")))
    req.go("#/mine")
    verify("Requester: My requests lists it as Fulfilled", page, lambda: expect(page.locator("tr", has_text=title).locator("[data-status]")).to_have_attribute("data-status", "FULFILLED"))
    for s in (req, mgr, fin):
        check(f"{s.who.title()}: no JavaScript errors in the approval flow", not s.errors, s.errors[:3])
        s.close()


@scenario()
def rejection_needs_a_note(browser, t):
    title = f"UI test reject {stamp()}"
    req = Session(browser, t, "requester")
    rid = submit_purchase(req, title, 300)
    wait_for_status(req.page, ["PENDING_APPROVAL"])
    mgr = Session(browser, t, "manager")
    open_from_inbox(mgr, title)
    mp = mgr.page
    posts = []
    mp.on("request", lambda r: posts.append(r.url) if r.method == "POST" and r.url.endswith("/decision") else None)
    mp.get_by_role("button", name="Reject", exact=True).click()
    verify("Rejecting without a note asks for one next to the note box, and sends nothing", mp, lambda: (
        expect(mp.locator(".dyn-field.has-error", has_text="Decision note")).to_contain_text("Add a note"),
        expect(mp.get_by_label("Decision note")).to_be_focused(),
        expect(mp.get_by_role("button", name="Reject", exact=True)).to_be_enabled()))
    check("No decision was sent without a note", not posts, posts)
    mp.get_by_label("Decision note").fill("No budget this quarter.")
    mp.get_by_role("button", name="Reject", exact=True).click()
    verify("Rejection with a note is recorded", mp, lambda: expect(mp.locator("#toast")).to_contain_text("Rejected. The workflow"))
    mgr.go("#/inbox")  # straight away, before the request page's own refresh is due
    mp.wait_for_timeout(2500)
    verify("Moving on right after a decision stays on the new page (the request page does not come back)", mp, lambda: (
        expect(mp.get_by_role("heading", name="Approvals", exact=True)).to_be_visible(),
        expect(mp.get_by_role("heading", name=title)).to_have_count(0)))
    req.go(f"#/requests/{rid}")
    status = wait_for_status(req.page, ["REJECTED"])
    check("Requester sees REJECTED", status == "REJECTED", status)
    verify("Requester sees the approver's note", req.page, lambda: expect(req.page.get_by_text("No budget this quarter.").first).to_be_visible())
    req.close()
    mgr.close()


@scenario()
def auto_approval_granted_in_admin(browser, t):
    adm = Session(browser, t, "admin")
    ap = adm.page
    adm.go("#/admin/auto-approval")
    expect(ap.get_by_role("heading", name="Auto-approval", exact=True)).to_be_visible()
    choose(ap.get_by_label("Person"), t.ids["requester"])
    choose(ap.get_by_label("Module"), "PURCHASE")
    ap.get_by_label("Up to (approval value)").fill("500")
    ap.get_by_label("Until").fill((datetime.date.today() + datetime.timedelta(days=60)).isoformat())
    ap.get_by_label("Note").fill("UI test grant")
    ap.get_by_role("button", name="Grant", exact=True).click()
    verify("Admin: granting asks for confirmation", ap, lambda: expect(ap.locator("#confirm")).to_be_visible())
    ap.locator("#confirmOk").click()
    verify("Admin: the rule is listed", ap, lambda: expect(ap.locator("table tr", has_text="Purchasing")).to_contain_text("500"))

    req = Session(browser, t, "requester")
    page = req.page
    req.go("#/new")
    page.locator("label.choice", has_text="Purchasing").click()
    page.get_by_label("Total cost").fill("300")
    verify("Requester: the form says 300 will be approved automatically", page, lambda: expect(page.locator(".note.ok")).to_contain_text("approved automatically"))
    page.get_by_label("Total cost").fill("600")
    verify("Requester: 600 is over the limit and follows the route", page, lambda: expect(page.locator(".route-preview .note")).to_contain_text("This value is higher"))
    rid = submit_purchase(req, f"UI test auto {stamp()}", 300)
    status = wait_for_status(page, ["AUTO_APPROVED", "FULFILLED"], tries=12)
    check("Requester: within the limit it is auto-approved", status in ("AUTO APPROVED", "FULFILLED"), status)
    verify("Requester: the routing slip explains the auto-approval", page, lambda: expect(slip(page).first).to_contain_text("Approved automatically"))
    verify("Requester: the reason names the module as people know it", page, lambda: expect(page.locator(".reasons")).to_contain_text("self-approve Purchasing requests"))

    adm.go("#/admin/auto-approval")
    row = ap.locator("table tr", has_text="Purchasing")
    expect(row).to_be_visible()
    row.get_by_role("button", name="Revoke").click()
    ap.locator("#confirmOk").click()
    verify("Admin: revoking removes the rule", ap, lambda: expect(ap.locator("table tr", has_text="Purchasing")).to_have_count(0, timeout=10000))
    check("No JavaScript errors while granting and revoking", not adm.errors and not req.errors, (adm.errors + req.errors)[:3])
    del rid
    adm.close()
    req.close()


@scenario()
def admin_screens(browser, t):
    adm = Session(browser, t, "admin")
    ap = adm.page
    adm.go("#/admin/users")
    verify("Users: lists people with role, modules and manager", ap, lambda: (
        expect(ap.get_by_role("heading", name="Users")).to_be_visible(),
        expect(ap.locator("tbody tr").first).to_be_visible(),
        expect(ap.get_by_role("columnheader", name="Reports to")).to_be_visible()))
    adm.go(f"#/admin/users/{t.ids['requester']}")
    verify("User detail: access, reporting line, auto-approval and history", ap, lambda: (
        expect(ap.get_by_role("heading", name="Access and reporting line")).to_be_visible(),
        expect(ap.get_by_role("heading", name="Auto-approval", exact=True)).to_be_visible(),
        expect(ap.get_by_role("heading", name="Access history")).to_be_visible(),
        expect(ap.get_by_label("Manager (level 1 approver)")).to_have_attribute("data-value", t.ids["manager"])))
    adm.go(f"#/admin/users/{t.ids['admin']}")
    expect(ap.get_by_text("You cannot change your own role")).to_be_visible()  # the admin's own page has rendered
    values = option_values(ap.get_by_label("Manager (level 1 approver)"))
    check("Manager picker never offers yourself or someone below you (no loops)",
          t.ids["admin"] not in values and t.ids["manager"] not in values and t.ids["requester"] not in values, values)
    adm.go("#/admin/org")
    verify("Org chart: the requester sits under the manager", ap, lambda: expect(
        ap.locator(".org-tree li", has=ap.locator("a.org-card", has_text=t.names["manager"])).locator("ul a.org-card", has_text=t.names["requester"]).first).to_be_visible())
    adm.go("#/admin/groups")
    verify("Approver groups: Finance has members", ap, lambda: expect(ap.locator(".card", has_text="Finance").first).to_be_visible())
    adm.go("#/admin/workflows/PURCHASE")
    verify("Workflows: Purchasing has its levels and a plain-English summary", ap, lambda: (
        expect(ap.locator(".level-row")).to_have_count(3),
        expect(ap.locator(".note", has_text="Every Purchasing request needs")).to_be_visible()))
    adm.go("#/admin/forms/PURCHASE")
    verify("Forms: the builder lists fields and previews them", ap, lambda: (
        expect(ap.locator(".builder-field")).not_to_have_count(0),
        expect(ap.locator(".preview").get_by_text("Total cost")).to_be_visible()))
    check("No JavaScript errors across the admin screens", not adm.errors, adm.errors[:3])
    adm.close()


@scenario()
def attachments(browser, t):
    req = Session(browser, t, "requester")
    page = req.page
    # Show the new request as still SUBMITTED, as on AWS while the workflow picks it up, so the page schedules
    # its automatic re-check. Choosing a file during that window must survive the re-check.
    first = {"done": False}

    def still_submitted(route):
        if route.request.method == "GET" and not first["done"]:
            first["done"] = True
            response = route.fetch()
            body = response.json()
            body["status"] = "SUBMITTED"
            return route.fulfill(response=response, json=body)
        return route.continue_()
    page.route(re.compile(r".*/requests/[0-9a-f-]{36}$"), still_submitted)
    rid = submit_purchase(req, f"UI test attachment {stamp()}", 200)
    page.get_by_label("Choose a file to attach").set_input_files(files=[{"name": "quote.txt", "mimeType": "text/plain", "buffer": b"Quote 42"}])
    page.wait_for_timeout(3500)  # longer than the 2.5 s automatic re-check
    check("A file chosen while the new request is being routed is still selected after the automatic re-check",
          page.evaluate("() => { const i = document.querySelector('#app input[type=file]'); return !!(i && i.files.length); }"))
    page.unroute(re.compile(r".*/requests/[0-9a-f-]{36}$"))
    page.get_by_role("button", name="Upload").click()
    verify("Upload goes straight to S3 and the file is listed with a download link", page, lambda: (
        expect(page.locator("#toast")).to_contain_text("quote.txt uploaded"),
        expect(page.locator("ul.files a", has_text="quote.txt")).to_be_visible(timeout=15000)))
    link = page.locator("ul.files a", has_text="quote.txt")
    check("The download link is a short-lived presigned HTTPS URL", (link.get_attribute("href") or "").startswith("https://")
          and "Expires=" in (link.get_attribute("href") or "") or "X-Amz-Expires" in (link.get_attribute("href") or ""), (link.get_attribute("href") or "")[:60])
    with page.expect_download(timeout=15000) as download:
        link.click()
    saved = download.value
    check("Download: the link downloads the file (never opened inside the site) with its name and content",
          saved.suggested_filename == "quote.txt" and pathlib.Path(saved.path()).read_bytes() == b"Quote 42",
          (saved.suggested_filename, pathlib.Path(saved.path()).read_bytes()[:20]))
    req.close()
    close_request(browser, t, rid)


def close_request(browser, t, request_id, mgr=None):
    """Rejects a test request at level 1 so live runs never leave work in anyone's inbox.
    Pass a manager Session to close several requests with one sign-in (fewer API calls live)."""
    own = mgr is None
    mgr = mgr or Session(browser, t, "manager")
    mgr.go(f"#/requests/{request_id}")
    expect(mgr.page.get_by_role("button", name="Reject", exact=True)).to_be_visible(timeout=15000)
    mgr.page.get_by_label("Decision note").fill("Closing a UI test request.")
    mgr.page.get_by_role("button", name="Reject", exact=True).click()
    verify("Test request closed so nothing is left in an inbox", mgr.page, lambda: expect(mgr.page.locator("#toast")).to_contain_text("Rejected. The workflow"))
    if own:
        mgr.close()


@scenario()
def mobile_layout(browser, t):
    s = Session(browser, t, "requester", viewport={"width": 390, "height": 844})
    page = s.page
    rid = submit_purchase(s, f"UI test phone {stamp()}", 90)
    wait_for_status(page, ["PENDING_APPROVAL"])
    for hash_, heading in (("#/mine", "My requests"), ("#/new", "New request"), (f"#/requests/{rid}", None)):
        s.go(hash_)
        if heading:
            expect(page.get_by_role("heading", name=heading)).to_be_visible()
        else:
            expect(page.locator("#slip")).to_be_visible()
            heading = "the request page"
            first = page.evaluate("""() => { const slip = document.querySelector('#slip').getBoundingClientRect().top;
                const answers = document.querySelector('.answers-card').getBoundingClientRect().top; return slip < answers; }""")
            check("Phone width: the routing slip comes before the answers", first)
        if hash_ == "#/new":
            page.locator("label.choice", has_text="Purchasing").click()
        overflow = page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")
        check(f"Phone width: {heading} has no sideways scrolling", overflow <= 1, f"{overflow}px too wide")
        clipped = page.evaluate("""() => [...document.querySelectorAll('#app .badge, #app button, #app a.btn')].filter((e) => {
            const r = e.getBoundingClientRect(); return e.offsetParent && (r.right > window.innerWidth + 1 || r.left < -1); }).map((e) => e.textContent.trim())""")
        check(f"Phone width: every status badge and button on {heading} is fully on screen", not clipped, clipped[:5])
    rows = page.evaluate("() => Math.round(document.querySelector('header.topbar').getBoundingClientRect().height)")
    check("Phone width: the header is two rows at most (logo and account, then the menu)", rows <= 110, f"{rows}px")
    s.close()
    close_request(browser, t, rid)


@scenario()
def sign_out(browser, t):
    s = Session(browser, t, "requester")
    page = s.page
    visited = []
    page.on("request", lambda r: visited.append(r.url) if r.is_navigation_request() else None)
    page.get_by_role("button", name="Account menu").click()
    verify("The account menu shows who is signed in and their role", page, lambda: (
        expect(page.get_by_role("menu")).to_contain_text(t.names["requester"]),
        expect(page.get_by_role("menu")).to_contain_text("Requester")))
    page.get_by_role("menuitem", name="Sign out").click()
    verify("Sign out clears the session and returns to the landing page", page, lambda: (
        expect(page.get_by_role("button", name="Sign in")).to_be_visible(timeout=15000),
        expect(page.locator("#nav")).to_be_hidden()))
    has_token = page.evaluate("() => sessionStorage.getItem('id_token')")
    check("No token is left in session storage", not has_token)
    check("Sign out goes through the identity provider's logout endpoint and back to the app",
          any("/logout?" in u and "logout_uri=" in u for u in visited) and page.url.startswith(t.base_url), [u[:90] for u in visited[-3:]])
    s.close()


@scenario()
def sign_in_redirect_uses_pkce(browser, t):
    s = Session(browser, t, None)
    page = s.page
    config = page.evaluate("async () => (await fetch('config.json', {cache: 'no-store'})).json()")
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_url(re.compile(r".*/(oauth2/authorize|login)\?.*"), timeout=15000)
    url = urllib.parse.urlparse(page.url)
    query = dict(urllib.parse.parse_qsl(url.query))
    check("Sign in goes to the identity provider's hosted sign-in page", page.url.startswith(config["cognitoDomain"]), page.url[:80])
    check("It asks for an authorization code for this app and returns to the app",
          query.get("client_id") == config["clientId"] and query.get("redirect_uri") == config["redirectUri"]
          and query.get("response_type") == "code", {k: query.get(k) for k in ("client_id", "redirect_uri", "response_type")})
    check("It uses PKCE (S256 challenge) and a random state value",
          query.get("code_challenge_method") == "S256" and len(query.get("code_challenge", "")) == 43 and len(query.get("state", "")) >= 16,
          {k: query.get(k, "")[:12] for k in ("code_challenge_method", "code_challenge", "state")})
    if t.live:
        verify("The hosted sign-in page loads with email and password boxes, without an error", page, lambda: (
            expect(page.locator("input[name=username]:visible").first).to_be_visible(timeout=15000),
            expect(page.locator("input[name=password]:visible").first).to_be_visible(),
            expect(page.get_by_text(re.compile("error was encountered|redirect_mismatch", re.I))).to_have_count(0)))
    page.go_back()
    page.wait_for_load_state()
    stored = page.evaluate("() => [sessionStorage.getItem('oauth_state'), (sessionStorage.getItem('pkce_verifier') || '').length]")
    check("The app kept the state and a 43-character verifier for the return trip", stored[0] == query.get("state") and stored[1] == 43, stored)
    s.close()


@scenario()
def sign_in_callback_guards(browser, t):
    s = Session(browser, t, None)
    page = s.page
    page.goto(t.base_url + "?code=forged-code&state=forged-state")
    verify("A sign-in response with the wrong state is refused", page, lambda: (
        expect(page.locator("#toast")).to_contain_text("could not be verified"),
        expect(page.get_by_role("button", name="Sign in")).to_be_visible()))
    check("...and the code is removed from the address bar", "code=" not in page.url, page.url)
    page.evaluate("() => { sessionStorage.setItem('oauth_state', 'state-ok-1234567890'); sessionStorage.setItem('pkce_verifier', 'v'.repeat(43)); }")
    page.goto(t.base_url + "?code=not-a-real-code&state=state-ok-1234567890")
    verify("A made-up authorization code is rejected by the token endpoint", page, lambda: (
        expect(page.locator("#toast")).to_contain_text("Could not complete sign-in"),
        expect(page.get_by_role("button", name="Sign in")).to_be_visible()))
    check("No token is stored after a failed sign-in", not page.evaluate("() => sessionStorage.getItem('id_token')"))
    check("No JavaScript errors while handling bad sign-in responses", not s.errors, s.errors[:3])
    s.close()


@scenario()
def approver_raises_own_request(browser, t):
    title = f"UI test approver's own {stamp()}"
    mgr = Session(browser, t, "manager")
    rid = submit_purchase(mgr, title, 200)
    page = mgr.page
    wait_for_status(page, ["PENDING_APPROVAL"])
    verify("Manager: their own request goes to their own manager", page, lambda: (
        expect(slip(page).nth(0)).to_contain_text("Line manager"),
        expect(slip(page).nth(0)).to_contain_text(t.names["admin"])))
    verify("Manager: no decision buttons on their own request", page, lambda: expect(page.get_by_role("button", name="Approve", exact=True)).to_have_count(0))
    adm = Session(browser, t, "admin")
    open_from_inbox(adm, title)
    verify("Admin (their manager): decision panel at level 1 of 1", adm.page, lambda: expect(
        adm.page.locator(".decision")).to_contain_text("Level 1 of 1"))
    adm.page.get_by_role("button", name="Approve", exact=True).click()
    verify("Admin: approval recorded", adm.page, lambda: expect(adm.page.locator("#toast")).to_contain_text("Approved. The workflow"))
    mgr.go(f"#/requests/{rid}")
    status = wait_for_status(page, ["FULFILLED"], tries=12)
    check("Manager: their request is approved by their manager and fulfilled", status == "FULFILLED", status)
    t.shared["manager_request"] = rid
    mgr.close()
    adm.close()


@scenario()
def admin_sees_everything_but_cannot_jump_the_chain(browser, t):
    title = f"UI test chain order {stamp()}"
    req = Session(browser, t, "requester")
    rid = submit_purchase(req, title, 150)
    wait_for_status(req.page, ["PENDING_APPROVAL"])
    adm = Session(browser, t, "admin")
    adm.go(f"#/requests/{rid}")
    verify("Admin: can open anyone's request", adm.page, lambda: expect(adm.page.get_by_role("heading", name=title)).to_be_visible())
    verify("Admin: no decision buttons while it waits for the line manager", adm.page, lambda: (
        expect(adm.page.locator("#slip li.slip-step.active")).to_contain_text("Line manager"),
        expect(adm.page.get_by_role("button", name="Approve", exact=True)).to_have_count(0)))
    mgr = Session(browser, t, "manager")
    open_from_inbox(mgr, title)
    mgr.page.get_by_label("Decision note").fill("Closing the UI test request.")
    mgr.page.get_by_role("button", name="Reject", exact=True).click()
    verify("Manager: closes it (rejected with a note)", mgr.page, lambda: expect(mgr.page.locator("#toast")).to_contain_text("Rejected. The workflow"))
    for s in (req, adm, mgr):
        s.close()


@scenario()
def requester_cannot_open_other_peoples_requests(browser, t):
    other = t.shared.get("manager_request")
    if not other:
        check("Requester isolation needs the manager's request from approver_raises_own_request", False, "run it first")
        return
    req = Session(browser, t, "requester")
    req.go(f"#/requests/{other}")
    verify("Requester: someone else's request is not found (its existence is not revealed)", req.page, lambda: (
        expect(req.page.locator("#toast")).to_contain_text("Request not found"),
        expect(req.page.get_by_role("heading", name="Not found")).to_be_visible(),
        expect(req.page.get_by_role("heading", name=re.compile("UI test approver's own"))).to_have_count(0)))
    req.page.get_by_role("button", name="Try again").click()
    verify("Requester: Try again asks once more and gets the same answer", req.page, lambda: expect(req.page.get_by_role("heading", name="Not found")).to_be_visible())
    req.go("#/inbox")
    verify("Requester: the approvals inbox is not available", req.page, lambda: expect(req.page).to_have_url(re.compile(r"#/mine$")))
    req.close()



# ------------------------------------------------------------------ every control, every module
MODULE_CASES = {  # module: (label on the picker, answers by field label, approval levels expected)
    "PURCHASE": ("Purchasing", {"What are you buying?": "Docking stations", "Vendor": "Dell", "Category": "Hardware",
                                "Total cost": "1500", "Needed by": "2026-12-15", "Business justification": "Hot desks need them."},
                 ["Line manager", "Finance"]),
    "ACCESS": ("Access management", {"System": "GitHub", "Access level": "Read only", "Duration (days)": "30",
                                     "Why do you need it?": "Reviewing the platform repos."}, ["Line manager", "IT security"]),
    "LEAVE": ("Leave", {"Leave type": "Annual", "First day": "2026-12-21", "Last day": "2027-01-08", "Working days": "15",
                        "Handover notes": "Mia covers on-call."}, ["Line manager", "HR"]),
    "CHANGE": ("Change management", {"System or service": "Payments API", "Environment": "Production", "Change window": "2026-12-05",
                                     "Risk score (1 to 10)": "8", "Rollback plan": "Redeploy the previous image.", "Tested in staging": True},
               ["Line manager", "Change advisory board", "Admins"]),
    "GENERAL": ("General requests", {"Value (if any)": "0", "Details": "Book the large meeting room for the offsite."}, ["Line manager"]),
}


def fill_answers(page, answers):
    for label, value in answers.items():
        control = page.get_by_label(re.compile(rf"^{re.escape(label)}( \*)?$"))
        kind = control.evaluate("e => e.getAttribute('role') || e.tagName")
        if value is True:
            control.check()
        elif kind == "combobox":
            choose(control, value)
        else:
            control.fill(value)


@scenario()
def every_module_end_to_end(browser, t):
    req = Session(browser, t, "requester")
    page = req.page
    opened = []
    for module, (label, answers, levels) in MODULE_CASES.items():
        req.go(f"#/new?module={module}")
        verify(f"{label}: opening New request with ?module= preselects it", page, lambda: expect(
            page.locator("label.choice", has_text=label).locator("input")).to_be_checked())
        page.locator("label.choice", has_text=label).click()
        title = f"UI test {label.lower()} {stamp()}"
        page.get_by_label("Title").fill(title)
        if module == "PURCHASE":
            choose(page.get_by_label("Currency"), "EUR")
        fill_answers(page, answers)
        page.get_by_role("button", name="Submit request").click()
        expect(page).to_have_url(re.compile(r"#/requests/[0-9a-f-]{36}$"), timeout=15000)
        rid = page.url.rsplit("/", 1)[-1]
        status = wait_for_status(page, ["PENDING_APPROVAL", "UNROUTABLE"])  # stop early when it cannot route
        check(f"{label}: the request routes to approvers", status == "PENDING APPROVAL", status)
        if status == "PENDING APPROVAL":
            opened.append(rid)  # an unroutable request is in nobody's inbox, so there is nothing to close
        steps = slip(page)
        verify(f"{label}: the chain has the expected levels ({' -> '.join(levels)})", page, lambda: (
            expect(steps).to_have_count(len(levels)),
            *[expect(steps.nth(i)).to_contain_text(name) for i, name in enumerate(levels)]))
        facts = page.locator(".answers")
        shown = []
        for field_label, value in answers.items():
            if value is True:
                shown.append("Yes")
            elif field_label in ("Needed by", "First day", "Last day", "Change window"):
                shown.append(value[:4])  # dates are shown in the reader's locale; the year is always there
            elif field_label == "Total cost":
                shown.append("€1,500.00")
            elif field_label in ("Working days", "Duration (days)", "Risk score (1 to 10)", "Value (if any)"):
                shown.append(value)
            else:
                shown.append(value)
        verify(f"{label}: every answer is shown on the request, formatted for its type", page, lambda: [
            expect(facts).to_contain_text(text) for text in shown])
        verify(f"{label}: the form version is recorded", page, lambda: expect(page.locator(".field", has_text="Form version")).to_contain_text(re.compile(r"\d+")))
    req.close()
    mgr = Session(browser, t, "manager")
    for rid in opened:
        close_request(browser, t, rid, mgr=mgr)
    mgr.close()


@scenario()
def header_and_page_buttons(browser, t):
    s = Session(browser, t, "admin")
    page = s.page
    header = page.locator("header.topbar")
    for link, heading in (("New request", "New request"), ("Approvals", "Approvals"), ("Admin", "Users"), ("My requests", "My requests")):
        header.get_by_role("link", name=link, exact=True).click()
        verify(f"Header: {link} opens {heading}", page, lambda: expect(page.get_by_role("heading", name=heading, exact=True)).to_be_visible())
    s.go("#/inbox")
    expect(page.get_by_role("heading", name="Approvals", exact=True)).to_be_visible()
    page.locator(".section-head").get_by_role("button", name="Refresh").click()
    verify("Approvals: Refresh reloads the inbox", page, lambda: expect(page.get_by_role("heading", name="Approvals", exact=True)).to_be_visible())
    header.get_by_role("link", name="Request Hub", exact=True).click()
    verify("Header: the logo goes home to My requests", page, lambda: expect(page.get_by_role("heading", name="My requests")).to_be_visible())
    s.close()

    req = Session(browser, t, "requester")
    page = req.page
    page.locator(".section-head").get_by_role("button", name="Refresh").click()
    verify("My requests: Refresh reloads the list", page, lambda: expect(page.locator("tbody tr").first).to_be_visible())
    page.locator(".section-head").get_by_role("link", name="New request").click()
    verify("My requests: the New request button opens the form", page, lambda: expect(page.get_by_role("heading", name="New request")).to_be_visible())
    page.locator("label.choice", has_text="Purchasing").click()
    page.get_by_role("link", name="Cancel").click()
    verify("New request: Cancel goes back to My requests", page, lambda: expect(page.get_by_role("heading", name="My requests")).to_be_visible())
    title = page.locator("tbody tr .title").first.inner_text()
    page.locator("tbody tr").first.click()
    verify("My requests: clicking a row opens the request", page, lambda: expect(page.get_by_role("heading", name=title)).to_be_visible())
    page.locator(".back-link", has_text="My requests").click()
    verify("Request: the back link returns to My requests", page, lambda: expect(page.get_by_role("heading", name="My requests")).to_be_visible())
    req.close()


@scenario()
def admin_lists_menus_and_filters(browser, t):
    s = Session(browser, t, "admin")
    page = s.page
    s.go("#/admin/users")
    expect(page.get_by_role("heading", name="Users", exact=True)).to_be_visible()
    menu = page.locator("nav.rail")
    for link, heading in (("Org chart", "Org chart"), ("Approver groups", "Approver groups"), ("Workflows", "Approval workflows"),
                          ("Forms", "Request forms"), ("Auto-approval", "Auto-approval"), ("Users", "Users")):
        menu.get_by_role("link", name=link, exact=True).click()
        verify(f"Admin menu: {link} opens {heading}", page, lambda: expect(page.get_by_role("heading", name=heading, exact=True)).to_be_visible())
    expect(page.locator("tbody tr.clickable").first).to_be_visible()  # the list loads after the page heading
    total = page.locator("tbody tr.clickable").count()
    page.get_by_label("Search users").fill(t.names["requester"])
    verify("Users: search narrows the list as you type", page, lambda: (
        expect(page.locator("tbody tr.clickable")).to_have_count(1), expect(page.get_by_text(f"1 of {total} users")).to_be_visible()))
    page.get_by_label("Search users").fill("")
    choose(page.get_by_label("Filter by role"), "approvers")
    verify("Users: the role filter shows only that role", page, lambda: expect(page.locator("tbody tr.clickable", has_text="Requester")).to_have_count(0))
    choose(page.get_by_label("Filter by role"), "")
    choose(page.get_by_label("Filter by status"), "NO_MANAGER")
    verify("Users: the No manager filter leaves out people with one, and admins (who never have one)", page, lambda: (
        expect(page.locator("tbody tr.clickable", has_text=t.names["requester"])).to_have_count(0),
        expect(page.locator("tbody tr.clickable", has_text="Admin")).to_have_count(0)))
    choose(page.get_by_label("Filter by status"), "")
    page.locator(".section-head").get_by_role("button", name="Refresh").click()
    verify("Users: Refresh reloads the directory", page, lambda: expect(page.locator("tbody tr.clickable")).to_have_count(total))
    page.locator("tbody tr.clickable").filter(has=page.locator(".title", has_text=t.names["requester"])).click()
    verify("Users: clicking a row opens the person", page, lambda: expect(page.get_by_role("heading", name="Access and reporting line")).to_be_visible())
    page.get_by_role("link", name="Back to users").click()
    row = page.locator("tbody tr.clickable").filter(has=page.locator(".title", has_text=t.names["manager"]))
    row.focus()
    page.keyboard.press("Enter")
    verify("Users: a row opens with the keyboard (Enter) too", page, lambda: expect(page.get_by_role("heading", name="Access and reporting line")).to_be_visible())
    page.get_by_role("link", name="Back to users").click()
    page.locator(".section-head").get_by_role("link", name="Invite user").click()
    verify("Users: Invite user opens the invite form", page, lambda: expect(page.get_by_role("heading", name="Invite a user")).to_be_visible())
    page.get_by_role("link", name="Cancel").click()
    verify("Invite: Cancel returns to Users without creating anyone", page, lambda: expect(page.locator("tbody tr.clickable")).to_have_count(total))
    s.go("#/admin/org")
    expect(page.get_by_role("heading", name="Org chart")).to_be_visible()
    page.locator(".section-head").get_by_role("button", name="Refresh").click()
    page.wait_for_load_state("networkidle")
    verify("Org chart: Refresh redraws the chart", page, lambda: expect(page.locator("a.org-card").first).to_be_visible())
    page.locator("a.org-card", has_text=t.names["finance"]).first.click()
    verify("Org chart: clicking a person opens their page", page, lambda: expect(page.get_by_role("heading", name="Access and reporting line")).to_be_visible())
    for module in ("Access management", "Leave", "Change management", "General requests", "Purchasing"):
        s.go("#/admin/workflows")
        expect(page.get_by_role("heading", name="Approval workflows")).to_be_visible()
        page.locator(".pills").get_by_role("link", name=module).click()
        verify(f"Workflows: the {module} tab shows its own levels", page, lambda: expect(page.get_by_role("heading", name=f"{module} approval levels")).to_be_visible())
    for module in ("Access management", "Leave", "Change management", "General requests", "Purchasing"):
        s.go("#/admin/forms")
        expect(page.get_by_role("heading", name="Request forms")).to_be_visible()
        page.locator(".pills").get_by_role("link", name=module).click()
        verify(f"Forms: the {module} tab shows its own fields", page, lambda: expect(page.get_by_role("heading", name=f"{module} fields")).to_be_visible())
    check("No JavaScript errors in the admin menus and filters", not s.errors, s.errors[:3])
    s.close()


@scenario(local_only=True)
def signed_in_without_access(browser, t):
    s = Session(browser, t, None)
    page = s.page
    page.evaluate("t => sessionStorage.setItem('id_token', t)", t.hub.token_for_unknown("ghost@example.com"))
    page.reload()
    verify("Signed in but no access profile: the app says access is not set up and offers nothing else", page, lambda: (
        expect(page.get_by_role("heading", name="Your access is not set up yet")).to_be_visible(timeout=10000),
        expect(page.locator("#nav")).to_be_hidden()))
    s.close()


@scenario(local_only=True)
def invite_user(browser, t):
    adm = Session(browser, t, "admin")
    ap = adm.page
    adm.go("#/admin/users/new")
    ap.get_by_label(re.compile(r"^Email( \*)?$")).fill("new.joiner@example.com")
    ap.get_by_label("Full name").fill("New Joiner")
    choose(ap.get_by_label("Manager (level 1 approver)"), t.ids["manager"])
    ap.get_by_label("Department").fill("Engineering")
    ap.get_by_role("button", name="Invite user").click()
    verify("Invite: lands on the new person's page with the manager set", ap, lambda: (
        expect(ap).to_have_url(re.compile(r"#/admin/users/[0-9a-f-]{36}$"), timeout=10000),
        expect(ap.get_by_label("Manager (level 1 approver)")).to_have_attribute("data-value", t.ids["manager"])))
    adm.go("#/admin/users")
    verify("Invite: listed as Invite pending", ap, lambda: expect(ap.locator("tr", has_text="new.joiner@example.com")).to_contain_text("Invite pending"))
    adm.close()


@scenario(local_only=True)
def form_builder_new_version(browser, t):
    adm = Session(browser, t, "admin")
    ap = adm.page
    adm.go("#/admin/forms/LEAVE")
    expect(ap.locator(".builder-field").first).to_be_visible()
    ap.get_by_role("button", name="Add field").click()
    new_field = ap.locator(".builder-field").last
    new_field.get_by_label("Label").fill("Cover arranged with")
    verify("Builder: the preview shows the new field as you type", ap, lambda: expect(ap.locator(".preview").get_by_text("Cover arranged with")).to_be_visible())
    ap.get_by_role("button", name="Save new version").click()
    verify("Builder: saved as version 2", ap, lambda: expect(ap.get_by_text(re.compile(r"^Version 2"))).to_be_visible(timeout=10000))
    req = Session(browser, t, "requester")
    rp = req.page
    req.go("#/new")
    rp.locator("label.choice", has_text="Leave").click()
    verify("Requester: Leave now asks the new question on form version 2", rp, lambda: (
        expect(rp.get_by_label("Cover arranged with")).to_be_visible(), expect(rp.get_by_text("Form version 2")).to_be_visible()))
    adm.close()
    req.close()


@scenario(local_only=True)
def workflow_editor(browser, t):
    adm = Session(browser, t, "admin")
    ap = adm.page
    adm.go("#/admin/workflows/GENERAL")
    expect(ap.locator(".level-row")).to_have_count(2)
    ap.get_by_role("button", name="Add level").click()
    choose(ap.get_by_label("Level 3 approver"), "GROUP")
    choose(ap.get_by_label("Level 3 group"), "finance")
    ap.get_by_label("Level 3 threshold").fill("2500")
    verify("Workflow editor: the summary updates before saving", ap, lambda: expect(ap.locator(".note", has_text="Every General requests request needs")).to_contain_text("any member of Finance when"))
    ap.get_by_role("button", name="Save workflow").click()
    verify("Workflow editor: saved", ap, lambda: expect(ap.locator("#toast")).to_contain_text("General requests workflow version"))
    req = Session(browser, t, "requester")
    rp = req.page
    req.go("#/new")
    rp.locator("label.choice", has_text="General").click()
    verify("Requester: the General route preview now includes Finance", rp, lambda: expect(rp.locator(".route-preview li.slip-step", has_text="Finance")).to_be_visible())
    adm.close()
    req.close()


@scenario(local_only=True)
def escalation_to_admins(browser, t):
    title = f"UI test escalation {stamp()}"
    req = Session(browser, t, "requester")
    rid = submit_purchase(req, title, 120)
    wait_for_status(req.page, ["PENDING_APPROVAL"])
    t.time_out(rid)
    req.go("#/mine")
    req.go(f"#/requests/{rid}")
    status = wait_for_status(req.page, ["ESCALATED"])
    check("A missed SLA escalates the level", status == "ESCALATED", status)
    verify("Requester: the routing slip says it went to admins after the deadline", req.page, lambda: (
        expect(req.page.locator("#slip li.slip-step.escalated")).to_contain_text("Missed its deadline, so it went to admins"),
        expect(req.page.locator("#slip li.slip-step.escalated .countdown")).to_contain_text("Expires")))
    adm = Session(browser, t, "admin2")
    open_from_inbox(adm, title)
    verify("Admin: the decision panel says it was escalated to them", adm.page, lambda: expect(adm.page.get_by_role("heading", name="Escalated to you: decision needed")).to_be_visible())
    adm.close()
    req.close()


@scenario(local_only=True)
def disable_and_enable(browser, t):
    adm = Session(browser, t, "admin")
    ap = adm.page
    req = Session(browser, t, "requester")
    adm.go(f"#/admin/users/{t.ids['requester']}")
    ap.get_by_role("button", name="Disable account").click()
    ap.locator("#confirmOk").click()
    verify("Admin: the account shows as disabled", ap, lambda: expect(ap.get_by_role("button", name="Enable account")).to_be_visible(timeout=10000))
    req.page.evaluate("() => { window.location.hash = '#/new'; }")
    req.page.reload()
    verify("Requester: the same still-valid token now gets 'Your account is disabled'", req.page, lambda: expect(
        req.page.get_by_role("heading", name="Your account is disabled")).to_be_visible(timeout=10000))
    ap.get_by_role("button", name="Enable account").click()
    verify("Admin: enabled again", ap, lambda: expect(ap.get_by_role("button", name="Disable account")).to_be_visible(timeout=10000))
    req.page.get_by_role("button", name="Check again").click()
    verify("Requester: back in after Check again", req.page, lambda: expect(req.page.locator("#nav").get_by_role("link", name="New request")).to_be_visible(timeout=10000))
    adm.close()
    req.close()



@scenario(local_only=True)
def invite_user_every_option(browser, t):
    adm = Session(browser, t, "admin")
    page = adm.page
    adm.go("#/admin/users/new")
    page.get_by_label(re.compile(r"^Email( \*)?$")).fill("temp.contractor@example.com")
    page.get_by_label("Full name").fill("Temp Contractor")
    page.get_by_label("Department").fill("Platform")
    page.get_by_label("Job title").fill("Contractor")
    page.locator("label.choice", has_text="Approver").locator("input").check()
    page.locator("label.choice", has_text="Requester").first.locator("input").check()
    verify("Invite: role cards switch between Approver and Requester", page, lambda: expect(
        page.locator("label.choice", has_text="Requester").first.locator("input")).to_be_checked())
    page.locator("label.choice", has_text="Approver").locator("input").check()
    for module in ("Purchasing", "Access management", "Leave", "Change management", "General requests"):
        page.locator(".access-editor label.choice", has_text=module).locator("input").check()
    verify("Invite: each module box can be ticked", page, lambda: expect(page.locator(".access-editor input[type=checkbox]:checked")).to_have_count(5))
    page.get_by_role("button", name="Clear").click()
    page.get_by_role("button", name="Select all").click()
    verify("Invite: Select all ticks every module", page, lambda: expect(page.locator(".access-editor input[type=checkbox]:checked")).to_have_count(5))
    page.get_by_role("button", name="Clear").click()
    verify("Invite: Clear unticks them", page, lambda: expect(page.locator(".access-editor input[type=checkbox]:checked")).to_have_count(0))
    page.locator(".access-editor label.choice", has_text="General requests").locator("input").check()
    page.locator(".access-editor label.choice", has_text="Leave").locator("input").check()
    page.get_by_label(re.compile("Email the invitation now")).uncheck()
    page.get_by_role("button", name="Invite user").click()
    verify("Invite without a manager asks first", page, lambda: expect(page.locator("#confirm")).to_contain_text("Invite without a manager?"))
    page.locator("#confirm").get_by_role("button", name="Cancel").click()
    verify("Cancel keeps you on the form with nothing created", page, lambda: (
        expect(page.locator("#confirm")).to_be_hidden(), expect(page.get_by_role("heading", name="Invite a user")).to_be_visible()))
    page.get_by_role("button", name="Invite user").click()
    page.locator("#confirmOk").click()
    verify("Invite anyway: created without sending the email yet", page, lambda: (
        expect(page.locator("#toast")).to_contain_text("Send the invitation when they are ready"),
        expect(page.get_by_role("heading", name="Temp Contractor")).to_be_visible()))
    t.shared["temp_user"] = page.url.rsplit("/", 1)[-1]
    adm.go("#/admin/users/new")
    page.get_by_role("link", name="Back to users").click()
    verify("Invite: Back to users leaves the form", page, lambda: expect(page.get_by_role("heading", name="Users", exact=True)).to_be_visible())
    adm.go("#/admin/users/new")
    page.get_by_label(re.compile(r"^Email( \*)?$")).fill("second.admin@example.com")
    page.locator("label.choice", has_text="Admin").first.locator("input").check()
    verify("Invite: choosing Admin hides module choices (admins hold every module)", page, lambda: expect(page.get_by_role("button", name="Select all")).to_be_hidden())
    page.get_by_role("button", name="Invite user").click()
    verify("Inviting an admin asks for confirmation, marked as sensitive", page, lambda: expect(page.locator("#confirm")).to_contain_text("Invite a new admin?"))
    page.locator("#confirmOk").click()
    verify("The new admin is created and emailed", page, lambda: expect(page.locator("#toast")).to_contain_text("Invitation emailed to second.admin@example.com"))
    adm.close()


@scenario(local_only=True)
def user_page_every_button(browser, t):
    uid = t.shared.get("temp_user")
    if not check("A temporary user exists for the user page buttons", uid, "invite_user_every_option must run first"):
        return
    adm = Session(browser, t, "admin")
    page = adm.page
    adm.go(f"#/admin/users/{uid}")
    expect(page.get_by_role("heading", name="Access and reporting line")).to_be_visible()
    page.get_by_label("Full name").fill("Temp Contractor-Smith")
    choose(page.get_by_label("Manager (level 1 approver)"), t.ids["manager"])
    page.get_by_label("Department").fill("Platform Ops")
    page.get_by_label("Job title").fill("Senior contractor")
    page.locator("label.choice", has_text="Requester").first.locator("input").check()
    page.locator("label.choice", has_text="Approver").first.locator("input").check()
    page.get_by_role("button", name="Select all").click()
    page.get_by_role("button", name="Clear").click()
    for module in ("Access management", "Leave", "Change management", "General requests", "Purchasing"):
        page.locator(".access-editor label.choice", has_text=module).locator("input").check()
    for module in ("Access management", "Leave", "Change management", "General requests"):
        page.locator(".access-editor label.choice", has_text=module).locator("input").uncheck()
    page.get_by_role("button", name="Save", exact=True).click()
    verify("Save applies name, manager, department, title and modules", page, lambda: (
        expect(page.locator("#toast")).to_contain_text("Saved"),
        expect(page.get_by_role("heading", name="Temp Contractor-Smith")).to_be_visible(),
        expect(page.locator(".card", has_text="Access history")).to_contain_text("manager set to")))
    page.locator("label.choice", has_text="Admin").first.locator("input").check()
    page.get_by_role("button", name="Save", exact=True).click()
    verify("Making someone an admin asks first", page, lambda: expect(page.locator("#confirm")).to_contain_text("an admin?"))
    page.locator("#confirm").get_by_role("button", name="Cancel").click()
    adm.go("#/admin/users")
    adm.go(f"#/admin/users/{uid}")
    verify("Cancelled: they are still an Approver", page, lambda: expect(
        page.locator("label.choice", has_text="Approver").first.locator("input")).to_be_checked())
    card = page.locator(".card", has=page.get_by_role("heading", name="Auto-approval", exact=True))
    choose(card.get_by_label("Module"), "PURCHASE")
    card.get_by_label("Up to (approval value)").fill("250")
    card.get_by_label("Until").fill((datetime.date.today() + datetime.timedelta(days=31)).isoformat())
    card.get_by_role("button", name="Choose date").click()
    page.keyboard.press("ArrowLeft")
    page.keyboard.press("Enter")
    verify("User page: picking the day before in the calendar sets the end date", page, lambda: expect(card.get_by_label("Until")).to_have_value(
        (datetime.date.today() + datetime.timedelta(days=30)).isoformat()))
    card.get_by_label("Note").fill("Small consumables")
    card.get_by_role("button", name="Grant auto-approval").click()
    page.locator("#confirmOk").click()
    verify("User page: auto-approval granted and listed with its note", page, lambda: expect(
        page.locator(".card", has=page.get_by_role("heading", name="Auto-approval", exact=True)).locator("tr", has_text="Purchasing")).to_contain_text("Small consumables"))
    page.locator(".card", has=page.get_by_role("heading", name="Auto-approval", exact=True)).get_by_role("button", name="Revoke").click()
    page.locator("#confirmOk").click()
    verify("User page: auto-approval revoked", page, lambda: expect(page.get_by_text("None. Every request this person raises goes through")).to_be_visible())
    for button, expected in (("Resend invitation", "invitation"), ("Sign out everywhere", "signed out")):
        page.get_by_role("button", name=button).click()
        page.locator("#confirmOk").click()
        verify(f"{button}: confirmed and done", page, lambda: expect(page.locator("#toast")).to_contain_text(re.compile(expected, re.I)))
    page.get_by_role("button", name="Disable account").click()
    page.locator("#confirm").get_by_role("button", name="Cancel").click()
    verify("Disable, then Cancel: the account stays active", page, lambda: expect(page.get_by_role("button", name="Disable account")).to_be_visible())
    page.get_by_role("button", name="Delete user").click()
    verify("Delete needs the email typed before the button works", page, lambda: expect(page.locator("#confirmOk")).to_be_disabled())
    page.locator("#confirmType").fill("temp.contractor@example.com")
    page.locator("#confirmOk").click()
    verify("Deleted: back on Users and they are gone", page, lambda: (
        expect(page.get_by_role("heading", name="Users", exact=True)).to_be_visible(),
        expect(page.locator("tbody tr", has_text="temp.contractor@example.com")).to_have_count(0)))
    adm.go(f"#/admin/users/{t.ids['finance']}")
    expect(page.get_by_role("heading", name="Access and reporting line")).to_be_visible()
    for button, expected in (("Reset password", "reset"), ("Reset authenticator", "authenticator")):
        page.get_by_role("button", name=button).click()
        page.locator("#confirmOk").click()
        verify(f"{button} (someone who has signed in before): confirmed and done", page, lambda: expect(page.locator("#toast")).to_contain_text(re.compile(expected, re.I)))
    check("No JavaScript errors on the user page", not adm.errors, adm.errors[:3])
    adm.close()


@scenario(local_only=True)
def org_chart_csv_import(browser, t):
    adm = Session(browser, t, "admin")
    page = adm.page
    adm.go("#/admin/org")
    expect(page.get_by_role("heading", name="Org chart")).to_be_visible()
    loop = "email,managerEmail,department,jobTitle\nadmin@example.com,admin2@example.com,IT,Head\nadmin2@example.com,admin@example.com,IT,Deputy\n"
    page.get_by_label("Choose a CSV file").set_input_files(files=[{"name": "lines.csv", "mimeType": "text/csv", "buffer": loop.encode()}])
    verify("Choosing a CSV file loads it into the box", page, lambda: expect(page.get_by_label("CSV rows")).to_have_value(re.compile("admin2@example.com,admin@example.com")))
    page.get_by_role("button", name="Check").click()
    verify("Check finds the loop across rows, and Apply stays off", page, lambda: (
        expect(page.locator(".card", has_text="Import reporting lines").locator("td.danger-text")).not_to_have_count(0),
        expect(page.get_by_role("button", name="Apply")).to_be_disabled()))
    page.get_by_label("CSV rows").fill("email,managerEmail,department,jobTitle\nadmin2@example.com,admin@example.com,IT,Deputy head of IT\n")
    page.get_by_role("button", name="Check").click()
    verify("A clean CSV passes the check and enables Apply", page, lambda: expect(page.get_by_role("button", name="Apply")).to_be_enabled())
    page.get_by_role("button", name="Apply").click()
    verify("Apply saves it and the chart redraws with Omar under Asha", page, lambda: expect(
        page.locator(".org-tree li", has=page.locator("a.org-card", has_text="Asha Admin")).locator("ul a.org-card", has_text="Omar Admin").first).to_be_visible(timeout=10000))
    adm.close()


@scenario(local_only=True)
def approver_groups_create_edit_delete(browser, t):
    adm = Session(browser, t, "admin")
    page = adm.page
    adm.go("#/admin/groups")
    page.locator(".section-head").get_by_role("button", name="Refresh").click()
    page.wait_for_load_state("networkidle")
    verify("Approver groups: Refresh reloads the groups", page, lambda: expect(page.get_by_role("heading", name="Finance", exact=True)).to_be_visible())
    page.get_by_role("button", name="New group").click()
    page.locator(".card", has_text="New approver group").get_by_role("button", name="Cancel").click()
    verify("New group: Cancel closes the form", page, lambda: expect(page.locator(".card", has_text="New approver group")).to_have_count(0))
    page.get_by_role("button", name="New group").click()
    form = page.locator(".card", has_text="New approver group")
    form.get_by_label("Name").fill("Security champions")
    form.get_by_label("Description").fill("Reviews risky access requests.")
    form.locator("label.choice", has_text="Mia Manager").locator("input").check()
    form.get_by_role("button", name="Create group").click()
    group = page.locator(".card", has=page.get_by_role("heading", name="Security champions"))
    verify("New group: created with its member", page, lambda: expect(group).to_contain_text("Mia Manager"))
    group.get_by_role("button", name="Edit").click()
    group.get_by_label("Name").fill("Security champions team")
    group.locator("label.choice", has_text="Farah Finance").locator("input").check()
    group.get_by_role("button", name="Save group").click()
    renamed = page.locator(".card", has=page.get_by_role("heading", name="Security champions team"))
    verify("Edit: renamed and a second member added", page, lambda: expect(renamed).to_contain_text("Farah Finance"))
    renamed.get_by_role("button", name="Delete").click()
    page.locator("#confirmOk").click()
    verify("Delete: an unused group is removed", page, lambda: expect(page.get_by_role("heading", name="Security champions team")).to_have_count(0))
    page.locator(".card", has=page.get_by_role("heading", name="Finance", exact=True)).get_by_role("button", name="Delete").click()
    page.locator("#confirmOk").click()
    verify("Delete: a group a workflow uses is refused with a reason", page, lambda: (
        expect(page.locator("#toast")).to_have_class(re.compile("danger")),
        expect(page.get_by_role("heading", name="Finance", exact=True)).to_be_visible()))
    adm.close()


@scenario(local_only=True)
def workflow_editor_every_control(browser, t):
    adm = Session(browser, t, "admin")
    page = adm.page
    adm.go("#/admin/workflows/ACCESS")
    expect(page.locator(".level-row")).to_have_count(2)
    page.locator(".level-row").nth(1).get_by_role("button", name="Move up").click()
    page.get_by_role("button", name="Save workflow").click()
    verify("Moving a group above the line manager is refused on save", page, lambda: expect(page.locator("#toast")).to_contain_text("line manager can only be level 1"))
    adm.go("#/admin/users")
    adm.go("#/admin/workflows/ACCESS")
    page.locator(".level-row").first.get_by_role("button", name="Move down").click()
    page.locator(".level-row").nth(1).get_by_role("button", name="Move up").click()
    page.get_by_role("button", name="Add level").click()
    choose(page.get_by_label("Level 3 approver"), "ADMINS")
    page.get_by_label("Level 3 threshold").fill("90")
    choose(page.get_by_label("Level 2 group"), "finance")
    page.get_by_label("Level 2 threshold").fill("5")
    page.get_by_label("Minutes per level before escalating to admins").fill("45")
    page.get_by_label("Minutes admins have before the request expires").fill("120")
    choose(page.get_by_label("Level 1 approver"), "MANAGER")
    page.get_by_role("button", name="Save workflow").click()
    verify("Saved: levels, groups, thresholds and timers", page, lambda: expect(page.locator("#toast")).to_contain_text("Access management workflow version"))
    adm.go("#/admin/users")
    adm.go("#/admin/workflows/ACCESS")
    verify("After reload: three levels and the new timers are kept", page, lambda: (
        expect(page.locator(".level-row")).to_have_count(3),
        expect(page.get_by_label("Minutes per level before escalating to admins")).to_have_value("45"),
        expect(page.get_by_label("Level 2 group")).to_have_attribute("data-value", "finance")))
    page.locator(".level-row").nth(2).get_by_role("button", name="Remove level").click()
    verify("Remove level takes it out of the summary", page, lambda: expect(page.locator(".level-row")).to_have_count(2))
    adm.close()


@scenario(local_only=True)
def form_builder_every_control(browser, t):
    adm = Session(browser, t, "admin")
    page = adm.page
    adm.go("#/admin/forms/GENERAL")
    expect(page.locator(".builder-field").first).to_be_visible()
    page.get_by_role("button", name="Add field").click()
    f = page.locator(".builder-field").last
    f.get_by_label("Label").fill("Cost centre")
    choose(f.get_by_label("Type"), "select")
    f.get_by_label("Options", exact=True).fill("CC-100\nCC-200\nCC-300")
    f.get_by_label("Help text").fill("Ask Finance if unsure.")
    f.get_by_label("Required").check()
    f.get_by_label("Key").fill("costCentre")
    page.get_by_role("button", name="Add field").click()
    g = page.locator(".builder-field").last
    g.get_by_label("Label").fill("Attendees")
    choose(g.get_by_label("Type"), "number")
    g.get_by_label("Minimum").fill("1")
    g.get_by_label("Maximum").fill("200")
    page.get_by_role("button", name="Add field").click()
    h = page.locator(".builder-field").last
    h.get_by_label("Label").fill("Temporary")
    h.get_by_label("Max characters").fill("40")
    h.get_by_role("button", name="Remove field").click()
    page.locator(".builder-field").last.get_by_role("button", name="Move up").click()
    page.locator(".builder-field").nth(page.locator(".builder-field").count() - 2).get_by_role("button", name="Move down").click()
    verify("Preview shows the new dropdown with its help text", page, lambda: (
        expect(page.locator(".preview").get_by_text("Cost centre")).to_be_visible(),
        expect(page.locator(".preview").get_by_text("Ask Finance if unsure")).to_be_visible()))
    choose(page.get_by_label("Approval value (drives workflow thresholds and auto-approval limits)"), "amount")
    page.locator(".pills").get_by_role("link", name="Leave").click()
    verify("Unsaved changes: switching modules asks first", page, lambda: expect(page.locator("#confirm")).to_contain_text("Discard unsaved changes?"))
    page.locator("#confirm").get_by_role("button", name="Cancel").click()
    verify("...and Cancel stays on the form with the changes", page, lambda: (
        expect(page.get_by_role("heading", name="General requests fields")).to_be_visible(),
        expect(page.locator(".builder-field", has_text="Cost centre")).to_have_count(1)))
    page.get_by_role("button", name="Save new version").click()
    verify("Saved as a new version", page, lambda: expect(page.locator("#toast")).to_contain_text("form saved as version"))
    req = Session(browser, t, "requester")
    rp = req.page
    req.go("#/new?module=GENERAL")
    expect(rp.get_by_label("Cost centre")).to_be_visible()
    offered = option_values(rp.get_by_label("Cost centre"))
    check("Requester: the new dropdown offers the options the admin typed", offered == ["CC-100", "CC-200", "CC-300"], offered)
    verify("Requester: the new dropdown has its help, the number its range", rp, lambda: (
        expect(rp.get_by_text("Ask Finance if unsure")).to_be_visible(),
        expect(rp.locator(".dyn-field", has_text="Attendees")).to_contain_text("Between 1 and 200")))
    rp.get_by_label("Title").fill("Team offsite")
    rp.get_by_label("Value (if any)").fill("0")
    rp.get_by_label("Details").fill("Two days.")
    rp.get_by_role("button", name="Submit request").click()
    verify("Requester: the required dropdown is enforced", rp, lambda: expect(rp.locator(".dyn-field.has-error", has_text="Cost centre")).to_be_visible())
    req.close()
    adm.close()


# ------------------------------------------------------------------ the new UI: lists, re-raising, deadlines, components
@scenario()
def my_requests_search_filter_and_times(browser, t):
    req = Session(browser, t, "requester")
    page = req.page
    title = f"UI test findable {stamp()}"
    rid = submit_purchase(req, title, 250)
    wait_for_status(page, ["PENDING_APPROVAL"])
    req.go("#/mine")
    expect(page.locator("tbody tr.clickable").first).to_be_visible()
    total = page.locator("tbody tr.clickable").count()
    page.get_by_label("Search my requests").fill(title)
    verify("My requests: search narrows the list to the matching request", page, lambda: (
        expect(page.locator("tbody tr.clickable")).to_have_count(1),
        expect(page.locator("tbody tr.clickable")).to_contain_text(title),
        expect(page.locator(".toolbar .count")).to_have_text(f"1 of {total}")))
    page.get_by_label("Search my requests").fill("laptop stand")
    verify("My requests: search also matches the answers, not just the title", page, lambda: expect(page.locator("tbody tr.clickable", has_text=title)).to_have_count(1))
    page.get_by_label("Search my requests").fill("zz-nothing-matches-zz")
    verify("My requests: no match says so and how to get out of it", page, lambda: expect(page.locator("td.empty")).to_contain_text("No requests match"))
    page.get_by_label("Search my requests").fill("")
    for name, statuses in (("In progress", {"SUBMITTED", "PENDING_APPROVAL", "ESCALATED"}),
                           ("Approved", {"APPROVED", "AUTO_APPROVED", "FULFILLED"}),
                           ("Not approved", {"REJECTED", "EXPIRED", "UNROUTABLE", "FAILED"})):
        button = page.locator(".segmented").get_by_role("button", name=re.compile(rf"^{name} \(\d+\)$"))
        button.click()
        shown = set(page.locator("tbody tr.clickable [data-status]").evaluate_all("els => els.map(e => e.dataset.status)"))
        check(f"My requests: the {name} filter shows only those statuses", shown <= statuses, shown)
        verify(f"My requests: {name} is marked as the chosen filter", page, lambda: expect(button).to_have_attribute("aria-pressed", "true"))
    page.locator(".segmented").get_by_role("button", name=re.compile(r"^All \(\d+\)$")).click()
    verify("My requests: All shows everything again", page, lambda: expect(page.locator("tbody tr.clickable")).to_have_count(total))
    when = page.locator("tbody tr", has_text=title).locator("time.when")
    verify("My requests: times read like 'Just now' or '5 min ago', with the full date and time on hover", page, lambda: (
        expect(when).to_have_text(re.compile(r"^(Just now|\d+ min ago|\d+ h ago|Yesterday|\d+ days ago|.*\d{4}.*|[A-Z][a-z]{2} \d+|\d+ [A-Z][a-z]{2})$")),
        expect(when).to_have_attribute("title", re.compile(r"\d{4}")),
        expect(when).to_have_attribute("datetime", re.compile(r"^\d{4}-\d{2}-\d{2}T"))))
    req.close()
    close_request(browser, t, rid)


@scenario()
def raise_again_from_a_rejected_request(browser, t):
    title = f"UI test raise again {stamp()}"
    req = Session(browser, t, "requester")
    page = req.page
    rid = submit_purchase(req, title, 320)
    wait_for_status(page, ["PENDING_APPROVAL"])
    verify("A request in progress does not offer Raise again", page, lambda: expect(page.get_by_role("link", name="Raise again")).to_have_count(0))
    close_request(browser, t, rid)
    req.go(f"#/mine")
    req.go(f"#/requests/{rid}")
    status = wait_for_status(page, ["REJECTED"])
    check("The request was rejected", status == "REJECTED", status)
    page.get_by_role("link", name="Raise again").click()
    verify("Raise again opens New request with the same module and answers filled in", page, lambda: (
        expect(page.get_by_role("heading", name="New request")).to_be_visible(),
        expect(page.locator("label.choice", has_text="Purchasing").locator("input")).to_be_checked(),
        expect(page.get_by_label("Title")).to_have_value(title),
        expect(page.get_by_label("Total cost")).to_have_value("320"),
        expect(page.get_by_label("Category")).to_have_attribute("data-value", "Hardware"),
        expect(page.locator("form .note")).to_contain_text(f'Copied from "{title}"')))
    page.get_by_label("Title").fill(f"{title} (second try)")
    page.get_by_label("Business justification").fill("Cheaper model this time.")
    page.get_by_role("button", name="Submit request").click()
    expect(page).to_have_url(re.compile(r"#/requests/[0-9a-f-]{36}$"), timeout=15000)
    again = page.url.rsplit("/", 1)[-1]
    verify("The new request is separate from the rejected one", page, lambda: (
        expect(page.get_by_role("heading", name=f"{title} (second try)")).to_be_visible(),
        expect(page.locator(".toast")).to_contain_text("Submitted")))
    check("A new request ID was issued", again != rid, again)
    wait_for_status(page, ["PENDING_APPROVAL"])
    req.close()
    close_request(browser, t, again)


@scenario()
def approvers_see_their_deadline(browser, t):
    title = f"UI test deadline {stamp()}"
    req = Session(browser, t, "requester")
    rid = submit_purchase(req, title, 180)
    wait_for_status(req.page, ["PENDING_APPROVAL"])
    verify("Requester: the waiting level says when it moves on", req.page, lambda: expect(
        req.page.locator("#slip li.slip-step.active .countdown")).to_contain_text(re.compile(r"Escalates in (\d+ min|\d+ h|under a minute)")))
    req.close()
    mgr = Session(browser, t, "manager")
    mp = mgr.page
    mgr.go("#/inbox")
    ticket = mp.locator("a.card.ticket", has_text=title)
    verify("Approvals: the card says how long is left before it escalates", mp, lambda: (
        expect(ticket.locator(".countdown")).to_contain_text(re.compile(r"Escalates in (\d+ min|\d+ h|under a minute)")),
        expect(ticket.locator(".countdown")).to_have_attribute("title", re.compile(r"^Escalates at "))))
    ticket.click()
    verify("Decision panel: the same deadline next to the level", mp, lambda: (
        expect(mp.locator(".decision .countdown")).to_contain_text("Escalates in"),
        expect(mp.locator(".decision")).to_contain_text("Level 1 of 1")))
    mp.locator(".back-link", has_text="Approvals").click()
    verify("Request: the back link returns an approver to Approvals", mp, lambda: expect(mp.get_by_role("heading", name="Approvals", exact=True)).to_be_visible())
    mgr.close()
    close_request(browser, t, rid)


@scenario()
def why_this_route_and_messages(browser, t):
    req = Session(browser, t, "requester")
    page = req.page
    rid = submit_purchase(req, f"UI test why {stamp()}", 4200)
    wait_for_status(page, ["PENDING_APPROVAL"])
    reasons = page.locator("details.reasons")
    verify("Request: the reasons are folded away behind Why this route?", page, lambda: (
        expect(reasons.locator("summary")).to_have_text("Why this route?"),
        expect(reasons.locator("ul")).to_be_hidden()))
    reasons.locator("summary").click()
    verify("Why this route? opens plain-language reasons", page, lambda: (
        expect(reasons.locator("ul")).to_be_visible(),
        expect(reasons).to_contain_text("Approval order: Line manager"),
        expect(reasons).to_contain_text("Admins only approve from 10,000"),
        expect(reasons).not_to_contain_text("->")))
    page.locator(".section-head").get_by_role("button", name="Refresh").click()
    verify("Refresh keeps Why this route? open", page, lambda: expect(reasons.locator("ul")).to_be_visible())
    reasons.locator("summary").click()
    verify("...and folds away again", page, lambda: expect(reasons.locator("ul")).to_be_hidden())
    page.get_by_role("button", name="Upload").click()
    toast = page.locator("#toast")
    verify("A message appears when there is no file to upload", page, lambda: expect(toast).to_contain_text("Choose a file first"))
    toast.get_by_role("button", name="Dismiss message").click()
    verify("The message can be dismissed", page, lambda: expect(toast).to_be_hidden())
    page.get_by_role("button", name="Upload").click()
    expect(toast).to_be_visible()
    req.go("#/mine")
    verify("A page's message does not follow you to the next page", page, lambda: (
        expect(page.get_by_role("heading", name="My requests")).to_be_visible(), expect(toast).to_be_hidden()))
    check("No JavaScript errors on the request page", not req.errors, req.errors[:3])
    req.close()
    close_request(browser, t, rid)


@scenario()
def select_and_calendar_with_the_keyboard(browser, t):
    req = Session(browser, t, "requester")
    page = req.page
    req.go("#/new?module=PURCHASE")
    category = page.get_by_label("Category")
    expect(category).to_be_visible()
    listbox = page.locator(f"#{category.get_attribute('aria-controls')}")
    category.focus()
    page.keyboard.press("ArrowDown")
    verify("Select: the arrow key opens the list and says so", page, lambda: (
        expect(listbox).to_be_visible(), expect(category).to_have_attribute("aria-expanded", "true")))
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Enter")
    verify("Select: arrows move, Enter picks, and focus returns to the field", page, lambda: (
        expect(category).to_have_attribute("data-value", "Software"), expect(listbox).to_be_hidden(), expect(category).to_be_focused()))
    page.keyboard.press("Enter")
    page.keyboard.type("tr")
    page.keyboard.press("Enter")
    verify("Select: typing jumps to the option that starts with the letters", page, lambda: expect(category).to_have_attribute("data-value", "Travel"))
    page.keyboard.press("Space")
    page.keyboard.press("End")
    page.keyboard.press("Escape")
    verify("Select: Escape closes without changing the choice", page, lambda: (
        expect(listbox).to_be_hidden(), expect(category).to_have_attribute("data-value", "Travel")))
    category.click()
    page.get_by_role("heading", name="New request").click()
    verify("Select: clicking elsewhere closes the list", page, lambda: expect(listbox).to_be_hidden())
    currency = page.get_by_label("Currency")
    choose(currency, "INR")
    verify("Currency: the amount box shows the chosen currency", page, lambda: expect(page.locator(".adorned .adorn")).to_have_text("INR"))

    needed = page.get_by_label("Needed by")
    toggle = page.locator(".date", has=needed).get_by_role("button", name="Choose date")
    toggle.click()
    calendar = page.get_by_role("dialog", name="Choose a date")
    verify("Calendar: opens on today, marked as today", page, lambda: (
        expect(calendar).to_be_visible(),
        expect(calendar.locator("[aria-current=date]")).to_be_focused()))
    page.keyboard.press("ArrowRight")
    page.keyboard.press("Enter")
    # "Today" is the browser's local date, which can differ from this process's date near midnight.
    today = datetime.date.fromisoformat(page.evaluate(
        "() => { const d = new Date(); return [d.getFullYear(), String(d.getMonth() + 1).padStart(2, '0'), String(d.getDate()).padStart(2, '0')].join('-'); }"))
    tomorrow = today + datetime.timedelta(days=1)
    verify("Calendar: arrows move by day and Enter picks the date", page, lambda: (
        expect(needed).to_have_value(tomorrow.isoformat()), expect(calendar).to_be_hidden(), expect(needed).to_be_focused()))
    verify("Calendar: the date is spelled out under the field", page, lambda: expect(page.locator(".date", has=needed).locator(".date-friendly")).to_contain_text(str(tomorrow.year)))
    toggle.click()
    title_before = calendar.locator(".cal-title").inner_text()
    page.keyboard.press("PageDown")
    verify("Calendar: Page Down goes to the next month", page, lambda: expect(calendar.locator(".cal-title")).not_to_have_text(title_before))
    calendar.get_by_role("button", name="Previous month").click()
    calendar.get_by_role("button", name="Next month").click()
    calendar.get_by_role("button", name="Today").click()
    verify("Calendar: Today picks today", page, lambda: expect(needed).to_have_value(today.isoformat()))
    toggle.click()
    calendar.get_by_role("button", name="Clear").click()
    verify("Calendar: Clear empties the field", page, lambda: expect(needed).to_have_value(""))
    toggle.click()
    page.keyboard.press("Escape")
    verify("Calendar: Escape closes it", page, lambda: expect(calendar).to_be_hidden())
    needed.fill("2026-13-45")
    verify("Calendar: a typed date that does not exist is pointed out straight away", page, lambda: expect(
        page.locator(".date", has=needed).locator(".date-friendly")).to_contain_text("Use the format YYYY-MM-DD"))
    page.get_by_role("button", name="Submit request").click()
    verify("...and stops the form with a message next to the field", page, lambda: expect(
        page.locator(".dyn-field.has-error", has_text="Needed by")).to_contain_text("YYYY-MM-DD"))
    req.close()

    if not t.live:
        adm = Session(browser, t, "admin")
        ap = adm.page
        adm.go("#/admin/auto-approval")
        until = ap.get_by_label("Until")
        expect(until).to_be_visible()
        ap.locator(".date", has=until).get_by_role("button", name="Choose date").click()
        cal = ap.get_by_role("dialog", name="Choose a date")
        cal.locator(".cal-day[aria-pressed=true]").focus()
        for _ in range(4):
            ap.keyboard.press("PageUp")
        local_today = ap.evaluate("() => { const d = new Date(); return [d.getFullYear(), String(d.getMonth() + 1).padStart(2, '0'), String(d.getDate()).padStart(2, '0')].join('-'); }")
        today = cal.locator(f'.cal-day[data-date="{local_today}"]')
        verify("Calendar with limits: today is before the earliest allowed date, so it cannot be picked", ap, lambda: expect(today).to_be_disabled())
        ap.keyboard.press("Escape")
        adm.close()


@scenario()
def account_menu_and_skip_link(browser, t):
    s = Session(browser, t, "manager")
    page = s.page
    trigger = page.get_by_role("button", name="Account menu")
    trigger.focus()
    page.keyboard.press("Enter")
    menu = page.get_by_role("menu")
    verify("Account menu: opens from the keyboard with the first action focused", page, lambda: (
        expect(menu).to_be_visible(), expect(trigger).to_have_attribute("aria-expanded", "true"),
        expect(page.get_by_role("menuitem", name="Sign out")).to_be_focused(),
        expect(menu).to_contain_text("Approver")))
    page.keyboard.press("ArrowDown")
    verify("Account menu: arrows keep focus inside the menu", page, lambda: expect(page.get_by_role("menuitem", name="Sign out")).to_be_focused())
    page.keyboard.press("Escape")
    verify("Account menu: Escape closes it and returns focus", page, lambda: (expect(menu).to_be_hidden(), expect(trigger).to_be_focused()))
    trigger.click()
    page.get_by_role("heading", name="My requests").click()
    verify("Account menu: clicking elsewhere closes it", page, lambda: expect(menu).to_be_hidden())
    page.reload()
    expect(page.get_by_role("heading", name="My requests")).to_be_visible()
    page.keyboard.press("Tab")
    skip = page.get_by_role("link", name="Skip to content")
    verify("Keyboard: the first Tab shows a Skip to content link", page, lambda: (expect(skip).to_be_focused(), expect(skip).to_be_in_viewport()))
    page.keyboard.press("Enter")
    verify("Skip to content moves focus to the page content and leaves the address alone", page, lambda: (
        expect(page.locator("#app")).to_be_focused(), expect(page).not_to_have_url(re.compile(r"#app$"))))
    s.close()


AXE = pathlib.Path(__import__("os").environ.get("AXE_JS", "/tmp/axe/package/axe.min.js"))


@scenario()
def accessibility_audit(browser, t):
    """axe-core on every screen, light and dark. Skipped when axe-core is not available (set AXE_JS)."""
    if not AXE.exists():
        print(f"  (skipped: axe-core not found at {AXE}; npm pack axe-core and set AXE_JS to run it)")
        return
    source = AXE.read_text()
    req = Session(browser, t, "requester")
    rid = submit_purchase(req, f"UI test audit {stamp()}", 2500)
    wait_for_status(req.page, ["PENDING_APPROVAL"])
    req.close()
    screens = {
        None: [""],
        "requester": ["#/mine", "#/new?module=PURCHASE", f"#/requests/{rid}"],
        "manager": [f"#/requests/{rid}"],
        "admin": ["#/inbox", "#/admin/users", f"#/admin/users/{t.ids['requester']}", "#/admin/users/new", "#/admin/org", "#/admin/groups",
                  "#/admin/workflows/PURCHASE", "#/admin/forms/PURCHASE", "#/admin/auto-approval"],
    }
    schemes = [x.strip() for x in __import__("os").environ.get("AXE_SCHEMES", "light,dark").split(",") if x.strip()]
    for scheme in schemes:
        problems = []
        for who, hashes in screens.items():
            s = Session(browser, t, who, color_scheme=scheme, bypass_csp=True, wait_for_home=bool(who))
            for hash_ in hashes:
                if hash_:
                    s.go(hash_)
                s.page.wait_for_load_state("networkidle")
                s.page.wait_for_timeout(400)
                s.page.add_script_tag(content=source)
                found = s.page.evaluate("""async () => (await axe.run(document, { resultTypes: ['violations'] })).violations
                    .filter(v => ['critical', 'serious'].includes(v.impact)).map(v => `${v.id}: ${v.nodes.length}`)""")
                problems += [f"{who or 'signed out'} {hash_ or 'landing'}: {p}" for p in found]
            s.close()
        check(f"axe-core ({scheme} theme): no critical or serious accessibility issues on any screen", not problems, problems[:8])
    close_request(browser, t, rid)


@scenario(local_only=True)
def every_control_is_tested(browser, t):
    """Visits every screen as each role and fails for any control no scenario has used, or any unlabelled field."""
    rid = t.shared.get("manager_request")
    pending = None
    req = Session(browser, t, "requester")
    pending = submit_purchase(req, f"UI test inventory {stamp()}", 1200)
    req.close()
    screens = {
        "requester": ["#/mine", "#/new?module=PURCHASE", f"#/requests/{pending}"],
        "manager": ["#/inbox", f"#/requests/{pending}"],
        "admin": ["#/admin/users", "#/admin/users/new", f"#/admin/users/{t.ids['requester']}", "#/admin/org", "#/admin/groups",
                  "#/admin/workflows/PURCHASE", "#/admin/forms/PURCHASE", "#/admin/auto-approval"],
    }
    present, unlabeled = {}, set()
    for who, hashes in screens.items():
        s = Session(browser, t, who)
        for hash_ in hashes:
            s.go(hash_)
            s.page.wait_for_load_state("networkidle")
            s.page.wait_for_timeout(300)
            for d in s.page.evaluate(CONTROLS_JS):
                present.setdefault((d["view"], d["control"]), hash_)
                if d["unlabeled"]:
                    unlabeled.add(f"{d['view']}: {d['control']}")
        s.close()
    missing = sorted(f"{view}: {control}" for (view, control) in present if (view, control) not in USED)
    print(f"  ({len(present)} distinct controls on {sum(len(h) for h in screens.values())} screens; {len(present) - len(missing)} used by the tests)")
    check("Every button, link and field on every screen is used by at least one test", not missing, missing)
    check("Every field has a label (screen readers, voice control)", not unlabeled, sorted(unlabeled))
    del rid


# ------------------------------------------------------------------ main
def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--live", action="store_true", help="test the deployed stack (CloudShell)")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--slow-mo", type=int, default=0)
    parser.add_argument("--only", default="")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--shots", default=str(SHOTS))
    parser.add_argument("--pace", type=int, default=220, metavar="CALLS",
                        help="live only: before each scenario, wait while any Lambda function has this many calls in the "
                             "last hour (the playground deletes one at 300). 0 turns pacing off. Default 220")
    args = parser.parse_args()
    sys.stdout.reconfigure(line_buffering=True)  # progress shows up in a redirected log as it happens
    if args.list:
        for name, _, local_only in SCENARIOS:
            print(f"{name}{'  (local only)' if local_only else ''}")
        return 0
    globals()["SHOTS"] = pathlib.Path(args.shots)
    SHOTS.mkdir(parents=True, exist_ok=True)
    wanted = {n.strip() for n in args.only.split(",") if n.strip()}
    started = time.time()
    target = LiveTarget() if args.live else LocalTarget()
    print(f"Target: {'live ' + target.base_url if target.live else 'local ' + target.base_url}")
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=not args.headed, slow_mo=args.slow_mo)
            for name, fn, local_only in SCENARIOS:
                if (wanted and name not in wanted) or (local_only and target.live):
                    continue
                if target.live:
                    target.pace(args.pace)
                print(f"\n{name.replace('_', ' ').capitalize()}")
                try:
                    fn(browser, target)
                except (AssertionError, PlaywrightError, KeyError, RuntimeError) as exc:
                    check(f"{name} finished without an unexpected error", False, f"{type(exc).__name__}: {str(exc).splitlines()[0][:300]}")
                    traceback.print_exc(limit=3)
            browser.close()
    finally:
        target.close()
    passed = sum(1 for _, ok in results if ok)
    print(f"\n{passed}/{len(results)} UI checks passed in {int(time.time() - started)}s"
          + ("" if passed == len(results) else f"; screenshots of failures are in {SHOTS}"))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())

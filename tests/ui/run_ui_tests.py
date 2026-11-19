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

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import expect, sync_playwright

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
results = []
SHOTS = ROOT / "ui-test-results"
SCENARIOS = []


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
        return check(name, False, f"{str(exc).splitlines()[0][:200]} (see {shot})")


# ------------------------------------------------------------------ targets
class LocalTarget:
    live = False

    def __init__(self):
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
            body = route.request.post_data_buffer or b""
            key = re.search(rb'name="key"\r\n\r\n([^\r]+)', body)
            if key:
                self.hub.put_attachment(key.group(1).decode(), b"local upload")
            route.fulfill(status=204, body="")
        context.route(re.compile(r"https://[^/]*amazonaws\.com/.*"), handle)

    def close(self):
        self.hub.stop()


class LiveTarget:
    live = True

    def __init__(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import hubkit
        self.kit = hubkit
        self.out = hubkit.outputs()
        self.base_url = self.out["AppUrl"].rstrip("/") + "/"
        self.api = self.out["ApiBaseUrl"]
        users = hubkit.load_users()
        # The scripted test users: requester -> approver (manager) -> admin. The admin also sits in Finance for the run.
        self.users = {"requester": users["requester"], "manager": users["approver"], "admin": users["admin"], "finance": users["admin"]}
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

    def __init__(self, browser, target, who, viewport=None):
        self.who, self.target = who, target
        self.context = browser.new_context(viewport=viewport or {"width": 1280, "height": 900})
        target.route_uploads(self.context)
        self.errors = []
        self.page = self.context.new_page()
        self.page.on("pageerror", lambda exc: self.errors.append(str(exc)))
        self.page.on("console", lambda msg: self.errors.append(msg.text) if msg.type == "error" and "Failed to load resource" not in msg.text else None)
        self.page.goto(target.base_url)
        if who:
            self.page.evaluate("t => sessionStorage.setItem('id_token', t)", target.token(who))
            self.page.reload()
            expect(self.page.get_by_role("button", name="Sign out")).to_be_visible(timeout=15000)

    def go(self, hash_):
        self.page.evaluate("h => { window.location.hash = h; }", hash_)

    def close(self):
        self.context.close()


def badge(page):
    return page.locator(".section-head .badge").first


def wait_for_status(page, statuses, tries=10, pause=2.5):
    """Re-reads the request (the Refresh button) until its status is one of statuses."""
    wanted = {s.replace("_", " ") for s in statuses}
    for _ in range(tries):
        try:
            text = badge(page).inner_text(timeout=5000).strip().upper()
        except PlaywrightError:
            text = ""
        if text in wanted:
            return text
        time.sleep(pause)
        page.locator(".section-head").get_by_role("button", name="Refresh").click()
        page.wait_for_load_state("networkidle")
    return badge(page).inner_text().strip().upper()


def fill_purchase(page, title, amount, item="Laptop stand"):
    page.locator("label.choice", has_text="Purchasing").click()
    page.get_by_label("Title").fill(title)
    page.get_by_label("What are you buying?").fill(item)
    page.get_by_label("Category").select_option("Hardware")
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
    expect(page.get_by_role("heading", name="Approvals waiting for you")).to_be_visible()
    card = page.locator("a.card.tile", has_text=title)
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
    verify("Route preview: 500 skips Finance", page, lambda: expect(page.locator("ol.route li", has_text="Finance")).to_have_class(re.compile("skipped")))
    page.get_by_label("Total cost").fill("1500")
    verify("Route preview: 1,500 adds Finance", page, lambda: expect(page.locator("ol.route li", has_text="Finance")).to_have_class(re.compile("applies")))
    unlabeled = page.evaluate("""() => [...document.querySelectorAll('form.card input, form.card select, form.card textarea')]
        .filter(el => el.type !== 'hidden' && !(el.labels && el.labels.length) && !el.getAttribute('aria-label')).map(el => el.name || el.outerHTML.slice(0, 60))""")
    check("Every control on the request form has a label (screen readers, get_by_label)", not unlabeled, unlabeled)
    page.get_by_label("Total cost").fill("")
    page.get_by_role("button", name="Submit request").click()
    verify("Submitting an empty form shows errors next to each required field", page, lambda: (
        expect(page.locator(".dyn-field.has-error")).not_to_have_count(0),
        expect(page.locator(".dyn-field.has-error", has_text="Total cost")).to_be_visible(),
        expect(page.locator("#toast")).to_have_class(re.compile("danger"))))
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
        expect(page.locator("ol.chain li.step").nth(0)).to_have_class(re.compile("active")),
        expect(page.locator("ol.chain li.step").nth(0)).to_contain_text("Line manager"),
        expect(page.locator("ol.chain li.step").nth(1)).to_contain_text("Finance")))
    verify("Requester: no decision buttons on their own request", page, lambda: expect(page.get_by_role("button", name="Approve")).to_have_count(0))

    mgr = Session(browser, t, "manager")
    open_from_inbox(mgr, title)
    mp = mgr.page
    verify("Manager: decision panel says level 1 of 2", mp, lambda: expect(mp.get_by_role("heading", name="Your decision (level 1 of 2)")).to_be_visible())
    mp.get_by_label("Decision note").fill("Agreed, the old ones are failing.")
    mp.get_by_role("button", name="Approve").click()
    verify("Manager: approval recorded", mp, lambda: expect(mp.locator("#toast")).to_contain_text("Recorded: approved"))
    mgr.go("#/inbox")
    verify("Manager: it leaves their inbox (they cannot approve level 2 as well)", mp, lambda: (
        expect(mp.get_by_role("heading", name="Approvals waiting for you")).to_be_visible(),
        expect(mp.locator("a.card.tile", has_text=title)).to_have_count(0, timeout=10000)))

    fin = Session(browser, t, "finance")
    open_from_inbox(fin, title)
    fp = fin.page
    verify("Finance: level 2 of 2, the last level", fp, lambda: (
        expect(fp.get_by_role("heading", name="Your decision (level 2 of 2)")).to_be_visible(),
        expect(fp.get_by_text("This is the last level")).to_be_visible()))
    fp.get_by_role("button", name="Approve").click()
    verify("Finance: approval recorded", fp, lambda: expect(fp.locator("#toast")).to_contain_text("Recorded: approved"))

    req.go(f"#/requests/{rid}")
    status = wait_for_status(page, ["FULFILLED"], tries=12)
    check("Requester: approved at both levels and fulfilled", status == "FULFILLED", status)
    verify("Requester: both decisions and the PO reference are shown", page, lambda: (
        expect(page.locator("ol.chain li.step.done")).to_have_count(2),
        expect(page.locator(".field", has_text="Fulfilment reference")).to_contain_text("PO-")))
    req.go("#/mine")
    verify("Requester: My requests lists it as FULFILLED", page, lambda: expect(page.locator("tr", has_text=title)).to_contain_text("FULFILLED"))
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
    mp.get_by_role("button", name="Reject").click()
    verify("Rejecting without a note is refused with a message", mp, lambda: (
        expect(mp.locator("#toast")).to_have_class(re.compile("danger")),
        expect(mp.get_by_role("button", name="Reject")).to_be_enabled()))
    mp.get_by_label("Decision note").fill("No budget this quarter.")
    mp.get_by_role("button", name="Reject").click()
    verify("Rejection with a note is recorded", mp, lambda: expect(mp.locator("#toast")).to_contain_text("Recorded: rejected"))
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
    ap.get_by_label("Person").select_option(t.ids["requester"])
    ap.get_by_label("Module").select_option("PURCHASE")
    ap.get_by_label("Up to (approval value)").fill("500")
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
    verify("Requester: 600 is over the limit and follows the route", page, lambda: expect(page.locator(".route-box .note")).to_contain_text("This value is higher"))
    rid = submit_purchase(req, f"UI test auto {stamp()}", 300)
    status = wait_for_status(page, ["AUTO_APPROVED", "FULFILLED"], tries=12)
    check("Requester: within the limit it is auto-approved", status in ("AUTO APPROVED", "FULFILLED"), status)
    verify("Requester: the chain explains the auto-approval", page, lambda: expect(page.get_by_text("Approved automatically under an auto-approval rule")).to_be_visible())
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
        expect(ap.get_by_role("heading", name="Auto-approval")).to_be_visible(),
        expect(ap.get_by_role("heading", name="Access history")).to_be_visible(),
        expect(ap.get_by_label("Manager (level 1 approver)")).to_have_value(t.ids["manager"])))
    adm.go(f"#/admin/users/{t.ids['admin']}")
    expect(ap.get_by_text("You cannot change your own role")).to_be_visible()  # the admin's own page has rendered
    options = ap.get_by_label("Manager (level 1 approver)").locator("option")
    values = options.evaluate_all("els => els.map(e => e.value)")
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
        expect(ap.locator("p.note").first).to_contain_text("Every Purchasing request needs")))
    adm.go("#/admin/forms/PURCHASE")
    verify("Forms: the builder lists fields and previews them", ap, lambda: (
        expect(ap.locator(".builder-field")).not_to_have_count(0),
        expect(ap.locator(".preview").get_by_text("Total cost")).to_be_visible()))
    check("No JavaScript errors across the admin screens", not adm.errors, adm.errors[:3])
    adm.close()


@scenario()
def attachments(browser, t):
    req = Session(browser, t, "requester")
    rid = submit_purchase(req, f"UI test attachment {stamp()}", 200)
    page = req.page
    wait_for_status(page, ["PENDING_APPROVAL"])
    page.locator("input[type=file]").set_input_files(files=[{"name": "quote.txt", "mimeType": "text/plain", "buffer": b"Quote 42"}])
    page.get_by_role("button", name="Upload").click()
    verify("Upload goes straight to S3 and the file is listed with a download link", page, lambda: (
        expect(page.locator("#toast")).to_contain_text("Uploaded"),
        expect(page.locator("ul.files a", has_text="quote.txt")).to_be_visible(timeout=15000)))
    del rid
    req.close()


@scenario()
def mobile_layout(browser, t):
    s = Session(browser, t, "requester", viewport={"width": 390, "height": 844})
    page = s.page
    for hash_, heading in (("#/mine", "My requests"), ("#/new", "New request")):
        s.go(hash_)
        expect(page.get_by_role("heading", name=heading)).to_be_visible()
        if hash_ == "#/new":
            page.locator("label.choice", has_text="Purchasing").click()
        overflow = page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")
        check(f"Phone width: {heading} has no sideways scrolling", overflow <= 1, f"{overflow}px too wide")
    s.close()


@scenario()
def sign_out(browser, t):
    s = Session(browser, t, "requester")
    page = s.page
    page.get_by_role("button", name="Sign out").click()
    verify("Sign out clears the session and returns to the landing page", page, lambda: (
        expect(page.get_by_role("button", name="Sign in")).to_be_visible(timeout=15000),
        expect(page.locator("#nav")).to_be_hidden()))
    has_token = page.evaluate("() => sessionStorage.getItem('id_token')")
    check("No token is left in session storage", not has_token)
    s.close()


@scenario(local_only=True)
def invite_user(browser, t):
    adm = Session(browser, t, "admin")
    ap = adm.page
    adm.go("#/admin/users/new")
    ap.get_by_label("Email", exact=True).fill("new.joiner@example.com")
    ap.get_by_label("Full name").fill("New Joiner")
    ap.get_by_label("Manager (level 1 approver)").select_option(t.ids["manager"])
    ap.get_by_label("Department").fill("Engineering")
    ap.get_by_role("button", name="Invite user").click()
    verify("Invite: lands on the new person's page with the manager set", ap, lambda: (
        expect(ap).to_have_url(re.compile(r"#/admin/users/[0-9a-f-]{36}$"), timeout=10000),
        expect(ap.get_by_label("Manager (level 1 approver)")).to_have_value(t.ids["manager"])))
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
    ap.get_by_label("Level 3 approver").select_option("GROUP")
    ap.get_by_label("Level 3 group").select_option("finance")
    ap.get_by_label("Level 3 threshold").fill("2500")
    verify("Workflow editor: the summary updates before saving", ap, lambda: expect(ap.locator("p.note").first).to_contain_text("any member of Finance when"))
    ap.get_by_role("button", name="Save workflow").click()
    verify("Workflow editor: saved", ap, lambda: expect(ap.locator("#toast")).to_contain_text("General requests workflow version"))
    req = Session(browser, t, "requester")
    rp = req.page
    req.go("#/new")
    rp.locator("label.choice", has_text="General").click()
    verify("Requester: the General route preview now includes Finance", rp, lambda: expect(rp.locator("ol.route li", has_text="Finance")).to_be_visible())
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
    verify("Requester: the chain says it was escalated to admins", req.page, lambda: expect(req.page.get_by_text("SLA missed, escalated to admins")).to_be_visible())
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
    verify("Requester: back in after Check again", req.page, lambda: expect(req.page.get_by_role("link", name="New request")).to_be_visible(timeout=10000))
    adm.close()
    req.close()


# ------------------------------------------------------------------ main
def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--live", action="store_true", help="test the deployed stack (CloudShell)")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--slow-mo", type=int, default=0)
    parser.add_argument("--only", default="")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--shots", default=str(SHOTS))
    args = parser.parse_args()
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

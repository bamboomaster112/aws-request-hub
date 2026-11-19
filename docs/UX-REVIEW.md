# UX review, 19 Nov 2026

How it was tested: the live instance in a KodeKloud playground (landing page, the hosted sign-in, and
`tests/ui/ux_probe.py` timing every screen as each role), plus the identical front end against the
local backend: every screen as requester, approver and admin at desktop (1280 px) and phone (390 px)
width, light and dark mode, with realistic data in every state (pending, rejected, fulfilled,
validation errors), an axe-core WCAG audit of 18 screens, and the browser test suite.

## What works well

* Navigation follows the role: requesters see two tabs, approvers add Approvals, admins add Admin.
* The New request form shows only the chosen module's fields, previews the approval route as you type
  the value, and lists every problem next to its field at once.
* The approval chain on each request is a clear timeline: who approved, who is next, notes, escalations.
* Risky admin actions confirm first; deleting a user needs the email typed.
* Dark mode follows the device setting. Tables open with Enter. A strict Content Security Policy.
* Fast once loaded: screen changes take 0.1 to 0.7 s; API calls median 82 ms, 90th percentile 171 ms.

## Fixed during the review (live, with tests)

| Severity | Finding | Fix |
| --- | --- | --- |
| High | On a phone, My requests clipped the Status column, so requesters could not see outcomes. | Phone layout keeps title and status, with module and value under the title. A test now fails for any badge or button off screen. |
| High (a11y) | Green "ok" badges had 3.84:1 contrast (WCAG AA needs 4.5:1). | 5.18:1. |
| High (a11y) | In dark mode, the Approve and red confirm buttons had white text on a light fill. | Dark text on those fills. |
| Medium (a11y) | The logo link's accessible name did not contain its visible text. | Name now comes from the visible text. |
| Medium | Closed requests (rejected, expired, fulfilled) still offered Upload. | Hidden, with a one-line explanation. |
| Low | Raw request IDs cluttered the My requests list. | Removed from the list; still on the request page. |

axe-core after the fixes: no critical or serious issues on any of the 18 screens.

## Recommendations, and how the redesign carried them out

All of these were done in the UI redesign the same day (verified locally; live verification follows the next deploy).

| Priority | Finding | Done |
| --- | --- | --- |
| High | The hosted sign-in page is unbranded: empty grey banner, tab title "Signin", no app name, always light. | Managed login (version 2) with `AWS::Cognito::ManagedLoginBranding`: the Request Hub logo, ink-blue buttons, the app's page and form colours, and light or dark following the browser. `LOGIN_STYLE=default` or `classic` in `deploy.sh` switches it back, and `deploy.sh` keeps the classic page when branding is blocked. |
| High | A rejected request is a dead end. | **Raise again** on rejected, expired and unroutable requests opens New request with the module, title, currency and every answer filled in, and says which request it was copied from. |
| Medium | Approvers cannot see how long they have before a request escalates. | "Escalates in 23 min" (or "Expires in" once escalated) on the routing slip, every Approvals card and the decision panel, with the exact time on hover; it refreshes every 30 seconds. |
| Medium | My requests has no search, filter or sort, and shows full timestamps. | Search across titles, modules and answers; filters for All, In progress, Approved and Not approved with counts; "Just now", "5 min ago", "Yesterday" with the full time on hover. |
| Medium | "How the route was decided" uses system wording. | Folded behind **Why this route?** and rewritten in the policy engine: "Admins only approve from 10,000, and the total cost is 4,200, so this level is skipped." and "Approval order: Line manager: Mia Manager, then Finance." |
| Medium | Admin pages re-fetch the profile on every click; Approver groups took 2.3 s on first visit. | The admin config and user directory are cached for 45 seconds and cleared by any change; admin pages no longer force a profile reload. |
| Medium | Users counts admins as "without a manager". | The count and the No manager filter leave admins out. |
| Low | The landing page mentions Amazon Cognito and lists AWS services. | The landing page explains the app with a sample routing slip and gives first-time steps: find the invitation email, choose a new password, set up an authenticator app. The services footer is gone. |
| Low | List values read "For how many days?: 14". | The default label is "Duration (days)" and lists read "14 days"; a trailing "?" is dropped from labels in lists. |
| Low | A toast from one page can linger on the next. | Messages clear when you change page, except one shown just before moving on (such as "Submitted"); every message has a dismiss button. |
| Low (a11y) | No level-one heading, and skipped levels. | Every page has one h1 with panels at h2; a Skip to content link; a test runs axe-core on every screen in both themes. |
| Low | On a phone the header takes three rows. | Name, role and Sign out are in an account menu, so the phone header is the logo and menu, then the navigation. |
| Low | The manager dropdown truncates entries. | A new select shows the name with the email and department on a second line, and a filter box for long lists. |

## Live timings (19 Nov 2026, us-east-1)

| Role | Screen | Ready | API |
| --- | --- | --- | --- |
| requester | first screen after sign-in | 2.4 s | cold start |
| requester | My requests / New request | 0.6 s / 0.2 s | /requests 83 ms, /forms 174 ms |
| manager | Approvals | 0.6 s | /approvals 65 ms |
| admin | Users / Org chart | 0.7 s / 0.3 s | /admin/users 134 to 156 ms |
| admin | Approver groups (first visit) | 2.3 s | /admin/config 1,947 ms (cold start) |
| admin | Workflows / Forms / Auto-approval | 0.2 to 0.3 s | /admin/config 134 to 156 ms |

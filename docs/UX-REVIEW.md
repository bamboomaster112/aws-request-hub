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

## Recommended next (not changed yet)

| Priority | Finding | Suggestion |
| --- | --- | --- |
| High | The hosted sign-in page is unbranded: empty grey banner, tab title "Signin", no app name, always light. People cannot tell what they are signing in to, and the jump from the dark app is jarring. | Switch the user pool domain to Managed Login (version 2) with `AWS::Cognito::ManagedLoginBranding` (logo, colours, dark mode), or brand the classic page with a logo and CSS. |
| High | A rejected request is a dead end: no way to fix and resubmit. | "Raise again" that pre-fills a new request from the rejected one. |
| Medium | Approvers cannot see how long they have before a request escalates. | "Escalates in 23 min" on inbox cards and the decision panel. |
| Medium | My requests has no search, filter or sort, and shows full timestamps. | Status filter and search; relative times ("2 h ago") with the full time on hover. |
| Medium | "How the route was decided" uses system wording ("No auto-approval rule applies...", "Admins not needed: 4,200 is below 10,000"). | Collapse it behind "Why this route?" and rephrase for requesters. |
| Medium | Admin pages re-fetch the profile on every click and load config and users in parallel; the first visit to Approver groups took 2.3 s (a second Lambda cold start). | Cache the admin config for the session for 30 to 60 s. This also saves Lambda calls under the playground's 300-per-hour limit. |
| Medium | Users shows "2 without a manager" when those two are admins, who never have one. | Count only non-admins. |
| Low | The landing page mentions Amazon Cognito and lists AWS services in the footer: developer wording for end users. | First-time guidance instead: use the temporary password from the invitation email, then set up an authenticator app. |
| Low | List values read "For how many days?: 14". | Short list labels ("14 days"). |
| Low | A toast from one page can linger on the next (for example "Check the highlighted fields" on a request page). | Clear page-specific messages on navigation. |
| Low (a11y) | Pages have no level-one heading, and some skip a level (h2 to h4). | Page titles as h1; card headings one level below. |
| Low | On a phone the header takes three rows (logo, tabs, name and Sign out). | Put name and Sign out behind a small account menu. |
| Low | The manager dropdown truncates entries ("Mia Manager (manag..."). | Show the name only; put the email in a hint below. |

## Live timings (19 Nov 2026, us-east-1)

| Role | Screen | Ready | API |
| --- | --- | --- | --- |
| requester | first screen after sign-in | 2.4 s | cold start |
| requester | My requests / New request | 0.6 s / 0.2 s | /requests 83 ms, /forms 174 ms |
| manager | Approvals | 0.6 s | /approvals 65 ms |
| admin | Users / Org chart | 0.7 s / 0.3 s | /admin/users 134 to 156 ms |
| admin | Approver groups (first visit) | 2.3 s | /admin/config 1,947 ms (cold start) |
| admin | Workflows / Forms / Auto-approval | 0.2 to 0.3 s | /admin/config 134 to 156 ms |

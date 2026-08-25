# Claude Prompt — Request Management App Fixes

I need you to fix the following issues in our request management application. Please investigate the relevant code, identify the root cause for each, and implement fixes.

## 1. Email Requests page — access control

- Currently, users have edit/full access to the Email Requests page, which is incorrect.
- **Required behavior:** only users with the **View** permission should be able to access this page. All other roles must be blocked from accessing it (no read/write/edit).
- Verify the role-based access control (RBAC) middleware/guard for this route and the page-level permission checks.

## 2. Requestor role — request visibility scoping

- A user with the **Requestor** role is currently able to see requests created by other users.
- **Required behavior:** a Requestor must only see requests they themselves created. Requests created by other users must not appear in their list/queue.
- Check the query that fetches requests for the Requestor view — it should filter by `created_by = current_user_id`.

## 3. Pending Approvals link — incorrect filtering

- The "Pending Approvals" link currently shows items that do not require action from the logged-in user.
- **Required behavior:** this list must show **only** items where the current user is the assigned approver and the item is awaiting their action (e.g., `status = pending` AND `approver = current_user`). Items pending someone else's approval, already approved, or rejected should not appear.

## 4. Approve button — not working

- The Approve button on the approval screen does nothing when clicked (or fails silently).
- Please debug: check the click handler, the API endpoint it calls, the request payload, server-side validation, and any permission checks. Confirm whether the failure is on the frontend (handler not wired / event blocked) or backend (endpoint error, permission denied, validation failure).
- After fixing, ensure a success toast/confirmation is shown and the request status updates in the UI without requiring a page reload.

## Deliverables — for each fix, please:

- Explain the root cause briefly.
- Show the file(s) and lines you changed.
- Note any test cases that should be added or updated to prevent regression.
- Flag any related areas (e.g., Reject button, other role-based pages) that may have the same pattern of bug.

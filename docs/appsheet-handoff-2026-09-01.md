# StreetSmart AppSheet handoff — 2026-09-01

## Outcome

The deployed StreetSmart agency app was safely modernized and its production data source was moved to a StreetSmart-owned workbook. The current release keeps ROBIE as a native feature inside the main app, restores employee photos, simplifies Home and Administration, and gives employees a private My Profile view with their own WOW Performance and Smart Rewards.

This is an AppSheet configuration and Google Sheets data-source handoff. It does not deploy or modify ROBIE/Hermes code in this repository.

## Production identifiers

| Item | Identifier |
| --- | --- |
| App | StreetSmart APP |
| App ID | `a1d9136f-5471-48b3-bcd7-9137cdbf6b15` |
| Final saved and deployed version | `1.002310` |
| Immediate pre-change rollback | `1.002304` |
| Clean historical baseline | `1.002253` |
| Recovery checkpoint | `1.002257` |
| Pre-cutover rollback | `1.002299` |
| Owned production workbook | `db 1 - StreetSmart Production Copy` |
| Owned workbook ID | `1ZyX1rUzDmRLMLMGrRzS48BlwvUdfkBd060ekZqoKsvo` |
| Original unchanged workbook | `db 1` |
| Original workbook ID | `1dFmE_J-YM9ua7v_B2UDgHHFr6BofIpIuU9qEXXtoZnA` |
| Preserved employee-photo folder ID | `13uaiF8BZYOaUojJtVmNRhOd3YMRGZRdJ` |

The separate ROBIE Control Center Test app, ID `2d26bb66-6047-4804-8061-9f3c8ad87b5b`, was not modified.

## Changes now live

### Agency interface

- Replaced the bright blue presentation with white app chrome and a restrained teal accent.
- Reduced the employee sidebar to Home, Organizational Chart, Quick Links, My Profile, and PTO. ROBIE utility views are not individual main-menu entries.
- Simplified Home to a flat, text-first Menu with consistent chevrons and no oversized or broken image icons.
- Hid obsolete Home modules requested by ownership: Task/Tasks, Monthly Buzz, Vendors, Employee Feedback(s), and Holidays.
- Simplified Administration to the same flat text-and-chevron presentation and removed broken icon placeholders.
- Kept ROBIE Operations as one Home destination. It opens the native ROBIE dashboard in the same StreetSmart browser tab rather than launching the separate test app.

### Data ownership and employee photos

- Repointed all 52 agency tables to the owned production workbook, retaining each table's worksheet mapping.
- Left all eight ROBIE tables on their separate `ROBIE Operations Control Center` source: Artifacts, Assignments, Dashboard, Evidence, Jobs, Schedules, Skills Categories, and Skills Registry.
- Preserved the original workbook as an independent rollback source.
- Restored Organizational Chart photos from the sibling `Employees_Images` folder. Employees with no usable source photo remain blank; no employee row was edited to manufacture an image.

### My Profile and manager access

- My Profile is a flat three-section workspace: My Information, WOW Performance, and Smart Rewards.
- Enabled the same eight non-sensitive work-profile fields for all 26 populated position/permission rows: Employee Type, Phone Number, Date of Enrollment, Department, License Status, Employment Status, Shirt Size, and Handbook Signed status.
- Private/admin fields such as home address, birth date, SSN, compensation, benefits, and device/equipment fields were not broadened.
- WOW Performance remains employee-private through the existing `New Profile` slice condition:

  ```appsheet
  [Email] = USEREMAIL()
  ```

- My Profile Smart Rewards is employee-private and read-only through the `My Smart Rewards` slice condition:

  ```appsheet
  [Rewardees] = LOOKUP(USEREMAIL(), "Employees", "Email", "Name")
  ```

- The employee-facing label was corrected from Quality Reviews to Smart Rewards. The Add control is absent on the read-only employee slice.
- The existing Employee Dashboard still provides manager/admin oversight. Non-admin managers select employees only from their own department; admins retain agency-wide access. Its Smart Rewards panel follows the selected employee, while My Profile continues to use the signed-in employee-only slice.

## Preserved ROBIE controls

- Create a ROBIE Job: Assignments remains add-only with exactly seven fields.
- ROBIE Jobs: Jobs remains read-only and newest-first.
- Skills and Propose a Skill remain available inside native ROBIE Operations.
- Skill Approvals remains limited to Carlo and Jake; Approve Skill is Draft-only and requires confirmation.
- Scheduled Jobs remains an update-only configuration surface pending verified runtime cron wiring.
- Jobs, Evidence, Dashboard, Artifacts, and Skills Categories remain read-only.
- Skills Registry remains add/update with no delete.

## Verification evidence

- Saved and synchronized the deployed desktop app at version `1.002310`.
- Confirmed Home, Administration, Organizational Chart, native same-tab ROBIE Operations, and My Profile load in the main app.
- Confirmed employee photos render after the owned-workbook cutover.
- Confirmed My Profile displays My Information, WOW Performance, and Smart Rewards.
- Confirmed My Smart Rewards is read-only and returns only the signed-in employee's matching rows; an employee with no matches receives an empty state.
- Confirmed compact mobile/tab behavior in the editor preview and previously at a 390×844 responsive viewport.
- No form was submitted, no approval action was invoked, and no live business record was added, edited, or deleted during verification.

## Required human QA and decisions

1. Ashley should sync or reopen StreetSmart APP and confirm the eight work-profile fields plus her own WOW Performance and Smart Rewards are visible under My Profile.
2. A department manager should open one direct report through Employee Dashboard and confirm the profile, performance, and Smart Rewards panels follow that selected employee. No impersonation was used during implementation.
3. Carlo or Jake should decide whether the remaining backend labels should be modernized after dependency review.
4. Carlo or Jake should approve any future source-row icon replacement set; source images were not overwritten in this pass.
5. Scheduled Jobs must remain configuration-only until external cron/runtime wiring is independently verified.
6. The Skills Registry row-number key warning still needs a controlled `skill_id` migration audit before any key change.

## Rollback

- For the latest My Profile/rewards change, restore AppSheet version `1.002304`.
- For the owned-source cutover, restore AppSheet version `1.002299` and verify all 52 agency tables point to original workbook `1dFmE_J-YM9ua7v_B2UDgHHFr6BofIpIuU9qEXXtoZnA` before saving.
- The original workbook was intentionally left unchanged. Do not delete the owned workbook or photo folder during rollback; retain both until live validation is complete.

## Repository release status

- Requirement: record the completed AppSheet modernization and operational handoff in Git.
- Change class: documentation only.
- ROBIE/Hermes Test release: not applicable.
- Release digest: not applicable.
- Production deployment from this repository: none.
- Automated application tests: not required for this documentation-only change; Markdown and diff checks are sufficient.

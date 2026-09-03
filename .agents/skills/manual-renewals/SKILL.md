---
name: manual-renewals
description: Operates, tests, and monitors the StreetSmart Autonomous Manual Renewal System across EZLynx, Carrier Portals, and Dual-Inbox Gmail.
---

# Manual Renewals Skill Guide

## Overview
Automates the retrieval of manual policy renewals for StreetSmart Insurance across carrier portals and underwriter email outreach.

## Execution Workflow
1. **Intake**: Ingest daily renewal CSV reports from EZLynx.
2. **Routing**: Determine if carrier is `PORTAL` (Coterie, Hartford, Tapco) or `EMAIL` (Trinity, AmWINS, Johnson & Johnson).
3. **Portal Crawl**: Launch headless Playwright session, fetch 2FA OTP from Gmail API if challenged, and download renewal proposal PDF.
4. **Email Outreach**: Dispatch tagged request (`[RENEWAL-REQ-###]`) from `robie@streetsmart.insurance` and manage follow-ups.
5. **EZLynx Update**: Upload quote PDF to Applicant Documents, post Discussion note, and assign task to Jake Ferrara.

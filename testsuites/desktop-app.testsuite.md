# Native Desktop App — Repository-Grounded Appium Suite

## Purpose

This suite is a reusable native desktop baseline. The AI planner must use the
configured desktop repository and build inspection to replace generic wording
with real windows, controls, accessibility identifiers, and workflows.

# 1. Application Context

**Target Type:** `desktop`  
**Automation:** Appium Mac2 (macOS) or Windows driver

# 2. Page 1 — Application Launch

## 2.1 Launch and Primary Window

- Verify the application's primary window is visible
- Verify the application name and main navigation discovered in the repository are visible
- Verify the application remains responsive after initial loading completes

## 2.2 Window Controls

- Verify the primary window can receive keyboard focus
- Verify important toolbar, sidebar, or menu controls discovered in the repository are enabled

# 3. Page 2 — Critical User Workflow

## 3.1 Navigation

- Click the first stable primary navigation control discovered in the repository
- Verify the expected destination view is visible
- Verify the destination exposes its expected primary action

## 3.2 Input Validation

- Verify required input controls discovered in the repository are visible
- Enter representative non-sensitive data into the primary input control
- Verify the entered value is accepted without an application error

# 4. Page 3 — Reliability

## 4.1 Dialog and Keyboard Behavior

- Verify any primary dialog can be dismissed with the platform-standard escape action
- Verify keyboard focus returns to the primary window

## 4.2 Relaunch

- Verify the application can be activated after being sent to the background
- Verify the primary window is visible after activation

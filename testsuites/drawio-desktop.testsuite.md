# draw.io Desktop — Appium Testing Instruction Suite

## Purpose

Test the macOS draw.io desktop build configured in `DESKTOP_APP_BUILD_PATH`.
Inspect the configured `DESKTOP_APP_REPO_URL` before generating tests. The
Electron main process is `src/main/electron.js`; its editor is loaded from
`drawio/src/main/webapp/index.html`. The draw.io submodule contains editor
actions in `js/diagramly/Menus.js` and English labels in `resources/dia.txt`.
Use labels and controls actually present in the inspected source and running
build. Each test must include a visible assertion and can start from a fresh
window or restore a known editor state.

# 1. Application Context

**Target Type:** `desktop`  
**Automation:** Appium Mac2 on macOS  
**Build:** `${DESKTOP_APP_BUILD_PATH}` (DMG containing `draw.io.app`)  
**Expected bundle ID:** `com.jgraph.drawio.desktop`

# 2. Page 1 — Application Launch

## 2.1 Window and Initial State

- Verify the draw.io application launches and its primary window is visible.
- Verify the primary window has an accessible draw.io title or application identity.
- Verify the initial diagram chooser or editor view becomes visible after startup.

## 2.2 Primary Navigation

- Verify the File menu is accessible from the draw.io application.
- Verify the Edit and View menus are accessible from the draw.io application.
- Verify a New or New Diagram action is visible from the startup view or File menu.

# 3. Page 2 — Diagram Creation

## 3.1 New Diagram Dialog

- Open the New Diagram workflow and verify the template or diagram chooser appears.
- Verify the Blank Diagram option is visible in the chooser.
- Select Blank Diagram and verify the editor workspace opens.

## 3.2 Editor Workspace

- Verify a blank diagram displays an editable canvas.
- Verify the Shapes palette or sidebar is available in the editor.
- Verify the editor exposes Save or Save as without writing to an existing user file.
- Verify the editor shows a page tab or page management control.

# 4. Page 3 — Editor Controls

## 4.1 Menus and View

- Verify the Insert menu or an equivalent insert action is available in the editor.
- Verify the Arrange menu is available in the editor.
- Verify the View menu exposes a Zoom In or Zoom Out control.

## 4.2 Keyboard and Dialogs

- Open a non-destructive application dialog and verify it is visible.
- Dismiss the dialog with Escape and verify the editor or chooser regains focus.
- Verify the application remains responsive after switching between the File and View menus.

# 5. Page 4 — Reliability

## 5.1 Relaunch and Recovery

- Activate draw.io using bundle ID `com.jgraph.drawio.desktop` and verify its primary window is visible afterward.
- Verify the primary window remains visible after activation.

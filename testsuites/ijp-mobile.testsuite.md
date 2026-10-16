# IJustPaid Mobile App — AI Testing Instruction Suite

## Purpose

This file is the **project-specific testing instruction source** for the IJustPaid Mobile application.

The AI test planner reads this file, connects to the mobile app via Appium, executes deterministic actions (tap, type, verify), validates behavior, and captures failure screenshots and metrics.

---

# 1. Application Context

**Application:** IJustPaid Mobile  
**Target Type:** `mobile`  
**Platform:** Android (via Appium)  
**Package:** `com.ijust_paid_react_native`  
**Activity:** `com.ijust_paid_react_native.MainActivity`  

Known application screens:

```text
1. Sign In Screen
2. Sign Up Screen / Account Selection Modal
3. Forgot Password Screen
4. Home / Dashboard Screen
5. Explore / Search Screen
6. Messages Screen
7. Create Post Modal
8. My Profile Screen
```

---

# 2. Page 1 — Sign In & Authentication

**Route:** `signin`

## 2.1 Screen Elements Verification

- Verify the "Sign In" tab is visible
- Verify the "Sign Up" tab is visible
- Verify the "Email" input field with placeholder "Enter your email" is visible
- Verify the "Password" input field is visible
- Verify the "Remember Me" option is visible
- Verify the "Forgot password?" link is visible
- Verify the "Sign In" button is visible
- Verify social login options ("Continue With") are visible

## 2.2 Form Validation

- Tap the "Sign In" button without entering credentials
- Verify the user remains on the Sign In screen

## 2.3 Sign Up & Account Selection Modal

- Tap the "Sign Up" tab
- Verify the "General Account" button is visible
- Verify the "Vendor Account" button is visible
- Press "Back" to dismiss the modal

## 2.4 Forgot Password Link Verification

- Verify the "Forgot password?" link is visible

## 2.5 Password Visibility Toggle

- Enter "${VALID_USER_PASSWORD}" into the Password input field
- Tap the password visibility toggle icon
- Verify the password field is visible

## 2.6 Valid User Login

- Enter "${VALID_USER_EMAIL}" into the Email input field
- Enter "${VALID_USER_PASSWORD}" into the Password input field
- Tap "Remember Me" to toggle the checkbox
- Tap the "Sign In" button
- Verify the "Ijustpaid" brand title is visible on the Home screen

---

# 3. Page 2 — Home & Feed Dashboard

**Route:** `home`

## 3.1 Dashboard Elements & Feed

- Verify the "Ijustpaid" brand title is visible in the header
- Verify category items including "Smart Tv" and "Motorcycle" are visible
- Verify feed card for user "mimi_pascual" is visible
- Verify the "Following" button is visible
- Verify bottom navigation bar contains "Home", "Search", "Messages", and "Profile" tabs

---

# 4. Page 3 — Explore & Search

**Route:** `explore`

## 4.1 Search Screen Navigation & Content

- Tap "Search" in bottom navigation
- Verify the "Explore" header is visible
- Verify the "Search Here" input field is visible
- Verify category filters ("All", "Mobiles", "Laptops") are visible
- Verify item price "$500" is visible

---

# 5. Page 4 — Messages & Chats

**Route:** `messages`

## 5.1 Messages Screen Navigation & Threads

- Tap "Messages" in bottom navigation
- Verify the "Messages" header is visible
- Verify the "Search Users" search bar is visible
- Verify chat conversation with "Devine Roy" is visible
- Verify group conversation with "Grocery Group" is visible

---

# 6. Page 5 — Create Post Action Sheet

**Route:** `create-post`

## 6.1 Create Post Modal & Options

- Tap the center "+" create button
- Verify the "Regular Post" option is visible
- Verify the "Group Buy Post" option is visible
- Press "Back" to dismiss the post creation modal

---

# 7. Page 6 — My Profile

**Route:** `profile`

## 7.1 Profile Screen & Account Verification

- Tap "Profile" in bottom navigation
- Verify the "My Profile" header is visible
- Verify the user display name "Rishab Arora" is visible
- Verify the configured user email "${VALID_USER_EMAIL}" is visible
- Verify the "Edit Profile" button is visible
- Verify stats including "Followers", "Following", and "Post" are visible
- Verify the "Overview" section is visible
- Tap "Home" in bottom navigation to return to the Home screen

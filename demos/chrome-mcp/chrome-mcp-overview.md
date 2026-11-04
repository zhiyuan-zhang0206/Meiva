# Chrome MCP — The Real Browser That Comes with Meiva

**What this shows**: Meiva comes with Chrome DevTools MCP built-in. Chrome DevTools MCP, for security policy, prohibits using the default profile—raw users have to create a new profile themselves, manage it, and log in to each website again. Meiva's approach is to make "creating and managing a dedicated Chrome profile" explicit: Meiva maintains a clean Chrome profile for you, guides you to log in to your Google account on it, and use it as your daily Chrome.

This way, **the user and agent share the exact same browser context**—the same login state, the same session, the same cookies. You log in once to Google / X / bank, and all agents can use it. This is one of the most fundamental differences of Meiva compared to other agent frameworks.

> ❌ Raw Chrome DevTools MCP: You have to install it yourself → create profile yourself → manage it yourself → log in again for each website
>
> ✅ Meiva Chrome MCP: Meiva creates and manages the profile → guides you to log in once → you and the agent share the same context.

---

## Prompt

```
You are an Meiva agent, directly controlling Meiva's built-in logged-in Chrome browser via Chrome MCP.

Your task: Execute the following four scenarios in order, save screenshots of each step to the workspace, and finally generate a report.

---

## Scenario 1: X.com — Social Media Operations in a Real Browser

The user is already logged into X in Chrome. Use the real browser to operate, comparing with the read-only API token of X MCP:

1. Navigate to https://x.com/home
2. Use `take_snapshot` to read the content and author of the first 5 tweets on the timeline
3. Navigate to https://x.com/explore
4. Use `take_snapshot` to read the current trending topics
5. Search for a trending topic, take a screenshot of the search results page and save as `workspace/x_search.png`
6. Use `take_screenshot` to capture the full timeline and save as `workspace/x_timeline.png`

**Note**: Do not tweet, do not like, do not follow—read-only mode. This scenario demonstrates: "real browser = full logged-in user capability, far beyond read-only API token".

---

## Scenario 2: Travel Portal — Search Provider + Fill Booking

Simulate a real travel-booking flow: search for a hotel on a travel site, check availability, and fill out a booking form.

1. Navigate to the travel member portal (e.g., the one you're already logged into in Chrome)
2. Use `take_snapshot` to find the hotel search / booking entry point
3. Search criteria: 3-4 star hotel in a specific city, 2 adults, 2 nights
4. Use `take_snapshot` to read the search results—hotel name, address, price, availability
5. Select a hotel, enter the booking page
6. Use `fill_form` to fill out the booking form (do not submit)—guest name, contact email, special requests
7. Take a screenshot of the form and save as `workspace/travel_booking.png`

**Why only the browser can do this scenario**:
- Each travel site is completely different, no unified API
- Requires real login state (username/password + possible 2FA)
- Search results are dynamically rendered, not available via API
- Booking process is a multi-step form, not a single API call

---

## Scenario 3: Chase Bank — Monthly Reconciliation

Log into Chase, download the current month's statement, summarize income and expenses.

1. Navigate to https://www.chase.com
2. The user is already logged in—directly go to the accounts page
3. Use `take_snapshot` to read the account list and balances
4. Enter checking account → find recent transactions
5. Use `take_snapshot` to read this month's transaction list
6. Use `evaluate_script` to extract transaction data (date, description, amount)
7. Summarize: total income, total expenses, net amount, largest single expense
8. Take a screenshot of the account summary and save as `workspace/chase_summary.png`

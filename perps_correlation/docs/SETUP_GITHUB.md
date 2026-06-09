# Putting ListingLabs online, free, forever — step by step

This walks you from "nothing set up" to "a public website that updates itself
every ~20 minutes at $0", assuming you've never used GitHub. Take it slowly;
each step is copy-paste.

> **What you'll end up with:** a URL like `https://YOURNAME.github.io/listinglabs/`
> that rebuilds the charts and listing signals on its own, around the clock,
> with no computer of yours needing to stay on.

---

## The security promise (read this once)

- Your RootData API key lives **only** in `C:\Users\PC\.config\verifysheet\secrets.env`,
  which is **outside** this repo and is **never uploaded**.
- The cloud build runs **without any key** — it only calls free, public,
  keyless endpoints. So there is no secret in the cloud to leak.
- The `.gitignore` at the repo root is a **whitelist**: it uploads ONLY the
  `perps_correlation/` and `cache/` folders. Everything else in your
  `verifysheet` folder stays on your machine. Even if you type `git add .`,
  nothing outside those two folders can be committed.

After your first upload, **verify** it: open your repo on github.com and confirm
you see only `perps_correlation/`, `cache/`, `.github/`, and `.gitignore`. If you
ever see `secrets.env` or a `.config` folder there, stop and tell me.

---

## Step 1 — Make a free GitHub account

1. Go to <https://github.com/signup> and create an account (free tier is enough).
2. Verify your email.

## Step 2 — Install Git on Windows

1. Download from <https://git-scm.com/download/win> (the install starts itself).
2. Click **Next** through the installer (defaults are fine).
3. Open a fresh **PowerShell** window and check it worked:
   ```powershell
   git --version
   ```
   You should see something like `git version 2.x`.

## Step 3 — Tell Git who you are (one time)

```powershell
git config --global user.name  "Your Name"
git config --global user.email "ahmedhailan6@gmail.com"
```

## Step 4 — Turn your folder into a repo

We make the **`verifysheet` folder** the repo. The whitelist `.gitignore`
guarantees only the two needed subfolders get uploaded.

```powershell
cd C:\Users\PC\Documents\verifysheet
git init
git branch -M main
git add .
git status
```

`git status` lists what *will* be uploaded. **Confirm it shows only**
`perps_correlation/`, `cache/`, `.github/`, and `.gitignore` — nothing else.
If that looks right:

```powershell
git commit -m "ListingLabs: initial site + auto-update workflow"
```

## Step 5 — Create the empty repo on GitHub

1. Go to <https://github.com/new>.
2. **Repository name:** `listinglabs`
3. Set it to **Public** (required for free Pages + unlimited Actions minutes —
   remember, nothing secret is in here).
4. Do **NOT** tick "Add a README / .gitignore / license" (we already have files).
5. Click **Create repository**.

GitHub now shows a page with commands. Use the **"…or push an existing
repository"** ones, which look like this (replace `YOURNAME`):

```powershell
git remote add origin https://github.com/YOURNAME/listinglabs.git
git push -u origin main
```

The first push will ask you to sign in to GitHub in a browser window — do it.
(Uploading ~100 MB the first time takes a few minutes.)

## Step 6 — Turn on GitHub Pages

1. On github.com, open your repo → **Settings** (top tab) → **Pages** (left menu).
2. Under **Build and deployment → Source**, choose **GitHub Actions**.
   (Not "Deploy from a branch".) That's it — no other Pages setting needed.

## Step 7 — Let the workflow run

1. Open the **Actions** tab of your repo.
2. If it asks you to enable workflows, click **"I understand… enable them"**.
3. Click **"Update ListingLabs site"** in the left list → **Run workflow** →
   **Run workflow** (this triggers the first build by hand instead of waiting
   for the cron).
4. Watch it run (~3–5 min). Green check = success.
5. Your site is now live at:
   ```
   https://YOURNAME.github.io/listinglabs/
   ```
   (The exact URL is shown in **Settings → Pages** after the first deploy.)

From now on it rebuilds itself every ~20 minutes. You don't have to do anything.

---

## ⚠️ The one thing we can't know until the first run

The cloud build fetches from GeckoTerminal / CoinMarketCap from a GitHub data
center IP. Those free endpoints *usually* allow it, but occasionally rate-limit
data-center IPs. **The first Actions run is the real test.** If the "Refresh
price candles" or "Build the site" step shows fetch errors / 403s in its log:
tell me, and I'll switch that piece to run on your PC (Windows Task Scheduler)
and push the results up — same end result, just the fetching happens locally.

---

## How it stays $0 and the limits to know

| Thing | Why it's free | The catch |
|---|---|---|
| GitHub Actions | Unlimited minutes on **public** repos | Must stay public |
| GitHub Pages | Free static hosting | Public repos only on the free plan |
| BWEnews RSS / GeckoTerminal / CMC-web | Public, keyless | Best-effort; may rate-limit |
| The `*/20` cron | — | GitHub often delays it 10–30 min and can skip runs under load. "Updated 24/7" really means "rebuilt roughly every 20–40 min." |

The websocket (`wss://bwenews-api.bwe-ws.com/ws`) gives instant, structured
alerts but needs an always-on process — a cron job can't hold a socket open. If
you ever want true real-time, that's the upgrade: a small listener on your PC or
a free always-on VM. The cloud cron is the no-PC-needed baseline.

---

## Everyday use after setup

- **You changed something locally and want it live now:**
  ```powershell
  cd C:\Users\PC\Documents\verifysheet
  git add .
  git commit -m "what I changed"
  git push
  ```
  Pushing rebuilds and redeploys automatically.

- **You want to see it rebuild without code changes:** Actions tab → Run workflow.

- **The site stopped updating:** open the Actions tab and look at the latest run's
  log. Scheduled workflows get auto-disabled only after long inactivity — our
  auto-commit step prevents that, but if it ever happens, click **Enable workflow**.

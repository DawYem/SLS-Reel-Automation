# SLS Instagram Reel -> monday.com Automation

Automatically files new @servantleaderscholars reels into the
[Servant Leader Scholars marketing board](https://servantleaderscholars.monday.com/boards/18348530348),
matching them to an existing item (by "Day of Service" / "community service"
keywords) or creating a new item in the current month's group. Runs hourly via
GitHub Actions — completely free for this volume, no Zapier subscription
needed.

## How it works

1. **GitHub Actions** wakes up on a schedule (hourly by default) and runs
   `scripts/instagram_to_monday.py`.
2. The script calls **Apify's** `instagram-reel-scraper` Actor directly via
   its REST API to pull the latest reels from the public account.
3. It skips anything already processed (tracked in
   `state/processed_reels.json`, committed back to the repo after each run).
4. For each new reel, it checks the caption for the matching keywords, finds
   or creates the right monday.com item, and drops the reel link into the
   Link column via monday's GraphQL API.

## One-time setup (about 10 minutes)

### 1. Get an Apify API token
1. Go to [apify.com](https://apify.com) and sign in (or create a free
   account — no credit card required).
2. Go to **Settings -> Integrations** and copy your **Personal API token**.

### 2. Get a monday.com API token
1. In monday.com, click your avatar (bottom left) -> **Developers**.
2. Go to **My Access Tokens** and copy your personal API token.

### 3. Create the GitHub repo
1. Create a new **private** GitHub repository (e.g. `sls-reel-automation`).
2. Upload/push all the files in this folder (`scripts/`, `.github/`,
   `state/`, this README) to that repo.

### 4. Add your tokens as GitHub secrets
In your new repo: **Settings -> Secrets and variables -> Actions -> New
repository secret**. Add two secrets:
- `APIFY_TOKEN` — the token from step 1
- `MONDAY_TOKEN` — the token from step 2

### 5. Turn it on
GitHub Actions is enabled by default on new repos. The workflow will start
running on its own hourly schedule. You can also trigger it manually any
time from the **Actions** tab -> "Instagram Reel -> monday.com" ->
**Run workflow**, which is the easiest way to test it the first time.

## Adjusting things later

- **Change how often it runs**: edit the `cron` line in
  `.github/workflows/instagram-to-monday.yml`.
- **Change matching keywords**: edit `MATCH_KEYWORDS` in
  `scripts/instagram_to_monday.py`.
- **Change how many reels get checked per run**: edit `RESULTS_LIMIT` and
  `ONLY_NEWER_THAN` in the same file.

## A note on the first run

The Apify actor's exact output field names (caption, url, id, etc.) are
handled defensively in the script, but it's worth checking the Actions log
after the first real run to confirm reels are being read correctly — search
for "New reel:" and "Caption/hashtags:" in the log output. If a field isn't
being picked up, the Actions log will show the raw item so it's easy to
adjust the field name in `reel_id()` / `reel_url()` / `reel_caption()`.

## Cost

- **Apify**: free tier includes $5/month in credits, which comfortably
  covers hourly checks of one account at this posting frequency (a few
  dollars per month at most, based on this actor's pay-per-reel pricing).
- **GitHub Actions**: free tier includes generous scheduled-workflow
  minutes; a script like this that runs in a few seconds, hourly, is nowhere
  close to the limit.
- **monday.com**: uses your existing account's API access — no extra cost.

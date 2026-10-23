# MaizeTix IU vs Michigan price monitor

Checks the Indiana vs. Michigan football listings on [MaizeTix](https://www.maizetix.com/games) every 10 minutes and pushes the cheapest price (fees included) to the ntfy topic **`preston-maizetix-iu-x7k2`**.

- Runs on GitHub Actions, started by a push to `main` or manually from the Actions tab.
- Stops on its own at `STOP_AT` (7:35 AM ET, Sat Oct 24, 2026). Change it in `.github/workflows/monitor.yml`.
- Two chained 6-hour jobs cover the 12-hour window.
- The first check of each job saves a screenshot and page text as a `debug-*` artifact.
- If a check fails, a warning is pushed (on the 1st, 3rd, and every 6th consecutive failure).

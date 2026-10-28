# PH Lotto Odds

Site: https://phlottoodds.com

- `site/` : the published website (GitHub Pages).
- `scripts/update.py` : fetches new PCSO results, updates `site/pcso.json` and rebuilds the pages.
- `.github/workflows/update-results.yml` : runs the script automatically 4 times a day and publishes the site.

To force an update: **Actions** tab → *Update results and publish* → **Run workflow**.

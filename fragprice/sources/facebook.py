"""Facebook buy/sell groups — placeholder for a future scraper.

Everything downstream is already wired: run.py stores what this returns, Claude reads each post at the ages in
config.yaml (facebook.check_ages_days), and the search page / Excel get a Facebook column once it has sales.

To implement, make `facebook(cfg)` return a list of dicts, one per group post:

    {
        "post_id":     "1234567890",                # stable, unique per post
        "url":         "https://www.facebook.com/groups/<group>/posts/<id>/",
        "title":       "[FB] <seller display name>",  # run.py reads the seller name from here
        "text":        "<post body>\\n\\n---COMMENTS---\\n<name>: <comment>\\n...",
        "created_utc": 1759900000,                  # post time, unix seconds
    }

Re-return posts from the last `cfg["lookback"]` on every run (text gets refreshed, so later "SOLD" edits and
seller comments are seen at the re-check). Group URLs come from `cfg["groups"]`.
Then set `facebook.enabled: true` in config.yaml.
"""


def facebook(cfg):
    return []

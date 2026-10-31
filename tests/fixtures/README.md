# Feed fixtures

**Provenance:** every file in `feeds/` is a real, unmodified response body, fetched once on
**2026-10-30** with User-Agent `SignalScout/0.1 (+https://github.com/asmipahwa/signal-scout)` after
checking that the host's robots.txt allows the URL. They are snapshots for offline tests, not live data;
their contents (titles, dates, counts) describe that day only.

| File | Source URL | Format | Entries |
|---|---|---|---|
| `feeds/notion_releases.xml` | https://www.notion.com/releases/rss.xml | RSS 2.0 (served as `application/atom+xml`; dates in JavaScript `Date.toString()` format) | 154 |
| `feeds/bing_news_asana.xml` | https://www.bing.com/news/search?q=%22Asana%22&format=rss | RSS 2.0 (links are `bing.com/news/apiclick.aspx` redirects) | 10 |
| `feeds/theverge_atom.xml` | https://www.theverge.com/rss/index.xml | Atom 1.0 | 10 |
| `feeds/airtable_blog.xml` | https://blog.airtable.com/rss/ | RSS 2.0 (full `content:encoded` HTML) | 15 |
| `feeds/hn_algolia_asana.json` | https://hn.algolia.com/api/v1/search_by_date?query=Asana&tags=story&hitsPerPage=50 | JSON | 50 hits |

Bing News results are licensed for personal, non-commercial use (see the feed's `<copyright>`); they are
kept here only as a test fixture for this non-commercial project.

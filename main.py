import hashlib
import html
import json
import time
import re
import unicodedata
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import quote_plus, urlsplit, urlunsplit, parse_qsl, urlencode

import feedparser
import trafilatura
from googlenewsdecoder import gnewsdecoder

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "config.json"
DATA_DIR = ROOT / "data"
DOCS_DIR = ROOT / "docs"
ARTICLES_FILE = DATA_DIR / "articles.json"
FEED_FILE = DOCS_DIR / "feed.xml"
INDEX_FILE = DOCS_DIR / "index.html"

SPANISH_STOPWORDS = {
    "a","al","algo","ante","como","con","contra","de","del","desde","donde",
    "el","ella","en","entre","era","es","esta","este","estos","fue","ha","hay",
    "la","las","lo","los","mas","más","muy","no","o","para","pero","por","que",
    "qué","se","sin","sobre","su","sus","tras","un","una","uno","unos","unas",
    "y","ya"
}


def load_config():
    with CONFIG_FILE.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_articles():
    if not ARTICLES_FILE.exists():
        return []
    try:
        with ARTICLES_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_articles(articles):
    DATA_DIR.mkdir(exist_ok=True)
    with ARTICLES_FILE.open("w", encoding="utf-8") as f:
        json.dump(articles, f, ensure_ascii=False, indent=2)


def clean_url(url):
    try:
        parts = urlsplit(url)
        params = []
        for key, value in parse_qsl(parts.query, keep_blank_values=True):
            key_lower = key.lower()
            if key_lower.startswith("utm_"):
                continue
            if key_lower in {"fbclid", "gclid", "mc_cid", "mc_eid"}:
                continue
            params.append((key, value))
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(params), ""))
    except Exception:
        return url


def is_blocked_domain(url):
    """
    Skip Al Bat completely.
    Blocks both albat.com and any subdomain such as www.albat.com.
    """
    try:
        hostname = (urlsplit(url).hostname or "").lower().strip(".")
        return hostname == "albat.com" or hostname.endswith(".albat.com")
    except Exception:
        return False


def published_datetime(entry):
    if getattr(entry, "published_parsed", None):
        p = entry.published_parsed
        return datetime(p.tm_year, p.tm_mon, p.tm_mday, p.tm_hour, p.tm_min, p.tm_sec, tzinfo=timezone.utc)
    if getattr(entry, "updated_parsed", None):
        p = entry.updated_parsed
        return datetime(p.tm_year, p.tm_mon, p.tm_mday, p.tm_hour, p.tm_min, p.tm_sec, tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def google_news_feed_url(query, config):
    language = config.get("google_news", {}).get("language", "es-419")
    country = config.get("google_news", {}).get("country", "MX")
    edition = config.get("google_news", {}).get("edition", "MX:es-419")
    return (
        "https://news.google.com/rss/search?"
        f"q={quote_plus(query)}&hl={quote_plus(language)}"
        f"&gl={quote_plus(country)}&ceid={quote_plus(edition)}"
    )


def decode_google_news_url(url):
    if "news.google.com" not in url:
        return clean_url(url)

    try:
        result = gnewsdecoder(url, interval=1)
        if isinstance(result, dict) and result.get("status"):
            decoded = result.get("decoded_url")
            if decoded:
                return clean_url(decoded)
        print(f"  Could not decode Google News URL: {url[:90]}...")
        return None
    except Exception as exc:
        print(f"  Google URL decode failed: {exc}")
        return None


def extract_article(url):
    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return None

        extracted = trafilatura.extract(
            downloaded,
            url=url,
            output_format="json",
            with_metadata=True,
            include_comments=False,
            include_tables=True,
            favor_precision=True,
        )

        if not extracted:
            return None

        data = json.loads(extracted)
        body = (data.get("text") or "").strip()
        if not body:
            return None

        return {
            "title": (data.get("title") or "").strip(),
            "author": (data.get("author") or "").strip(),
            "date": (data.get("date") or "").strip(),
            "body": body,
        }
    except Exception as exc:
        print(f"  Extraction failed for {url}: {exc}")
        return None


def source_name(entry):
    try:
        if getattr(entry, "source", None):
            return entry.source.get("title", "") or ""
    except Exception:
        pass

    title = getattr(entry, "title", "") or ""
    if " - " in title:
        return title.rsplit(" - ", 1)[-1].strip()
    return ""


def normalize_text_for_compare(text):
    text = (text or "").lower().strip()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[^a-z0-9ñü\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def meaningful_title_tokens(title):
    tokens = normalize_text_for_compare(title).split()
    return {
        token for token in tokens
        if len(token) >= 3 and token not in SPANISH_STOPWORDS
    }


def title_similarity(title_a, title_b):
    a = normalize_text_for_compare(title_a)
    b = normalize_text_for_compare(title_b)
    if not a or not b:
        return 0.0

    sequence_score = SequenceMatcher(None, a, b).ratio()

    ta = meaningful_title_tokens(title_a)
    tb = meaningful_title_tokens(title_b)
    if ta and tb:
        jaccard = len(ta & tb) / len(ta | tb)
    else:
        jaccard = 0.0

    return max(sequence_score, jaccard)


def body_lead_similarity(body_a, body_b, max_chars=1800):
    a = normalize_text_for_compare((body_a or "")[:max_chars])
    b = normalize_text_for_compare((body_b or "")[:max_chars])
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def same_story(article_a, article_b):
    """
    Conservative duplicate detection.

    It considers two articles duplicates when:
    1. Their headlines are very similar, OR
    2. Their headlines are moderately similar AND their article openings
       are strongly similar.
    """
    headline_score = title_similarity(
        article_a.get("title", ""),
        article_b.get("title", "")
    )

    if headline_score >= 0.78:
        return True

    if headline_score >= 0.48:
        lead_score = body_lead_similarity(
            article_a.get("body", ""),
            article_b.get("body", "")
        )
        if lead_score >= 0.72:
            return True

    return False


def merge_duplicate_articles(articles):
    """
    Keep only one version of repeated news.
    The article with the longest extracted body is considered the
    most complete and becomes the version kept in the RSS/database.
    """
    ordered = sorted(
        articles,
        key=lambda x: x.get("published_iso", ""),
        reverse=True,
    )

    kept = []

    for candidate in ordered:
        duplicate_index = None

        for i, existing in enumerate(kept):
            # Do not compare stories published more than 72 hours apart.
            try:
                dt_a = datetime.fromisoformat(candidate.get("published_iso", ""))
                dt_b = datetime.fromisoformat(existing.get("published_iso", ""))
                if abs((dt_a - dt_b).total_seconds()) > 72 * 3600:
                    continue
            except Exception:
                pass

            if same_story(candidate, existing):
                duplicate_index = i
                break

        if duplicate_index is None:
            kept.append(candidate)
            continue

        existing = kept[duplicate_index]
        candidate_len = len(candidate.get("body", ""))
        existing_len = len(existing.get("body", ""))

        if candidate_len > existing_len:
            winner = candidate
            loser = existing
            kept[duplicate_index] = winner
        else:
            winner = existing
            loser = candidate

        winner["matched_queries"] = sorted(set(
            winner.get("matched_queries", []) +
            loser.get("matched_queries", [])
        ))

        winner["duplicate_sources"] = sorted(set(
            winner.get("duplicate_sources", []) +
            loser.get("duplicate_sources", []) +
            ([loser.get("source")] if loser.get("source") else [])
        ))

    return kept


def article_id(url):
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:20]


def cdata(text):
    return "<![CDATA[" + str(text).replace("]]>", "]]]]><![CDATA[>") + "]]>"


def make_rss(articles, config):
    feed_title = config.get("feed", {}).get("title", "My News Feed")
    feed_description = config.get("feed", {}).get("description", "News collected from Google News searches.")
    feed_link = config.get("feed", {}).get("site_url", "http://localhost:8000/")
    max_feed_items = int(config.get("feed", {}).get("max_items", 150))

    newest = sorted(
        [a for a in articles if a.get("body")],
        key=lambda x: x.get("published_iso", ""),
        reverse=True,
    )[:max_feed_items]

    items = []
    for a in newest:
        body_html = "<p>" + html.escape(a["body"]).replace("\n\n", "</p><p>").replace("\n", "<br>") + "</p>"
        description = a["body"][:450].strip()
        if len(a["body"]) > 450:
            description += "…"

        categories = "\n".join(
            f"      <category>{html.escape(q)}</category>"
            for q in a.get("matched_queries", [])
        )

        author_xml = ""
        if a.get("author"):
            author_xml = f"      <dc:creator>{cdata(a['author'])}</dc:creator>\n"

        display_title = a["title"]
        if a.get("source"):
            display_title = f"{display_title} | {a['source']}"

        items.append(
            f"""    <item>
      <title>{cdata(display_title)}</title>
      <link>{html.escape(a['url'])}</link>
      <guid isPermaLink="false">{html.escape(a['id'])}</guid>
      <pubDate>{html.escape(a['published_rfc2822'])}</pubDate>
      <source>{cdata(a.get('source', ''))}</source>
{author_xml}      <description>{cdata(description)}</description>
      <content:encoded>{cdata(body_html)}</content:encoded>
{categories}
    </item>"""
        )

    now_rfc = format_datetime(datetime.now(timezone.utc))
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
     xmlns:content="http://purl.org/rss/1.0/modules/content/"
     xmlns:dc="http://purl.org/dc/elements/1.1/">
  <channel>
    <title>{cdata(feed_title)}</title>
    <link>{html.escape(feed_link)}</link>
    <description>{cdata(feed_description)}</description>
    <lastBuildDate>{html.escape(now_rfc)}</lastBuildDate>
    <generator>Free Python News RSS Bot</generator>
{chr(10).join(items)}
  </channel>
</rss>
"""
    DOCS_DIR.mkdir(exist_ok=True)
    FEED_FILE.write_text(xml, encoding="utf-8")


def make_index(articles, config):
    newest = sorted(
        [a for a in articles if a.get("body")],
        key=lambda x: x.get("published_iso", ""),
        reverse=True,
    )[:100]

    rows = []
    for a in newest:
        title = a["title"]
        if a.get("source"):
            title = f"{title} | {a['source']}"

        rows.append(
            f"""<article>
<h2><a href="{html.escape(a['url'])}" target="_blank" rel="noopener">{html.escape(title)}</a></h2>
<p><strong>{html.escape(a.get('source', ''))}</strong> · {html.escape(a.get('published_display', ''))} · {len(a.get('body', '')):,} characters extracted</p>
<p>Matched: {html.escape(", ".join(a.get('matched_queries', [])))}</p>
</article>"""
        )

    title = html.escape(config.get("feed", {}).get("title", "My News Feed"))
    page = f"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
body {{ font-family: Arial, sans-serif; max-width: 950px; margin: 40px auto; padding: 0 20px; line-height: 1.5; }}
article {{ padding: 14px 0; border-bottom: 1px solid #ddd; }}
h1 {{ margin-bottom: 5px; }}
h2 {{ font-size: 20px; margin-bottom: 5px; }}
p {{ margin: 5px 0; }}
code {{ background: #f2f2f2; padding: 2px 5px; }}
</style>
</head>
<body>
<h1>{title}</h1>
<p>RSS: <a href="feed.xml"><code>feed.xml</code></a></p>
{''.join(rows) if rows else '<p>No hay artículos todavía. Ejecuta <code>python main.py</code>.</p>'}
</body>
</html>
"""
    INDEX_FILE.write_text(page, encoding="utf-8")


def main():
    config = load_config()
    old_articles = load_articles()

    # Remove any previously stored Al Bat articles before doing anything else.
    old_articles = [
        a for a in old_articles
        if not is_blocked_domain(a.get("url", ""))
    ]

    existing_by_url = {a.get("url"): a for a in old_articles if a.get("url")}

    max_age_hours = int(config.get("settings", {}).get("max_age_hours", 48))
    max_per_search = int(config.get("settings", {}).get("max_items_per_search", 10))
    max_articles = int(config.get("settings", {}).get("max_stored_articles", 1000))
    min_body_chars = int(config.get("settings", {}).get("minimum_body_characters", 300))
    delay_seconds = float(config.get("settings", {}).get("delay_between_articles_seconds", 1.0))

    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    new_count = 0

    print("Starting news scan...")
    print(f"Searches: {len(config['queries'])}")

    for query in config["queries"]:
        print(f"\nSEARCH: {query}")
        feed_url = google_news_feed_url(query, config)
        feed = feedparser.parse(feed_url)

        entries = list(getattr(feed, "entries", []))[:max_per_search]
        print(f"  Results checked: {len(entries)}")

        for entry in entries:
            pub_dt = published_datetime(entry)
            if pub_dt < cutoff:
                continue

            google_url = getattr(entry, "link", "")
            if not google_url:
                continue

            original_url = decode_google_news_url(google_url)
            if not original_url:
                continue

            if is_blocked_domain(original_url):
                print("    Skipped: albat.com is excluded.")
                continue

            if original_url in existing_by_url:
                article = existing_by_url[original_url]
                matched = article.setdefault("matched_queries", [])
                if query not in matched:
                    matched.append(query)
                continue

            print(f"  Fetching: {getattr(entry, 'title', '')[:80]}")
            extracted = extract_article(original_url)

            if not extracted or len(extracted["body"]) < min_body_chars:
                print("    Skipped: article body could not be extracted or was too short.")
                time.sleep(delay_seconds)
                continue

            rss_title = (getattr(entry, "title", "") or "").strip()
            extracted_title = extracted.get("title", "")
            title = extracted_title or rss_title
            source = source_name(entry)

            article = {
                "id": article_id(original_url),
                "title": title,
                "source": source,
                "author": extracted.get("author", ""),
                "published_iso": pub_dt.isoformat(),
                "published_rfc2822": format_datetime(pub_dt),
                "published_display": pub_dt.strftime("%Y-%m-%d %H:%M UTC"),
                "url": original_url,
                "google_news_url": google_url,
                "body": extracted["body"],
                "matched_queries": [query],
                "collected_iso": datetime.now(timezone.utc).isoformat(),
            }

            old_articles.append(article)
            existing_by_url[original_url] = article
            new_count += 1
            time.sleep(delay_seconds)

    before_dedup = len(old_articles)
    old_articles = merge_duplicate_articles(old_articles)
    duplicates_removed = before_dedup - len(old_articles)

    old_articles = sorted(
        old_articles,
        key=lambda x: x.get("published_iso", ""),
        reverse=True,
    )[:max_articles]

    save_articles(old_articles)
    make_rss(old_articles, config)
    make_index(old_articles, config)

    print("\nDONE")
    print(f"New articles added: {new_count}")
    print(f"Duplicate stories removed/merged: {duplicates_removed}")
    print(f"Total stored articles: {len(old_articles)}")
    print(f"RSS file: {FEED_FILE}")
    print(f"Dashboard: {INDEX_FILE}")


if __name__ == "__main__":
    main()

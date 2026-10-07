import hashlib
import html
import json
import time
from datetime import datetime, timezone, timedelta
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
    """Remove common tracking parameters so duplicate URLs are easier to spot."""
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


def published_datetime(entry):
    if getattr(entry, "published_parsed", None):
        p = entry.published_parsed
        return datetime(p.tm_year, p.tm_mon, p.tm_mday, p.tm_hour, p.tm_min, p.tm_sec, tzinfo=timezone.utc)
    if getattr(entry, "updated_parsed", None):
        p = entry.updated_parsed
        return datetime(p.tm_year, p.tm_mon, p.tm_mday, p.tm_hour, p.tm_min, p.tm_sec, tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def google_news_feed_url(query, config):
    language = config.get("google_news", {}).get("language", "en-US")
    country = config.get("google_news", {}).get("country", "US")
    edition = config.get("google_news", {}).get("edition", "US:en")
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


def parse_extracted_article_date(value):
    """Parse only article dates that contain a usable time component."""
    if not value:
        return None
    raw=str(value).strip()

    # A date such as 2026-10-07 has no clock time, so it cannot safely be
    # used for a 3-hour freshness cutoff. Let it pass instead of guessing.
    if "T" not in raw and ":" not in raw:
        return None

    for candidate in (raw,raw.replace("Z","+00:00")):
        try:
            d=datetime.fromisoformat(candidate)
            if d.tzinfo is None:
                d=d.replace(tzinfo=timezone.utc)
            return d.astimezone(timezone.utc)
        except Exception:
            pass

    for fmt in ("%Y-%m-%d %H:%M:%S","%Y-%m-%d %H:%M","%Y/%m/%d %H:%M:%S","%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(raw,fmt).replace(tzinfo=timezone.utc)
        except Exception:
            pass
    return None

def actual_article_is_too_old(extracted,max_age_hours=3):
    if not extracted:
        return False
    actual=extracted.get("article_date_dt")
    if not actual:
        return False
    cutoff=datetime.now(timezone.utc)-timedelta(hours=float(max_age_hours))
    return actual < cutoff

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
        raw_date=(data.get("date") or "").strip()
        return {
            "title": (data.get("title") or "").strip(),
            "author": (data.get("author") or "").strip(),
            "date": raw_date,
            "body": body,
            "article_date":raw_date,
            "article_date_dt":parse_extracted_article_date(raw_date),
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


def article_id(url):
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:20]


def cdata(text):
    # CDATA cannot contain the exact sequence ]]>
    return "<![CDATA[" + str(text).replace("]]>", "]]]]><![CDATA[>") + "]]>"


def make_rss(articles, config):
    feed_title = config.get("feed", {}).get("title", "My News Feed")
    feed_description = config.get("feed", {}).get("description", "News collected from Google News searches.")
    feed_link = config.get("feed", {}).get("site_url", "http://localhost:8000/")
    max_feed_items = int(config.get("feed", {}).get("max_items", 100))

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

        items.append(
            f"""    <item>
      <title>{cdata(a['title'])}</title>
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
        rows.append(
            f"""<article>
<h2><a href="{html.escape(a['url'])}" target="_blank" rel="noopener">{html.escape(a['title'])}</a></h2>
<p><strong>{html.escape(a.get('source', ''))}</strong> · {html.escape(a.get('published_display', ''))} · {len(a.get('body', '')):,} characters extracted</p>
<p>Matched: {html.escape(", ".join(a.get('matched_queries', [])))}</p>
</article>"""
        )

    title = html.escape(config.get("feed", {}).get("title", "My News Feed"))
    page = f"""<!doctype html>
<html lang="en">
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
<p>RSS URL: <a href="feed.xml"><code>feed.xml</code></a></p>
<p>This page lists successfully extracted articles. The full extracted text is stored in the RSS feed and in <code>data/articles.json</code>.</p>
{''.join(rows) if rows else '<p>No articles yet. Run <code>python main.py</code>.</p>'}
</body>
</html>
"""
    INDEX_FILE.write_text(page, encoding="utf-8")


def main():
    config=load_config()
    old_articles=load_articles()
    existing_by_url={a.get("url"):a for a in old_articles if a.get("url")}

    settings=config.get("settings",{})
    max_age_hours=int(settings.get("max_age_hours",6))
    inspect_limit=int(settings.get("google_rss_entries_to_inspect",25))
    fresh_limit=int(settings.get("max_fresh_articles_per_search",6))
    max_articles=int(settings.get("max_stored_articles",1000))
    min_body_chars=int(settings.get("minimum_body_characters",300))
    delay_seconds=float(settings.get("delay_between_articles_seconds",0.25))

    cutoff=datetime.now(timezone.utc)-timedelta(hours=max_age_hours)
    new_count=0

    print("Starting news scan...")
    print(f"Searches: {len(config['queries'])}")
    print(f"RSS entries inspected/search: {inspect_limit}")
    print(f"Fresh unseen articles processed/search: max {fresh_limit}")

    for query in config["queries"]:
        print(f"\nSEARCH: {query}")
        feed_url=google_news_feed_url(query,config)
        feed=feedparser.parse(feed_url)

        if getattr(feed,"bozo",False):
            print("  Warning: Google News feed had a parsing/network problem.")

        entries=list(getattr(feed,"entries",[]))[:inspect_limit]
        fresh_processed=0

        for entry in entries:
            if fresh_processed>=fresh_limit:
                break

            pub_dt=published_datetime(entry)
            if pub_dt<cutoff:
                continue

            google_url=getattr(entry,"link","")
            if not google_url:
                continue

            # Decode only entries that passed the cheap publication-date filter.
            original_url=decode_google_news_url(google_url)
            if not original_url:
                continue

            if original_url in existing_by_url:
                article=existing_by_url[original_url]
                matched=article.setdefault("matched_queries",[])
                if query not in matched:
                    matched.append(query)
                continue

            fresh_processed+=1
            print(f"  Fetching: {getattr(entry,'title','')[:80]}")
            extracted=extract_article(original_url)

            if actual_article_is_too_old(extracted,settings.get("max_extracted_article_age_hours",3)):
                print("    Skipped: publisher article date is older than 3 hours")
                continue

            if not extracted or len(extracted["body"])<min_body_chars:
                print("    Skipped: article body could not be extracted or was too short.")
                time.sleep(delay_seconds)
                continue

            rss_title=(getattr(entry,"title","") or "").strip()
            extracted_title=extracted.get("title","")
            title=extracted_title or rss_title
            source=source_name(entry)

            article={
                "id":article_id(original_url),
                "title":title,
                "source":source,
                "author":extracted.get("author",""),
                "published_iso":pub_dt.isoformat(),
                "published_rfc2822":format_datetime(pub_dt),
                "published_display":pub_dt.strftime("%Y-%m-%d %H:%M UTC"),
                "url":original_url,
                "google_news_url":google_url,
                "body":extracted["body"],
                "article_date":extracted.get("article_date",""),
                "matched_queries":[query],
                "collected_iso":datetime.now(timezone.utc).isoformat(),
            }

            old_articles.append(article)
            existing_by_url[original_url]=article
            new_count+=1
            time.sleep(delay_seconds)

    old_articles=sorted(
        old_articles,
        key=lambda x:x.get("published_iso",""),
        reverse=True,
    )[:max_articles]

    save_articles(old_articles)
    make_rss(old_articles,config)
    make_index(old_articles,config)

    print("\nDONE")
    print("New articles found:",new_count)
    print("Stored articles:",len(old_articles))

if __name__ == "__main__":
    main()

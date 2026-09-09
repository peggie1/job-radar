#!/usr/bin/env python3
"""Discover geography-related 2027 recruitment leads without API keys.

Verified jobs remain in jobs.json. This collector writes broad, clearly-labelled
leads to leads.json for later human verification.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "collector-sources.json"
LEADS = ROOT / "leads.json"
JOBS = ROOT / "jobs.json"
STATUS = ROOT / "collector-status.json"
CN_TZ = timezone(timedelta(hours=8))
TODAY = datetime.now(CN_TZ).date()

RECRUIT_RE = re.compile(r"��Ƹ|У��|У԰|Ӧ��|��ҵ��|�˲�����|��λ|ְλ|��¼")
GEO_RE = re.compile(r"���ĵ���|����ѧ|������ѧ|������ʦ|���е���|���е���|������Ϣ|GIS|�����ռ�|����滮|���й滮|���ع���|��Ȼ��Դ|����滮|��ҵ�滮|���ù滮|�����о�", re.I)
SECTOR_RE = re.compile(r"����|�ǽ�|����|�滮|���Ժ|�о�Ժ|����|��Դ|��ͨ|��·|����|���|����|����|����|԰��|�ز�|�õ�|ͨ��|����", re.I)
SOE_RE = re.compile(r"����|����|�й�(?:����|��·|����|��ͨ|��Դ|����|ʯ��|ʯ��|����|�ƶ�|��ͨ|ұ��|����|����)|�н�|����|�н�|��ұ|����|��Ͷ|����|���̾�|����(?:���޹�˾|�ɷ����޹�˾)")
EXCLUDE_RE = re.compile(r"��������|�о�������|����|����|����|����Ӫ|¼ȡ����|��ʿ��|����|����֪ͨ")
CITY_NAMES = ["���", "����", "����", "����", "����", "����", "����", "̫ԭ", "����", "����", "�Ͼ�", "��ͨ", "��", "����", "����", "����", "��", "�Ϻ�", "����", "����", "�ൺ", "�Ϸ�", "֣��", "����", "�人", "��ɳ", "�ɶ�", "����", "����"]


def clean_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", html.unescape(value or ""))
    return re.sub(r"\s+", " ", value).strip()


def canonical_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value.strip())
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in {"from", "source", "spm"}]
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc.lower(), parsed.path, urllib.parse.urlencode(query), ""))


def source_level(url: str) -> str:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    if host.endswith(".gov.cn") or host.endswith(".edu.cn") or host in {"gov.cn", "ncss.cn", "www.ncss.cn", "24365.smartedu.cn"}:
        return "����/��У��Դ"
    if any(host == d or host.endswith("." + d) for d in ["mokahr.com", "zhiye.com", "hotjob.cn", "51job.com"]):
        return "���˵�λ��Ƹ���"
    return "������������"


def infer_type(text: str, hint: str) -> str:
    if "��ʦ" in text or "ѧУ" in text or "������" in text:
        return "��ʦ��"
    if "����Ա" in text or "��¼" in text:
        return "����Ա"
    if "��ҵ��λ" in text or "�˲�����" in text:
        return "��ҵ��"
    return hint or "�����"


def infer_channel(text: str) -> str:
    if "�˲�����" in text:
        return "�˲�����"
    if "����Ա" in text or "��ҵ��λ" in text or "��¼" in text:
        return "ͳһ�п�"
    if "У��" in text or "У԰��Ƹ" in text or "Ӧ��" in text or "��ҵ��" in text:
        return "У԰��Ƹ"
    return "�����Ƹ"


def infer_category(text: str) -> str:
    pairs = [
        ("������ʦ", r"������ʦ|���е���|���е���"),
        ("�����ռ�/����滮", r"�����ռ�|����滮|���й滮"),
        ("GIS/�ռ�����", r"������Ϣ|GIS|�ռ�����|���"),
        ("��Ȼ��Դ/����", r"��Ȼ��Դ|���ع���|���ع滮"),
        ("����/��ҵ�о�", r"�����о�|����滮|��ҵ�滮|���ù滮"),
    ]
    for category, pattern in pairs:
        if re.search(pattern, text, re.I):
            return category
    return "2027У�и�λ�أ�������ѡ�ڣ�"


def relevant_lead(text: str) -> bool:
    return bool(GEO_RE.search(text) or (SECTOR_RE.search(text) and SOE_RE.search(text)))


def infer_city(text: str) -> str:
    found = [city for city in CITY_NAMES if city in text]
    return "/".join(found[:5]) if found else "ȫ����������/��ȷ��"


def fetch_rss(query: str) -> list[dict[str, str]]:
    url = "https://www.bing.com/search?format=rss&count=50&q=" + urllib.parse.quote(query)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 JobRadar/1.0"})
    with urllib.request.urlopen(req, timeout=12) as response:
        root = ET.fromstring(response.read())
    rows = []
    for item in root.findall(".//item"):
        rows.append({
            "title": clean_text(item.findtext("title", "")),
            "url": canonical_url(item.findtext("link", "")),
            "description": clean_text(item.findtext("description", "")),
        })
    return rows


class PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []
        self.parts: list[str] = []
        self.in_title = False
        self.title_parts: list[str] = []
        self.current_href: str | None = None
        self.current_anchor_parts: list[str] = []
        self.anchors: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)
                self.current_href = href
                self.current_anchor_parts = []
        if tag == "title":
            self.in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False
        if tag == "a" and self.current_href:
            self.anchors.append((self.current_href, clean_text(" ".join(self.current_anchor_parts))))
            self.current_href = None
            self.current_anchor_parts = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)
        if self.in_title:
            self.title_parts.append(data)
        if self.current_href:
            self.current_anchor_parts.append(data)


def fetch_page(url: str) -> tuple[str, PageParser]:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 JobRadar/1.0"})
    with urllib.request.urlopen(req, timeout=12) as response:
        raw = response.read()
        charset = response.headers.get_content_charset() or "utf-8"
    try:
        body = raw.decode(charset, "replace")
    except LookupError:
        body = raw.decode("utf-8", "replace")
    parser = PageParser()
    parser.feed(body)
    return body, parser


def crawl_listing(source: dict[str, str]) -> tuple[list[dict[str, str]], dict]:
    detail_urls: set[str] = set()
    page_errors = 0
    listing_urls = [source["urlPattern"].format(page=page) for page in range(1, int(source.get("pages", 5)) + 1)]

    def read_listing(listing_url: str) -> tuple[str, PageParser]:
        _, parsed = fetch_page(listing_url)
        return listing_url, parsed

    with ThreadPoolExecutor(max_workers=10) as listing_pool:
        listing_futures = [listing_pool.submit(read_listing, url) for url in listing_urls]
        for future in as_completed(listing_futures):
            try:
                listing_url, parsed = future.result()
                for href, anchor_text in parsed.anchors:
                    absolute = canonical_url(urllib.parse.urljoin(listing_url, href))
                    if re.search(source["detailPattern"], absolute) and ("2027" in anchor_text or GEO_RE.search(anchor_text)):
                        detail_urls.add(absolute)
            except Exception:
                page_errors += 1

    rows: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = {pool.submit(fetch_page, url): url for url in detail_urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                _, parsed = future.result()
                full_text = clean_text(" ".join(parsed.parts))
                title = clean_text(" ".join(parsed.title_parts)) or full_text[:100]
                rows.append({"title": title, "url": url, "description": full_text[:4000]})
            except Exception:
                continue
    return rows, {"listingPagesFailed": page_errors, "detailPages": len(detail_urls)}


def crawl_keyword_search(source: dict[str, str]) -> tuple[list[dict[str, str]], dict]:
    detail_urls: set[str] = set()
    search_errors = 0

    def search_keyword(keyword: str) -> tuple[str, PageParser]:
        payload = urllib.parse.urlencode({"type": "0", "keywords": keyword, "sel_cate": "0", "sel_area": "0"}).encode()
        req = urllib.request.Request(source["url"], data=payload, headers={"User-Agent": "Mozilla/5.0 JobRadar/1.0"})
        with urllib.request.urlopen(req, timeout=12) as response:
            raw = response.read()
            charset = response.headers.get_content_charset() or "utf-8"
        parser = PageParser()
        parser.feed(raw.decode(charset, "replace"))
        return keyword, parser

    with ThreadPoolExecutor(max_workers=10) as search_pool:
        search_futures = [search_pool.submit(search_keyword, keyword) for keyword in source["keywords"]]
        for future in as_completed(search_futures):
            try:
                _, parser = future.result()
                for href in parser.links:
                    absolute = canonical_url(urllib.parse.urljoin(source["url"], href))
                    if re.search(source["detailPattern"], absolute):
                        detail_urls.add(absolute)
            except Exception:
                search_errors += 1

    rows: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = {pool.submit(fetch_page, url): url for url in detail_urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                _, parsed = future.result()
                full_text = clean_text(" ".join(parsed.parts))
                title = clean_text(" ".join(parsed.title_parts)) or full_text[:100]
                rows.append({"title": title, "url": url, "description": full_text[:4000]})
            except Exception:
                continue
    return rows, {"searchesFailed": search_errors, "detailPages": len(detail_urls)}


def make_lead(row: dict[str, str], source: dict[str, str], old: dict | None) -> dict:
    title, url, description = row["title"], row["url"], row["description"]
    text = f"{title} {description}"
    key = "lead-" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    just_event = "������" in text and not re.search(r"��λ|ְλ|��Ƹ����|У��", text)
    level = source_level(url)
    direct_geo = bool(GEO_RE.search(text))
    important = ["�Զ��ɼ���������δ�������鹫�����ĺ͸���", "Ͷ��ǰ����ȷ��2027�졢רҵ���ص�ͽ�ֹʱ��"]
    if not direct_geo:
        important.append("���������ҵ��У�и�λ�أ���δȷ�����������ĵ����ɱ���λ��������������ѡ��")
    if level == "������������":
        important.append("��ǰ����������ѧУ�����˵�λ��Ƹ��ڣ���׷��ԭʼ����")
    if just_event:
        important.append("��ǰ������������������������뵽�������������ʸ�")
    return {
        "id": key,
        "title": title[:120],
        "positionCategory": infer_category(text),
        "org": "���ӹ���ȷ��",
        "unitCategory": "������",
        "province": "δ˵��",
        "city": infer_city(text),
        "type": infer_type(text, source.get("type", "�����")),
        "nature": "�Զ����������������飩",
        "headcount": "δ˵��",
        "major": "����������عؼ��ʣ�����רҵĿ¼���ԭ��ȷ��" if direct_geo else "�����ҵУ�и�λ�أ��Ƿ��������ĵ����ɱ���λ������������ɸѡ",
        "degree": "δ˵��",
        "graduateYear": "2027��ɱ�" if "2027" in text else "Ӧ�����ɱ�����ȷ�Ͻ��",
        "applyStart": "δ˵��",
        "applyEnd": "����δ������ֹ��/������",
        "deadline": None,
        "publishedAt": "δ˵��",
        "assessment": "δ˵��",
        "consistency": "δ˵��",
        "politics": "δ˵��",
        "hukou": "δ˵��",
        "certificates": "δ˵��",
        "salary": "����δ����/������",
        "salaryMin": None,
        "match": "�ɳ���" if re.search(r"���ĵ���|����ѧ|������ѧ|������ʦ", text) else "��ѡ",
        "important": important,
        "summary": (description or "�������δ�ṩժҪ�������Դ���顣")[:500],
        "sourceTitle": title[:160],
        "sourceLevel": level,
        "verificationStatus": "����������",
        "automated": True,
        "addedAt": (old or {}).get("addedAt", TODAY.isoformat()),
        "lastSeenAt": TODAY.isoformat(),
        "url": url,
        "recruitChannel": infer_channel(text),
        "interviewMode": "δ˵��",
        "travelAdvice": "������ר��ȥ" if just_event or level == "������������" or not direct_geo else "�õ���ʽ������ȥ",
        "travelReason": "����һ���Զ����֡���δ�������������������겢ȷ��רҵ�Ͽɺ���ʽ���԰��ţ���ҪֻΪ�������ǡ�",
        "matchedQueries": sorted(set(((old or {}).get("matchedQueries") or []) + [source["name"]])),
    }


def main() -> None:
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    old_payload = json.loads(LEADS.read_text(encoding="utf-8")) if LEADS.exists() else {"leads": []}
    old_by_url = {canonical_url(x["url"]): x for x in old_payload.get("leads", []) if x.get("url")}
    verified_urls = set()
    if JOBS.exists():
        verified = json.loads(JOBS.read_text(encoding="utf-8"))
        verified_urls = {canonical_url(x["url"]) for x in verified.get("jobs", []) if x.get("url")}

    discovered: dict[str, dict] = {}
    reports, successful = [], 0
    for source in config["queries"]:
        try:
            rows = fetch_rss(source["query"])
            successful += 1
            accepted = 0
            for row in rows:
                text = f'{row["title"]} {row["description"]}'
                if not row["url"] or row["url"] in verified_urls:
                    continue
                if "2027" not in text or not RECRUIT_RE.search(text) or not relevant_lead(text) or EXCLUDE_RE.search(text):
                    continue
                lead = make_lead(row, source, old_by_url.get(row["url"]))
                if row["url"] in discovered:
                    lead["matchedQueries"] = sorted(set(discovered[row["url"]]["matchedQueries"] + lead["matchedQueries"]))
                discovered[row["url"]] = lead
                accepted += 1
            reports.append({"name": source["name"], "status": "ok", "found": len(rows), "accepted": accepted})
        except Exception as exc:
            reports.append({"name": source["name"], "status": "error", "error": str(exc)[:180]})
        time.sleep(0.4)

    for source in config.get("listingSources", []):
        try:
            rows, extra = crawl_listing(source)
            successful += 1
            accepted = 0
            for row in rows:
                text = f'{row["title"]} {row["description"]}'
                if not row["url"] or row["url"] in verified_urls:
                    continue
                if "2027" not in text or not RECRUIT_RE.search(text) or not relevant_lead(text) or EXCLUDE_RE.search(text):
                    continue
                lead = make_lead(row, source, old_by_url.get(row["url"]))
                if row["url"] in discovered:
                    lead["matchedQueries"] = sorted(set(discovered[row["url"]]["matchedQueries"] + lead["matchedQueries"]))
                discovered[row["url"]] = lead
                accepted += 1
            reports.append({"name": source["name"], "status": "ok", "found": len(rows), "accepted": accepted, **extra})
        except Exception as exc:
            reports.append({"name": source["name"], "status": "error", "error": str(exc)[:180]})

    for source in config.get("keywordSources", []):
        try:
            rows, extra = crawl_keyword_search(source)
            successful += 1
            accepted = 0
            for row in rows:
                text = f'{row["title"]} {row["description"]}'
                if not row["url"] or row["url"] in verified_urls:
                    continue
                if "2027" not in text or not RECRUIT_RE.search(text) or not relevant_lead(text) or EXCLUDE_RE.search(text):
                    continue
                lead = make_lead(row, source, old_by_url.get(row["url"]))
                if row["url"] in discovered:
                    lead["matchedQueries"] = sorted(set(discovered[row["url"]]["matchedQueries"] + lead["matchedQueries"]))
                discovered[row["url"]] = lead
                accepted += 1
            reports.append({"name": source["name"], "status": "ok", "found": len(rows), "accepted": accepted, **extra})
        except Exception as exc:
            reports.append({"name": source["name"], "status": "error", "error": str(exc)[:180]})

    if successful == 0:
        raise SystemExit("All discovery queries failed; preserving the existing lead pool")

    keep_after = TODAY - timedelta(days=config.get("retentionDays", 35))
    for url, old in old_by_url.items():
        if url in discovered or url in verified_urls:
            continue
        try:
            seen = datetime.strptime(old.get("lastSeenAt", old.get("addedAt", "2000-01-01")), "%Y-%m-%d").date()
        except ValueError:
            seen = keep_after
        if seen >= keep_after:
            discovered[url] = old

    leads = sorted(discovered.values(), key=lambda x: (x.get("lastSeenAt", ""), x.get("addedAt", "")), reverse=True)[:config.get("maxLeads", 500)]
    now = datetime.now(CN_TZ).strftime("����ʱ�� %Y-%m-%d %H:%M")
    LEADS.write_text(json.dumps({"updatedAt": now, "leadCount": len(leads), "leads": leads}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    total_sources = len(config["queries"]) + len(config.get("listingSources", [])) + len(config.get("keywordSources", []))
    STATUS.write_text(json.dumps({"updatedAt": now, "successfulSources": successful, "totalSources": total_sources, "newOrSeen": len(discovered), "reports": reports}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Collected {len(leads)} active leads from {successful}/{total_sources} sources")


if __name__ == "__main__":
    main()


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

RECRUIT_RE = re.compile(r"招聘|校招|校园|应届|毕业生|人才引进|岗位|职位|招录")
GEO_RE = re.compile(r"人文地理|地理学|地理科学|地理教师|高中地理|初中地理|地理信息|GIS|国土空间|城乡规划|城市规划|土地管理|自然资源|区域规划|产业规划|文旅规划|城市研究", re.I)
SECTOR_RE = re.compile(r"城市|城建|建筑|规划|设计院|研究院|电力|能源|交通|铁路|地铁|测绘|地质|土地|文旅|园区|地产|置地|通信|电信", re.I)
SOE_RE = re.compile(r"央企|国企|中国(?:建筑|铁路|铁建|交通|能源|电力|石油|石化|电信|移动|联通|冶金|航天|航空)|中建|中铁|中交|中冶|华润|国投|保利|招商局|集团(?:有限公司|股份有限公司)")
EXCLUDE_RE = re.compile(r"招生简章|研究生招生|考研|复试|调剂|夏令营|录取名单|博士后|讲座|会议通知")
CITY_NAMES = ["天津", "苏州", "无锡", "常州", "杭州", "湖州", "嘉兴", "太原", "晋城", "长治", "南京", "南通", "镇江", "扬州", "绍兴", "宁波", "金华", "上海", "北京", "济南", "青岛", "合肥", "郑州", "西安", "武汉", "长沙", "成都", "广州", "深圳"]


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
        return "政府/高校来源"
    if any(host == d or host.endswith("." + d) for d in ["mokahr.com", "zhiye.com", "hotjob.cn", "51job.com"]):
        return "用人单位招聘入口"
    return "公开检索线索"


def infer_type(text: str, hint: str) -> str:
    if "教师" in text or "学校" in text or "教育局" in text:
        return "教师编"
    if "公务员" in text or "招录" in text:
        return "公务员"
    if "事业单位" in text or "人才引进" in text:
        return "事业编"
    return hint or "央国企"


def infer_channel(text: str) -> str:
    if "人才引进" in text:
        return "人才引进"
    if "公务员" in text or "事业单位" in text or "招录" in text:
        return "统一招考"
    if "校招" in text or "校园招聘" in text or "应届" in text or "毕业生" in text:
        return "校园招聘"
    return "社会招聘"


def infer_category(text: str) -> str:
    pairs = [
        ("地理教师", r"地理教师|高中地理|初中地理"),
        ("国土空间/城乡规划", r"国土空间|城乡规划|城市规划"),
        ("GIS/空间数据", r"地理信息|GIS|空间数据|测绘"),
        ("自然资源/土地", r"自然资源|土地管理|土地规划"),
        ("城市/产业研究", r"城市研究|区域规划|产业规划|文旅规划"),
    ]
    for category, pattern in pairs:
        if re.search(pattern, text, re.I):
            return category
    return "2027校招岗位池（待二次选岗）"


def relevant_lead(text: str) -> bool:
    return bool(GEO_RE.search(text) or (SECTOR_RE.search(text) and SOE_RE.search(text)))


def infer_city(text: str) -> str:
    found = [city for city in CITY_NAMES if city in text]
    return "/".join(found[:5]) if found else "全国其他地区/待确认"


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
    just_event = "宣讲会" in text and not re.search(r"岗位|职位|招聘简章|校招", text)
    level = source_level(url)
    direct_geo = bool(GEO_RE.search(text))
    important = ["自动采集线索，尚未逐条核验公告正文和附件", "投递前必须确认2027届、专业、地点和截止时间"]
    if not direct_geo:
        important.append("这是相关行业的校招岗位池，尚未确认其中有人文地理可报岗位，需进入官网二次选岗")
    if level == "公开检索线索":
        important.append("当前不是政府、学校或用人单位招聘入口，需追溯原始公告")
    if just_event:
        important.append("当前更像宣讲活动线索，不代表必须到场或已有面试资格")
    return {
        "id": key,
        "title": title[:120],
        "positionCategory": infer_category(text),
        "org": "待从公告确认",
        "unitCategory": "待核验",
        "province": "未说明",
        "city": infer_city(text),
        "type": infer_type(text, source.get("type", "央国企")),
        "nature": "自动发现线索（待核验）",
        "headcount": "未说明",
        "major": "包含地理相关关键词；具体专业目录需打开原文确认" if direct_geo else "相关行业校招岗位池；是否设置人文地理可报岗位需进入官网二次筛选",
        "degree": "未说明",
        "graduateYear": "2027届可报" if "2027" in text else "应届生可报（待确认届别）",
        "applyStart": "未说明",
        "applyEnd": "官网未公布截止日/待核验",
        "deadline": None,
        "publishedAt": "未说明",
        "assessment": "未说明",
        "consistency": "未说明",
        "politics": "未说明",
        "hukou": "未说明",
        "certificates": "未说明",
        "salary": "待遇未公开/待核验",
        "salaryMin": None,
        "match": "可尝试" if re.search(r"人文地理|地理学|地理科学|地理教师", text) else "备选",
        "important": important,
        "summary": (description or "搜索结果未提供摘要，请打开来源核验。")[:500],
        "sourceTitle": title[:160],
        "sourceLevel": level,
        "verificationStatus": "待核验线索",
        "automated": True,
        "addedAt": (old or {}).get("addedAt", TODAY.isoformat()),
        "lastSeenAt": TODAY.isoformat(),
        "url": url,
        "recruitChannel": infer_channel(text),
        "interviewMode": "未说明",
        "travelAdvice": "不建议专程去" if just_event or level == "公开检索线索" or not direct_geo else "拿到正式面试再去",
        "travelReason": "这是一条自动发现、尚未核验的线索。先完成网申并确认专业认可和正式面试安排，不要只为宣讲会跨城。",
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
    now = datetime.now(CN_TZ).strftime("北京时间 %Y-%m-%d %H:%M")
    LEADS.write_text(json.dumps({"updatedAt": now, "leadCount": len(leads), "leads": leads}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    total_sources = len(config["queries"]) + len(config.get("listingSources", [])) + len(config.get("keywordSources", []))
    STATUS.write_text(json.dumps({"updatedAt": now, "successfulSources": successful, "totalSources": total_sources, "newOrSeen": len(discovered), "reports": reports}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Collected {len(leads)} active leads from {successful}/{total_sources} sources")


if __name__ == "__main__":
    main()

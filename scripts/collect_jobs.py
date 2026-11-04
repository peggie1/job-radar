#!/usr/bin/env python3
"""Discover geography-related 2027 recruitment leads without API keys.

Verified jobs remain in jobs.json. This collector writes broad, clearly-labelled
leads to leads.json for later human verification.
"""

from __future__ import annotations

import argparse
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
EXCLUDE_RE = re.compile(r"招生简章|研究生招生|考研|夏令营|博士后|录取名单")
TEACHER_RE = re.compile(r"教师|师资|教育系统|教育局|中小学|中学|学校|社会法治|初中社政|初中社会")
ATTACHMENT_RE = re.compile(r"\.(?:pdf|xlsx?|docx?|zip)(?:[?&#]|$)|附件|岗位表|专业要求|download|downfile", re.I)
FOLLOW_RE = re.compile(r"教师招聘|人事招聘|招聘信息|招聘公告|公告公示|公告通知|通知公告|公示公告|教师招考|招考信息|公开招聘")
COLUMN_RE = re.compile(r"^(?:教师招聘|人才招聘|人事招聘|招聘信息|招聘公告|公告公示|公告通知|通知公告|公示公告|教师招考|招考信息|公开招聘|招考录用|招录信息)$")
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
    if host.endswith(".gov.cn") or host == "gov.cn":
        return "政府公告（具体岗位及附件待核）"
    if host.endswith(".edu.cn"):
        return "高校发布/转载（用人单位原文与岗位待核）"
    if host in {"ncss.cn", "www.ncss.cn", "24365.smartedu.cn"}:
        return "国家就业平台（具体单位及岗位待核）"
    if any(host == d or host.endswith("." + d) for d in ["mokahr.com", "zhiye.com", "hotjob.cn", "51job.com"]):
        return "用人单位招聘入口"
    return "公开检索线索"


def infer_type(text: str, hint: str = "") -> str:
    """Classify only explicit evidence; a university navigation bar proves nothing."""
    if re.search(r"(?:事业编制|全额拨款事业单位编制).{0,25}教师|教师.{0,25}事业编制", text):
        return "教师编"
    if re.search(r"公务员.{0,12}(?:招录|录用|招聘)", text):
        return "公务员"
    if re.search(r"事业单位.{0,12}公开招聘", text) and not TEACHER_RE.search(text[:150]):
        return "事业编"
    if re.search(r"中央企业|国有独资|国有控股|国有企业|国务院国资委", text):
        return "央国企"
    return "未说明"

def infer_channel(text: str) -> str:
    if "人才引进" in text:
        return "人才引进"
    if "公务员" in text or "事业单位" in text or "招录" in text:
        return "统一招考"
    if "校招" in text or "校园招聘" in text or "应届" in text or "毕业生" in text:
        return "校园招聘"
    return "社会招聘" if "社会招聘" in text or "面向社会" in text else "未说明"


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
    return bool(GEO_RE.search(text) or TEACHER_RE.search(text[:250]) or (SECTOR_RE.search(text) and SOE_RE.search(text)))


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
    """Collect visible article text, not script/CSS or university navigation."""
    VOID = {"br", "hr", "img", "meta", "link", "input", "source", "wbr", "area", "base"}
    IGNORE = {"script", "style", "nav", "header", "footer", "noscript", "template"}
    HIDDEN = {"script", "style", "noscript", "template"}
    NAV_TOKEN = re.compile(r"^(?:nav|navigation|navbar|menu|breadcrumb|header|footer|sidebar)(?:$|[-_])", re.I)
    CONTENT = re.compile(r"^(?:zoom|zoomcon|zoomtext|article|article-content|article_content|articleContent|artContent|artCon|newsContent|news_content|newscontent|v_news_content|vsb_content|TRS_Editor|UCAP-CONTENT|readcontent|content-body)$", re.I)

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links = []
        self.parts = []
        self.title_parts = []
        self.heading_parts = []
        self.anchors = []
        self.meta = {}
        self.blocks = []
        self.stack = []
        self.in_title = False
        self.current_href = None
        self.current_anchor_parts = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        a = dict(attrs)
        if tag == "meta":
            self.meta[(a.get("name") or a.get("property") or "").lower()] = a.get("content", "")
        attrs_tokens = (a.get("id", "") + " " + a.get("class", "")).split()
        skipped = (any(f[1] for f in self.stack) or tag in self.IGNORE or
                   a.get("role") == "navigation" or any(self.NAV_TOKEN.match(t) for t in attrs_tokens))
        block = None
        if not skipped and (tag == "article" or any(self.CONTENT.match(t) for t in attrs_tokens)):
            block = []
            self.blocks.append(block)
        if tag not in self.VOID:
            self.stack.append((tag, skipped, block))
        if tag == "title":
            self.in_title = True
        # Navigation links are useful for discovering official columns, but their
        # text must never become evidence about the article's job or ownership.
        hidden = any(f[0] in self.HIDDEN for f in self.stack)
        if tag == "a" and not hidden and a.get("href"):
            self.links.append(a["href"])
            self.current_href = a["href"]
            self.current_anchor_parts = []

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if tag == "a" and self.current_href:
            self.anchors.append((self.current_href, clean_text(" ".join(self.current_anchor_parts))))
            self.current_href = None
            self.current_anchor_parts = []
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                self.stack = self.stack[:index]
                break

    def handle_data(self, data):
        if self.in_title:
            self.title_parts.append(data)
            return
        if self.current_href and not any(f[0] in self.HIDDEN for f in self.stack):
            self.current_anchor_parts.append(data)
        if any(f[1] for f in self.stack) or not data.strip():
            return
        self.parts.append(data)
        for _, _, block in self.stack:
            if block is not None:
                block.append(data)
        if any(f[0] == "h1" for f in self.stack):
            self.heading_parts.append(data)

    def article_text(self):
        blocks = [clean_text(" ".join(b)) for b in self.blocks]
        substantial = [b for b in blocks if len(b) > 80]
        return max(substantial, key=len) if substantial else clean_text(" ".join(self.parts))

    def headline(self):
        heading = clean_text(" ".join(self.heading_parts))
        title = clean_text(" ".join(self.title_parts))
        return heading if len(heading) > 8 else title

    def published_at(self):
        for key in ("pubdate", "publishdate", "publishtime", "article:published_time", "date", "dc.date.issued"):
            value = self.meta.get(key, "")
            match = re.search(r"(20\d{2})[-年/](\d{1,2})[-月/](\d{1,2})", value)
            if match:
                try:
                    return datetime(*(int(x) for x in match.groups())).date().isoformat()
                except ValueError:
                    continue
        text = self.article_text()[:1500]
        # A generic '时间' also matches 笔试时间/报名时间: never use it as publication.
        match = re.search(r"(?:发布日期|发布时间|发文日期|更新日期|更新时间)\s*[：:]\s*(20\d{2})[-年/](\d{1,2})[-月/](\d{1,2})", text)
        if match:
            try:
                return datetime(*(int(x) for x in match.groups())).date().isoformat()
            except ValueError:
                pass
        return "未说明"

    def attachment_links(self, url):
        links, seen = [], set()
        for href, title in self.anchors:
            absolute = canonical_url(urllib.parse.urljoin(url, href))
            # Check separately: adding the label after '.pdf' breaks its end anchor.
            if (absolute.startswith(("https://", "http://")) and absolute not in seen and
                    (ATTACHMENT_RE.search(href) or ATTACHMENT_RE.search(title))):
                links.append({"title": title or "附件（需人工核验）", "url": absolute})
                seen.add(absolute)
        return links[:40]


def is_column_title(title):
    """Do not mistake a menu named 招聘信息 for a concrete announcement."""
    pieces = re.split(r"\s*[|_｜]\s*", clean_text(title))
    first = pieces[0].strip(" >»→")
    return bool(COLUMN_RE.fullmatch(first) or
                (not RECRUIT_RE.search(first) and any(COLUMN_RE.fullmatch(p.strip(" >»→")) for p in pieces[1:])))


def page_problem(parsed):
    headline, article = parsed.headline(), parsed.article_text()
    if re.fullmatch(r"跳转中[.。…]*|页面跳转中[.。…]*", headline):
        return "栏目仅返回跳转页，需要人工核验"
    if len(article) < 60 and not parsed.anchors:
        return "动态页/正文及链接为空，需要人工核验"
    return None


def fetch_page(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 JobRadar/2.0", "Referer": url})
    with urllib.request.urlopen(req, timeout=14) as response:
        raw = response.read(4_000_000)
        charset = response.headers.get_content_charset()
    if not charset:
        match = re.search(br"charset\s*=\s*[\"']?([a-zA-Z0-9_-]+)", raw[:4096], re.I)
        charset = match[1].decode("ascii") if match else "utf-8"
    try:
        body = raw.decode(charset, "replace")
    except LookupError:
        body = raw.decode("utf-8", "replace")
    parser = PageParser()
    parser.feed(body)
    return body, parser


def detail_rows(detail_urls, max_workers=8):
    rows, errors = [], []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(fetch_page, url): (url, title) for url, title in detail_urls.items()}
        for future in as_completed(futures):
            url, anchor_title = futures[future]
            try:
                raw, parsed = future.result()
                article = parsed.article_text()
                title = parsed.headline() or anchor_title or article[:100]
                if len(article) < 60:
                    raise ValueError("动态页/正文为空，需要人工核验")
                rows.append({"title": title, "url": url, "description": article[:16000],
                             "publishedAt": parsed.published_at(),
                             "attachments": parsed.attachment_links(url)})
            except Exception as exc:
                errors.append({"url": url, "error": str(exc)[:160]})
    return rows, {"detailPages": len(detail_urls), "detailPagesFailed": len(errors),
                  "detailErrors": errors[:20],
                  "attachmentsPending": sum(len(r.get("attachments", [])) for r in rows)}


def listing_candidate(title):
    # Generic teacher announcements often put every subject and year in an attachment.
    return bool("2027" in title or GEO_RE.search(title) or
                (RECRUIT_RE.search(title) and TEACHER_RE.search(title)))


def crawl_listing(source):
    urls = {}
    errors = []
    listing_urls = [source["urlPattern"].format(page=page)
                    for page in range(1, int(source.get("pages", 5)) + 1)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(fetch_page, url): url for url in listing_urls}
        for future in as_completed(futures):
            try:
                _, parsed = future.result()
                for href, title in parsed.anchors:
                    url = canonical_url(urllib.parse.urljoin(futures[future], href))
                    if re.search(source["detailPattern"], url) and listing_candidate(title):
                        urls.setdefault(url, title)
            except Exception as exc:
                errors.append({"url": futures[future], "error": str(exc)[:160]})
    rows, extra = detail_rows(dict(list(urls.items())[:source.get("maxDetails", 120)]))
    return rows, {"listingPagesFailed": len(errors), "listingErrors": errors[:10], **extra}


def crawl_keyword_search(source):
    urls = {}
    errors = []
    def search_keyword(keyword):
        payload = urllib.parse.urlencode({"type": "0", "keywords": keyword, "sel_cate": "0", "sel_area": "0"}).encode()
        req = urllib.request.Request(source["url"], data=payload, headers={"User-Agent": "Mozilla/5.0 JobRadar/2.0"})
        with urllib.request.urlopen(req, timeout=14) as response:
            raw = response.read(4_000_000)
            charset = response.headers.get_content_charset() or "utf-8"
        parsed = PageParser()
        parsed.feed(raw.decode(charset, "replace"))
        return parsed
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(search_keyword, k): k for k in source["keywords"]}
        for future in as_completed(futures):
            try:
                parsed = future.result()
                for href, title in parsed.anchors:
                    url = canonical_url(urllib.parse.urljoin(source["url"], href))
                    if re.search(source["detailPattern"], url):
                        urls.setdefault(url, title)
            except Exception as exc:
                errors.append({"keyword": futures[future], "error": str(exc)[:160]})
    rows, extra = detail_rows(dict(list(urls.items())[:source.get("maxDetails", 120)]))
    return rows, {"searchesFailed": len(errors), "searchErrors": errors[:10], **extra}


def crawl_official(source):
    """Read named official columns even when neither 2027 nor geography is in the title."""
    roots = source.get("urls") or [source["url"]]
    visited, details, errors = set(), {}, []
    frontier = list(roots)
    listing_pages = 0
    lookback = TODAY - timedelta(days=int(source.get("lookbackDays", 60)))
    allowed_hosts = {(urllib.parse.urlsplit(u).hostname or "").lower() for u in roots}
    for depth in range(int(source.get("followDepth", 1)) + 1):
        pending, frontier = frontier, []
        for current in pending[:source.get("maxListingPages", 4)]:
            if current in visited:
                continue
            visited.add(current)
            try:
                _, parsed = fetch_page(current)
                problem = page_problem(parsed)
                if problem:
                    raise ValueError(problem)
                listing_pages += 1
                for href, title in parsed.anchors:
                    absolute = canonical_url(urllib.parse.urljoin(current, href))
                    host = (urllib.parse.urlsplit(absolute).hostname or "").lower()
                    if not absolute.startswith(("https://", "http://")) or host not in allowed_hosts:
                        continue
                    if is_column_title(title):
                        if depth < int(source.get("followDepth", 1)):
                            frontier.append(absolute)
                        continue
                    if RECRUIT_RE.search(title) and not EXCLUDE_RE.search(title):
                        if not re.search(r"成绩|拟聘|拟录|录用名单|体检名单|面试名单|准考证|资格复审名单", title):
                            if not ATTACHMENT_RE.search(absolute):
                                details.setdefault(absolute, title)
                    elif depth < int(source.get("followDepth", 1)) and FOLLOW_RE.search(title):
                        frontier.append(absolute)
                # A configured exact announcement is also a valid discovery source.
                if (RECRUIT_RE.search(parsed.headline()) and not is_column_title(parsed.headline())
                        and not EXCLUDE_RE.search(parsed.headline()) and len(parsed.article_text()) > 100):
                    details.setdefault(current, parsed.headline())
            except Exception as exc:
                errors.append({"url": current, "error": str(exc)[:160]})
    details = dict(list(details.items())[:int(source.get("maxDetails", 10))])
    rows, extra = detail_rows(details, max_workers=4)
    recent, historical = [], 0
    for row in rows:
        value = row.get("publishedAt", "未说明")
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) and value < lookback.isoformat():
            historical += 1
            row["historical"] = True
        recent.append(row)
    return recent, {"listingPages": listing_pages, "listingPagesFailed": len(errors),
                    "listingErrors": errors[:10], "historicalPending": historical,
                    "official": True, **extra}


def should_accept(row, source):
    title = row["title"]
    text = f'{title} {row["description"]}'
    if EXCLUDE_RE.search(title):
        return False
    if source.get("scope") == "sector":
        # Bing may silently return the same national event for unrelated queries.
        # A configured topic must occur in the returned text, not only the query.
        keywords = source.get("keywords") or []
        if isinstance(keywords, str):
            keywords = [keywords]
        topic_match = any(str(k).strip() and str(k).casefold() in text.casefold() for k in keywords)
        return ("2027" in text and topic_match and
                bool(re.search(r"招聘|校招|招录|人才引进|诚聘", text)))
    if not RECRUIT_RE.search(text):
        return False
    if source.get("query") and source.get("scope") == "teacher":
        # Apply geographic relevance only to search results. An official district
        # notice may legitimately omit its city from the title and article text.
        city = source.get("city", "").strip()
        teacher_recruitment = re.search(r"教师|师资|教育系统|教育局|中小学|中学|高中|初中|小学|幼儿园|教职", text)
        return bool(city and city in text and "2027" in text and teacher_recruitment)
    teacher = source.get("scope") == "teacher" or bool(TEACHER_RE.search(title))
    if teacher:
        # Subject/year may be entirely in PDF/XLS/WeChat images: retain as pending only.
        return True
    return "2027" in text and relevant_lead(text)


def make_lead(row: dict[str, str], source: dict[str, str], old: dict | None) -> dict:
    title, url, description = row["title"], row["url"], row["description"]
    text = f"{title} {description}"
    key = "lead-" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    just_event = "宣讲会" in text and not re.search(r"岗位|职位|招聘简章|校招", text)
    level = source_level(url)
    direct_geo = bool(GEO_RE.search(text))
    important = ["自动采集的是公告/线索，不是已核验具体岗位；单位性质、编制和全部岗位附件仍需人工核验", "投递前必须确认2027届、专业、地点和截止时间"]
    if source.get("scope") == "teacher" or TEACHER_RE.search(title):
        important.append("教师统招可能把学科、专业、届别写在附件；尚不能确认有地理/社政岗位或符合个人资格")
    if row.get("attachments"):
        important.append("已发现岗位/专业等附件链接，但自动程序未解读附件，不能据此确认可报")
    if row.get("historical"):
        important.append("发布日期早于本来源回溯窗口：仅作历史单位发现线索，不证明当前仍在招聘；须另查当届新公告、延期和截止")
    if not direct_geo:
        important.append("这是相关方向的招聘线索，尚未确认有人文地理可报岗位或属于校园招聘；单位性质、实际职责及专业目录均须核验")
    if level == "公开检索线索":
        important.append("当前不是政府、学校或用人单位招聘入口，需追溯原始公告")
    if just_event:
        important.append("当前更像宣讲活动线索，不代表必须到场或已有面试资格")
    return {
        "id": key,
        "title": title[:120],
        "positionCategory": "相关方向招聘线索（待核验）" if source.get("scope") == "sector" and not direct_geo else infer_category(text),
        "org": "待从公告确认",
        "unitCategory": "待核验",
        "province": "未说明",
        "city": source.get("city") or infer_city(text),
        "type": infer_type(text, source.get("type", "")),
        "nature": "自动发现公告/线索（单位性质、编制及岗位条件待核验）",
        "headcount": "未说明",
        "major": "包含地理相关关键词；具体专业目录需打开原文确认" if direct_geo else "相关方向招聘线索；是否接受人文地理、所需硬技能及资格须核验",
        "degree": "未说明",
        "graduateYear": "未说明" if source.get("scope") == "sector" else "应届生可报（待确认届别）",
        "applyStart": "未说明",
        "applyEnd": "官网未公布截止日/待核验",
        "deadline": None,
        "publishedAt": row.get("publishedAt", "未说明"),
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
        "historical": bool(row.get("historical")),
        "addedAt": (old or {}).get("addedAt", TODAY.isoformat()),
        "lastSeenAt": TODAY.isoformat(),
        "url": url,
        "recruitChannel": infer_channel(text),
        "interviewMode": "未说明",
        "travelAdvice": "不建议专程去" if just_event or level == "公开检索线索" or not direct_geo else "拿到正式面试再去",
        "travelReason": "这是一条自动发现、尚未核验的线索。先完成网申并确认专业认可和正式面试安排，不要只为宣讲会跨城。",
        "attachments": row.get("attachments", []),
        "sourceCity": source.get("city", "未说明"),
        "matchedQueries": sorted(set(((old or {}).get("matchedQueries") or []) + [source["name"]])),
    }


def run_collection(config, old_payload, jobs_payload):
    old_by_url = {canonical_url(x["url"]): x for x in old_payload.get("leads", []) if x.get("url")}
    verified_urls = {canonical_url(x["url"]) for x in jobs_payload.get("jobs", [])
                     if x.get("url") and x.get("verificationStatus") != "待核验线索"}
    sources = ([(s, "query", fetch_rss) for s in config.get("queries", [])] +
               [(s, "listing", crawl_listing) for s in config.get("listingSources", [])] +
               [(s, "keyword", crawl_keyword_search) for s in config.get("keywordSources", [])] +
               [(s, "official", crawl_official) for s in config.get("officialSources", [])])
    discovered, reports = {}, []
    def process(item):
        source, kind, function = item
        started = datetime.now(CN_TZ).isoformat(timespec="seconds")
        try:
            if kind == "query":
                rows, extra = function(source["query"]), {}
            else:
                rows, extra = function(source)
            failures = sum(extra.get(k, 0) for k in ("listingPagesFailed", "searchesFailed", "detailPagesFailed"))
            relevant_found = sum(should_accept(row, source) for row in rows)
            report = {"name": source["name"], "kind": kind, "city": source.get("city", "未说明"),
                      "sourceUrl": source.get("url") or source.get("urlPattern") or source.get("query"),
                      "checkedAt": started, "status": "partial" if failures else "ok",
                      "found": len(rows), "relevantFound": relevant_found, "accepted": 0, **extra}
            if kind == "query" and not relevant_found:
                report["status"] = "manual-review"
                report["note"] = "搜索未命中目标城市/招聘主题；可能无结果或检索服务降级，不能当作地区已健康覆盖"
            elif kind == "official" and not rows and not failures:
                report["status"] = "manual-review"
                report["note"] = "未解析到近期具体招聘正文；可能无新公告、动态栏目或需进入下级单位，不能当作无招聘"
            elif (kind == "official" and not rows and failures and
                  extra.get("listingPages") == 0):
                report["status"] = "error"
                report["note"] = "所有配置官方页面读取失败或只返回空白/跳转；不能当作已正常检查"
            return source, rows, report
        except Exception as exc:
            return source, [], {"name": source["name"], "kind": kind, "city": source.get("city", "未说明"),
                                "checkedAt": started, "status": "error", "found": 0, "accepted": 0,
                                "error": str(exc)[:180]}
    with ThreadPoolExecutor(max_workers=int(config.get("sourceWorkers", 5))) as pool:
        futures = [pool.submit(process, item) for item in sources]
        for future in as_completed(futures):
            source, rows, report = future.result()
            for row in rows:
                url = canonical_url(row.get("url", ""))
                if not url or url in verified_urls or not should_accept(row, source):
                    continue
                row["url"] = url
                old = old_by_url.get(url)
                if old and old.get("curated"):
                    # Manual pre-screening remains pending; never overwrite its
                    # contact details, real deadline, scope, or unresolved caveats.
                    lead = dict(old)
                    lead.update({"url": url, "verificationStatus": "待核验线索", "automated": False,
                                 "lastSeenAt": TODAY.isoformat(),
                                 "matchedQueries": sorted(set((old.get("matchedQueries") or []) + [source["name"]]))})
                else:
                    lead = make_lead(row, source, old)
                if url in discovered:
                    lead["matchedQueries"] = sorted(set(discovered[url]["matchedQueries"] + lead["matchedQueries"]))
                discovered[url] = lead
                report["accepted"] += 1
            reports.append(report)
            print(json.dumps({"source": report["name"], "status": report["status"],
                              "accepted": report["accepted"]}, ensure_ascii=False), flush=True)
    connected = sum(r["status"] != "error" for r in reports)
    healthy = sum(r["status"] == "ok" for r in reports)
    if not connected:
        raise RuntimeError("All discovery sources failed; preserve existing leads and report failure")
    keep_after = TODAY - timedelta(days=int(config.get("retentionDays", 35)))
    for url, old in old_by_url.items():
        if url in discovered or url in verified_urls:
            continue
        try:
            seen = datetime.strptime(old.get("lastSeenAt", old.get("addedAt", "2000-01-01")), "%Y-%m-%d").date()
        except ValueError:
            seen = keep_after
        if seen >= keep_after:
            kept = dict(old)
            if kept.get("curated"):
                kept["automated"] = False
            else:
                # Repair legacy university-nav misclassification without inventing ownership.
                kept["type"] = infer_type(kept.get("title", ""))
                kept["graduateYear"] = "应届生可报（待确认届别）"
                kept["nature"] = "自动发现公告/线索（单位性质、编制及岗位条件待核验）"
            kept["verificationStatus"] = "待核验线索"
            discovered[url] = kept
    leads = sorted(discovered.values(), key=lambda x: (x.get("lastSeenAt", ""), x.get("addedAt", ""), x["id"]), reverse=True)
    leads = leads[:int(config.get("maxLeads", 1000))]
    now = datetime.now(CN_TZ).strftime("北京时间 %Y-%m-%d %H:%M")
    city_coverage = []
    for city in config.get("teacherCoverageCities", []):
        city_reports = [r for r in reports if r.get("city") == city and r.get("kind") == "official"]
        state = "未配置官方直连" if not city_reports else (
            "需人工补查" if any(r["status"] != "ok" for r in city_reports) else "已读取配置栏目，仍须核验岗位附件及下属学校")
        city_coverage.append({"city": city, "state": state, "officialSources": len(city_reports),
                              "healthySources": sum(r["status"] == "ok" for r in city_reports),
                              "discoveredNoticeLeads": sum(r["accepted"] for r in city_reports)})
    status = {"updatedAt": now, "successfulSources": healthy, "connectedSources": connected,
              "totalSources": len(sources), "partialOrManualSources": sum(r["status"] in ("partial", "manual-review") for r in reports),
              "newOrSeen": sum(r.get("accepted", 0) for r in reports),
              "verificationDisclaimer": "来源成功只指已解析配置页面，不代表地区全量覆盖、资格核验或没有遗漏；自动线索绝不能替代jobs.json",
              "teacherCoverage": city_coverage,
              "reports": sorted(reports, key=lambda r: (r["kind"], r.get("city", ""), r["name"]))}
    return {"updatedAt": now, "leadCount": len(leads), "leads": leads}, status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="Directory containing jobs/config/leads/status")
    parser.add_argument("--dry-run", action="store_true", help="Read sources and print status; do not replace data files")
    args = parser.parse_args()
    root = args.root.resolve()
    config = json.loads((root / "collector-sources.json").read_text(encoding="utf-8"))
    old = json.loads((root / "leads.json").read_text(encoding="utf-8")) if (root / "leads.json").exists() else {"leads": []}
    jobs = json.loads((root / "jobs.json").read_text(encoding="utf-8")) if (root / "jobs.json").exists() else {"jobs": []}
    payload, status = run_collection(config, old, jobs)
    if not args.dry_run:
        for name, data in (("leads.json", payload), ("collector-status.json", status)):
            # Same-directory atomic replace, never truncate the existing valid pool first.
            target = root / name
            temporary = target.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(target)
    print(json.dumps({"leadCount": payload["leadCount"], "healthySources": status["successfulSources"],
                      "totalSources": status["totalSources"], "dryRun": args.dry_run}, ensure_ascii=False))


if __name__ == "__main__":
    main()

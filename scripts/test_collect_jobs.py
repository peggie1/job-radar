import importlib.util
import json
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('collector', Path(__file__).with_name('collect_jobs.py'))
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)


class CollectorTests(unittest.TestCase):
    def page(self, text):
        p = c.PageParser()
        p.feed(text)
        return p

    def test_script_and_navigation_are_not_article(self):
        p = self.page('<title>亦庄控股2027校招</title><nav>教师 学生 学校</nav>'
                      '<script>var x="教师招聘2027";</script><style>教师</style>'
                      '<article><h1>亦庄控股2027校招</h1>' + '国有企业校园招聘欢迎应届毕业生。' * 8 + '</article>')
        self.assertNotIn('教师', p.article_text())
        self.assertEqual(c.infer_type(p.article_text(), '教师编'), '央国企')

    def test_university_nav_cannot_prove_teacher_or_soe(self):
        p = self.page('<nav>教师登录</nav><main>普通企业2027年招聘，单位性质待确认。</main>')
        self.assertEqual(c.infer_type(p.article_text(), '央国企'), '未说明')

    def test_navigation_links_are_followable_but_not_classification_evidence(self):
        p = self.page('<div class="nav-main"><a href="/recruit/">招聘信息</a>'
                      '<a href="/teacher">事业编制教师 国有企业</a></div>'
                      '<article>普通企业2027年招聘，单位性质待确认。</article>')
        self.assertIn(('/recruit/', '招聘信息'), p.anchors)
        self.assertNotIn('国有企业', p.article_text())
        self.assertNotIn('事业编制', p.article_text())
        self.assertEqual(c.infer_type(p.article_text()), '未说明')

    def test_generic_teacher_title_without_year_is_discovered(self):
        self.assertTrue(c.listing_candidate('某区教育系统公开招聘教师公告'))
        row = {'title': '某区教师招聘公告', 'description': '学科和专业详见附件', 'url': 'https://a.gov.cn/1'}
        self.assertTrue(c.should_accept(row, {'scope': 'teacher'}))

    def test_teacher_retest_and_adjustment_are_not_blanket_excluded(self):
        row = {'title': '某区2027教师招聘公告', 'description': '资格复试及岗位调剂。', 'url': 'https://a.gov.cn/1'}
        self.assertTrue(c.should_accept(row, {'scope': 'teacher'}))

    def test_student_admission_is_excluded(self):
        row = {'title': '2027研究生招生简章', 'description': '学校招聘说明', 'url': 'https://a.edu.cn/1'}
        self.assertFalse(c.should_accept(row, {'scope': 'teacher'}))

    def test_attachments_are_retained_but_not_verified(self):
        p = self.page('<article><h1>教师招聘</h1><a href="/岗位计划.xlsx">岗位表</a>'
                      '<a href="/file.pdf">资格说明</a></article>')
        links = p.attachment_links('https://a.gov.cn/news/1')
        self.assertEqual(len(links), 2)
        row = {'title': '2027教师招聘', 'description': '专业详见附件', 'url': 'https://a.gov.cn/1', 'attachments': links}
        lead = c.make_lead(row, {'name': '测试', 'scope': 'teacher', 'city': '天津'}, None)
        self.assertEqual(lead['verificationStatus'], '待核验线索')
        self.assertEqual(lead['graduateYear'], '应届生可报（待确认届别）')
        self.assertIsNone(lead['deadline'])
        self.assertEqual(lead['city'], '天津')
        self.assertEqual(lead['type'], '未说明')

    def test_known_teacher_establishment_requires_explicit_text(self):
        self.assertEqual(c.infer_type('公开招聘事业编制教师80名'), '教师编')
        self.assertEqual(c.infer_type('民办学校招聘高中教师'), '未说明')

    def test_attachment_extensions_labels_queries_and_protocols(self):
        p = self.page('<article><a href="/plan.PDF">资格条件</a>'
                      '<a href="/roles.xlsx?download=1">学科要求</a>'
                      '<a href="/opaque?id=3">附件1：招聘岗位表</a>'
                      '<a href="/plan.PDF#page=2">再次下载</a>'
                      '<a href="javascript:download()">附件下载</a></article>')
        links = p.attachment_links('https://a.gov.cn/news/1')
        self.assertEqual([x['url'] for x in links], [
            'https://a.gov.cn/plan.PDF', 'https://a.gov.cn/roles.xlsx?download=1',
            'https://a.gov.cn/opaque?id=3'])

    def test_recruitment_column_is_followed_not_treated_as_announcement(self):
        root, column, notice = ('https://a.gov.cn/', 'https://a.gov.cn/recruit/',
                                'https://a.gov.cn/recruit/notice.html')
        pages = {
            root: '<title>某区政府</title><nav><a href="/recruit/">招聘信息</a></nav>',
            column: '<title>招聘信息_某区政府</title><a href="notice.html">某区2027教师招聘公告</a>',
            notice: '<title>某区2027教师招聘公告</title><article>' + '2027地理教师招聘专业详见附件。' * 8 + '</article>'
        }
        def fetch(url):
            return pages[url], self.page(pages[url])
        with patch.object(c, 'fetch_page', side_effect=fetch) as mocked:
            rows, extra = c.crawl_official({'url': root, 'followDepth': 1})
        self.assertEqual([r['url'] for r in rows], [notice])
        self.assertEqual(extra['listingPages'], 2)
        self.assertEqual(extra['detailPages'], 1)
        self.assertEqual([call.args[0] for call in mocked.call_args_list].count(column), 1)

    def test_generic_column_headline_cannot_promote_whole_listing(self):
        source = {'url': 'https://a.gov.cn/recruit/', 'name': '栏目', 'scope': 'teacher'}
        p = self.page('<title>招聘信息_某区政府</title><article>' + '栏目介绍欢迎访问。' * 20 + '</article>')
        with patch.object(c, 'fetch_page', return_value=('', p)):
            rows, extra = c.crawl_official(source)
            _, status = c.run_collection({'officialSources': [source]}, {'leads': []}, {'jobs': []})
        self.assertEqual(rows, [])
        self.assertEqual(extra['detailPages'], 0)
        self.assertEqual(status['reports'][0]['status'], 'manual-review')
        self.assertEqual(status['successfulSources'], 0)

    def test_actual_notice_with_column_suffix_is_not_mistaken_for_menu(self):
        self.assertFalse(c.is_column_title('某中学2027年地理教师招聘公告_招聘信息_学校官网'))
        self.assertTrue(c.is_column_title('招聘信息_学校官网'))
        self.assertTrue(c.is_column_title('学校官网_招聘信息'))

    def test_empty_and_redirect_official_roots_report_failure_not_no_jobs(self):
        for markup in ('<title>招聘</title><div id="app"></div>', '<title>跳转中</title>'):
            with self.subTest(markup=markup):
                p = self.page(markup)
                with patch.object(c, 'fetch_page', return_value=('', p)):
                    rows, extra = c.crawl_official({'url': 'https://a.gov.cn/'})
                    with self.assertRaises(RuntimeError):
                        c.run_collection({'officialSources': [{'name': '空栏目', 'url': 'https://a.gov.cn/'}]},
                                         {'leads': []}, {'jobs': []})
                self.assertEqual(rows, [])
                self.assertEqual(extra['listingPages'], 0)
                self.assertEqual(extra['listingPagesFailed'], 1)

    def test_historical_announcement_is_only_a_pending_discovery(self):
        old_date = (c.TODAY - timedelta(days=200)).isoformat()
        p = self.page('<title>某校2027地理教师招聘公告</title>'
                      f'<meta name="PubDate" content="{old_date}"><article>'
                      + '2027地理教师招聘，报名期限需另查。' * 8 + '</article>')
        source = {'url': 'https://a.edu.cn/notice.html', 'name': '旧公告', 'scope': 'teacher', 'lookbackDays': 60}
        with patch.object(c, 'fetch_page', return_value=('', p)):
            rows, extra = c.crawl_official(source)
        self.assertEqual(len(rows), 1)
        self.assertEqual(extra['historicalPending'], 1)
        lead = c.make_lead(rows[0], source, None)
        self.assertTrue(lead['historical'])
        self.assertEqual(lead['verificationStatus'], '待核验线索')
        self.assertIsNone(lead['deadline'])
        self.assertTrue(any('仅作历史单位发现线索' in text for text in lead['important']))

    def test_collection_never_infers_deadline_from_an_exam_date(self):
        row = {'title': '某校2027地理教师招聘', 'url': 'https://a.gov.cn/notice.html',
               'description': '笔试2026年11月21日，报名期限未写。'}
        lead = c.make_lead(row, {'name': '学校官网', 'scope': 'teacher'}, None)
        self.assertIsNone(lead['deadline'])
        self.assertEqual(lead['applyStart'], '未说明')

    def test_main_only_writes_leads_and_status_never_jobs(self):
        root = Path('/collector-fixture')
        files = {'collector-sources.json': {}, 'leads.json': {'leads': []}, 'jobs.json': {'jobs': []}}
        def read(path, **kwargs):
            return json.dumps(files[path.name])
        output = ({'leadCount': 0, 'leads': []}, {'successfulSources': 1, 'totalSources': 1})
        with patch.object(c.Path, 'read_text', autospec=True, side_effect=read), \
             patch.object(c.Path, 'exists', return_value=True), \
             patch.object(c.Path, 'write_text', autospec=True) as written, \
             patch.object(c.Path, 'replace', autospec=True) as replaced, \
             patch.object(c, 'run_collection', return_value=output), \
             patch('sys.argv', ['collector', '--root', str(root)]):
            c.main()
        self.assertEqual({call.args[0].name for call in written.call_args_list},
                         {'leads.json.tmp', 'collector-status.json.tmp'})
        self.assertEqual({call.args[1].name for call in replaced.call_args_list},
                         {'leads.json', 'collector-status.json'})

    def test_dry_run_performs_no_data_file_writes(self):
        output = ({'leadCount': 0, 'leads': []}, {'successfulSources': 1, 'totalSources': 1})
        with patch.object(c.Path, 'read_text', return_value='{}'), \
             patch.object(c.Path, 'exists', return_value=True), \
             patch.object(c.Path, 'write_text', side_effect=AssertionError('unexpected write')), \
             patch.object(c.Path, 'replace', side_effect=AssertionError('unexpected replace')), \
             patch.object(c, 'run_collection', return_value=output), \
             patch('sys.argv', ['collector', '--dry-run']):
            c.main()

    def test_metadata_date_not_exam_date(self):
        p = self.page('<meta name="PubDate" content="2026-11-03 17:00"><article>笔试11月21日</article>')
        self.assertEqual(p.published_at(), '2026-11-03')

    def test_body_exam_or_registration_date_is_not_publication_date(self):
        p = self.page('<article>笔试时间：2026年11月21日，报名时间：2026-11-05。</article>')
        self.assertEqual(p.published_at(), '未说明')
        p = self.page('<article>发布日期：2026年11月3日，笔试时间：2026-11-21。</article>')
        self.assertEqual(p.published_at(), '2026-11-03')

    def test_invalid_publication_date_remains_unknown(self):
        p = self.page('<meta name="PubDate" content="2026-15-99"><article>发布时间：2026年2月30日</article>')
        self.assertEqual(p.published_at(), '未说明')

    def test_keywords_in_late_article_not_lost_to_first_4000_script_chars(self):
        p = self.page('<script>' + 'x' * 6000 + '</script><article>' + '正文' * 2500 + '人文地理2027校园招聘</article>')
        self.assertIn('人文地理', p.article_text())
        self.assertNotIn('xxxx', p.article_text())

    def test_source_levels_are_distinct(self):
        self.assertNotEqual(c.source_level('https://a.gov.cn/1'), c.source_level('https://a.edu.cn/1'))

    def test_failed_and_partial_sources_are_not_all_success(self):
        src = {'name': '学校官网', 'url': 'https://a.gov.cn', 'city': '天津', 'scope': 'teacher'}
        old = {'id': 'old', 'url': 'https://old.edu.cn/1', 'title': '企业2027校招', 'type': '教师编',
               'addedAt': c.TODAY.isoformat(), 'lastSeenAt': c.TODAY.isoformat()}
        with patch.object(c, 'crawl_official', return_value=([], {'listingPagesFailed': 1, 'detailPagesFailed': 0})):
            payload, status = c.run_collection({'officialSources': [src], 'teacherCoverageCities': ['天津']}, {'leads': [old]}, {'jobs': []})
        self.assertEqual(status['successfulSources'], 0)
        self.assertEqual(status['connectedSources'], 1)
        self.assertEqual(status['reports'][0]['status'], 'partial')
        self.assertEqual(status['teacherCoverage'][0]['state'], '需人工补查')
        self.assertEqual(payload['leads'][0]['type'], '未说明')

    def test_all_failures_preserve_pool_by_raising_before_write(self):
        with patch.object(c, 'crawl_official', side_effect=TimeoutError('timeout')):
            with self.assertRaises(RuntimeError):
                c.run_collection({'officialSources': [{'name': 'x', 'url': 'https://a.gov.cn'}]}, {'leads': []}, {'jobs': []})

    def test_verified_url_dedup_but_pending_jobs_not_treated_as_verified(self):
        row = {'title': '教师招聘公告', 'description': '专业详见岗位表', 'url': 'https://a.gov.cn/1'}
        src = {'name': '测试', 'url': 'https://a.gov.cn', 'scope': 'teacher'}
        with patch.object(c, 'crawl_official', return_value=([row], {})):
            payload, _ = c.run_collection({'officialSources': [src]}, {'leads': []},
                                         {'jobs': [{'url': row['url'], 'verificationStatus': '待核验线索'}]})
        self.assertEqual(payload['leadCount'], 1)
        with patch.object(c, 'crawl_official', return_value=([row], {})):
            payload, _ = c.run_collection({'officialSources': [src]}, {'leads': []}, {'jobs': [{'url': row['url']}]})
        self.assertEqual(payload['leadCount'], 0)

    def assert_sector_pending(self, title, description, keywords):
        source = {'name': '补充方向', 'scope': 'sector', 'keywords': keywords}
        row = {'title': title, 'description': description, 'url': 'https://example.com/recruit'}
        self.assertTrue(c.should_accept(row, source))
        lead = c.make_lead(row, source, None)
        self.assertEqual(lead['verificationStatus'], '待核验线索')
        self.assertEqual(lead['type'], '未说明')
        self.assertEqual(lead['major'], '相关方向招聘线索；是否接受人文地理、所需硬技能及资格须核验')
        self.assertEqual(lead['graduateYear'], '未说明')
        self.assertIsNone(lead['deadline'])
        self.assertFalse(c.should_accept({**row, 'title': title.replace('2027', '2026')}, source))

    def test_sector_publishing_editors_retained_without_assuming_ownership(self):
        self.assert_sector_pending('某出版社2027编辑招聘', '教育内容研发编辑，专业及劳动主体详见原文。', ['出版', '地图'])

    def test_sector_marine_industry_research_retained_without_old_keywords(self):
        self.assert_sector_pending('某海洋机构2027招聘', '海洋渔业产业链研究、渔业经济分析，专业资格待核。', ['海洋', '渔业'])

    def test_sector_university_research_assistant_retained_pending(self):
        self.assert_sector_pending('某高校2027科研助理招聘', '项目管理与科研管理助理，合同性质与专业待核。', ['科研助理', '科研管理'])
        source = {'scope': 'sector'}
        self.assertFalse(c.should_accept({'title': '2027海洋论坛', 'description': '应届毕业生论文展示'}, source))

    def curated_fixture(self):
        return {'id': 'manual-geo', 'url': 'https://a.edu.cn/geo', 'title': '高中地理教师（先收简历）',
                'curated': True, 'automated': False, 'verificationStatus': '待核验线索',
                'type': '未说明', 'nature': '公办学校意向收简历，正式资格待核',
                'sourceLevel': '用人单位原始简章', 'deadline': '2026-11-06',
                'important': ['邮箱geo@example.edu.cn；院校范围及正式条件待核'],
                'summary': '人工逐项整理，不能当作正式报名完成', 'addedAt': c.TODAY.isoformat(),
                'lastSeenAt': c.TODAY.isoformat(), 'matchedQueries': ['人工核验']}

    def test_curated_content_preserved_when_refetched_and_when_retained(self):
        old = self.curated_fixture()
        source = {'name': '学校官网', 'url': 'https://a.edu.cn/', 'scope': 'teacher'}
        row = {'title': '自动抓到的教师招聘', 'url': old['url'], 'description': '附件详见原文'}
        for rows in ([row], []):
            with self.subTest(refetched=bool(rows)), patch.object(c, 'crawl_official', return_value=(rows, {})):
                payload, _ = c.run_collection({'officialSources': [source]}, {'leads': [old]}, {'jobs': []})
            kept = payload['leads'][0]
            for key in ('id', 'title', 'type', 'nature', 'sourceLevel', 'deadline', 'important', 'summary', 'addedAt'):
                self.assertEqual(kept[key], old[key])
            self.assertFalse(kept['automated'])
            self.assertEqual(kept['verificationStatus'], '待核验线索')
            if rows:
                self.assertIn('学校官网', kept['matchedQueries'])

    def test_verified_url_excludes_curated_lead_too(self):
        old = self.curated_fixture()
        source = {'name': '学校官网', 'url': 'https://a.edu.cn/', 'scope': 'teacher'}
        row = {'title': '教师招聘', 'url': old['url'], 'description': '专业详见附件'}
        with patch.object(c, 'crawl_official', return_value=([row], {})):
            payload, _ = c.run_collection({'officialSources': [source]}, {'leads': [old]},
                                         {'jobs': [{'url': old['url']}]})
        self.assertEqual(payload['leadCount'], 0)

    def test_uncurated_legacy_nav_classification_still_repaired(self):
        old = {**self.curated_fixture(), 'curated': False, 'automated': True,
               'title': '普通企业2027招聘', 'type': '教师编', 'nature': '错误性质'}
        source = {'name': '官网', 'url': 'https://a.gov.cn/', 'scope': 'teacher'}
        with patch.object(c, 'crawl_official', return_value=([], {})):
            payload, _ = c.run_collection({'officialSources': [source]}, {'leads': [old]}, {'jobs': []})
        kept = payload['leads'][0]
        self.assertEqual(kept['type'], '未说明')
        self.assertEqual(kept['nature'], '自动发现公告/线索（单位性质、编制及岗位条件待核验）')

    def test_calendar_and_national_events_cannot_fake_27_city_coverage(self):
        cities = ['天津', '苏州', '无锡', '常州', '杭州', '湖州', '嘉兴', '太原', '晋城', '长治',
                  '南京', '南通', '镇江', '扬州', '绍兴', '宁波', '金华', '上海', '北京',
                  '济南', '青岛', '合肥', '郑州', '西安', '武汉', '长沙', '成都']
        rows = [
            {'title': '2027年日历百科', 'description': '2027公历农历日期对照', 'url': 'https://example.com/calendar'},
            {'title': '国家就业平台2027届高校毕业生招聘活动', 'description': '全国招聘活动，为应届毕业生提供就业服务。', 'url': 'https://example.com/national'}
        ]
        sources = [{'name': city + '教师宽搜', 'query': city + ' 2027 教师招聘',
                    'city': city, 'scope': 'teacher'} for city in cities]
        self.assertTrue(all(not c.should_accept(row, s) for s in sources for row in rows))
        with patch.object(c, 'fetch_rss', return_value=rows):
            payload, status = c.run_collection({'queries': sources}, {'leads': []}, {'jobs': []})
        self.assertEqual(payload['leadCount'], 0)
        self.assertEqual(status['successfulSources'], 0)
        self.assertTrue(all(r['status'] == 'manual-review' and r['relevantFound'] == 0 for r in status['reports']))

    def test_teacher_query_requires_target_city_and_teacher_topic_not_geo_literal(self):
        source = {'scope': 'teacher', 'query': '天津 2027教师招聘', 'city': '天津'}
        row = {'title': '天津某公办学校2027年教师招聘', 'description': '学科专业及报名期限详见附件。'}
        self.assertTrue(c.should_accept(row, source))
        self.assertFalse(c.should_accept({**row, 'title': '苏州某公办学校2027年教师招聘'}, source))
        self.assertFalse(c.should_accept({'title': '天津2027企业招聘', 'description': '面向毕业生的市场岗位'}, source))
        self.assertTrue(c.should_accept({'title': '教师招聘公告', 'description': '专业详见附件'},
                                       {'url': 'https://a.gov.cn', 'scope': 'teacher', 'city': '天津'}))

    def test_sector_query_requires_configured_topic(self):
        source = {'scope': 'sector', 'query': '2027 出版招聘', 'keywords': ['出版', '地图编辑']}
        self.assertTrue(c.should_accept({'title': '某出版社2027编辑招聘', 'description': '条件见简章'}, source))
        self.assertFalse(c.should_accept({'title': '2027届全国高校毕业生招聘活动', 'description': '国家就业服务活动'}, source))
        self.assertFalse(c.should_accept({'title': '某出版社2027编辑招聘', 'description': '条件见简章'}, {**source, 'keywords': []}))

    def test_verified_dedup_does_not_hide_relevant_search_health(self):
        row = {'title': '天津某中学2027年教师招聘', 'description': '专业详见附件', 'url': 'https://a.gov.cn/notice'}
        source = {'name': '天津教师', 'scope': 'teacher', 'city': '天津', 'query': '天津 2027教师招聘'}
        with patch.object(c, 'fetch_rss', return_value=[row]):
            payload, status = c.run_collection({'queries': [source]}, {'leads': []}, {'jobs': [{'url': row['url']}]})
        self.assertEqual(payload['leadCount'], 0)
        self.assertEqual(status['reports'][0]['accepted'], 0)
        self.assertEqual(status['reports'][0]['relevantFound'], 1)
        self.assertEqual(status['reports'][0]['status'], 'ok')
        self.assertEqual(status['successfulSources'], 1)


if __name__ == '__main__':
    unittest.main()

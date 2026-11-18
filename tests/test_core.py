"""核心层单元测试：薪资解析、入库去重与合规白名单、每日额度、HTML 输出加固。

运行：python -m unittest discover -s tests -v
"""
import io
import json
import os
import re
import socket
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager, redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from playwright.sync_api import Error as PlaywrightError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobstats import (analyze, browser, config, crossref, db, details, html_text,
                      relevance, report, server, skills)
from jobstats import collectors
from jobstats.collectors import boss

import cli  # noqa: E402  status 自检那几个函数住在这里

SLOT_NAMES = ("__SUB_LINE__", "__GENERATED_AT__", "__N__", "__DATA__", "__TOKEN__",
              "__CITY_OPTIONS__", "__COLLECT_PANEL__", "__COLLECT_SCRIPT__", "__BOOT_HOOK__",
              "__DETAIL_SCRIPT__", "__DETAIL_TH__", "__IS_WEB__",
              "__STATUS_SCRIPT__", "__STATUS_CONSTS__", "__STALE_DAYS__", "__NO_DETAIL__",
              "__NO_LIST_SKILLS__",
              "__DELETE_TH__", "__DELETE_BAR__", "__DELETE_SCRIPT__",
              "__TRIAGE_BUTTON__", "__TRIAGE_PANEL__", "__TRIAGE_SCRIPT__")


def make_record(**over):
    rec = {
        "platform": "job51", "platform_job_id": "j1", "title": "Python开发工程师",
        "company": "某某科技", "salary_raw": "15-25K·14薪", "salary_min": 15.0,
        "salary_max": 25.0, "salary_months": 14, "city": "深圳", "district": "南山",
        "experience": "3-5年", "education": "本科", "industry": "互联网",
        "company_scale": "100-499人", "skills": ["python", "django"],
        "welfare": ["五险一金"], "url": "https://example.com/job/j1",
    }
    rec.update(over)
    return rec


class TempDbCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = db.DB_PATH
        db.DB_PATH = Path(self._tmp.name) / "jobs.db"
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self._orig
        self._tmp.cleanup()


class TestParseSalary(unittest.TestCase):
    def test_ranges(self):
        cases = {
            "15-25K·14薪": (15.0, 25.0, 14),
            "1.2-2.4万": (12.0, 24.0, 12),
            "8千-1.2万": (8.0, 12.0, 12),
            "15-25千": (15.0, 25.0, 12),
            "20-30K": (20.0, 30.0, 12),
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(db.parse_salary(raw), expected)

    def test_non_monthly_or_empty_returns_none(self):
        for raw in ("200-300元/天", "50-80元/小时", "20-30万/年", "面议", "", None):
            with self.subTest(raw=raw):
                self.assertIsNone(db.parse_salary(raw))

    def test_single_value_keeps_floor_and_ceiling_equal(self):
        """平台只给一个数时不猜上限：上下限都按下限记，均值统计才不会被凭空抬高。"""
        for raw, expected in (("15K", (15.0, 15.0, 12)), ("15K以上", (15.0, 15.0, 12)),
                              ("1.5万", (15.0, 15.0, 12)), ("8千", (8.0, 8.0, 12)),
                              ("20K·13薪", (20.0, 20.0, 13))):
            with self.subTest(raw=raw):
                self.assertEqual(db.parse_salary(raw), expected)

    def test_unitless_numbers_are_read_by_magnitude(self):
        """'3000-5000' 是元不是 K——旧实现按 K 处理会得出 300 万/月。"""
        for raw, expected in (("3000-5000", (3.0, 5.0, 12)),
                              ("8-12", (8.0, 12.0, 12)),
                              ("12000-15000", (12.0, 15.0, 12))):
            with self.subTest(raw=raw):
                self.assertEqual(db.parse_salary(raw), expected)


class TestSkills(unittest.TestCase):
    """三家平台往技能列里塞的东西口径不同，读取前必须归一。"""

    def test_casing_and_aliases_unified(self):
        for raw, want in (("python", "Python"), ("PYTHON", "Python"), ("mysql", "MySQL"),
                          ("linux", "Linux"), ("英文", "英语"), ("C/C++", "C/C++"),
                          ("高并发", "高并发")):
            with self.subTest(raw=raw):
                self.assertEqual(skills.canonical(raw), want)

    def test_dropped_tags_duplicate_other_columns(self):
        for raw in ("大专", "本科", "3-5年", "1到3年网络工程师经验", "经验不限",
                    "在校/应届", "五险一金", "包住", ""):
            with self.subTest(raw=raw):
                self.assertEqual(skills.canonical(raw), "")

    def test_clean_dedupes_and_keeps_order(self):
        self.assertEqual(skills.clean(["Java", "python", "Java", None, "  "]),
                         ["Java", "Python"])

    def test_clean_is_idempotent(self):
        once = skills.clean(["python", "大专", "mysql"])
        self.assertEqual(skills.clean(once), once)

    def test_welfare_synonyms_merged(self):
        self.assertEqual(skills.welfare(["年终奖", "定期体检", "免费体检", "五险一金"]),
                         ["年终奖金", "定期体检", "五险一金"])

    def test_welfare_detector_for_tags_smuggled_into_skills(self):
        self.assertTrue(skills.is_welfare("包吃包住"))
        self.assertFalse(skills.is_welfare("Python"))


# 2026-10-14 从 data/jobs.db 里数出来的 47 种原文（同一个要求三家各写各的）。
# 归一测试用真实写法当输入，不自己编——编出来的样本永远命中不了 `经验经验不限` 这种。
REAL_EXP_WORDS = (
    "经验经验不限", "3年及以上", "经验3年及以上", "3-5年", "1年及以上", "1-3年",
    "经验5年及以上", "经验3-10年", "5-10年", "经验2年及以上", "无需经验", "经验3年",
    "2年及以上", "5年及以上", "经验5-10年", "经验2-10年", "经验3-5年", "经验1年及以上",
    "经验1年", "经验不限", "经验1-3年", "在校/应届", "经验2年", "应届毕业生及以上",
    "经验8年及以上", "经验4年及以上", "应届毕业生-2年", "经验2-3年", "1年", "1年以内",
    "1-5年", "10年以上", "经验4-10年", "应届毕业生-3年", "经验1-10年", "在读学生-应届毕业生",
    "1-2年", "3年", "2年", "经验2-5年", "经验5年", "经验5-8年", "经验6年", "经验2-4年",
    "应届毕业生-1年", "应届毕业生", "经验3-8年")


class TestLabelBuckets(unittest.TestCase):
    """经验/学历归一：47 种经验写法必须收进 7 档，归档只看下限。"""

    def test_real_wordings_all_land_in_a_bucket(self):
        self.assertEqual(len(REAL_EXP_WORDS), 47)
        got = {skills.exp_bucket(w) for w in REAL_EXP_WORDS}
        self.assertLessEqual(len(got), 7, got)
        self.assertFalse(got - set(skills.EXP_BUCKETS), got - set(skills.EXP_BUCKETS))
        self.assertNotIn("其他", got)     # 这 47 种里没有落不进档的
        self.assertNotIn("未标注", got)

    def test_bucketed_by_the_floor_not_the_ceiling(self):
        for raw, want in (("3-5年", "3-5年"), ("3年及以上", "3-5年"), ("经验3-10年", "3-5年"),
                          ("经验2-10年", "1-3年"), ("经验5-8年", "5-10年"),
                          ("1年及以上", "1-3年"), ("经验1年", "1-3年")):
            with self.subTest(raw=raw):
                self.assertEqual(skills.exp_bucket(raw), want)

    def test_upper_bound_wording_is_not_read_as_a_floor(self):
        self.assertEqual(skills.exp_bucket("1年以内"), "1年以内")
        self.assertEqual(skills.exp_bucket("经验1年以内"), "1年以内")
        self.assertEqual(skills.exp_bucket("3年以下"), "1-3年")
        # "以上"不是上限说法，别被上面那条 `以下` 规则误伤（`10年以上` 是 10 年以上）
        self.assertEqual(skills.exp_bucket("10年以上"), "10年以上")

    def test_no_limit_and_student_wordings_merge(self):
        for raw in ("经验不限", "经验经验不限", "无需经验"):
            self.assertEqual(skills.exp_bucket(raw), "经验不限", raw)
        for raw in ("在校/应届", "应届毕业生-2年", "在读学生-应届毕业生", "应届毕业生及以上"):
            self.assertEqual(skills.exp_bucket(raw), "应届/在校", raw)

    def test_blank_and_unparseable_experience(self):
        self.assertEqual(skills.exp_bucket(""), "未标注")
        self.assertEqual(skills.exp_bucket(None), "未标注")
        self.assertEqual(skills.exp_bucket("面议"), "其他")

    def test_education_variants_merged(self):
        for raw, want in (("本科", "本科"), ("学士", "本科"), ("大专", "大专"), ("专科", "大专"),
                          ("中专", "高中/中专"), ("中专/中技", "高中/中专"),
                          ("中技/中专", "高中/中专"), ("高中", "高中/中专"),
                          ("职高", "高中/中专"), ("学历不限", "学历不限"), ("不限", "学历不限"),
                          ("硕士", "硕士及以上"), ("研究生", "硕士及以上"), ("博士", "硕士及以上"),
                          ("初中及以下", "初中及以下"), ("", "未标注"), ("面议", "其他")):
            with self.subTest(raw=raw):
                self.assertEqual(skills.edu_bucket(raw), want)

    def test_bucket_lists_are_the_short_ones_we_promised(self):
        self.assertEqual([b for b in skills.EXP_BUCKETS if b not in ("未标注", "其他")],
                         ["经验不限", "应届/在校", "1年以内", "1-3年", "3-5年", "5-10年", "10年以上"])
        self.assertEqual([b for b in skills.EDU_BUCKETS if b not in ("未标注", "其他")],
                         ["学历不限", "初中及以下", "高中/中专", "大专", "本科", "硕士及以上"])

    def test_bucketing_is_idempotent_over_raw_fields(self):
        rec = {"experience": "经验3年及以上", "education": "中专/中技"}
        skills.add_buckets(skills.add_buckets(rec))
        self.assertEqual(rec["experience"], "经验3年及以上")   # 原文一个字没改
        self.assertEqual(rec["experience_bucket"], "3-5年")
        self.assertEqual(rec["education_bucket"], "高中/中专")


class TestBucketPlumbing(TempDbCase):
    def test_fetch_all_carries_buckets_next_to_the_raw_text(self):
        db.save_jobs([make_record(experience="经验3年及以上", education="中专/中技")])
        row = db.fetch_all()[0]
        self.assertEqual(row["experience"], "经验3年及以上")
        self.assertEqual(row["experience_bucket"], "3-5年")
        self.assertEqual(row["education_bucket"], "高中/中专")

    def test_csv_export_still_writes_platform_wording(self):
        db.save_jobs([make_record(experience="经验3年及以上")])
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "e.csv"
            db.export_csv(out)
            text = out.read_text(encoding="utf-8-sig")
        self.assertIn("经验3年及以上", text)
        self.assertEqual(text.splitlines()[0].split(","), db.EXPORT_FIELDS)

    def test_stats_charts_count_buckets_not_raw_wordings(self):
        recs = [make_record(experience=w, education=e)
                for w, e in (("3年及以上", "本科"), ("经验3-10年", "中专/中技"), ("3-5年", "本科"))]
        stats = report.build_stats(recs)
        self.assertEqual(dict(stats["experiences"])["3-5年"], 3)
        self.assertEqual(dict(stats["educations"])["本科"], 2)
        self.assertNotIn("经验3-10年", dict(stats["experiences"]))

    def test_page_chips_are_driven_by_buckets_with_raw_in_tooltip(self):
        rec = skills.add_buckets(make_record(experience="经验3年及以上"))
        html = report.render_web([rec], "T", "")
        for needle in ("const EXP_ORDER = ", "const EDU_ORDER = ",
                       "chip('experience_bucket', 'experience', 'exp'",
                       "chip('education_bucket', 'education', 'edu'",
                       "bucketTd(r.experience_bucket, r.experience)",
                       "经验3年及以上", "3-5年"):
            self.assertIn(needle, html)

    def test_row_cells_line_up_with_the_header(self):
        """bucketTd 自己就带 <td>，再套一层就整行串列（这次真踩到了）。列数对不上就报错。"""
        row = re.search(r"return `<tr>(.*?)</tr>`;", report.FILTER_TEMPLATE, re.S).group(1)
        whole_cells = ("fetchedTd}", "seenTd}", "pubTd}", "stCell}", "scoreCell}",
                       "det}", "delCell}", "skillTd}")
        cells = (row.count("<td") + row.count("bucketTd(")
                 + sum(row.count(x) for x in whole_cells))
        self.assertEqual(15, cells, "网页版 15 列：静态页去掉『正文』『删』两列 = 13")
        self.assertNotIn("<td>${bucketTd", report.FILTER_TEMPLATE)
        self.assertNotIn("<td>${seenTd", report.FILTER_TEMPLATE)
        self.assertNotIn("<td>${skillTd", report.FILTER_TEMPLATE)


class TestSaveJobs(TempDbCase):
    def test_dedup_by_platform_and_job_id(self):
        self.assertEqual(db.save_jobs([make_record()]), 1)
        self.assertEqual(db.save_jobs([make_record()]), 0)
        self.assertEqual(db.save_jobs([make_record(title="标题变了")]), 0)
        self.assertEqual(len(db.fetch_all()), 1)

    def test_same_job_id_on_other_platform_is_kept(self):
        db.save_jobs([make_record()])
        self.assertEqual(db.save_jobs([make_record(platform="boss")]), 1)

    def test_batch_counts_only_new(self):
        recs = [make_record(), make_record(platform_job_id="j2"), make_record()]
        self.assertEqual(db.save_jobs(recs), 2)
        self.assertEqual(db.save_jobs(recs), 0)

    def test_non_whitelisted_field_rejected(self):
        with self.assertRaises(ValueError):
            db.save_jobs([make_record(candidate_phone="13800000000")])
        self.assertEqual(db.fetch_all(), [])

    def test_raw_tags_kept_in_db_and_cleaned_on_read(self):
        db.save_jobs([make_record(skills=["python", "django", "大专", "五险一金", "3-5年"])])
        row = db.fetch_all()[0]
        self.assertEqual(row["skills"], ["Python", "Django"])
        self.assertIn("五险一金", row["welfare"])
        with db._conn() as c:  # 库里存的仍是平台原文
            self.assertIn("大专", c.execute("SELECT skills FROM jobs").fetchone()[0])

    def test_input_records_not_mutated(self):
        rec = make_record()
        snapshot = dict(rec)
        db.save_jobs([rec])
        self.assertEqual(rec, snapshot)

    def test_count_today(self):
        self.assertEqual(db.count_today(), 0)
        db.save_jobs([make_record()])
        self.assertEqual(db.count_today(), 1)

    def test_count_today_without_table_is_zero(self):
        db.DB_PATH.unlink()
        self.assertEqual(db.count_today(), 0)

    def test_csv_export(self):
        db.save_jobs([make_record()])
        out = Path(db.DB_PATH.parent) / "export.csv"
        db.export_csv(out)
        text = out.read_text(encoding="utf-8-sig")
        self.assertIn("Python开发工程师", text)
        self.assertEqual(len(text.strip().splitlines()), 2)

    def test_csv_export_has_numeric_salary_and_tag_columns(self):
        db.save_jobs([make_record()])
        out = Path(db.DB_PATH.parent) / "export.csv"
        db.export_csv(out)
        head, line = out.read_text(encoding="utf-8-sig").strip().splitlines()
        cols = dict(zip(head.split(","), line.split(",")))
        self.assertEqual(cols["salary_min"], "15.0")
        self.assertEqual(cols["salary_months"], "14")
        # 15-25K 中位数 20K × 14 薪 = 28 万/年
        self.assertEqual(cols["salary_annual_wan"], "28.0")
        self.assertEqual(cols["skills"], "Python / Django")
        self.assertEqual(cols["welfare"], "五险一金")

    def test_annual_wan_defaults_to_12_salaries(self):
        self.assertEqual(db.annual_wan({"salary_min": 10.0, "salary_max": 20.0}), 18.0)
        self.assertIsNone(db.annual_wan({"salary_min": None, "salary_max": 20.0}))
        self.assertIsNone(db.annual_wan({"salary_min": 10.0, "salary_max": None}))


class TestRecollectRefresh(TempDbCase):
    """同一个职位第二次采到时的行为：刷新平台字段，但别动我自己的标注和额度。"""

    def test_refresh_updates_salary_but_keeps_my_marks(self):
        db.save_jobs([make_record()])
        row = db.fetch_all()[0]
        db.set_status(row["id"], "已投", "已聊过 HR")
        st = db.save_stats([make_record(salary_raw="18-30K", salary_min=18.0,
                                        salary_max=30.0)])
        self.assertEqual((st["added"], st["refreshed"]), (0, 1))
        r = db.fetch_all()[0]
        self.assertEqual((r["salary_min"], r["salary_max"]), (18.0, 30.0))
        self.assertEqual((r["status"], r["note"]), ("已投", "已聊过 HR"))

    def test_refresh_does_not_burn_the_daily_quota(self):
        """额度按 fetched_at 计：fetched_at 是"第一次采到"，刷新时不该往后推。"""
        db.save_jobs([make_record()])
        before = db.fetch_all()[0]
        db.save_jobs([make_record(salary_raw="30-40K")])
        after = db.fetch_all()[0]
        self.assertEqual(after["fetched_at"], before["fetched_at"])
        self.assertEqual(db.count_today(), 1)

    def test_empty_values_do_not_erase_what_we_already_had(self):
        """列表页经常不给行业/规模/技能，照直刷会把详情里拿到的抹平。"""
        db.save_jobs([make_record()])
        db.save_jobs([make_record(industry="", company_scale="", skills=[],
                                  welfare=[], salary_raw="", district="")])
        r = db.fetch_all()[0]
        self.assertEqual(r["industry"], "互联网")
        self.assertEqual(r["company_scale"], "100-499人")
        self.assertEqual(r["salary_raw"], "15-25K·14薪")
        self.assertEqual(r["district"], "南山")
        self.assertEqual(r["skills"], ["Python", "Django"])

    def test_new_job_gets_both_timestamps(self):
        db.save_jobs([make_record()])
        r = db.fetch_all()[0]
        self.assertEqual(r["last_seen_at"], r["fetched_at"])

    def test_old_database_gets_the_column_backfilled(self):
        """老库（没有 last_seen_at 那列）打开后要补列并回填，不能一片空白。"""
        import sqlite3
        db.save_jobs([make_record()])
        old = db.DB_PATH
        conn = sqlite3.connect(old)
        try:
            conn.execute("ALTER TABLE jobs RENAME TO jobs_old")
            # 2026-10 之前的表结构：没有 last_seen_at，也没有屏蔽名单
            conn.execute("CREATE TABLE jobs AS SELECT id, platform, platform_job_id, title,"
                         " company, salary_raw, salary_min, salary_max, salary_months, city,"
                         " district, experience, education, industry, company_scale, skills,"
                         " welfare, url, fetched_at, status, note FROM jobs_old")
            conn.execute("DROP TABLE jobs_old")
            conn.execute("DROP TABLE blocked_jobs")
            conn.commit()
        finally:
            conn.close()
        db.init_db()
        rows = db.fetch_all()
        self.assertEqual(len(rows), 1)
        self.assertIsNotNone(rows[0]["last_seen_at"])
        self.assertEqual(db.save_stats([make_record()])["refreshed"], 1)


class TestDailyCap(TempDbCase):
    def test_returns_remaining_then_blocks(self):
        original = browser.DAILY_CAP
        browser.DAILY_CAP = 3
        try:
            self.assertEqual(browser.check_daily_cap(), 3)
            db.save_jobs([make_record(), make_record(platform_job_id="j2")])
            self.assertEqual(browser.check_daily_cap(), 1)
            db.save_jobs([make_record(platform_job_id=f"j{i}") for i in range(3, 6)])
            with self.assertRaises(RuntimeError):
                browser.check_daily_cap()
        finally:
            browser.DAILY_CAP = original


class TestEmbedJson(unittest.TestCase):
    def test_script_tag_cannot_break_out(self):
        payload = report._embed_json([make_record(title="</script><script>alert(1)</script>")])
        self.assertNotIn("</script>", payload)
        self.assertNotIn("<script>", payload)

    def test_ampersand_angle_escaped(self):
        self.assertEqual(report._embed_json({"x": "a&b<c>d"}),
                         '{"x": "a\\u0026b\\u003cc\\u003ed"}')

    def test_json_still_parses_back(self):
        recs = [make_record(skills=["<b>", "a&b"])]
        self.assertEqual(json.loads(report._embed_json(recs)), recs)

    def test_js_line_separators_escaped(self):
        # U+2028/2029 在 JS 字符串字面量里是非法换行，必须转义掉
        value = " a "
        payload = report._embed_json({"x": value})
        for sep in (" ", " "):
            self.assertNotIn(sep, payload)
        self.assertEqual(json.loads(payload)["x"], value)


class TestIsHttp(unittest.TestCase):
    def test_accepted(self):
        for u in ("https://x.com/a", "http://x.com", "HTTPS://X.COM"):
            self.assertTrue(report._is_http(u), u)

    def test_rejected(self):
        for u in ("javascript:alert(1)", "data:text/html,x", "", None, "//x.com"):
            self.assertFalse(report._is_http(u), u)


class TestRenderers(unittest.TestCase):
    def test_skill_chart_counts_tags_not_welfare(self):
        """以前第一名是五险一金：混进技能列的福利词不该参与技能排行。"""
        recs = [make_record(skills=["五险一金", "python"]),
                make_record(platform_job_id="j2", skills=["包吃包住", "Java"])]
        stats = report.build_stats(recs)
        names = [k for k, _ in stats["top_skills"]]
        self.assertEqual(sorted(names), ["Java", "Python"])
        self.assertIn("五险一金", dict(stats["top_welfare"]))

    def test_stats_report_escapes_cells_and_links(self):
        recs = [make_record(title='<img src=x onerror="alert(1)">', url="javascript:alert(1)")]
        html = report.render(report.build_stats(recs))
        self.assertIn("&lt;img", html)
        self.assertNotIn("<img src=x", html)
        self.assertNotIn('<a href="javascript:', html)

    def test_no_unreplaced_slots(self):
        for html in (report.render_web([make_record()], "TOKEN", '<option value="深圳">深圳</option>'),
                     report._filter_html(report.STATIC_SUB_LINE, [make_record()], web=False)):
            for slot in SLOT_NAMES:
                self.assertNotIn(slot, html, slot)

    def test_static_page_has_no_collect_ui(self):
        static = report._filter_html(report.STATIC_SUB_LINE, [make_record()], web=False)
        for marker in ('id="btn-collect"', "resumeTask()", "__TOKEN__"):
            self.assertNotIn(marker, static)
        self.assertIn("const TOKEN = '';", static)

    def test_web_page_wires_token_and_city_options(self):
        city_html = '<label><input type="checkbox" data-city="深圳"> 深圳</label>'
        html = report.render_web([make_record()], "TOK123", city_html)
        self.assertIn("const TOKEN = 'TOK123';", html)
        self.assertIn('id="btn-collect"', html)
        self.assertIn("resumeTask();", html)
        self.assertIn(city_html, html)
        # 平台/城市都是多选：平台是 chip 按钮，城市是复选框列表；旧的两个单选下拉不该再出现
        self.assertIn('id="c-platforms"', html)
        self.assertIn('data-cplat="zhitong"', html)
        self.assertIn('id="c-city-list"', html)
        self.assertNotIn('id="c-platform"', html)
        self.assertNotIn('id="c-city"', html)

    def test_triage_prototype_is_web_only(self):
        """「过一遍」要写 /api/status，静态页没有服务端可写，整块都不该出现。"""
        web = report.render_web([make_record()], "T", "")
        static = report._filter_html(report.STATIC_SUB_LINE, [make_record()], web=False)
        self.assertIn('id="btn-triage"', web)
        self.assertIn('id="triage-body"', web)
        self.assertIn("data-tri=\"想投\"", web)
        self.assertIn("triagePool()", web)
        for marker in ('id="btn-triage"', 'id="triage"', "startTriage("):
            self.assertNotIn(marker, static)

    def test_triage_only_offers_unmarked_jobs_and_reuses_status_api(self):
        """候选只收未标注的行；标记动作必须走现有的 /api/status，不另造一套写库口。"""
        web = report.render_web([make_record()], "T", "")
        js = report.TRIAGE_JS
        self.assertIn("!(r.status || '')", js)
        self.assertIn("api('/api/status'", js)
        self.assertIn("TRI.seen", js)
        # 静态页的匹配度/正文用的是同一份脚本，别让 triage 的按钮绑到空元素上
        self.assertIn('id="triage-close"', web)

    def test_both_variants_share_the_filter_ui(self):
        static = report._filter_html(report.STATIC_SUB_LINE, [make_record()], web=False)
        web = report.render_web([make_record()], "T", "")
        for widget in ("plat-btns", "edu-chips", "exp-chips", "salary-presets",
                       "active-tags", "btn-export", "const safeUrl"):
            self.assertIn(widget, static)
            self.assertIn(widget, web)

    def test_slot_values_are_not_rescanned(self):
        """数据里出现占位符字面量时不得被二次展开（token 不应被数据顶替）。"""
        recs = [make_record(title="__TOKEN__ __DATA__")]
        html = report.render_web(recs, "SECRETTOKEN", "")
        self.assertIn("const TOKEN = 'SECRETTOKEN';", html)
        self.assertEqual(len(re.findall(r"const TOKEN = ", html)), 1)


def job_item(jid: str, name: str = "IT运维工程师"):
    return {"encryptJobId": jid, "jobName": name, "salaryDesc": "6-8K",
            "cityName": "东莞", "jobExperience": "1-3年", "jobDegree": "大专"}


def job_page(code=0, items=(), has_more=True, message="Success"):
    return {"code": code, "message": message,
            "zpData": {"jobList": list(items), "hasMore": has_more}}


class FakeResp:
    """假 HTTP 响应，够覆盖 _harvest 用到的 status/json 两个成员。"""

    def __init__(self, payload, status=200):
        self.payload, self.status = payload, status

    def json(self):
        return self.payload


class TestBossHarvest(unittest.TestCase):
    """翻页逻辑离线可测：接口返回什么就怎么停，不能因为一页出错丢掉已采到的。"""

    class _Chan:
        """假通道：按顺序吐出 payloads；None 表示这一页没拿到内容。"""

        def __init__(self, payloads):
            self.calls, self.payloads = [], list(payloads)

        def get_json(self, params):
            self.calls.append(dict(params))
            return self.payloads.pop(0)

    def setUp(self):
        patch = mock.patch.object(boss, "polite_sleep", lambda: None)
        patch.start()
        self.addCleanup(patch.stop)

    def test_pages_until_has_more_false(self):
        chan = self._Chan([
            job_page(items=[job_item("a"), job_item("b")], has_more=True),
            job_page(items=[job_item("c")], has_more=False),
        ])
        jobs = boss._harvest(chan, "IT", "东莞", "101281600", 5, 200)
        self.assertEqual(sorted(jobs), ["a", "b", "c"])
        self.assertEqual([c["page"] for c in chan.calls], [1, 2])

    def test_stops_on_risk_code_and_keeps_partial(self):
        chan = self._Chan([
            job_page(items=[job_item("a")], has_more=True),
            job_page(code=37, items=[], message="您的环境存在异常"),
        ])
        jobs = boss._harvest(chan, "IT", "东莞", "101281600", 5, 200)
        self.assertEqual(list(jobs), ["a"])

    def test_dedupes_repeated_ids_and_stops(self):
        chan = self._Chan([
            job_page(items=[job_item("a")], has_more=True),
            job_page(items=[job_item("a")], has_more=True),
        ])
        jobs = boss._harvest(chan, "IT", "东莞", "101281600", 5, 200)
        self.assertEqual(list(jobs), ["a"])
        self.assertEqual(len(chan.calls), 2)  # 第二页无新增即停，不继续打接口

    def test_stops_at_daily_remaining(self):
        chan = self._Chan([job_page(items=[job_item("a"), job_item("b")])])
        jobs = boss._harvest(chan, "IT", "东莞", "101281600", 5, 1)
        self.assertEqual(len(jobs), 2)  # 当页收完即停，截断在 collect() 里做
        self.assertEqual(len(chan.calls), 1)

    def test_stops_when_channel_gives_nothing(self):
        chan = self._Chan([job_page(items=[job_item("a")], has_more=True), None])
        jobs = boss._harvest(chan, "IT", "东莞", "101281600", 5, 200)
        self.assertEqual(list(jobs), ["a"])
        self.assertEqual(len(chan.calls), 2)


class TestBossChannel(unittest.TestCase):
    """CDP 会话说断就断（Edge 窗口被关/被后台实例接管），通道必须自愈且不外泄凭证。"""

    # Playwright 的报错会把整段 Call log 带出来，里面原样含请求头
    DISPOSED = ("APIRequestContext.get: Request context disposed.\nCall log:\n"
                "  - → GET https://www.zhipin.com/wapi/zpgeek/search/joblist.json\n"
                "    - cookie: bst=SECRET_BST; zp_at=SECRET_ZP_AT")

    def _attach_fn(self, results):
        """造一个假的接管函数：每次 attach 交出一个 request.get 表现为 results[i] 的 ctx。"""
        @contextmanager
        def attach():
            outcome = results.pop(0)

            class Req:
                def get(self, url, params=None, headers=None):
                    if isinstance(outcome, Exception):
                        raise outcome
                    return outcome

            class Ctx:
                request = Req()

            yield Ctx()

        return attach

    def test_reconnects_and_retries_the_same_page(self):
        chan = boss.Channel(attach_fn=self._attach_fn(
            [PlaywrightError(self.DISPOSED), FakeResp(job_page(items=[job_item("a")]))]),
            retries=2)
        self.assertEqual(chan.get_json({"page": 1})["code"], 0)

    def test_gives_up_without_printing_cookies(self):
        chan = boss.Channel(attach_fn=self._attach_fn(
            [PlaywrightError(self.DISPOSED), PlaywrightError(self.DISPOSED)]), retries=2)
        with mock.patch("builtins.print") as out:
            with self.assertRaises(RuntimeError) as cm:
                chan.get_json({"page": 1})
        for line in [str(cm.exception)] + [c.args[0] for c in out.call_args_list if c.args]:
            self.assertNotIn("SECRET_BST", line)
            self.assertNotIn("SECRET_ZP_AT", line)
        self.assertIn("保持打开", str(cm.exception))

    def test_http_error_returns_none(self):
        chan = boss.Channel(attach_fn=self._attach_fn([FakeResp({}, status=503)]), retries=1)
        with mock.patch("builtins.print"):
            self.assertIsNone(chan.get_json({"page": 1}))


class TestEdgeChannel(unittest.TestCase):
    """Boss 的真实 Edge 通道：点一下『开始采集』就该自己把窗口弹出来。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.auth = Path(tmp.name)
        self.opened = []
        patch_auth = mock.patch.object(boss, "AUTH_DIR", self.auth)
        patch_auth.start()
        self.addCleanup(patch_auth.stop)
        patch_open = mock.patch.object(boss, "open_edge",
                                       lambda url="": self.opened.append(url))
        patch_open.start()
        self.addCleanup(patch_open.stop)

    def _logged_in(self):
        (self.auth / "boss_storage.json").write_text("{}", encoding="utf-8")

    def _alive(self, value):
        return mock.patch.object(boss, "cdp_alive", lambda port=0: value)

    def test_first_time_pops_login_window_and_tells_user_to_rescan(self):
        with self._alive(False):
            hint = boss.ensure_window()
        self.assertIn("扫码登录", hint)
        self.assertIn("再点一次", hint)
        self.assertEqual(self.opened, [boss.LOGIN_URL])

    def test_reopens_window_without_asking_for_commands(self):
        self._logged_in()
        with self._alive(False), mock.patch("builtins.print"):
            self.assertIsNone(boss.ensure_window())
        self.assertEqual(self.opened, [boss.LOGIN_URL])  # 自己弹，不叫用户去敲 login

    def test_silent_when_window_already_up(self):
        self._logged_in()
        with self._alive(True):
            self.assertIsNone(boss.ensure_window())
        self.assertEqual(self.opened, [])  # 已就绪就不该再开一个窗口

    def test_edge_exe_raises_when_not_installed(self):
        with mock.patch.object(browser, "EDGE_PATHS", ("Z:/nope/msedge.exe",)):
            with self.assertRaises(RuntimeError):
                browser.edge_exe()

    def test_cdp_alive_false_without_listener(self):
        self.assertFalse(browser.cdp_alive(port=1))

    def test_attach_refuses_without_window(self):
        with mock.patch.object(browser, "cdp_alive", lambda port=0: False):
            with self.assertRaises(RuntimeError) as cm:
                with browser.attach(relaunch=lambda: None):
                    self.fail("不该进入上下文")
        self.assertIn("login boss --edge", str(cm.exception))

    def test_attach_reopens_window_itself_when_it_was_closed(self):
        """窗口被随手关掉是常态：自己弹回来，别一上来就叫用户去敲命令。"""
        state = {"alive": False, "tried": 0}

        def relaunch():
            state["tried"] += 1
            state["alive"] = True

        with mock.patch.object(browser, "cdp_alive", lambda port=0: state["alive"]), \
             mock.patch.object(browser, "sync_playwright") as sp:
            # p = sync_playwright().start() → p.chromium.connect_over_cdp(...) → p.contexts[0]
            sp.return_value.start.return_value.chromium \
                .connect_over_cdp.return_value.contexts = ["ctx"]
            with mock.patch("builtins.print"):
                with browser.attach(relaunch=relaunch) as ctx:
                    self.assertEqual(ctx, "ctx")
        self.assertEqual(state["tried"], 1)

    def test_attach_reports_reopen_failure_without_leaking_stack(self):
        boom = RuntimeError("Edge 调试端口 9223 起不来\nCall log: cookie: bst=SECRET")
        with mock.patch.object(browser, "cdp_alive", lambda port=0: False):
            with self.assertRaises(RuntimeError) as cm:
                with browser.attach(relaunch=lambda: (_ for _ in ()).throw(boom)):
                    self.fail("不该进入上下文")
        self.assertIn("自动恢复失败", str(cm.exception))
        self.assertNotIn("SECRET", str(cm.exception))

    def test_open_edge_launches_through_a_relay_not_as_a_child(self):
        """真窗口不能挂在启动它的那条命令的父子关系上。

        实测过：直接 Popen 起的 Edge 是命令的子进程，命令被按 PID 树清场
        （黑窗口被点掉 / RPA 任务超时被停 / 命令被打断）时端口 2 秒内就没了、
        进程全清；中间隔一个立刻退出的进程后，同一刀杀掉父进程，端口照用。
        这条用例钉住「隔一层」这个形状，别再退回直接子进程。
        """
        with mock.patch.object(browser.subprocess, "Popen") as popen, \
             mock.patch.object(browser, "cdp_alive", lambda port=0: True), \
             mock.patch.object(time, "sleep"):
            browser.open_edge("https://www.zhipin.com/")
        argv = popen.call_args[0][0]
        self.assertEqual(argv[0], sys.executable)          # 被杀的是这个立刻退出的中转
        self.assertNotIn("msedge", argv[0].lower())        # msedge 不再是命令的直接子进程
        edge = argv.index(browser.edge_exe())
        self.assertGreater(edge, 2)
        self.assertIn("--remote-debugging-port=9223", argv[edge + 1])
        self.assertEqual(argv[-1], "https://www.zhipin.com/")
        self.assertEqual(popen.call_count, 1)              # 只起那一个中转，不再补刀

    def test_open_edge_still_names_the_port_when_it_never_comes_up(self):
        """隔了中转就拿不到子进程状态了，坏情况只能等满 wait——报错那句话得照旧说清端口号。"""
        with mock.patch.object(browser.subprocess, "Popen"), \
             mock.patch.object(browser, "cdp_alive", lambda port=0: False):
            with self.assertRaises(RuntimeError) as cm:
                browser.open_edge("https://www.zhipin.com/", wait=0.0)
        self.assertIn("Edge 调试端口 9223 起不来", str(cm.exception))


class TestLoginState(unittest.TestCase):
    """登录失败不得留下登录态文件——否则服务端会误判「已登录」，采集才静默拿到 0 条。"""

    class _Ctx:
        def storage_state(self, path):
            Path(path).write_text("{}", encoding="utf-8")

    class _Closer:
        def close(self):
            pass

        def stop(self):
            pass

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.storage = Path(tmp.name) / "boss_storage.json"

    def _session(self, save_state):
        s = browser.Session("boss", save_state=save_state)
        s.storage = self.storage
        s._context = self._Ctx()
        s._browser = s._p = self._Closer()
        return s

    def test_login_session_saves_only_on_success(self):
        self._session(save_state=False).__exit__()
        self.assertFalse(self.storage.exists())
        self._session(save_state=False).save()
        self.assertTrue(self.storage.exists())

    def test_collect_session_still_refreshes_state(self):
        self._session(save_state=True).__exit__()
        self.assertTrue(self.storage.exists())


class TestAuthDirLocation(unittest.TestCase):
    """登录凭证默认在项目外（%LOCALAPPDATA%\\job-stats\\auth），可由 JOBSTATS_AUTH_DIR 改。"""

    def _reload(self):
        import importlib
        importlib.reload(config)
        return config.AUTH_DIR

    def _with_env(self, value):
        prev = os.environ.get("JOBSTATS_AUTH_DIR")
        if value is None:
            os.environ.pop("JOBSTATS_AUTH_DIR", None)
        else:
            os.environ["JOBSTATS_AUTH_DIR"] = value
        try:
            return self._reload()
        finally:
            if prev is None:
                os.environ.pop("JOBSTATS_AUTH_DIR", None)
            else:
                os.environ["JOBSTATS_AUTH_DIR"] = prev
            self._reload()

    def test_default_is_outside_the_project(self):
        """项目文件夹里任何时候都不该有登录凭证：挪回 data/ 就等于把外传风险请回来。"""
        d = self._with_env(None)
        self.assertEqual(d, config.CDP_PROFILE_DIR.parent / "auth")
        self.assertNotIn(config.PROJECT_DIR, d.parents)

    def test_env_var_wins(self):
        self.assertEqual(self._with_env(r"D:\secrets\jobstats-auth"),
                         Path(r"D:\secrets\jobstats-auth"))

    def test_blank_env_var_does_not_move_it_to_cwd(self):
        """设成空串或纯空格等于没设——Path('') 会变成当前目录，那就邪门了。"""
        self.assertEqual(self._with_env("   "), config.CDP_PROFILE_DIR.parent / "auth")

    def test_login_messages_print_the_real_path(self):
        """话术里不能写死 data/auth：挪了地址还那么说，人就照着错的地方找。"""
        src = (Path(config.PROJECT_DIR) / "jobstats" / "browser.py").read_text(encoding="utf-8")
        self.assertNotIn("已保存到 data/auth/", src)
        self.assertIn("登录凭证存放地址", src)


class TestLoginStateReport(unittest.TestCase):
    """`cli.py status` 的底层：只凭 cookie 名字和有效期下结论，值一个字节都不许出现在返回值里。

    体检报告有两条来源：Edge 通道开着读实时那份，没开才读快照文件。默认把所有用例都钉在
    快照那条路上（通道造成为关闭），否则本机 Edge 开没开会改变结果——影刀巡检跑的就是这段
    输出，随机变一次就是天天误报或者永远不报。实时那条路单独用 `_channel` 造。
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.auth = Path(tmp.name)
        self._patch = mock.patch.object(browser, "AUTH_DIR", self.auth)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self._channel_off()

    def _channel_off(self):
        off = mock.patch.object(browser, "cdp_alive", lambda port=0: False)
        off.start()
        self.addCleanup(off.stop)
        boom = mock.patch.object(browser, "attach",
                                 side_effect=AssertionError("通道没开不该 attach"))
        boom.start()
        self.addCleanup(boom.stop)

    def _channel(self, cookies):
        """把实时通道换成假 cookie（快照文件照旧留着，用来看有没有被刷）。"""
        ctx = mock.MagicMock()
        ctx.cookies.return_value = cookies
        ctx.__enter__.return_value = ctx
        alive = mock.patch.object(browser, "cdp_alive", lambda port=0: True)
        alive.start()
        self.addCleanup(alive.stop)
        attach = mock.patch.object(browser, "attach", return_value=ctx)
        attach.start()
        self.addCleanup(attach.stop)
        return ctx

    def _write(self, cookies):
        (self.auth / "boss_storage.json").write_text(
            json.dumps({"cookies": cookies}), encoding="utf-8")

    def _report(self, required=("bst", "zp_at")):
        return "\n".join(browser.login_state_report("boss", set(required)))

    def test_missing_file_tells_you_to_scan(self):
        out = self._report()
        self.assertIn("还没保存过登录态", out)
        self.assertIn("login-boss.bat", out)

    def test_a_never_scanned_state_still_prints_a_conclusion(self):
        """`结论` 这句话本身也是接口：影刀分支表第 1 行只看它在不在。

        从没扫过码是"该去扫码"（第 3 行），不是"巡检自己没跑成"（第 1 行）——
        两个动作不一样：前者是双击一个 bat 扫一次码，后者是去查 .venv 和 bat 还在不在。
        """
        out = self._report()
        self.assertIn("还没保存过登录态", out)
        self.assertIn("结论", out)

    def test_an_unreadable_snapshot_has_no_conclusion_on_purpose(self):
        """反过来的那一支故意不写 `结论`：连文件都读不出来就是"判不出来"，
        必须被第 1 行接住报"巡检没跑成"，绝不能一路掉进"以上都不命中=健康"。"""
        (self.auth / "boss_storage.json").write_text("{这不是 JSON", encoding="utf-8")
        out = self._report()
        self.assertIn("文件读不出来", out)
        self.assertNotIn("结论", out)

    def test_an_unreadable_snapshot_names_the_real_reason(self):
        """文件只是被别的程序占着，跟"这份坏了"是两件事。

        原话直接让人"重新扫一次码覆盖它"——照着做的人会把一份好端端的登录态覆盖掉。
        所以这里必须把真实原因（WinError 32 那种）带到屏幕上。
        """
        (self.auth / "boss_storage.json").write_text("{这不是 JSON", encoding="utf-8")
        out = self._report()
        self.assertIn("文件读不出来", out)
        self.assertIn("先关掉正在占用它的程序", out)
        self.assertIn("Expecting property name", out)

    def test_a_snapshot_with_no_cookie_list_is_judged_not_crashed(self):
        """解得开但里面没有 cookie 列表：判成"判据缺失"，而不是抛 AttributeError 把命令崩掉。"""
        for shape in ("[]", '{"cookies": []}', '[1, "bst", null]', '" bst"'):
            with self.subTest(shape=shape):
                (self.auth / "boss_storage.json").write_text(shape, encoding="utf-8")
                out = self._report()
                self.assertIn("bst: 没有（登录判据缺失）", out)

    def test_expired_cookie_says_rescan_not_retry_the_command(self):
        now = time.time()
        self._write([{"name": "bst", "value": "SECRET-BST-VALUE", "expires": now - 86400 * 9},
                     {"name": "zp_at", "value": "SECRET-ZP-VALUE", "expires": now + 86400 * 3}])
        out = self._report()
        self.assertIn("bst: 已过期 9.0 天", out)
        self.assertIn("zp_at: 还剩 3.0 天", out)
        self.assertIn("重新扫一次码才是正解", out)

    def test_cookie_values_never_reach_the_report(self):
        """返回值是要打印的，混进值等于把登录凭证贴到屏幕上、写进日志里。"""
        self._write([{"name": "bst", "value": "SECRET-BST-VALUE", "expires": time.time() + 86400},
                     {"name": "zp_at", "value": "SECRET-ZP-VALUE", "expires": -1}])
        self.assertNotIn("SECRET", self._report())

    def test_session_cookie_is_not_reported_as_expired(self):
        self._write([{"name": "bst", "value": "v1", "expires": -1},
                     {"name": "zp_at", "value": "v2"}])
        out = self._report()
        self.assertNotIn("已过期", out)
        self.assertIn("看不出有效期", out)

    def test_absent_login_cookie_is_called_anonymous(self):
        """只有 __a/lastCity 这类匿名 cookie 时，文件在但不能当登录态用。"""
        self._write([{"name": "__a", "value": "v", "expires": time.time() + 86400}])
        out = self._report()
        self.assertIn("bst: 没有", out)
        self.assertIn("匿名访问", out)

    def test_platform_without_login_need_says_so(self):
        out = self._report(required=())
        self.assertIn("不需要登录凭证", out)

    # 影刀那套登录巡检（README「影刀：每天早上的登录巡检」）不查库、不看退出码，
    # 只在这段中文里匹配下面几个词：命中就发消息，一个都不命中就算健康。
    # 所以这几个词是接口——文案随手一改，巡检就变成天天误报或者永远不报。
    RPA_BAD = ("已过期", "还没保存过登录态", "判据缺失", "看不出有效期")

    def test_healthy_state_matches_no_rpa_phrase(self):
        now = time.time()
        self._write([{"name": "bst", "value": "v1", "expires": now + 86400 * 5},
                     {"name": "zp_at", "value": "v2", "expires": now + 86400 * 5}])
        out = self._report()
        self.assertIn("还剩 5.0 天", out)
        for phrase in self.RPA_BAD:
            self.assertNotIn(phrase, out)

    def test_every_broken_state_matches_an_rpa_phrase(self):
        """四种坏状态都得被那几个词逮住，漏一种就是那种情况下它一声不响。"""
        now = time.time()
        cases = {
            "文件不在": None,
            "凭证过期": [{"name": "bst", "value": "v", "expires": now - 86400},
                         {"name": "zp_at", "value": "v", "expires": now + 86400}],
            "只有匿名 cookie": [{"name": "__a", "value": "v", "expires": now + 86400}],
            "会话 cookie": [{"name": "bst", "value": "v", "expires": -1},
                            {"name": "zp_at", "value": "v", "expires": -1}],
        }
        for label, cookies in cases.items():
            with self.subTest(label):
                if cookies is None:
                    (self.auth / "boss_storage.json").unlink(missing_ok=True)
                else:
                    self._write(cookies)
                out = self._report()
                self.assertTrue([p for p in self.RPA_BAD if p in out], f"{label}：{out}")

    # ---- 实时通道那条路（Edge 窗口开着）----------------------------------------------
    # 2026-10-24 那天的现场就是这条路没走：通道里 bst/zp_at 还剩 7.2 天，快照文件停在
    # 09-26，报告照着文件报"凭证已过期 20.8 天"，影刀跟着发提醒。所以这几条是重点。

    def test_closed_channel_admits_it_reads_history(self):
        """窗口没开时读的是快照，就得在输出里写明白这话不代表"现在能不能采"。"""
        now = time.time()
        self._write([{"name": "bst", "value": "v", "expires": now + 86400},
                     {"name": "zp_at", "value": "v", "expires": now + 86400}])
        out = self._report()
        self.assertIn("来源：快照文件", out)
        self.assertIn("不代表现在", out)
        self.assertIn("这份存于", out)

    def test_live_channel_is_not_fooled_by_a_stale_snapshot(self):
        """快照过期、通道好用：报实时那份，而且一个坏判据词都不许出现。"""
        now = time.time()
        self._write([{"name": "bst", "value": "v", "expires": now - 86400 * 20}])
        self._channel([{"name": "bst", "value": "v", "expires": now + 86400 * 5},
                       {"name": "zp_at", "value": "v", "expires": now + 86400 * 5}])
        out = self._report()
        self.assertIn("来源：实时通道", out)
        self.assertIn("还剩 5.0 天", out)
        for phrase in self.RPA_BAD:
            self.assertNotIn(phrase, out)

    def test_good_live_credentials_refresh_the_snapshot(self):
        """顺手把快照刷成实时那份：文件只是缓存，没人刷它就能停四周。"""
        now = time.time()
        self._write([{"name": "bst", "value": "v", "expires": now - 86400 * 20}])
        ctx = self._channel([{"name": "bst", "value": "v", "expires": now + 86400 * 5},
                             {"name": "zp_at", "value": "v", "expires": now + 86400 * 5}])
        out = self._report()
        self.assertIn("已顺手刷进快照", out)
        ctx.storage_state.assert_called_once_with(
            path=str(self.auth / "boss_storage.json"))

    def test_a_failed_snapshot_write_is_admitted(self):
        """写文件被挡住（杀毒软件/另一个进程占着）时不许说"已刷进快照"。

        2026-10-25 实测到的现场：屏幕上写着"已顺手刷进快照"，文件的 mtime 一动没动。
        采集不受这件事影响，所以话要说成"没写进去"，既不能假装刷过，也不能报成登录坏了。
        """
        now = time.time()
        ctx = self._channel([{"name": "bst", "value": "v", "expires": now + 86400 * 5},
                             {"name": "zp_at", "value": "v", "expires": now + 86400 * 5}])
        ctx.storage_state.side_effect = OSError("另一个程序正在使用此文件")
        out = self._report()
        self.assertIn("快照文件没写进去", out)
        self.assertIn("另一个程序正在使用此文件", out)
        self.assertNotIn("已顺手刷进快照", out)
        for phrase in self.RPA_BAD:
            self.assertNotIn(phrase, out)

    def test_broken_live_credentials_do_not_overwrite_the_snapshot(self):
        """实时那份是坏的就不许往文件里写：写进去等于把今天的错误变成明天的历史。"""
        now = time.time()
        self._write([{"name": "bst", "value": "v", "expires": now + 86400 * 9},
                     {"name": "zp_at", "value": "v", "expires": now + 86400 * 9}])
        before = (self.auth / "boss_storage.json").read_text(encoding="utf-8")
        ctx = self._channel([{"name": "bst", "value": "v", "expires": now - 86400},
                             {"name": "zp_at", "value": "v", "expires": now + 86400}])
        out = self._report()
        self.assertIn("凭证是坏的，没往快照里写", out)
        ctx.storage_state.assert_not_called()
        self.assertEqual(before, (self.auth / "boss_storage.json").read_text(encoding="utf-8"))

    def test_live_anonymous_channel_still_reports_the_missing_judgement(self):
        """通道开着但只有匿名 cookie：照样得被 `判据缺失` 逮住，且不往文件里写。"""
        ctx = self._channel([{"name": "__a", "value": "v", "expires": time.time() + 86400}])
        out = self._report()
        self.assertIn("bst: 没有（登录判据缺失）", out)
        self.assertIn("匿名访问", out)
        ctx.storage_state.assert_not_called()

    def test_live_cookie_values_never_reach_the_report(self):
        self._channel([{"name": "bst", "value": "SECRET-BST-VALUE", "expires": time.time() + 86400},
                       {"name": "zp_at", "value": "SECRET-ZP-VALUE", "expires": time.time() + 86400}])
        self.assertNotIn("SECRET", self._report())


class TestLoginBlockReason(unittest.TestCase):
    """`login_block_reason`：发请求之前问一句"这个登录态还能不能开工"。

    它只认**实时 Edge 通道**里的 cookie。`boss_storage.json` 只是份快照，平时没有命令会刷新它：
    实测用户重新扫过码、通道里 bst/zp_at 还剩 7.2 天，那份快照还停在 20 多天前——
    照快照判就把刚登录的人拦在门外，而拦掉的那批一条请求都没发。
    所以测试必须把通道本身换掉，光 patch AUTH_DIR 会去读本机真实登录态，结果随环境变。
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.auth = Path(tmp.name)
        self._patch = mock.patch.object(browser, "AUTH_DIR", self.auth)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def _channel(self, cookies, alive=True):
        """把实时通道换成假 cookie；返回那个假 context 以便断言快照有没有被刷新。

        `cdp_alive` 也得造：不然本机 Edge 没开时这些用例走的是"通道没开"那条路，
        测试结果就跟着巡检的人当天开没开浏览器变。
        """
        ctx = mock.MagicMock()
        ctx.cookies.return_value = cookies
        ctx.__enter__.return_value = ctx
        patches = [mock.patch.object(browser, "cdp_alive",
                                     lambda port=0, _a=alive: _a)]
        if alive:
            patches.append(mock.patch.object(browser, "attach", return_value=ctx))
        else:
            patches.append(mock.patch.object(
                browser, "attach",
                side_effect=AssertionError("通道没开不该 attach")))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return ctx

    def test_expired_login_names_the_days(self):
        now = time.time()
        self._channel([{"name": "bst", "value": "SECRET", "expires": now - 86400 * 20},
                       {"name": "zp_at", "value": "SECRET", "expires": now + 86400}])
        reason = browser.login_block_reason("boss", {"bst", "zp_at"})
        self.assertIn("已过期 20.0 天", reason)
        self.assertNotIn("SECRET", reason)

    def test_fresh_login_beats_a_stale_snapshot(self):
        """这就是那天那个 bug：快照写着过期 20.8 天，通道里其实还剩 7 天。"""
        now = time.time()
        (self.auth / "boss_storage.json").write_text(json.dumps({"cookies": [
            {"name": "bst", "value": "v", "expires": now - 86400 * 20.8},
            {"name": "zp_at", "value": "v", "expires": now - 86400 * 20.8}]}),
            encoding="utf-8")
        ctx = self._channel([{"name": "bst", "value": "v", "expires": now + 86400 * 7},
                             {"name": "zp_at", "value": "v", "expires": now + 86400 * 7}])
        self.assertIsNone(browser.login_block_reason("boss", {"bst", "zp_at"}))
        # 顺手把快照刷成实时那份，否则 status/login-check 会继续报旧日期
        ctx.storage_state.assert_called_once_with(
            path=str(self.auth / "boss_storage.json"))

    def test_missing_file_is_not_a_block_when_channel_down(self):
        """通道没开时不拦：ensure_window 会把窗口弹回来，那时才有证据可判。"""
        self._channel([], alive=False)
        self.assertIsNone(browser.login_block_reason("boss", {"bst", "zp_at"}))

    def test_anonymous_channel_is_a_block(self):
        self._channel([{"name": "__a", "value": "v", "expires": time.time() + 86400}])
        self.assertIn("没登录", browser.login_block_reason("boss", {"bst", "zp_at"}))

    def test_session_cookie_is_not_prejudged_dead(self):
        """会话 cookie 判不了有效期：只有真采一次才知道，别提前判死刑把补抓全停了。"""
        self._channel([{"name": "bst", "value": "v", "expires": -1},
                       {"name": "zp_at", "value": "v", "expires": -1}])
        self.assertIsNone(browser.login_block_reason("boss", {"bst", "zp_at"}))

    def test_dead_channel_is_not_reported_as_dead_login(self):
        """attach 抛错（窗口中途没了）是环境问题，不能说成"你的账号不行"。"""
        with mock.patch.object(browser, "cdp_alive", lambda port=0: True), \
                mock.patch.object(browser, "attach", side_effect=RuntimeError("no CDP")):
            self.assertIsNone(browser.login_block_reason("boss", {"bst", "zp_at"}))

    def test_bad_cookies_do_not_touch_the_snapshot(self):
        """实时凭证是坏的，就别把那份坏状态写回快照——快照留给体检报告当历史。"""
        self._channel([{"name": "__a", "value": "v", "expires": time.time()}])
        browser.login_block_reason("boss", {"bst", "zp_at"})
        self.assertFalse((self.auth / "boss_storage.json").exists())

    def test_platform_without_login_never_blocks(self):
        self.assertIsNone(browser.login_block_reason("zhitong", ()))


class TestRefreshSnapshot(unittest.TestCase):
    """采集前那一下刷新：只有"通道开着且实时凭证确实能用"才动快照文件。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.auth = Path(tmp.name)
        p = mock.patch.object(browser, "AUTH_DIR", self.auth)
        p.start()
        self.addCleanup(p.stop)

    def _channel(self, cookies, alive=True):
        ctx = mock.MagicMock()
        ctx.cookies.return_value = cookies
        ctx.__enter__.return_value = ctx
        a = mock.patch.object(browser, "cdp_alive", lambda port=0, _v=alive: _v)
        a.start()
        self.addCleanup(a.stop)
        t = mock.patch.object(browser, "attach",
                              return_value=ctx if alive else
                              mock.MagicMock(__enter__=mock.MagicMock(
                                  side_effect=AssertionError("通道没开不该 attach"))))
        t.start()
        self.addCleanup(t.stop)
        return ctx

    def test_closed_channel_writes_nothing(self):
        """窗口没开就是"不知道"，不是"坏了"：这时候一个字节都不许动。"""
        self._channel([], alive=False)
        self.assertFalse(browser.refresh_snapshot("boss", {"bst", "zp_at"}))

    def test_usable_credentials_are_pushed_into_the_snapshot(self):
        now = time.time()
        ctx = self._channel([{"name": "bst", "value": "v", "expires": now + 86400 * 5},
                             {"name": "zp_at", "value": "v", "expires": now + 86400 * 5}])
        self.assertTrue(browser.refresh_snapshot("boss", {"bst", "zp_at"}))
        ctx.storage_state.assert_called_once_with(
            path=str(self.auth / "boss_storage.json"))

    def test_a_blocked_write_counts_as_not_refreshed(self):
        """刷新这件事的成功判据是"文件真的写了"，不是"凭证看着挺好"。"""
        now = time.time()
        ctx = self._channel([{"name": "bst", "value": "v", "expires": now + 86400 * 5},
                             {"name": "zp_at", "value": "v", "expires": now + 86400 * 5}])
        ctx.storage_state.side_effect = OSError("另一个程序正在使用此文件")
        self.assertFalse(browser.refresh_snapshot("boss", {"bst", "zp_at"}))

    def test_dead_credentials_stay_out_of_the_file(self):
        now = time.time()
        ctx = self._channel([{"name": "bst", "value": "v", "expires": now - 86400},
                             {"name": "zp_at", "value": "v", "expires": now + 86400}])
        self.assertFalse(browser.refresh_snapshot("boss", {"bst", "zp_at"}))
        ctx.storage_state.assert_not_called()

    def test_platform_without_login_is_left_alone(self):
        self.assertFalse(browser.refresh_snapshot("zhitong", ()))


class TestLoginAutoSave(unittest.TestCase):
    """`login boss --edge` 的自动落盘：判据一凑齐就存，不用回黑窗口按回车。

    原来只有按回车那一次检查会写文件，于是"登录成功却没存下快照"成了常态空档
    ——回车按早了、或扫完码顺手关了那个窗口，凭证在浏览器里照样好用，快照能停四周。
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.auth = Path(tmp.name)
        self.ctx = mock.MagicMock()
        self.ctx.__enter__.return_value = self.ctx
        self._patches = [
            mock.patch.object(browser, "AUTH_DIR", self.auth),
            mock.patch.object(browser, "open_edge"),
            mock.patch.object(browser, "attach", return_value=self.ctx),
            mock.patch("time.sleep"),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def _cookies(self, *batches):
        """每轮询一次交一批 cookie 名字（模拟扫码过程中凭证是陆续出现的）。"""
        self.ctx.cookies.side_effect = [list(b) for b in batches]

    def _run(self, wait=300.0, input_side_effect=None):
        out = io.StringIO()
        with mock.patch("builtins.input", side_effect=input_side_effect or
                        [AssertionError("不该等人按回车")]):
            try:
                with redirect_stdout(out):
                    browser.login_with_edge("boss", "https://example.com/login",
                                            {"bst", "zp_at"}, wait=wait, poll=0.1)
            except AssertionError as e:
                raise AssertionError(f"走到了手动回车那条路：{e}")
            except EOFError:
                pass        # Ctrl+D 退出复查，等价于测试里的"到此为止"
        return out.getvalue()

    def test_completes_without_touching_enter(self):
        now = time.time()
        self._cookies([{"name": "__a", "value": "x", "expires": now}],
                      [{"name": "bst", "value": "x", "expires": now + 86400 * 7},
                       {"name": "zp_at", "value": "x", "expires": now + 86400 * 7}])
        text = self._run()
        self.assertIn("登录成功", text)
        self.ctx.storage_state.assert_called_once_with(
            path=str(self.auth / "boss_storage.json"))

    def test_a_half_set_is_never_written(self):
        """只有一笔判据时不存：半套快照写进文件后，体检报告读出来照样像"登录过"。"""
        now = time.time()
        self._cookies(*[[{"name": "bst", "value": "x", "expires": now + 86400 * 7}]] * 3)
        text = self._run(wait=0.0, input_side_effect=EOFError)
        self.ctx.storage_state.assert_not_called()
        self.assertIn("还缺 zp_at", text)
        self.assertIn("这一份没存", text)
        self.assertNotIn("登录成功", text)

    def test_cookie_values_never_reach_the_terminal(self):
        """这一屏会被录进 RPA 日志、也会被贴到群里，值不能出现。"""
        now = time.time()
        self._cookies([{"name": "bst", "value": "SECRET-BST", "expires": now + 86400 * 7},
                       {"name": "zp_at", "value": "SECRET-ZP", "expires": now + 86400 * 7}])
        self.assertNotIn("SECRET", self._run())

    def test_a_channel_break_falls_back_to_enter(self):
        """轮询中途窗口没了：说清楚是通道断了，然后交给回车那条路，别静崩。"""
        self.ctx.cookies.side_effect = RuntimeError("TargetClosedError: closed\n- Call log:")
        text = self._run(input_side_effect=EOFError)
        self.assertIn("通道断了", text)

    def test_window_closed_before_the_first_attach(self):
        with mock.patch.object(browser, "attach", side_effect=RuntimeError("no CDP")):
            text = self._run(input_side_effect=EOFError)
        self.assertIn("暂时接不上那个 Edge 窗口", text)

    def test_enter_recheck_can_still_save(self):
        """超时退回手动以后，按回车那一路照样能补齐落盘（老用法没被砍掉）。"""
        now = time.time()
        good = mock.MagicMock()
        good.__enter__.return_value = good
        good.cookies.return_value = [
            {"name": "bst", "value": "x", "expires": now + 86400 * 7},
            {"name": "zp_at", "value": "x", "expires": now + 86400 * 7}]
        self.ctx.cookies.return_value = []
        with mock.patch.object(browser, "attach", side_effect=[self.ctx, good]):
            out = io.StringIO()
            with mock.patch("builtins.input", return_value=""):
                with redirect_stdout(out):
                    browser.login_with_edge("boss", "https://example.com/login",
                                            {"bst", "zp_at"}, wait=0.0, poll=0.1)
        text = out.getvalue()
        self.assertIn("登录成功", text)
        good.storage_state.assert_called_once_with(
            path=str(self.auth / "boss_storage.json"))


class TestSnapshotRefreshBeforeCollect(TempDbCase):
    """采集是拿通道里的真凭证发请求的，快照只是给体检/status 看的缓存——开工前刷一次。"""

    def _fake_channel(self, name, **over):
        fake = mock.Mock()
        fake.PER_PAGE_SAVE = False
        fake.SELF_GATES = False
        fake.FAMILY_GATE = False
        fake.collect.return_value = []
        for k, v in over.items():
            setattr(fake, k, v)
        return fake

    def test_the_snapshot_is_refreshed_before_the_channel_runs(self):
        order = []
        fake = self._fake_channel("boss", LOGIN_COOKIES={"bst", "zp_at"})
        fake.collect.side_effect = lambda **k: order.append("collect") or []
        with mock.patch.dict(collectors.COLLECTORS, {"boss": fake}), \
                mock.patch.object(collectors.browser, "refresh_snapshot",
                                  side_effect=lambda p, r: order.append(("refresh", p, r))
                                  or True) as rs, \
                redirect_stdout(io.StringIO()):
            collectors.collect_and_save(fake, "IT运维", "东莞", 1)
        rs.assert_called_once_with("boss", ("bst", "zp_at"))
        self.assertEqual(["refresh", "collect"], [o[0] if isinstance(o, tuple) else o
                                                  for o in order])

    def test_a_channel_without_login_does_not_refresh(self):
        fake = self._fake_channel("zhitong", LOGIN_COOKIES=set())
        with mock.patch.dict(collectors.COLLECTORS, {"zhitong": fake}), \
                mock.patch.object(collectors.browser, "refresh_snapshot") as rs, \
                redirect_stdout(io.StringIO()):
            collectors.collect_and_save(fake, "IT运维", "东莞", 1)
        rs.assert_not_called()

    def test_an_unregistered_module_is_not_mistaken_for_a_platform(self):
        """测试里的假通道没进注册表：认不出平台名就不该去碰登录态。"""
        with mock.patch.object(collectors.browser, "refresh_snapshot") as rs, \
                redirect_stdout(io.StringIO()):
            collectors.collect_and_save(self._fake_channel("x"), "IT", "东莞", 1)
        rs.assert_not_called()


class TestTaskLogFile(TempDbCase):
    """页面里的采集日志必须落盘：服务一重启，昨晚那次为什么 0 条就该再也查不到了。

    继承 TempDbCase 是因为这两个用例会真的跑 `TaskManager._run`，整条落库链路都在里面：
    只 patch `init_db`/`save_jobs` 挡不住后来加在 collect_and_save 里的采集留痕写盘。
    """

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.logdir = Path(tmp.name)
        patch = mock.patch.object(server, "LOG_DIR", self.logdir)
        patch.start()
        self.addCleanup(patch.stop)
        # 这两个用例把假模块注册成了 boss，于是 collect_and_save 会去刷登录快照。
        # 通道一律造成为关闭：不然用例会去连本机 9223，结果跟着当天开没开 Edge 变。
        off = mock.patch.object(browser, "cdp_alive", lambda port=0: False)
        off.start()
        self.addCleanup(off.stop)

    def _today(self):
        return self.logdir / f"collect-{time.strftime('%Y-%m-%d')}.log"

    def test_log_lines_reach_the_file_with_task_identity(self):
        task = server.CollectTask(["zhitong"], "IT运维", ["东莞"], 3)
        task.log("第 1/3 页: IT运维 @ 东莞")
        text = self._today().read_text(encoding="utf-8")
        self.assertIn("[zhitong]", text)
        self.assertIn(task.id, text)
        self.assertIn("第 1/3 页", text)

    def test_failure_line_is_truncated_so_cookies_stay_out(self):
        """Playwright 的报错第二行原样带着请求头，日志文件里只能有第一行。"""
        boom = PlaywrightError("APIRequestContext.get: Request context disposed.\n"
                               "  - Cookie: bst=SECRET-BST-VALUE; zp_at=SECRET-ZP")
        fake = mock.Mock()
        fake.LOGIN_COOKIES = {"bst", "zp_at"}   # 注册成 boss 就得给出真的判据集合
        fake.collect.side_effect = boom
        task = server.CollectTask(["boss"], "IT", ["东莞"], 1)
        with mock.patch.dict(server.COLLECTORS, {"boss": fake}):
            server.TaskManager()._run(task)
        text = self._today().read_text(encoding="utf-8")
        self.assertIn("[失败] APIRequestContext.get", text)
        self.assertNotIn("SECRET", text)
        self.assertNotIn("SECRET", task.error or "")

    def _fake(self, records=1, side_effect=None):
        fake = mock.Mock()
        fake.PER_PAGE_SAVE = False       # 老通道：整轮跑完由调用方存一次
        fake.LOGIN_COOKIES = set()       # 不需要登录，采集前不该去刷快照
        fake.collect.return_value = [{"platform_job_id": str(i)} for i in range(records)]
        if side_effect is not None:
            fake.collect.side_effect = side_effect
        return fake

    def test_start_and_done_lines_are_written(self):
        task = server.CollectTask(["zhitong"], "IT", ["东莞"], 2)
        with mock.patch.dict(server.COLLECTORS, {"zhitong": self._fake()}), \
             mock.patch.object(server.db, "init_db"), \
             mock.patch.object(server.db, "save_jobs", return_value=1):
            server.TaskManager()._run(task)
        text = self._today().read_text(encoding="utf-8")
        self.assertIn("开始：1 个平台 × 1 个城市 = 1 组", text)
        self.assertIn("完成 1/1：智通直聘 @ 东莞，采到 1 条、入库新增 1 条、刷新 0 条", text)

    def test_multiple_platforms_and_cities_run_in_order_and_aggregate(self):
        a, b = self._fake(), self._fake()
        task = server.CollectTask(["zhitong", "job51"], "IT", ["东莞", "深圳"], 1)
        with mock.patch.dict(server.COLLECTORS, {"zhitong": a, "job51": b}), \
             mock.patch.object(server.db, "init_db"), \
             mock.patch.object(server.db, "save_jobs", return_value=1):
            server.TaskManager()._run(task)
        self.assertEqual(a.collect.call_count, 2)
        self.assertEqual(b.collect.call_count, 2)
        self.assertEqual([c.kwargs["city"] for c in a.collect.call_args_list], ["东莞", "深圳"])
        self.assertEqual([c.kwargs["city"] for c in b.collect.call_args_list], ["东莞", "深圳"])
        self.assertEqual((task.status, task.runs_total, task.runs_done, task.runs_failed),
                         ("done", 4, 4, 0))
        self.assertEqual((task.collected, task.added), (4, 4))
        text = self._today().read_text(encoding="utf-8")
        self.assertIn("全部完成：4 组成功，采到 4 条", text)
        self.assertIn("[zhitong]", text)
        self.assertIn("[job51]", text)

    def test_one_platform_failing_does_not_stop_the_rest(self):
        boom = self._fake(side_effect=RuntimeError("结构变了"))
        ok = self._fake()
        task = server.CollectTask(["zhitong", "job51"], "IT", ["东莞"], 1)
        with mock.patch.dict(server.COLLECTORS, {"zhitong": boom, "job51": ok}), \
             mock.patch.object(server.db, "init_db"), \
             mock.patch.object(server.db, "save_jobs", return_value=1):
            server.TaskManager()._run(task)
        self.assertEqual(ok.collect.call_count, 1)
        self.assertEqual((task.status, task.runs_failed), ("done", 1))
        self.assertIn("[失败] 智通直聘 @ 东莞：结构变了", self._today().read_text(encoding="utf-8"))

    def test_quota_error_stops_the_remaining_groups(self):
        quota = self._fake(side_effect=RuntimeError(
            "已触发每日额度熔断：今日已采集 200 条（上限 200）。"))
        later = self._fake()
        task = server.CollectTask(["zhitong", "job51"], "IT", ["东莞"], 1)
        with mock.patch.dict(server.COLLECTORS, {"zhitong": quota, "job51": later}), \
             mock.patch.object(server.db, "init_db"):
            server.TaskManager()._run(task)
        self.assertEqual(later.collect.call_count, 0)
        self.assertEqual(task.status, "failed")
        text = self._today().read_text(encoding="utf-8")
        self.assertIn("停止：后面的 1 组不再跑", text)

    def test_boss_window_failure_skips_boss_but_runs_the_rest(self):
        ok = self._fake()
        task = server.CollectTask(["boss", "zhitong"], "IT", ["东莞"], 1)
        with mock.patch.dict(server.COLLECTORS, {"zhitong": ok}), \
             mock.patch.object(server.boss, "ensure_window", return_value="Edge 窗口起不来"), \
             mock.patch.object(server.db, "init_db"), \
             mock.patch.object(server.db, "save_jobs", return_value=1):
            server.TaskManager()._run(task)
        self.assertEqual(ok.collect.call_count, 1)
        self.assertEqual(task.status, "done")
        text = self._today().read_text(encoding="utf-8")
        self.assertIn("[跳过] Boss直聘：Edge 窗口起不来", text)


class TestServeLogFile(TempDbCase):
    """服务自身的启停也要落盘：控制台重定向进文件时那份 stdout 基本是空的。

    `serve` 的启动横幅走 stdout，而 stdout 一旦指向文件就变成块缓冲，进程被 taskkill 掉
    那一行永远写不出来；访问日志又是刻意静默的。这台机器上出现过两个实例前后脚抢 8765，
    事后只看得到一个在听、一个都不剩——所以按天的 serve 日志是唯一能回答"几点起的、
    绑的哪个端口、令牌是不是随机"的地方。
    """

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.logdir = Path(tmp.name)
        patch = mock.patch.object(server, "LOG_DIR", self.logdir)
        patch.start()
        self.addCleanup(patch.stop)

    def _today(self):
        p = self.logdir / f"serve-{time.strftime('%Y-%m-%d')}.log"
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def _serve_until_ctrl_c(self, token_env=None):
        """把 serve_forever 换成"立刻收到 Ctrl+C"，只跑启动和收尾两段。

        返回 (日志文本, 那一次用的令牌值)：随机那路也用构造值顶掉 resolve_token，
        这样"令牌值不能进日志"才真的被测到，而不是拿每次现编的 uuid 自证。
        """
        secret = token_env or "RANDOM-TOKEN-SENTINEL"
        env = {k: v for k, v in os.environ.items() if k != server.TOKEN_ENV}
        if token_env:
            env[server.TOKEN_ENV] = token_env
        httpd = mock.Mock()
        httpd.serve_forever.side_effect = KeyboardInterrupt
        with mock.patch.object(server, "ThreadingHTTPServer", return_value=httpd), \
             mock.patch.object(server, "resolve_token", return_value=secret), \
             mock.patch.dict(os.environ, env), \
             mock.patch.multiple(server.Handler, token=None, manager=None), \
             redirect_stdout(io.StringIO()):
            server.serve(port=8765, open_browser=False)
        return self._today(), secret

    def test_startup_and_shutdown_record_pid_and_port(self):
        text, _ = self._serve_until_ctrl_c()
        self.assertIn(f"启动：监听 127.0.0.1:8765 pid {os.getpid()}", text)
        self.assertIn(f"停止：127.0.0.1:8765 pid {os.getpid()}", text)

    def test_random_token_is_described_never_written(self):
        """记"这次是随机令牌"就够了，令牌值本身不能进日志文件。"""
        text, secret = self._serve_until_ctrl_c()
        self.assertIn("随机（外部程序连不上）", text)
        self.assertNotIn(secret, text)

    def test_fixed_token_is_labelled_as_fixed(self):
        text, secret = self._serve_until_ctrl_c(token_env="SENTINEL-TOKEN-VALUE")
        self.assertIn("令牌固定", text)
        self.assertNotIn(secret, text)

    def test_port_clash_leaves_a_trace(self):
        """抢端口失败的那一次也得有痕迹：光看现在谁在听，猜不出刚才谁试过第二次。"""
        with mock.patch.object(
                server, "ThreadingHTTPServer",
                side_effect=OSError("通常每个套接字地址只允许使用一次")), \
             mock.patch.dict(os.environ, {server.TOKEN_ENV: "t"}), \
             mock.patch.multiple(server.Handler, token=None, manager=None), \
             redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):
                server.serve(port=8765, open_browser=False)
        self.assertIn("启动失败：无法监听 127.0.0.1:8765", self._today())

    def test_second_server_on_the_same_port_is_refused(self):
        """Windows 上第二次 bind 同一个端口不报错，两个实例会一起听、请求随机分。

        端口锁是唯一能挡住这件事的东西，所以这里要看到它真的把第二次起服务拦下来。
        """
        holder = server._acquire_port_lock(8765)
        try:
            with mock.patch.object(server, "ThreadingHTTPServer") as bind:
                with self.assertRaises(SystemExit) as cm:
                    server.serve(port=8765, open_browser=False)
        finally:
            holder.close()        # 别留到 tearDown：临时目录删不掉比断言失败更烦
        self.assertIn("已经有一个筛选台在跑", str(cm.exception))
        bind.assert_not_called()          # 拦在 bind 之前，不是撞了端口才发现
        self.assertIn("启动失败：127.0.0.1:8765 已经有另一个筛选台在跑", self._today())

    def test_lock_is_given_back_when_the_server_stops(self):
        """服务退出后锁要还给系统，否则 Ctrl+C 一次就再也起不来了。"""
        self._serve_until_ctrl_c()
        again = server._acquire_port_lock(8765)
        self.assertIsNotNone(again)
        again.close()

    def test_unopenable_lock_file_does_not_stop_the_server(self):
        """锁文件所在的地方读写不了（U 盘拔了、目录被锁），也不能把服务挡在门外。"""
        with mock.patch.object(server.db, "DB_PATH", self.logdir / "没有这个目录" / "jobs.db"):
            self.assertIs(server._acquire_port_lock(8765), server._LOCK_SKIPPED)

    def test_unwritable_log_dir_does_not_stop_the_server(self):
        """日志位置被一个同名文件占住时服务照样得起——记不上日志是小事，起不来才是事。"""
        blocker = self.logdir / "serve"
        blocker.write_text("占位", encoding="utf-8")
        httpd = mock.Mock()
        httpd.serve_forever.side_effect = KeyboardInterrupt
        with mock.patch.object(server, "LOG_DIR", blocker), \
             mock.patch.object(server, "ThreadingHTTPServer", return_value=httpd), \
             mock.patch.multiple(server.Handler, token=None, manager=None), \
             redirect_stdout(io.StringIO()) as buf:
            server.serve(port=8765, open_browser=False)   # 不该抛
        self.assertIn("职位筛选台已启动", buf.getvalue())


class TestStatusSelfCheck(unittest.TestCase):
    """status 得说清匹配度那一列为什么一直是空的：本机自述文件 + 模型端口。

    只探 127.0.0.1 上有没有人听，不发请求体，也不碰任何凭证。
    """

    def _free_port(self) -> int:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def test_open_port_reads_as_listening(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        self.addCleanup(srv.close)
        port = srv.getsockname()[1]
        ok, where = cli._llm_listening(f"http://127.0.0.1:{port}/v1")
        self.assertTrue(ok)
        self.assertEqual(where, f"127.0.0.1:{port}")

    def test_closed_port_reads_as_not_listening(self):
        ok, _ = cli._llm_listening(f"http://127.0.0.1:{self._free_port()}/v1")
        self.assertFalse(ok)

    def _check_text(self, profile: Path, listening_port: int, missing: int) -> str:
        buf = io.StringIO()
        with mock.patch.object(cli, "ANALYSIS_PROFILE_PATH", profile), \
             mock.patch.object(cli, "LLM_BASE_URL",
                               f"http://127.0.0.1:{listening_port}/v1"), \
             mock.patch.object(cli.db, "jobs_missing_details",
                               return_value=[{"platform": "zhitong"}
                                             for _ in range(missing)]), \
             mock.patch.object(cli.db, "pending_analysis", return_value=[]), \
             redirect_stdout(buf):
            cli._analysis_check()
        return buf.getvalue()

    def test_both_missing_prerequisites_are_named(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        text = self._check_text(Path(tmp.name) / "profile.txt", self._free_port(), 25)
        self.assertIn("本机自述文件 没有写", text)
        self.assertIn("没人听", text)
        # 25 条待补 / 每天 20 条 → 向上取整 2 天，并把"别等补完"的用法给出来
        self.assertIn("待补正文 25 条", text)
        self.assertIn("要 2 天", text)
        self.assertIn("--ids", text)

    def test_ready_prerequisites_are_not_flagged(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        profile = Path(tmp.name) / "profile.txt"
        profile.write_text("Python 三年，写过爬虫和接口，期望东莞，月薪下限 8k", encoding="utf-8")
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        self.addCleanup(srv.close)
        text = self._check_text(profile, srv.getsockname()[1], 5)
        self.assertIn("本机自述文件 已就绪", text)
        self.assertIn("有人在听", text)
        self.assertNotIn("瓶颈", text)

    def test_short_profile_counts_as_empty(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        profile = Path(tmp.name) / "profile.txt"
        profile.write_text("东莞", encoding="utf-8")
        text = self._check_text(profile, self._free_port(), 1)
        self.assertIn("基本是空的", text)


class TestDigest(TempDbCase):
    """`cli.py digest`：影刀/n8n 拿它当邮件正文，所以一行一件事，且不许有需要 parse 的长句。"""

    def _out(self, days=7):
        from types import SimpleNamespace
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_digest(SimpleNamespace(days=days))
        return buf.getvalue()

    def _backdate(self, pjid, day):
        """把某条的首采时刻挪到历史上：`fetched_at` 由 save_jobs 统一赋值，只能改库。"""
        import sqlite3
        conn = sqlite3.connect(db.DB_PATH)
        try:
            conn.execute("UPDATE jobs SET fetched_at=? WHERE platform_job_id=?",
                         (f"{day} 09:00:00", pjid))
            conn.commit()
        finally:
            conn.close()   # Windows 上不关掉临时目录删不掉

    def test_repeat_collect_counts_as_one_new(self):
        db.save_jobs([make_record(platform="zhitong", platform_job_id="j1")])
        db.save_jobs([make_record(platform="zhitong", platform_job_id="j1",
                                  salary_raw="18-25K")])
        text = self._out()
        self.assertIn("库里共 1 条", text)
        self.assertIn("今日新增 1 条", text)

    def test_empty_days_are_still_listed(self):
        from datetime import datetime, timedelta
        db.save_jobs([make_record(platform="zhitong", platform_job_id="j1")])
        self._backdate("j1", (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d"))
        text = self._out(days=3)
        self.assertIn("今日新增 0 条", text)
        for i in (0, 1, 2):
            self.assertIn((datetime.now() - timedelta(days=i)).strftime("%m-%d"), text)
        # 今天一条没新增就别硬凑一行"今日分平台"
        self.assertNotIn("今日分平台", text)

    def test_unreachable_login_is_named_and_left_out_of_the_day_estimate(self):
        """待补正文里混着登录过期平台的行时，"补完约 N 天"是句假话：那批一条都抓不到。"""
        _expired_auth(self)
        db.save_jobs([make_record(platform="boss", platform_job_id=f"b{i}")
                      for i in range(25)])
        db.save_jobs([make_record(platform="zhitong", platform_job_id=f"j{i}")
                      for i in range(25)])
        text = self._out()
        self.assertIn("待补正文 50 条", text)
        self.assertIn("能补的 25 条约 2 天", text)
        self.assertIn("Boss直聘 25 条卡在登录凭证已过期 20.0 天", text)

    def test_today_split_is_ordered_by_count(self):
        db.save_jobs([make_record(platform="zhitong", platform_job_id="a"),
                      make_record(platform="zhitong", platform_job_id="b"),
                      make_record(platform="boss", platform_job_id="c")])
        self.assertIn("今日分平台：智通直聘 2 / Boss直聘 1", self._out())

    def test_days_capped_at_thirty(self):
        db.save_jobs([make_record(platform="zhitong", platform_job_id="j1")])
        self.assertIn("近 30 天新增", self._out(days=999))

    def test_no_scores_says_so_instead_of_inventing_a_row(self):
        db.save_jobs([make_record(platform="zhitong", platform_job_id="j1")])
        text = self._out()
        self.assertIn("匹配度：还没跑过分析", text)
        self.assertNotIn("已分析", text)
        self.assertFalse([ln for ln in text.splitlines() if ln.startswith("  ")])

    def test_top_three_in_desc_order_and_unscored_stays_out(self):
        for pid, score in (("j1", 61), ("j2", 88), ("j3", 73.5), ("j4", 40)):
            db.save_jobs([make_record(platform="zhitong", platform_job_id=pid,
                                      title=f"职位{pid}", company=f"公司{pid}")])
            db.save_analysis({"platform": "zhitong", "platform_job_id": pid, "score": score})
        db.save_jobs([make_record(platform="zhitong", platform_job_id="j9",
                                  title="没分析过的", company="公司j9")])
        text = self._out()
        self.assertIn("已分析 4 条，最高 3 条", text)
        picked = [ln for ln in text.splitlines() if ln.startswith("  ")]
        self.assertEqual(len(picked), 3)
        self.assertTrue(picked[0].startswith("  88分 职位j2 · 公司j2"), picked[0])
        self.assertIn("73.5分", picked[1])       # 一位小数别丢
        self.assertIn("61分", picked[2])         # 88.0 那种要写成 88，不拖 .0
        self.assertNotIn("职位j4", text)
        self.assertNotIn("没分析过的", text)

    def test_empty_db_tells_you_what_to_run(self):
        text = self._out()
        self.assertIn("库里 0 条", text)
        self.assertIn("cli.py collect", text)

    def test_old_db_without_the_extra_tables_still_prints(self):
        """老库没建 job_details/job_analysis：digest 得自己补上，不能拿 OperationalError 当摘要。"""
        import sqlite3
        db.save_jobs([make_record(platform="zhitong", platform_job_id="j1")])
        conn = sqlite3.connect(db.DB_PATH)
        try:
            conn.execute("DROP TABLE job_details")
            conn.execute("DROP TABLE job_analysis")
            conn.commit()
        finally:
            conn.close()
        text = self._out()
        self.assertIn("库里共 1 条", text)
        self.assertIn("待补正文 1 条", text)
        self.assertIn("匹配度：还没跑过分析", text)


def zt_item(jid="12406875", poskey="TdFLo0K2", extra_tags=(), **over):
    """按智通真实结构造一条职位：<li data-*...> + 内部展示元素。"""
    attrs = {
        "id": jid, "positionkey": poskey, "urlcityen": "dongguan", "cityname": "东莞",
        "townname": "", "posname": "修模师傅", "comname": "东莞市利松五金科技有限公司",
        "first": "生产制造", "second": "模具", "reqdegreestr": "中专",
        "reqworkyear": "经验5年及以上", "minsalary": "10000", "maxsalary": "11000",
    }
    attrs.update(over)
    tag = "<li " + " ".join(f'data-{k}="{v}"' for k, v in attrs.items()) + \
          ' class="position pos-item PosName">'
    return (tag +
            '<div class="pos-item-top flex_Ac"><div class="salary">' +
            (over.get("shown_salary") or "10-11K") + "</div>"
            '<div class="info-tags flex_Ac">'
            '<div class="tag-list-item">' + (attrs["cityname"] + "-长安镇") + "</div>"
            '<div class="tag-list-item">' + attrs["reqdegreestr"] + "</div>"
            + "".join(f'<div class="tag-list-item">{t}</div>' for t in extra_tags)
            + '</div>'
            '<p class="pos-welfare" title="五险,包吃,包住">五险、包吃、包住</p>'
            "</div></li>")


def zt_page(*items):
    return "<html><ul>" + "".join(items) + "</ul></html>"


class TestZhitongParse(unittest.TestCase):
    def setUp(self):
        from jobstats.collectors import zhitong
        self.mod = zhitong

    def test_parses_fields_from_data_attrs(self):
        recs = self.mod.parse_page(zt_page(zt_item()))
        self.assertEqual(len(recs), 1)
        r = recs[0]
        self.assertEqual(r["platform"], "zhitong")
        self.assertEqual(r["platform_job_id"], "12406875")
        self.assertEqual(r["title"], "修模师傅")
        self.assertEqual(r["company"], "东莞市利松五金科技有限公司")
        self.assertEqual((r["salary_min"], r["salary_max"]), (10.0, 11.0))
        self.assertEqual(r["city"], "东莞")
        self.assertEqual(r["district"], "长安镇")
        self.assertEqual(r["industry"], "生产制造-模具")
        self.assertEqual(r["welfare"], ["五险", "包吃", "包住"])
        self.assertEqual(r["url"], "https://www.job5156.com/dongguan/job_TdFLo0K2")
        self.assertLessEqual(set(r), set(config.ALLOWED_FIELDS))

    def test_townname_attr_wins_over_tag(self):
        r = self.mod.parse_page(zt_page(zt_item(townname="松山湖")))[0]
        self.assertEqual(r["district"], "松山湖")

    def test_company_scale_comes_from_the_tags(self):
        """规模/性质跟镇区挤在同一串标签里，以前 company_scale 一直写死空串。"""
        for tag in ("1-100人", "200-500人", "2000以上人", "1000人以上"):
            r = self.mod.parse_page(
                zt_page(zt_item(extra_tags=[tag, "民营", "仪器仪表"])))[0]
            self.assertEqual(r["company_scale"], tag)

    def test_other_tags_are_not_mistaken_for_the_size(self):
        r = self.mod.parse_page(zt_page(zt_item(extra_tags=["民营", "仪器仪表"])))[0]
        self.assertEqual(r["company_scale"], "")
        self.assertEqual(r["district"], "长安镇")

    def test_negotiable_salary_has_no_numbers(self):
        body = zt_page(zt_item(shown_salary="面议", minsalary="", maxsalary=""))
        r = self.mod.parse_page(body)[0]
        self.assertEqual(r["salary_raw"], "面议")
        self.assertIsNone(r["salary_min"])
        self.assertIsNone(r["salary_max"])

    def test_only_li_items_count(self):
        body = ('<div class="position pos-item PosName"></div>'
                + zt_page(zt_item(jid="1"), zt_item(jid="2", poskey="B")))
        self.assertEqual(len(self.mod.parse_page(body)), 2)

    def test_item_without_id_or_title_is_dropped(self):
        self.assertEqual(self.mod.parse_page(zt_page(zt_item(posname=""))), [])

    def test_collect_stops_when_page_adds_nothing(self):
        pages = [zt_page(zt_item(jid="1", posname="IT工程师"),
                         zt_item(jid="2", poskey="B", posname="IT运维")),
                 zt_page(zt_item(jid="3", poskey="C", posname="IT专员")),
                 zt_page(zt_item(jid="3", poskey="C", posname="IT专员"))]
        with mock.patch.object(self.mod, "_fetch", side_effect=pages) as fetch, \
             mock.patch.object(self.mod, "check_daily_cap", return_value=200), \
             mock.patch.object(self.mod, "polite_sleep"):
            recs = self.mod.collect("IT", "东莞", max_pages=5)
        self.assertEqual(len(recs), 3)
        self.assertEqual(fetch.call_count, 3)

    def test_whole_page_blocked_by_gate_keeps_paging(self):
        """整页都被相关性闸门拦下 != 翻到头：否则下一页真正对口的岗位就永远拿不到。"""
        pages = [zt_page(zt_item(jid="1", posname="修模师傅"),
                         zt_item(jid="2", poskey="B", posname="装卸搬运工")),
                 zt_page(zt_item(jid="3", poskey="C", posname="IT系统工程师"),
                         zt_item(jid="4", poskey="D", posname="IT运维")),
                 zt_page(zt_item(jid="3", poskey="C", posname="IT系统工程师"),
                         zt_item(jid="4", poskey="D", posname="IT运维"))]
        with mock.patch.object(self.mod, "_fetch", side_effect=pages) as fetch, \
             mock.patch.object(self.mod, "check_daily_cap", return_value=200), \
             mock.patch.object(self.mod, "polite_sleep"):
            recs = self.mod.collect("IT", "东莞", max_pages=5)
        self.assertEqual([r["title"] for r in recs], ["IT系统工程师", "IT运维"])
        self.assertEqual(fetch.call_count, 3)      # 第 3 页才是真的翻到头

    def test_pinned_ads_never_reach_the_database(self):
        """实测智通把焊工/CNC编程这类推广位置在列表最前，data-top="true" 是唯一判据。"""
        body = zt_page(zt_item(jid="1", posname="焊工", top="true"),
                       zt_item(jid="2", poskey="B", posname="IT工程师", top="false"),
                       zt_item(jid="3", poskey="C", posname="IT专员"))
        recs = self.mod.parse_page(body)
        self.assertEqual([r["title"] for r in recs], ["IT工程师", "IT专员"])

    def _collect_once(self, body):
        buf = io.StringIO()
        with mock.patch.object(self.mod, "_fetch", return_value=body), \
             mock.patch.object(self.mod, "check_daily_cap", return_value=200), \
             mock.patch.object(self.mod, "polite_sleep"), \
             redirect_stdout(buf):
            recs = self.mod.collect("IT", "东莞", max_pages=3)
        return recs, buf.getvalue()

    def test_zero_rows_with_cards_on_page_is_reported_as_structure_change(self):
        """<li class="pos-item"> 还在但 data-* 没了 = 平台改版。说成"风控或无结果"
        会让人去重扫码、隔天再试，而真正要改的是选择器。"""
        body = ('<html><ul><li class="position pos-item PosName">'
                '<div class="salary">10-11K</div></li></ul></html>')
        recs, out = self._collect_once(body)
        self.assertEqual(recs, [])
        self.assertIn("结构变了", out)
        self.assertIn("1 个职位条目", out)
        self.assertIn("不是没结果也不是风控", out)

    def test_empty_page_says_no_items_not_structure(self):
        recs, out = self._collect_once(zt_page())
        self.assertEqual(recs, [])
        self.assertIn("没有职位条目", out)
        self.assertNotIn("结构变了", out)

    def test_all_pinned_page_is_called_pinned(self):
        """整页都是置顶推广位时不该喊"结构变了"——解析器工作正常，只是没结果。"""
        body = zt_page(zt_item(jid="1", top="true"), zt_item(jid="2", poskey="B", top="true"))
        recs, out = self._collect_once(body)
        self.assertEqual(recs, [])
        self.assertIn("置顶推广位", out)
        self.assertNotIn("结构变了", out)

    def test_a_fully_gated_page_stops_repeating_itself(self):
        """整页被闸门拦下时不该继续翻：这些编号已经"见过"了，翻下去只会拿到同一批。

        以前只按"有没有进库的新 id"判断，于是一个城市全不符的关键词能把 20 页翻满，
        一页不入库还一直请求平台。
        """
        body = zt_page(zt_item(jid="1", cityname="惠州"),
                       zt_item(jid="2", poskey="B", cityname="惠州"))
        fetch = mock.Mock(return_value=body)
        with mock.patch.object(self.mod, "_fetch", fetch), \
             mock.patch.object(self.mod, "check_daily_cap", return_value=200), \
             mock.patch.object(self.mod, "polite_sleep"), \
             redirect_stdout(io.StringIO()):
            recs = self.mod.collect("IT", "东莞", max_pages=6)
        self.assertEqual(recs, [])
        self.assertEqual(fetch.call_count, 2)     # 第 2 页还是那批编号，停

    def test_on_page_saves_every_page_separately(self):
        seen_batches = []
        fetch = mock.Mock(side_effect=[
            zt_page(zt_item(jid="1", posname="IT工程师")),
            zt_page(zt_item(jid="2", poskey="B", posname="IT运维"))])
        with mock.patch.object(self.mod, "_fetch", fetch), \
             mock.patch.object(self.mod, "check_daily_cap", return_value=200), \
             mock.patch.object(self.mod, "polite_sleep"), \
             redirect_stdout(io.StringIO()):
            recs = self.mod.collect("IT", "东莞", max_pages=2,
                                    on_page=seen_batches.append)
        self.assertEqual([len(b) for b in seen_batches], [1, 1])
        self.assertEqual(len(recs), 2)

    def test_fetch_error_keeps_what_was_collected(self):
        with mock.patch.object(self.mod, "_fetch",
                              side_effect=[zt_page(zt_item(jid="1", posname="IT工程师")),
                                           OSError("连接重置")]), \
             mock.patch.object(self.mod, "check_daily_cap", return_value=200), \
             mock.patch.object(self.mod, "polite_sleep"):
            recs = self.mod.collect("IT", "东莞", max_pages=5)
        self.assertEqual(len(recs), 1)

    def test_page_two_uses_pn_param(self):
        self.assertIn("pn=2", self.mod._search_url("IT", "东莞", 2))
        self.assertNotIn("pn=1", self.mod._search_url("IT", "东莞", 1))
        self.assertNotIn("city=", self.mod._search_url("IT", "全国", 1))


class TestJob51SearchApi(unittest.TestCase):
    """前程无忧的正文取自平台自己的搜索响应：不额外发请求，也不碰详情页的 WAF。"""

    @staticmethod
    def _resp(payload, status=200):
        r = mock.Mock()
        r.status = status
        r.json.return_value = payload
        return r

    def test_maps_job_id_to_describe_and_href(self):
        from jobstats.collectors import job51
        payload = {"resultbody": {"job": {"items": [
            {"jobId": 11, "jobDescribe": " 岗位职责 ",
             "jobHref": "https://jobs.51job.com/a/11.html?req=x"},
            {"jobId": "12", "jobDescribe": "",
             "jobHref": "https://jobs.51job.com/a/12.html"},
        ]}}}
        got = job51._search_api_jobs([self._resp(payload)])
        self.assertEqual(set(got), {"11", "12"})
        self.assertEqual(got["11"]["jobDescribe"].strip(), "岗位职责")
        # 调用方会把追踪参数切掉，这里只保证原始 jobHref 拿到了
        self.assertIn("jobs.51job.com/a/11.html", got["11"]["jobHref"])

    def test_error_or_unreadable_response_is_ignored(self):
        from jobstats.collectors import job51
        broken = mock.Mock(status=200)
        broken.json.side_effect = ValueError("bad json")
        self.assertEqual(job51._search_api_jobs(
            [self._resp({}, status=500), broken, self._resp({"resultbody": None})]), {})


class TestCollectAndSave(TempDbCase):
    """collect_and_save：支持每页一存的通道边采边存，不支持的照旧整轮存一次。"""

    def _pages_mod(self, batches, boom_at=None, drops=None):
        """像智通/前程无忧那样：采集器里自己过闸门，每页把累计的被拦明细交回来。"""
        mod = mock.Mock()
        mod.PER_PAGE_SAVE = True
        mod.SELF_GATES = True          # 前程无忧/智通那样，采集器里已经判过

        def collect(query, city, max_pages, on_page, on_drop=None):
            handed: dict[str, list[str]] = {}
            for i, batch in enumerate(batches):
                on_page(batch)
                for title in (drops or {}).get(i, []):
                    handed.setdefault("平台职类不是这个方向", []).append(title)
                if on_drop:
                    on_drop(handed)   # 交的是全量累计，交两次也不该翻倍
                if boom_at is not None and i >= boom_at:
                    raise RuntimeError("下一页崩了")
            return [r for b in batches for r in b]

        mod.collect.side_effect = collect
        return mod

    def _whole_run_mod(self, records):
        mod = mock.Mock()
        mod.PER_PAGE_SAVE = False                 # Boss 那条通道的样子
        mod.SELF_GATES = False                    # 而且它自己不过闸门

        def collect(query, city, max_pages):
            return records

        mod.collect.side_effect = collect
        return mod

    def test_every_page_lands_in_the_database(self):
        mod = self._pages_mod([[make_record(platform_job_id="j1")],
                               [make_record(platform_job_id="j2")]])
        records, st = collectors.collect_and_save(mod, "IT", "东莞", 2)
        self.assertEqual(len(records), 2)
        self.assertEqual(st["added"], 2)
        self.assertEqual(len(db.fetch_all()), 2)

    def test_a_crash_keeps_the_pages_already_done(self):
        """这条就是"每页一存"存在的理由：第 2 页之后崩了，第 1、2 页不该白采。"""
        mod = self._pages_mod([[make_record(platform_job_id="j1")],
                               [make_record(platform_job_id="j2")],
                               [make_record(platform_job_id="j3")]], boom_at=1)
        with self.assertRaises(RuntimeError):
            collectors.collect_and_save(mod, "IT", "东莞", 3)
        self.assertEqual(sorted(r["platform_job_id"] for r in db.fetch_all()),
                         ["j1", "j2"])

    def test_channel_without_page_saving_is_still_saved(self):
        mod = self._whole_run_mod([make_record(platform_job_id="j1")])
        records, st = collectors.collect_and_save(mod, "Python开发", "深圳", 1)
        self.assertEqual(st["added"], 1)
        self.assertEqual(len(db.fetch_all()), 1)

    def test_repeated_job_across_pages_counts_as_refresh(self):
        mod = self._pages_mod([[make_record(platform_job_id="j1")],
                               [make_record(platform_job_id="j1", salary_raw="30-40K")]])
        _, st = collectors.collect_and_save(mod, "IT", "东莞", 2)
        self.assertEqual((st["added"], st["refreshed"]), (1, 1))
        self.assertEqual(len(db.fetch_all()), 1)

    def test_channel_without_gate_is_filtered_before_writing(self):
        """Boss 那条通道唯一没自己过闸门的：落库前得把不对的挡在库外。"""
        bad_city = make_record(platform_job_id="j2", city="惠州")
        substring = make_record(platform_job_id="j3", title="TITOK运营专员", city="东莞")
        good = make_record(platform_job_id="j4", title="IT运维工程师", city="东莞")
        mod = self._whole_run_mod([good, bad_city, substring])
        records, st = collectors.collect_and_save(mod, "IT", "东莞", 1)
        self.assertEqual(len(records), 3, "采到的原始条数不该被闸门改写")
        self.assertEqual(st["dropped"], 2)
        self.assertEqual(st["added"], 1)
        self.assertEqual([r["platform_job_id"] for r in db.fetch_all()], ["j4"])

    def test_list_page_description_goes_to_details_not_jobs(self):
        """前程无忧搜索接口自带的正文随每页写入 job_details：
        jobs 表的白名单不收这个键，而且它不占逐条抓正文的额度。"""
        rec = make_record(platform_job_id="g1", _description="岗位职责\n1、维护 ERP")
        mod = self._pages_mod([[rec]])
        records, st = collectors.collect_and_save(mod, "IT", "东莞", 1)
        self.assertEqual(st["added"], 1)
        self.assertNotIn("_description", records[0])
        self.assertEqual(db.get_detail("job51", "g1")["description"], "岗位职责\n1、维护 ERP")
        self.assertEqual(db.count_details_today(), 0)

    def test_channel_that_self_gates_is_not_judged_twice(self):
        """自己声明 SELF_GATES 的通道，公共落库口不能再判一遍——
        否则同一批记录被两套口径过滤，页面上剔掉的条数会对不上采集器打印的。"""
        mod = self._pages_mod([[make_record(platform_job_id="j1", city="惠州")]])
        _, st = collectors.collect_and_save(mod, "IT", "东莞", 1)
        self.assertEqual(st["dropped"], 0)
        self.assertEqual(st["added"], 1)

    def test_wildcard_city_probe_is_not_filtered(self):
        """传了「全国」就是想看全部，闸门不该按城市拦（关键词规则照旧生效）。"""
        mod = self._whole_run_mod([make_record(platform_job_id="j1", city="贺州",
                                               title="IT支持")])
        _, st = collectors.collect_and_save(mod, "IT", "全国", 1)
        self.assertEqual((st["dropped"], st["added"]), (0, 1))

    def test_central_gate_leaves_the_blocked_titles_in_the_log(self):
        """被拦的标题得跟原因一起留痕：闸门在入库之前判，库里没这些行，
        光有个数字没法复核是不是误杀。"""
        mod = self._whole_run_mod([
            make_record(platform_job_id="j1", city="东莞", title="IT运维工程师"),
            make_record(platform_job_id="j2", city="东莞", title="锅炉操作工")])
        collectors.collect_and_save(mod, "IT运维", "东莞", 1)
        r = db.recent_collect_runs()[0]
        self.assertEqual(r["drop_reasons"], {"标题像一线操作岗": 1})
        self.assertEqual(r["drop_titles"], {"标题像一线操作岗": ["锅炉操作工"]})

    def test_self_gated_channel_hands_its_detail_to_the_log(self):
        """自判通道不经过公共落库口，明细只能由 on_drop 交回来；
        每页交的是全量累计，并两次不能把条数翻倍。"""
        mod = self._pages_mod([[make_record(platform_job_id="j1")],
                               [make_record(platform_job_id="j2")]],
                              drops={0: ["污水处理操作员"], 1: ["光伏运维1"]})
        _, st = collectors.collect_and_save(mod, "IT运维", "东莞", 2)
        r = db.recent_collect_runs()[0]
        self.assertEqual(r["drop_titles"],
                         {"平台职类不是这个方向": ["污水处理操作员", "光伏运维1"]})
        self.assertEqual(r["drop_reasons"], {"平台职类不是这个方向": 2})
        # 回执里的条数也得以明细为准，否则页面上"闸门剔掉 0 条"会跟采集器打印的对不上
        self.assertEqual(st["dropped"], 2)

    def test_run_is_recorded_with_query_and_drop_reasons(self):
        """命令行那条路径没有页面任务日志，事后全靠 collect_runs 这一行复盘。"""
        mod = self._whole_run_mod([
            make_record(platform_job_id="j1", city="东莞", title="IT运维工程师"),
            make_record(platform_job_id="j2", city="惠州", title="IT运维工程师")])
        collectors.collect_and_save(mod, "IT", "东莞", 2)
        rows = db.recent_collect_runs()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["query"], r["city"], r["pages"]), ("IT", "东莞", 2))
        self.assertEqual((r["collected"], r["added"], r["dropped"]), (2, 1, 1))
        self.assertEqual(r["drop_reasons"], {"城市不符": 1})
        self.assertIsNone(r["error"])

    def test_failed_run_still_leaves_a_record(self):
        """崩了也要留痕：那 5 页翻到一半失败，至少得查得到当时搜的是什么词。"""
        mod = mock.Mock()
        mod.PER_PAGE_SAVE = False
        mod.SELF_GATES = False
        mod.collect.side_effect = RuntimeError("登录态失效")
        with self.assertRaises(RuntimeError):
            collectors.collect_and_save(mod, "IT", "东莞", 1)
        r = db.recent_collect_runs()[0]
        self.assertEqual(r["error"], "登录态失效")
        self.assertEqual(r["collected"], 0)


class TestBlockRules(TempDbCase):
    """屏蔽规则：一条规则挡掉一整类，省得「仓库管理员」换家公司再出现一次。"""

    def test_keyword_rule_stops_the_row_before_it_lands(self):
        db.add_block_rule("keyword", "仓库")
        st = db.save_stats([make_record(platform_job_id="j1", title="仓库管理员"),
                            make_record(platform_job_id="j2", title="IT运维工程师")])
        self.assertEqual((st["added"], st["rules"]), (1, 1))
        self.assertEqual([r["platform_job_id"] for r in db.fetch_all()], ["j2"])

    def test_keyword_match_ignores_case(self):
        db.add_block_rule("keyword", "python")
        st = db.save_stats([make_record(platform_job_id="j1", title="Python开发工程师")])
        self.assertEqual((st["added"], st["rules"]), (0, 1))

    def test_company_rule_is_exact_match_not_substring(self):
        """公司名整串相等才拦：「某某科技」不该顺手把「某某科技有限公司」也挡掉，
        那是两家公司，误杀了就成了永久盲区。"""
        db.add_block_rule("company", "某某科技")
        st = db.save_stats([make_record(platform_job_id="j1", company="某某科技"),
                            make_record(platform_job_id="j2", company="某某科技有限公司")])
        self.assertEqual((st["added"], st["rules"]), (1, 1))
        self.assertEqual([r["platform_job_id"] for r in db.fetch_all()], ["j2"])

    def test_dropping_a_rule_lets_rows_in_again(self):
        db.add_block_rule("keyword", "维修")
        self.assertEqual(db.save_stats([make_record(title="打印机维修")])["added"], 0)
        self.assertTrue(db.remove_block_rule("keyword", "维修"))
        self.assertEqual(db.save_stats([make_record(title="打印机维修")])["added"], 1)

    def test_rule_beats_refresh_of_an_existing_row(self):
        """已经在库里的职位，加了规则之后再采一次不该把它刷新一遍——拦下来就是不碰。"""
        rec = make_record(platform_job_id="j1", title="仓库管理员")
        db.save_stats([rec])
        db.add_block_rule("keyword", "仓库")
        st = db.save_stats([dict(rec, salary_raw="30-40K")])
        self.assertEqual((st["added"], st["refreshed"], st["rules"]), (0, 0, 1))

    def test_adding_the_same_rule_twice_is_not_an_error(self):
        self.assertTrue(db.add_block_rule("keyword", "仓库"))
        self.assertFalse(db.add_block_rule("keyword", "仓库"))
        self.assertEqual(len(db.block_rule_list()), 1)

    def test_bad_kind_or_empty_value_is_rejected(self):
        with self.assertRaises(ValueError):
            db.add_block_rule("title", "仓库")
        with self.assertRaises(ValueError):
            db.add_block_rule("keyword", "   ")

    def test_cli_adds_lists_and_drops_rules(self):
        from types import SimpleNamespace
        buf = io.StringIO()
        args = SimpleNamespace(company="某某科技", keyword="仓库 普工", drop=None, clear=None)
        with redirect_stdout(buf):
            cli.cmd_blocked(args)
        self.assertIn("已加上屏蔽规则：公司「某某科技」", buf.getvalue())
        self.assertIn("关键词「普工」", buf.getvalue())

        buf2 = io.StringIO()
        with redirect_stdout(buf2):
            cli.cmd_blocked(SimpleNamespace(company=None, keyword=None,
                                           drop="关键词:仓库", clear=None))
        out = buf2.getvalue()
        self.assertIn("已放掉规则：关键词「仓库」", out)
        # 加了 1 条公司 + 2 个关键词，放掉「仓库」后还剩 2 条
        self.assertIn("剩下的规则：2 条", out)

        buf3 = io.StringIO()
        with redirect_stdout(buf3):
            cli.cmd_blocked(SimpleNamespace(company=None, keyword=None,
                                           drop="仓库", clear=None))
        self.assertIn("格式得是", buf3.getvalue())


class TestRelevanceGate(unittest.TestCase):
    """相关性闸门：用例照着两批真实返回写——2026-10-12 的 IT@东莞，
    2026-10-17 的 IT运维@东莞（智通 41 条，其中 11 条是锅炉操作工这类跨行岗位）。"""

    def rec(self, title, city="东莞", skills=None, industry="", platform="zhitong"):
        return {"platform": platform, "platform_job_id": "x", "title": title,
                "city": city, "skills": skills or [], "industry": industry}

    def test_short_keyword_must_stand_alone(self):
        keep = ["IT工程师（SRM经验）", "IT运维工程师", "硬件IT储干（非东莞上班）",
                "副总经理助理（ai/IT方向）", "IT 应用系统工程师"]
        drop = ["TITOK运营", "FIT模师傅", "外贸开发专员（furniture）", "洗水杂工", "叉车司机"]
        for t in keep:
            self.assertTrue(relevance.keyword_ok(self.rec(t), "IT"), t)
        for t in drop:
            self.assertFalse(relevance.keyword_ok(self.rec(t), "IT"), t)

    def test_keyword_can_come_from_skill_tags_and_tags_are_more_forgiving(self):
        # 标题按词边界，技能标签按子串：ITIL 是真相关的运维缩写，不该被误杀
        self.assertTrue(relevance.keyword_ok(self.rec("运维工程师", skills=["IT运维"]), "IT"))
        self.assertTrue(relevance.keyword_ok(self.rec("运维工程师", skills=["ITIL"]), "IT"))
        self.assertFalse(relevance.keyword_ok(self.rec("运维工程师", skills=["仓储"]), "IT"))

    def test_long_keywords_are_left_to_the_platform(self):
        # python / 软件开发 这类长词不做字面判断：标题没写语言的岗位也可能是对的
        for q in ("python", "Java", "软件开发", "数据分析", "web前端"):
            self.assertTrue(relevance.keyword_ok(self.rec("后端开发工程师"), q), q)

    def test_mixed_keyword_hits_by_chinese_two_gram(self):
        """'IT运维' 这种中英混写的关键词：字母部分按词边界、中文部分按二字组算命中。"""
        for t in ("IT运维工程师", "IT硬件运维", "运维工程师", "桌面运维技术员", "服务器运维"):
            self.assertTrue(relevance.query_hits(self.rec(t), "IT运维"), t)
        for t in ("锅炉操作工", "物业项目负责人", "技术员助理"):
            self.assertFalse(relevance.query_hits(self.rec(t), "IT运维"), t)
        # 二字组是为了不误杀：查询写"软件开发"，标题写"后端开发工程师"也算命中
        self.assertTrue(relevance.query_hits(self.rec("后端开发工程师"), "软件开发"))
        self.assertTrue(relevance.query_hits(self.rec("大数据工程师"), "数据分析"))
        self.assertTrue(relevance.query_hits(self.rec("运维工程师", skills=["IT"]), "IT运维"))

    def test_family_rule_needs_the_channel_to_opt_in(self):
        """智通的 industry 是「一级行业-二级职类」，第二段是职能，可以拿来判方向；
        没声明 FAMILY_GATE 的通道不判——Boss 那个字段装的是公司行业。"""
        rec = self.rec("技术员助理", industry="生产制造-智能制造")
        self.assertEqual(relevance.reject_reason(rec, "IT运维", "东莞"), None)
        self.assertEqual(relevance.reject_reason(rec, "IT运维", "东莞", family=True),
                         "平台职类不是这个方向")

    def test_family_rule_passes_it_roles_whose_title_lacks_the_keyword(self):
        """标题不带 IT/运维、但平台标的职类是技术方向的，得放行：
        2026-10-17 那页实测有 程序员、资讯主管、ERP实施工程师、系统架构师。"""
        for title, industry in (("程序员", "AI/互联网-运维/技术支持"),
                                ("资讯主管", "AI/互联网-技术项目管理"),
                                ("ERP实施工程师", "AI/互联网-技术项目管理"),
                                ("IT主管", "电子/电气/通信-通信"),
                                ("电工/IT", "生产制造-技工"),
                                ("IT兼ERP专员", "管培生/非企业从业者-社工/兼职")):
            rec = self.rec(title, industry=industry)
            self.assertIsNone(relevance.reject_reason(rec, "IT运维", "东莞", family=True),
                              f"{title} / {industry}")

    def test_company_industry_alone_is_never_a_family(self):
        """Boss 写的是公司行业（没有"一级-二级"那个连字符），拿它判方向会误杀：
        10-13 实测"园区弱电IT运维工程师"标的是环保。"""
        for title in ("园区弱电IT运维工程师", "IT运维工程师"):
            rec = self.rec(title, industry="环保", platform="boss")
            self.assertEqual(relevance.job_family(rec), "")
            self.assertIsNone(relevance.reject_reason(rec, "IT运维", "东莞", family=True))

    def test_cross_field_ops_titles_are_cut_even_when_the_keyword_hits(self):
        """光伏运维、污水运维：字面命中"运维"，但一眼不是 IT。实测拦得住。"""
        for title, industry in (("光伏运维1", "管培生/非企业从业者-社工/兼职"),
                                ("污水处理操作员", "能源/环保/农业-环保"),
                                ("空调维修工", "生产制造-技工"),
                                ("锅炉操作工", "生产制造-技工")):
            rec = self.rec(title, industry=industry)
            self.assertEqual(relevance.reject_reason(rec, "IT运维", "东莞", family=True),
                             "标题像一线操作岗", title)

    def test_a_marketing_title_is_cut_by_the_family_rule_not_the_word_list(self):
        """TITOK运营 那种是"方向不对"，不是"蓝岗"：原因得说准，不然复核时会去找词表。"""
        rec = self.rec("TITOK运营", industry="客服/运营-内容运营")
        self.assertEqual(relevance.reject_reason(rec, "IT运维", "东莞", family=True),
                         "平台职类不是这个方向")

    def test_one_welfare_word_in_the_title_is_not_evidence_enough(self):
        """厂里 IT 也包吃住、也兼司机——单个说法不拦；两个一起出现才拦。
        这四条标题全是库里真实采到的。"""
        keep = ("IT工程师（包吃住）", "IT技术支持兼行政司机",
                "IT专员/IT主管（包吃住+双休+五险一金）")
        for title in keep:
            rec = self.rec(title, industry="AI/互联网-运维/技术支持", platform="boss")
            self.assertFalse(relevance.blue_hit(rec, "IT运维"), title)
        rec = self.rec("7000-9000技术员包吃住 五险一金长白班运维", platform="boss")
        self.assertTrue(relevance.blue_hit(rec, "IT运维"))

    def test_blocked_titles_are_listed_for_review(self):
        dropped = {"平台职类不是这个方向": ["技术员助理", "体系工程师"],
                   "标题像一线操作岗": ["锅炉操作工"]}
        self.assertEqual(relevance.count(dropped), 3)
        self.assertEqual(relevance.summarize(dropped),
                         "平台职类不是这个方向 2 条、标题像一线操作岗 1 条")
        self.assertIn("技术员助理（平台职类不是这个方向）", relevance.titles(dropped))
        self.assertIn("…共 3 条", relevance.titles(dropped, limit=2))
        acc: dict[str, list[str]] = {}
        relevance.merge(dropped, acc)      # 逐页并：每页交的是本页新增的
        relevance.merge({"城市不符": ["IT主管"]}, acc)
        self.assertEqual(relevance.count(acc), 4)
        self.assertEqual(acc["平台职类不是这个方向"], ["技术员助理", "体系工程师"])

    def test_city_rule_follows_the_command_not_the_platform(self):
        self.assertEqual(relevance.reject_reason(self.rec("IT工程师", city="惠州"),
                                                 "IT", "东莞"), "城市不符")
        self.assertEqual(relevance.reject_reason(self.rec("IT工程师", city="贺州"),
                                                 "IT", "东莞市"), "城市不符")
        self.assertIsNone(relevance.reject_reason(self.rec("IT工程师", city="东莞市"),
                                                  "IT", "东莞"))
        # 全国/不传城市 = 不按城市拦
        self.assertIsNone(relevance.reject_reason(self.rec("IT工程师", city="贺州"),
                                                  "IT", "全国"))
        self.assertIsNone(relevance.reject_reason(self.rec("IT工程师", city=""),
                                                  "IT", "东莞"))

    def test_gate_counts_reasons(self):
        recs = [self.rec("IT工程师"), self.rec("TITOK运营"),
                self.rec("IT主管", city="惠州"), self.rec("洗水杂工")]
        kept, dropped = relevance.gate(recs, "IT", "东莞")
        self.assertEqual([r["title"] for r in kept], ["IT工程师"])
        self.assertEqual(dropped, {"标题没写这个关键词": ["TITOK运营", "洗水杂工"],
                                   "城市不符": ["IT主管"]})
        self.assertEqual(relevance.count(dropped), 3)
        self.assertIn("城市不符 1 条", relevance.summarize(dropped))

    def test_relaxed_query_unlocks_the_city_rule_only(self):
        """不传关键词/传全国 = 只想放宽城市，蓝岗那条词表判的是岗位本身，照样拦。"""
        kept, dropped = relevance.gate([self.rec("网络管理员", city="全国"),
                                        self.rec("IT工程师")], "", "")
        self.assertEqual(len(kept), 2)
        self.assertEqual(dropped, {})
        kept2, dropped2 = relevance.gate([self.rec("洗水杂工")], "", "")
        self.assertEqual(kept2, [])
        self.assertEqual(dropped2, {"标题像一线操作岗": ["洗水杂工"]})


ZT_DETAIL_HTML = """
<div class="main-left-box"><div class="left-common-info pos-base-info">
<ul class="keyword-tags flex_Fw">
  <li class="item-tag">ERP运维</li><li class="item-tag">PLM系统</li>
  <li class="item-tag">ERP运维</li><li class="item-tag">  </li>
</ul>
<pre class="common-text-desc pos-desc">一、岗位职责：
1. 负责 ERP&amp;OA 系统的日常运维。


2. 处理&lt;故障&gt;报修。</pre>
<pre class="common-text-desc com-desc">公司简介：本公司成立于1999年。</pre>
</div></div>
"""

# 2026-11-16 真实详情页的新写法：标签属性拆成多行（job_Y6LwN8iSB_S9dP6EtKqhag 实测）。
ZT_DETAIL_HTML_MULTILINE = """
<div class="common-text-box pos-requirement">
  <pre
    class="common-text-desc pos-desc"
  >岗位职责
1. 负责 ERP 系统运维。</pre>
  <pre
    class="common-text-desc com-desc"
  >公司简介：不应出现。</pre>
</div>
<ul
  class="keyword-tags flex_Fw"
>
  <li class="item-tag">ERP运维</li>
</ul>
"""

BOSS_DETAIL_HTML = """
<div class="job-detail-section">
  <h3>职位描述</h3>
  <ul class="job-keyword-list">
      <li>网络运维</li><li>Java</li><li>网络运维</li>
  </ul>
  <div class="job-sec-text">【岗位职责】<br/>1、负责员工电脑；<br/>
  2、负责&nbsp;网络设备。</div>
  <div class="job-boss-info"><h2 class="name">罗先生</h2></div>
</div>
"""


class TestHtmlText(unittest.TestCase):
    def test_line_breaks_kept_and_blank_runs_collapsed(self):
        self.assertEqual(
            html_text.to_lines("a<br/>b</p>c\n\n\nd   e&nbsp;f"),
            "a\nb\nc\n\nd e f")   # 段落空行留一个，全角/不间断空格当空白压掉

    def test_list_items_stripped_deduped_in_order(self):
        self.assertEqual(html_text.list_items("<li> a </li><li>b</li><li>a</li><li> </li>"),
                         ["a", "b"])


class TestDetailParsers(unittest.TestCase):
    """两家平台的正文块长得不一样，但都必须是服务端渲染的才能解析。"""

    def test_zhitong_takes_job_desc_not_company_intro(self):
        from jobstats.collectors import zhitong
        d = zhitong.parse_detail(ZT_DETAIL_HTML)
        self.assertIn("岗位职责", d["description"])
        self.assertIn("2. 处理<故障>报修。", d["description"])   # 实体已反转义
        self.assertIn("ERP&OA", d["description"])
        self.assertNotIn("公司简介", d["description"])
        self.assertEqual(d["keyword_tags"], ["ERP运维", "PLM系统"])

    def test_zhitong_page_without_desc_block_is_empty(self):
        from jobstats.collectors import zhitong
        self.assertEqual(zhitong.parse_detail("<html><body>下架</body></html>"),
                         {"description": "", "keyword_tags": []})

    def test_zhitong_multiline_tag_attributes_still_parse(self):
        """属性拆成多行也得认：2026-11-16 平台改版，旧正则只认单行写法，
        正文静默变 0 字、关键词却照常出——看起来像"职位下架"，其实是解析漏了。"""
        from jobstats.collectors import zhitong
        d = zhitong.parse_detail(ZT_DETAIL_HTML_MULTILINE)
        self.assertIn("岗位职责", d["description"])
        self.assertIn("1. 负责 ERP 系统运维。", d["description"])
        self.assertNotIn("公司简介", d["description"])
        self.assertEqual(d["keyword_tags"], ["ERP运维"])

    def test_boss_line_breaks_come_from_br_and_stop_at_container(self):
        from jobstats.collectors import boss
        d = boss.parse_detail(BOSS_DETAIL_HTML)
        self.assertEqual(d["description"].splitlines(),
                         ["【岗位职责】", "1、负责员工电脑；", "2、负责 网络设备。"])
        self.assertNotIn("公司简介", d["description"])
        self.assertNotIn("罗先生", d["description"])   # HR 信息不属于正文
        self.assertEqual(d["keyword_tags"], ["网络运维", "Java"])


class TestDetailStore(TempDbCase):
    def make_job(self, jid="j1", platform="zhitong"):
        return db.save_jobs([make_record(platform=platform, platform_job_id=jid,
                                         url=f"https://example.com/job/{jid}")])

    def test_roundtrip_and_overwrite(self):
        self.make_job()
        rec = {"platform": "zhitong", "platform_job_id": "j1",
               "description": "岗位\n职责", "keyword_tags": ["ERP运维"], "url": "u"}
        self.assertEqual(db.save_details([rec]), 1)
        got = db.get_detail("zhitong", "j1")
        self.assertEqual(got["description"], "岗位\n职责")
        self.assertEqual(got["keyword_tags"], ["ERP运维"])
        self.assertTrue(got["fetched_at"])
        self.assertEqual(db.save_details([dict(rec, description="新JD")]), 1)
        self.assertEqual(db.get_detail("zhitong", "j1")["description"], "新JD")
        self.assertEqual(len(db.detail_keys()), 1)   # 覆盖，不是追加

    def test_empty_description_rejected_and_whitelist_enforced(self):
        self.make_job()
        base = {"platform": "zhitong", "platform_job_id": "j1", "url": "u",
                "keyword_tags": []}
        self.assertEqual(db.save_details([dict(base, description="   ")]), 0)
        self.assertIsNone(db.get_detail("zhitong", "j1"))
        with self.assertRaises(ValueError):
            db.save_details([dict(base, description="x", hr_phone="13900000000")])

    def test_missing_listing_and_daily_counter(self):
        self.make_job("j1")
        self.make_job("j2")
        db.save_details([{"platform": "zhitong", "platform_job_id": "j1",
                          "description": "x", "keyword_tags": [], "url": "u"}])
        self.assertEqual([j["platform_job_id"] for j in db.jobs_missing_details()], ["j2"])
        self.assertEqual(db.jobs_missing_details(platform="boss"), [])
        self.assertEqual(db.detail_keys(), ["zhitong:j1"])
        self.assertEqual(db.count_details_today(), 1)

    def test_list_sourced_body_does_not_eat_the_detail_quota(self):
        """采集顺带写进来的正文没有额外发请求，不该把今天的逐条补抓额度假占满。"""
        self.make_job("g1", platform="job51")
        self.make_job("j1")
        db.save_list_details([{"platform": "job51", "platform_job_id": "g1",
                               "description": "x", "keyword_tags": [], "url": "u"}])
        self.assertEqual(db.get_detail("job51", "g1")["description"], "x")
        self.assertIn("job51:g1", db.detail_keys())
        self.assertEqual(db.count_details_today(), 0)
        db.save_details([{"platform": "zhitong", "platform_job_id": "j1",
                          "description": "y", "keyword_tags": [], "url": "u"}])
        self.assertEqual(db.count_details_today(), 1)

    def test_missing_listing_can_drop_channels_without_details(self):
        """前程无忧的行有详情页链接却永远补不上正文：留在候选里就是虚高的待补数。"""
        self.make_job("j1")
        self.make_job("g1", platform="job51")
        self.assertEqual(len(db.jobs_missing_details()), 2)
        self.assertEqual(
            [j["platform_job_id"] for j in
             db.jobs_missing_details(exclude_platforms=("job51",))], ["j1"])

    def test_export_csv_carries_description(self):
        self.make_job()
        db.save_details([{"platform": "zhitong", "platform_job_id": "j1",
                          "description": "多行\n正文", "keyword_tags": [], "url": "u"}])
        out = Path(self._tmp.name) / "e.csv"
        db.export_csv(out)
        text = out.read_text(encoding="utf-8-sig")
        self.assertIn("description", text.splitlines()[0])
        self.assertIn('"多行\n正文"', text)   # 换行留在引号里，Excel 不会串行


def det_job(jid="j1", platform="zhitong"):
    return {"id": 1, "platform": platform, "platform_job_id": jid,
            "title": "IT运维", "company": "某某公司", "url": "https://x/j"}


class TestCollectDetails(TempDbCase):
    def test_cap_blocks_before_any_request(self):
        with mock.patch.object(db, "count_details_today", return_value=20):
            with self.assertRaises(RuntimeError) as ctx:
                details.check_detail_cap()
        self.assertIn("正文额度熔断", str(ctx.exception))

    def test_already_fetched_skipped_unless_force(self):
        db.save_details([{"platform": "zhitong", "platform_job_id": "j1",
                          "description": "旧", "keyword_tags": [], "url": "u"}])
        calls = []
        fetchers = {"zhitong": lambda j: calls.append(j["platform_job_id"]) or
                    {"platform": "zhitong", "platform_job_id": "j1",
                     "description": "新", "keyword_tags": [], "url": "u"}}
        with mock.patch.object(details, "FETCHERS", fetchers), \
             mock.patch.object(details, "polite_sleep"):
            self.assertEqual(details.collect_details([det_job()])["skipped"], 1)
            self.assertEqual(calls, [])
            details.collect_details([det_job()], force=True)
        self.assertEqual(calls, ["j1"])
        self.assertEqual(db.get_detail("zhitong", "j1")["description"], "新")

    def test_sleeps_between_detail_pages_only(self):
        seen = []

        def fake(j):
            seen.append(j["platform_job_id"])
            return {"platform": "zhitong", "platform_job_id": j["platform_job_id"],
                    "description": "d", "keyword_tags": [], "url": "u"}

        jobs = [det_job("j1"), det_job("j2")]
        with mock.patch.object(details, "FETCHERS", {"zhitong": fake}), \
             mock.patch.object(details, "polite_sleep",
                               side_effect=lambda: seen.append("sleep")) as nap:
            res = details.collect_details(jobs)
        self.assertEqual(nap.call_count, 1)      # 两条之间歇一次，最后一条不用
        self.assertEqual(seen, ["j1", "sleep", "j2"])
        self.assertEqual(res["saved"], 2)

    def test_verification_error_aborts_whole_batch(self):
        """平台弹验证：立刻停手，剩下的那条也不碰。"""
        calls = []

        def blocked(j):
            calls.append(j["platform_job_id"])
            raise RuntimeError("Boss 详情页弹出了人机验证")

        jobs = [det_job("b1", "boss"), det_job("b2", "boss")]
        with mock.patch.object(details, "FETCHERS", {"boss": blocked}), \
             mock.patch.object(boss, "ensure_window", return_value=None), \
             mock.patch.object(details, "block_reason", return_value=None), \
             mock.patch.object(details, "polite_sleep"):
            with self.assertRaises(RuntimeError):
                details.collect_details(jobs)
        self.assertEqual(calls, ["b1"])

    def test_window_comes_up_but_login_is_dead(self):
        """窗口弹回来了不等于能开工：弹窗之后再问一次实时凭证，坏的就一条都不发。

        入口那次问不到（通道常常还没开，而快照文件的日期不作数），所以这一层是
        "永不拿已知坏凭证撞平台"唯一的兜底。
        """
        calls = []

        def fake(j):
            calls.append(j["platform_job_id"])
            return {"platform": j["platform"], "platform_job_id": j["platform_job_id"],
                    "description": "d", "keyword_tags": [], "url": "u"}

        jobs = [det_job("b1", "boss"), det_job("j1", "zhitong")]
        with mock.patch.object(details, "FETCHERS", {"boss": fake, "zhitong": fake}), \
             mock.patch.object(boss, "ensure_window", return_value=None), \
             mock.patch.object(details, "block_reason",
                               side_effect=lambda p: "登录凭证已过期 20.0 天"
                               if p == "boss" else None), \
             mock.patch.object(details, "polite_sleep") as nap, \
             redirect_stdout(io.StringIO()) as buf:
            res = details.collect_details(jobs)
        self.assertEqual(calls, ["j1"])
        self.assertIn("已过期 20.0 天", buf.getvalue())
        self.assertEqual(res["saved"], 1)

    def test_window_failure_drops_boss_but_keeps_the_rest(self):
        """Edge 窗口起不来只该废掉 Boss 那几条：整批 raise 的话，智通那些能抓的也陪葬。"""
        calls = []

        def fake(j):
            calls.append(j["platform_job_id"])
            return {"platform": j["platform"], "platform_job_id": j["platform_job_id"],
                    "description": "d", "keyword_tags": [], "url": "u"}

        jobs = [det_job("b1", "boss"), det_job("j1")]
        with mock.patch.object(details, "FETCHERS", {"zhitong": fake, "boss": fake}), \
             mock.patch.object(boss, "ensure_window",
                               return_value="已弹出 Edge 登录窗口，请扫码"), \
             mock.patch.object(details, "polite_sleep"):
            res = details.collect_details(jobs)
        self.assertEqual(calls, ["j1"])
        self.assertEqual(res["saved"], 1)

    def test_limit_caps_this_run(self):
        calls = []

        def fake(j):
            calls.append(j["platform_job_id"])
            return None

        with mock.patch.object(details, "FETCHERS", {"zhitong": fake}), \
             mock.patch.object(details, "polite_sleep"):
            details.collect_details([det_job("j1"), det_job("j2")], limit=1)
        self.assertEqual(calls, ["j1"])

    def test_no_detail_channel_never_touches_the_platform(self):
        """前程无忧详情页要滑块验证：按红线不绕，所以这一层连 fetcher 都不给它配。"""
        self.assertNotIn("job51", details.FETCHERS)
        self.assertIn("滑块验证", details.NO_DETAIL["job51"])

    def test_every_channel_is_fetchable_or_explained(self):
        """新增平台时二选一：能抓正文，或把不能的原因写进 NO_DETAIL。

        两边都空着的话，页面只能瞎猜，猜出来的就是"职位可能已下架"这种误导话。
        """
        from jobstats.collectors import COLLECTORS
        for name in COLLECTORS:
            self.assertTrue(name in details.FETCHERS or name in details.NO_DETAIL, name)
        self.assertFalse(set(details.FETCHERS) & set(details.NO_DETAIL))

    def test_no_detail_rows_take_neither_the_slot_nor_the_quota(self):
        """这些行要在挑名额之前就被摘掉：不占 --limit，也不许碰今日正文额度。"""
        calls = []

        def fake(j):
            calls.append(j["platform_job_id"])
            return {"platform": "zhitong", "platform_job_id": j["platform_job_id"],
                    "description": "d", "keyword_tags": [], "url": "u"}

        jobs = [det_job("g1", "job51"), det_job("g2", "job51"), det_job("j1")]
        with mock.patch.object(details, "FETCHERS", {"zhitong": fake}), \
             mock.patch.object(details, "polite_sleep"), \
             redirect_stdout(io.StringIO()) as buf:
            res = details.collect_details(jobs, limit=1)
        self.assertEqual(calls, ["j1"])
        self.assertEqual(res["unreachable"], 2)
        self.assertEqual(db.count_details_today(), 1)
        self.assertIn("滑块验证", buf.getvalue())


def _expired_auth(case, days=20):
    """把登录态换成"过期 N 天"——快照和实时通道两个都得造。

    闸门现在只认实时通道（`boss_storage.json` 只是份快照，没有命令会自动刷新它，
    照它判会把刚扫完码的人拦在门外），所以这里少 patch 通道的话，
    巡检时本机 Edge 一开，测试就会读到真实登录态、结果随环境变。
    """
    tmp = tempfile.TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    auth = Path(tmp.name)
    cookies = [{"name": "bst", "value": "v", "expires": time.time() - 86400 * days},
               {"name": "zp_at", "value": "v", "expires": time.time() - 86400 * days}]
    (auth / "boss_storage.json").write_text(json.dumps({"cookies": cookies}),
                                            encoding="utf-8")
    ctx = mock.MagicMock()
    ctx.cookies.return_value = cookies
    ctx.__enter__.return_value = ctx
    for p in (mock.patch.object(browser, "AUTH_DIR", auth),
              mock.patch.object(browser, "cdp_alive", lambda port=0: True),
              mock.patch.object(browser, "attach", return_value=ctx)):
        p.start()
        case.addCleanup(p.stop)
    return auth


class TestDetailSkipsDeadLogin(TempDbCase):
    """登录态过期的平台不进补抓批次。

    拿过期 20 天的凭证去撞平台，比不抓更糟（风控记的是账号）；而且自动补抓是按
    fetched_at 倒序挑的，智通行先补完就会轮到 Boss 行——那时候整批被一条死通道拖住。
    """

    def _run_detail(self, **over):
        from types import SimpleNamespace
        args = SimpleNamespace(platform=None, ids=None, limit=None, force=False)
        for k, v in over.items():
            setattr(args, k, v)
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_detail(args)
        return buf.getvalue()

    def test_auto_batch_drops_boss_and_keeps_zhitong(self):
        _expired_auth(self)
        db.save_jobs([make_record(platform="boss", platform_job_id="b1"),
                      make_record(platform="zhitong", platform_job_id="j1")])
        calls = []

        def fake(j):
            calls.append(j["platform_job_id"])
            return {"platform": j["platform"], "platform_job_id": j["platform_job_id"],
                    "description": "d", "keyword_tags": [], "url": "u"}

        with mock.patch.object(details, "FETCHERS", {"zhitong": fake, "boss": fake}), \
             mock.patch.object(details, "polite_sleep"):
            out = self._run_detail()
        self.assertEqual(calls, ["j1"])
        self.assertIn("过期 20.0 天", out)
        self.assertIn("其余平台照常补", out)

    def test_explicit_platform_says_rescan_and_sends_nothing(self):
        from types import SimpleNamespace
        _expired_auth(self)
        db.save_jobs([make_record(platform="boss", platform_job_id="b1")])
        with mock.patch.object(details, "collect_details",
                               side_effect=AssertionError("不该发请求")), \
             redirect_stdout(io.StringIO()) as buf:
            cli.cmd_detail(SimpleNamespace(platform="boss", ids=None,
                                           limit=None, force=False))
        self.assertIn("重新扫码之后再跑这条命令", buf.getvalue())
        self.assertIn("一条请求都没发", buf.getvalue())


class TestDetailConcurrency(TempDbCase):
    """详情页请求同一时刻只许一批：两个标签页同时进来，第二批直接拒绝。
    并发了额度会算乱（两边都读到 19/20 就各抓一条），平台风控也记账号。"""

    def test_second_batch_is_rejected_while_first_runs(self):
        with details._FETCH_LOCK:
            with self.assertRaises(RuntimeError) as ctx:
                details.collect_details([{"platform": "zhitong", "platform_job_id": "j1"}])
        self.assertIn("正在抓取", str(ctx.exception))
        self.assertFalse(details.fetch_in_progress())

    def test_lock_is_released_after_a_normal_run(self):
        db.save_jobs([make_record(platform="zhitong", platform_job_id="j1")])

        def fake(j):
            return {"platform": "zhitong", "platform_job_id": "j1",
                    "description": "d", "keyword_tags": [], "url": "u"}

        with mock.patch.object(details, "FETCHERS", {"zhitong": fake}), \
             mock.patch.object(details, "polite_sleep"):
            details.collect_details([db.fetch_all()[0]])
        self.assertFalse(details.fetch_in_progress())

    def test_lock_is_released_even_when_batch_raises(self):
        with mock.patch.object(details, "check_detail_cap",
                               side_effect=RuntimeError("额度熔断")):
            with self.assertRaises(RuntimeError):
                details.collect_details([{"platform": "zhitong", "platform_job_id": "j1"}])
        self.assertFalse(details.fetch_in_progress())

    def test_collect_api_waits_while_detail_fetch_runs(self):
        with details._FETCH_LOCK:
            res = post_api("/api/collect", {"platforms": ["zhitong"],
                                           "cities": ["东莞"], "query": "IT"})
        self.assertEqual(res["status"], 409)
        self.assertIn("正文抓取正在进行", res["obj"]["error"])


class TestDetailApiEndpoints(unittest.TestCase):
    def test_web_page_has_detail_column_static_does_not(self):
        web = report.render_web([make_record()], "T", "")
        static = report._filter_html(report.STATIC_SUB_LINE, [make_record()], web=False)
        self.assertIn("<th>正文</th>", web)
        self.assertIn("const WEB = true;", web)
        self.assertIn("/api/details", web)
        self.assertNotIn("<th>正文</th>", static)
        self.assertIn("const WEB = false;", static)
        self.assertNotIn("toggleDetail", static)

    def test_detail_text_rendered_as_text_not_html(self):
        """正文里带标签也只能当文本显示（用 textContent 注入）。"""
        js = report.DETAIL_JS
        self.assertIn("pre.textContent = d.description", js)
        self.assertNotIn("innerHTML = d.description", js)

    def test_stored_body_wins_over_the_channel_limitation(self):
        """前程无忧的正文现在随采集写入：有正文的行要显示"看正文"，
        不能因为平台不支持按条补抓就一律写成"不抓正文"。"""
        js = report.FILTER_TEMPLATE
        self.assertIn("const hasDet = DET.has(detKey(r));", js)
        self.assertIn("const nd = hasDet ? '' : (NO_DETAIL[r.platform] || '');", js)
        self.assertIn("hasDet ? '看正文'", js)

    def test_no_detail_map_reaches_the_page_and_leaves_no_placeholder(self):
        nd = {"job51": "前程无忧这条通道不抓正文"}
        web = report.render_web([make_record()], "T", "", no_detail=nd)
        self.assertIn('"job51": "前程无忧这条通道不抓正文"', web)
        self.assertNotIn("__NO_DETAIL__", web)      # 槽位值里嵌的标记要就地展开
        self.assertIn("data-nd", report.FILTER_TEMPLATE)   # 按平台改按钮文案
        static = report._filter_html(report.STATIC_SUB_LINE, [make_record()], web=False)
        self.assertIn("const NO_DETAIL = {};", static)   # 静态版没正文按钮，也不能引用不到
        self.assertNotIn("前程无忧这条通道", static)
        self.assertNotIn("__NO_DETAIL__", static)


class TestDetailApiReasons(TempDbCase):
    """正文接口不许把"这条通道拿不到正文"说成"职位已下架"——职位明明还在。"""

    def test_job51_row_is_answered_with_the_channel_reason(self):
        db.save_jobs([make_record(platform="job51", platform_job_id="g1")])
        jid = db.fetch_all()[0]["id"]
        with mock.patch.object(server.details, "collect_details",
                               side_effect=AssertionError("不该发请求")):
            res = post_api("/api/detail", {"id": jid})
        self.assertEqual(res["status"], 400)
        self.assertIn("滑块验证", res["obj"]["error"])
        self.assertNotIn("已下架", res["obj"]["error"])

    def test_expired_login_is_answered_without_blaming_the_job(self):
        """登录态不行就说登录态：写成"职位已下架/要人机验证"是让他去核对一条好好的职位。"""
        _expired_auth(self)
        db.save_jobs([make_record(platform="boss", platform_job_id="b1")])
        jid = db.fetch_all()[0]["id"]
        with mock.patch.object(server.details, "collect_details",
                               side_effect=AssertionError("不该发请求")):
            res = post_api("/api/detail", {"id": jid})
        self.assertEqual(res["status"], 400)
        self.assertIn("重新扫码", res["obj"]["error"])
        self.assertNotIn("已下架", res["obj"]["error"])


class TestSkillSource(TempDbCase):
    """技能列的两个来源：列表页标签，和列表页不给这一列的平台从详情页关键词兜过来。

    起因是 238 条智通的 skills 全是空——不是解析失败，是列表页没有这一列
    （实测：它的标签只有城市-镇区/学历/经验/公司性质/公司规模/行业）。
    """

    def zt(self, jid="z1", skills_list=()):
        db.save_jobs([make_record(platform="zhitong", platform_job_id=jid,
                                  skills=list(skills_list), company_scale="1-100人")])

    def jd(self, jid="z1", tags=("华为", "锐捷", "TCP/IP 协议栈")):
        db.save_details([{"platform": "zhitong", "platform_job_id": jid,
                          "description": "负责机房与网络运维", "keyword_tags": list(tags),
                          "url": "u"}])

    def row(self, jid="z1"):
        return next(r for r in db.fetch_all() if r["platform_job_id"] == jid)

    def test_detail_tags_fill_the_empty_skill_column(self):
        self.zt()
        self.jd()
        r = self.row()
        self.assertEqual(r["skills"], ["华为", "锐捷", "TCP/IP 协议栈"])
        self.assertEqual(r["skill_src"], "jd")

    def test_list_page_tags_win_when_both_exist(self):
        """列表页给了词就用列表页的：那才是统计口径一直在用的那一列，兜底只补空洞。"""
        self.zt(skills_list=["linux", "python"])
        self.jd(tags=("华为",))
        r = self.row()
        self.assertEqual(r["skills"], ["Linux", "Python"])
        self.assertEqual(r["skill_src"], "list")

    def test_nothing_invented_without_a_detail(self):
        self.zt()
        r = self.row()
        self.assertEqual(r["skills"], [])
        self.assertEqual(r["skill_src"], "")

    def test_welfare_words_in_detail_tags_stay_out_of_skills(self):
        self.zt()
        self.jd(tags=("包住", "五险一金", "ERP运维"))
        self.assertEqual(self.row()["skills"], ["ERP运维"])

    def test_the_analysis_queue_uses_the_same_skills(self):
        """两个读出口径必须一致。

        分叉过一次：筛选台写着"华为/锐捷/TCP/IP 协议栈"，`pending_analysis` 却把
        技能洗成空的，送给模型的提示词回一句「技能标签：未列」，
        模型照着"这条没写技能"打分——智通的行几乎全是这样。
        """
        self.zt()
        self.jd()
        pend = db.pending_analysis(limit=5)
        self.assertEqual(len(pend), 1)
        self.assertEqual(pend[0]["skills"], self.row()["skills"])
        self.assertEqual(pend[0]["skill_src"], "jd")
        # 下游（n8n）还要那份原样的关键词表，清洗不能把它吃掉
        self.assertEqual(pend[0]["keyword_tags"], ["华为", "锐捷", "TCP/IP 协议栈"])

    def test_prompt_stops_saying_skills_absent_when_jd_has_them(self):
        self.zt()
        self.jd()
        block = analyze.job_block(db.pending_analysis(limit=5)[0])
        self.assertIn("技能标签：华为 / 锐捷 / TCP/IP 协议栈", block)
        self.assertNotIn("技能标签：未列", block)

    def test_old_db_without_the_details_table_still_reads(self):
        """老库没建 job_details 表：没得兜就算了，不能整页读不出来。"""
        import sqlite3
        self.zt(skills_list=["linux"])
        conn = sqlite3.connect(db.DB_PATH)
        try:
            conn.execute("DROP TABLE job_details")
            conn.commit()
        finally:
            conn.close()
        r = self.row()
        self.assertEqual(r["skills"], ["Linux"])
        self.assertEqual(r["skill_src"], "list")

    def test_declared_platforms_are_real_channels(self):
        from jobstats.collectors import COLLECTORS
        for name, why in skills.NO_LIST_SKILLS.items():
            self.assertIn(name, COLLECTORS)
            self.assertIn("列表页", why)
            self.assertIn("详情页", why)

    def test_caption_counts_both_sources(self):
        self.zt("z1")
        self.zt("z2", skills_list=["linux"])
        self.zt("z3")
        self.jd("z1")
        cap = report.build_stats(db.fetch_all())["skill_caption"]
        for needle in ("1 条来自列表页标签", "1 条来自详情页关键词",
                       "1 条两处都没取到", "没词不等于没要求"):
            self.assertIn(needle, cap)

    def test_js_strings_never_span_a_line(self):
        """模板里要写 \\n：只写 \n 会被 Python 先吃掉，JS 里就成了跨行的字符串常量，
        整段 <script> SyntaxError，页面所有控件一起死掉（这次真踩到了）。"""
        def odd_quote_lines(src):
            return [i for i, ln in enumerate(src.splitlines(), 1) if ln.count("'") % 2]
        for name in ("FILTER_TEMPLATE", "WEB_COLLECT_PANEL", "WEB_COLLECT_SCRIPT",
                     "DETAIL_JS", "STATUS_JS", "DELETE_JS"):
            self.assertEqual([], odd_quote_lines(getattr(report, name)), name)
        # 自检：这个检测得真抓得到那种写法，不然只是个摆设
        self.assertEqual([1], odd_quote_lines("a = '一\ndef g = 2\n"))

    def test_pages_ship_the_capability_table(self):
        self.zt()
        self.jd()
        records = db.fetch_all()
        for html in (report.render_web(records, "T", ""),
                     report._filter_html(report.STATIC_SUB_LINE, records, web=False)):
            self.assertNotIn("__NO_LIST_SKILLS__", html)
            self.assertIn("列表页没有技能标签这一列", html)
        web = report.render_web(records, "T", "")
        for needle in ("const NO_LIST_SKILLS = ", 'id="skill-note"', "renderSkillNote(",
                       "来自详情页"):
            self.assertIn(needle, web)


class TestCrossRef(TempDbCase):
    """跨平台对照只按"归一公司名+归一标题"认，认不出就什么都不标——宁漏不错。"""

    def seed(self, *specs):
        for platform, pjid, company, title in specs:
            db.save_jobs([make_record(platform=platform, platform_job_id=pjid,
                                      company=company, title=title)])
        rows = db.fetch_all()
        return crossref.attach(rows)

    def by(self, rows, platform):
        return next(r for r in rows if r["platform"] == platform)

    def test_normalizers_fold_what_the_platforms_write_differently(self):
        # 一层层剥到剥不动：一边写全 `…科技有限公司`、一边写 `…科技`，得归到同一个头
        self.assertEqual(crossref.norm_company("东莞市微科光电科技有限公司"), "东莞市微科光电")
        self.assertEqual(crossref.norm_company("东莞市微科光电科技"), "东莞市微科光电")
        self.assertEqual(crossref.norm_title("IT运维工程师（包吃住）"), "it运维工程师")
        self.assertEqual(crossref.norm_title("it 运维工程师"), "it运维工程师")
        self.assertEqual(crossref.norm_title("运维工程师/JO0123456"), "运维工程师jo0123456")

    def test_sibling_on_another_platform_is_listed_without_itself(self):
        rows = self.seed(("job51", "g1", "东莞市微科光电科技有限公司", "IT运维工程师"),
                         ("zhitong", "z1", "东莞市微科光电科技", "IT运维工程师（包住）"))
        cross = self.by(rows, "job51")["cross"]
        self.assertEqual([x["platform"] for x in cross], ["zhitong"])
        # 面板要点开对面那条的正文，所以行号、平台职位号都得带上
        self.assertTrue(cross[0]["id"] and cross[0]["platform_job_id"])
        self.assertEqual(cross[0]["seen"], cross[0]["seen"][:10])

    def test_same_platform_twice_is_not_a_cross_match(self):
        """两条同公司同岗位都在智通，是同一平台采重了，不是跨平台对照。"""
        rows = self.seed(("zhitong", "a", "甲公司", "IT运维"),
                         ("zhitong", "b", "甲公司", "IT运维"))
        self.assertEqual([r["cross"] for r in rows], [[], []])

    def test_different_title_is_not_merged(self):
        rows = self.seed(("zhitong", "a", "甲公司", "IT运维"),
                         ("job51", "b", "甲公司", "行政专员"))
        self.assertEqual([r["cross"] for r in rows], [[], []])

    def test_missing_company_or_title_stays_out_of_the_index(self):
        rows = self.seed(("zhitong", "a", "", "IT运维"), ("job51", "b", "", "IT运维"))
        self.assertEqual([r["cross"] for r in rows], [[], []])

    def test_has_detail_marks_the_sibling_we_can_borrow(self):
        rows = self.seed(("job51", "g", "甲公司", "IT运维"), ("zhitong", "z", "甲公司", "IT运维"))
        db.save_details([{"platform": "zhitong", "platform_job_id": "z",
                          "description": "负责机房", "keyword_tags": [], "url": "u"}])
        cross = self.by(crossref.attach(db.fetch_all()), "job51")["cross"]
        self.assertTrue(cross[0]["has_detail"])

    def test_panel_keeps_the_fifteen_column_layout(self):
        """对照面板占满现有的 15 列（含发布/刷新），不新增表头——列数一变整张表就错位。"""
        self.assertIn("cell.colSpan = 15", report.DETAIL_JS)
        self.assertIn("toggleXref", report.DETAIL_JS)
        self.assertIn("data-xref", report.FILTER_TEMPLATE)
        static = report._filter_html(report.STATIC_SUB_LINE, [make_record()], web=False)
        self.assertNotIn("toggleXref", static)
        self.assertIn("另见", static)          # 静态页也有标签，只是点不开

    def test_cross_data_rides_in_the_embedded_records(self):
        rows = self.seed(("job51", "g", "甲公司", "IT运维工程师"),
                         ("zhitong", "z", "甲公司", "IT运维工程师（包住）"))
        html = report.render_web(rows, "T", "")
        self.assertIn('"cross": [{"id":', html)
        self.assertIn("15-25K", html)


class TestApplicationStatus(TempDbCase):
    """投递状态/备注是本地标注：采集路径写不到它，重采也冲不掉它。"""

    def one_job(self):
        db.save_jobs([make_record()])
        return db.fetch_all()[0]["id"]

    def test_old_database_gets_the_status_columns(self):
        import sqlite3
        old = ("CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT,"
               " platform TEXT NOT NULL, platform_job_id TEXT NOT NULL, title TEXT,"
               " company TEXT, salary_raw TEXT, salary_min REAL, salary_max REAL,"
               " salary_months INTEGER, city TEXT, district TEXT, experience TEXT,"
               " education TEXT, industry TEXT, company_scale TEXT, skills TEXT,"
               " welfare TEXT, url TEXT, fetched_at TEXT, UNIQUE(platform, platform_job_id));")
        db.DB_PATH.unlink()
        conn = sqlite3.connect(db.DB_PATH)
        try:
            conn.execute(old)
            conn.execute("INSERT INTO jobs(platform,platform_job_id,title,fetched_at) "
                         "VALUES('job51','j1','老职位','2026-09-16 10:00:00')")
            conn.commit()
        finally:
            conn.close()   # Windows 上不关掉句，tearDown 删不掉临时目录
        db.init_db()
        rows = db.fetch_all()
        self.assertEqual(rows[0]["title"], "老职位")
        self.assertEqual(rows[0]["status"], "")     # 迁移前的老行读出来是空串，不是 None
        self.assertEqual(rows[0]["note"], "")
        # status_at 不回填：这条是哪天标的没人知道，拿 fetched_at 顶上去等于给漏斗造假日期的
        self.assertIsNone(rows[0]["status_at"])

    def test_status_and_note_each_only_touch_their_own_column(self):
        jid = self.one_job()
        self.assertEqual(db.set_status(jid, "已投"), {"status": "已投", "note": ""})
        self.assertEqual(db.set_status(jid, note="约了周三面")["status"], "已投")
        row = db.fetch_all()[0]
        self.assertEqual((row["status"], row["note"]), ("已投", "约了周三面"))

    def test_bad_input_rejected(self):
        jid = self.one_job()
        with self.assertRaises(ValueError):
            db.set_status(jid, "已录取")            # 不在枚举里
        self.assertIsNone(db.set_status(jid))       # 两列都没给
        self.assertIsNone(db.set_status(9999, "已投"))
        self.assertEqual(db.fetch_all()[0]["status"], "")

    def test_note_is_capped(self):
        jid = self.one_job()
        self.assertEqual(len(db.set_status(jid, note="字" * (db.NOTE_MAX + 50))["note"]),
                         db.NOTE_MAX)

    def test_recollect_does_not_clobber_the_marks(self):
        jid = self.one_job()
        db.set_status(jid, "已投", "已发简历")
        db.save_jobs([make_record()])               # 同一个关键词再采一遍
        row = db.fetch_all()[0]
        self.assertEqual((row["status"], row["note"]), ("已投", "已发简历"))

    def test_collectors_cannot_write_these_columns(self):
        with self.assertRaises(ValueError) as ctx:
            db.save_jobs([dict(make_record(), status="已投")])
        self.assertIn("白名单", str(ctx.exception))
        self.assertNotIn("status", config.ALLOWED_FIELDS)
        self.assertNotIn("note", config.ALLOWED_DETAIL_FIELDS)

    def test_export_csv_has_status_and_note(self):
        jid = self.one_job()
        db.set_status(jid, "约面", '带逗号,"和引号"')
        out = Path(self._tmp.name) / "s.csv"
        db.export_csv(out)
        text = out.read_text(encoding="utf-8-sig")
        head = text.splitlines()[0].split(",")
        self.assertEqual(head[-4:], ["published_raw", "verified_at", "delisted_at",
                                     "description"])
        self.assertIn("status", head)
        self.assertIn("约面", text)
        self.assertIn('"带逗号,""和引号"""', text)
        # 状态时刻也在导出里：Excel 里按它排序才是一份有时间线台账，只按 status 排不出先后
        self.assertIn("status_at", head)
        self.assertIn(db.fetch_all()[0]["status_at"][:10], text)


class TestStatusFunnel(TempDbCase):
    """投递漏斗：把"投出去几条、回了几条"变成一屏能被打印出来的数（2026-10-26 加的）。

    第一次真数出来的是 40 天采进 444 条、1 条「想投」、0 条「已投」。所以这一节最要紧的
    不是算出百分比，而是**零分母时别把它说成 0%**——那两句是不同的动作：
    0% 是"投了没人理"，没有分母是"这一步压根还没发生"。
    """

    def seed(self, *labels):
        """采 len(labels) 条（职位号 j0..），第 i 条标成 labels[i]；None 表示不标。"""
        db.save_jobs([make_record(platform="zhitong", platform_job_id=f"j{i}")
                      for i in range(len(labels))])
        ids = {r["platform_job_id"]: r["id"] for r in db.fetch_all()}
        for i, lab in enumerate(labels):
            if lab:
                db.set_status(ids[f"j{i}"], lab)
        return ids

    def _sql(self, script, *args):
        import sqlite3
        conn = sqlite3.connect(db.DB_PATH)
        try:
            conn.execute(script, args)
            conn.commit()
        finally:
            conn.close()   # Windows 上不关掉句，tearDown 删不掉临时目录

    def backdate(self, jid, day):
        """把标注时刻挪到历史上：`status_at` 由 set_status 统一赋值，只能改库。"""
        self._sql("UPDATE jobs SET status_at=? WHERE id=?", f"{day} 09:00:00", jid)

    def _text(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli._funnel_check()
        return buf.getvalue()

    def test_marking_a_status_stamps_the_time(self):
        self.seed("已投")
        stamp = db.fetch_all()[0]["status_at"]
        self.assertEqual(stamp[:10], datetime.now().strftime("%Y-%m-%d"))
        self.assertEqual(db.status_funnel()["applied"], 1)
        self.assertEqual(db.status_funnel(days=7)["applied"], 1)
        self.assertEqual(db.status_funnel(days=7)["undated"], 0)

    def test_a_note_only_edit_does_not_move_the_date(self):
        """只改备注不该把投递日期推到今天，否则"近 7 天投了几条"永远数到最后一次碎碎念。"""
        ids = self.seed("已投")
        self.backdate(ids["j0"], "2026-09-01")
        db.set_status(ids["j0"], note="周三再问一下")
        self.assertEqual(db.fetch_all()[0]["status_at"][:7], "2026-09")
        self.assertEqual(db.status_funnel(days=7)["applied"], 0)
        self.assertEqual(db.status_funnel()["applied"], 1)

    def test_clearing_a_status_clears_its_date_too(self):
        """标回空串 = 撤掉这条标注：留着日期会数出一条"没有状态但有日期"的行，哪头都不算。"""
        ids = self.seed("已投")
        db.set_status(ids["j0"], "")
        self.assertIsNone(db.fetch_all()[0]["status_at"])
        f = db.status_funnel()
        self.assertEqual((f["applied"], f["total"], f["unmarked"]), (0, 0, 1))

    def test_wanting_to_apply_alone_is_not_a_denominator(self):
        """「想投」只是候选清单：两边都不算，算进分母会被没发生的投递稀释。"""
        self.seed("想投", "想投", None)
        f = db.status_funnel()
        self.assertEqual((f["total"], f["applied"], f["replied"], f["rate"]),
                         (2, 0, 0, None))
        self.assertEqual(f["unmarked"], 1)     # 没标过的那条就是队列本身
        text = self._text()
        self.assertIn("想投 2", text)
        self.assertIn("库里共 3 条", text)
        self.assertIn("分母还没出现", text)
        self.assertNotIn("条，有回音", text)      # 没有分母就别长出一行百分比

    def test_the_rate_is_replied_over_applied(self):
        self.seed("已投", "已投", "HR已读", "offer", "想投", "不合适")
        f = db.status_funnel()
        # 分母 = 已投/HR已读/约面/已面/offer = 4，分子 = HR已读/约面/已面/offer = 2
        self.assertEqual((f["applied"], f["replied"], f["rate"]), (4, 2, 50.0))
        text = self._text()
        self.assertIn("投出去 4 条，有回音 2 条 = 50%", text)

    def test_the_window_counts_by_the_mark_date_not_the_collect_date(self):
        """按天窗口只看 status_at：三个月前采到的那条今天投出去，就算进这一周。"""
        ids = self.seed("已投", "已投")
        self.backdate(ids["j0"], (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d"))
        self.assertEqual(db.status_funnel()["applied"], 2)
        w = db.status_funnel(days=7)
        self.assertEqual(w["applied"], 1)
        self.assertIn("近 7 天：投出 1 条", self._text())

    def test_an_undated_mark_is_reported_instead_of_guessed(self):
        """补列之前标的那条：按天窗口数不到它，但要把条数说出来，不能让它悄悄消失。"""
        ids = self.seed("已投")
        self._sql("UPDATE jobs SET status_at=NULL WHERE id=?", ids["j0"])
        f = db.status_funnel(days=7)
        self.assertEqual((f["applied"], f["undated"]), (0, 1))
        self.assertIn("另有 1 条标过状态但没记时刻", self._text())

    def test_the_digest_gets_one_line_even_with_no_analysis(self):
        """匹配度那段一条分析都没有时会自己 return：投递漏斗不能跟着它一起被跳过。"""
        from types import SimpleNamespace
        self.seed("想投", "已投", "HR已读")
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_digest(SimpleNamespace(days=7))
        text = buf.getvalue()
        self.assertIn("匹配度：还没跑过分析", text)
        line = [ln for ln in text.splitlines() if ln.startswith("投递漏斗：")]
        self.assertEqual(len(line), 1, text)
        self.assertIn("想投1", line[0])
        self.assertIn("投出 2 条、有回音 1 条 = 50%", line[0])


class TestStatusUi(unittest.TestCase):
    def test_web_page_marks_status_locally(self):
        web = report.render_web([make_record(status="已投", note="约了周三")], "T", "")
        self.assertIn("<th>状态</th>", web)
        self.assertIn("function bindStatus", web)
        self.assertIn("/api/status", web)      # 只打本地服务，不碰平台
        for host in ("zhipin", "51job", "http"):
            self.assertNotIn(host, report.STATUS_JS)

    def test_static_page_shows_but_cannot_edit(self):
        static = report._filter_html(report.STATIC_SUB_LINE, [make_record(status="已投")],
                                     web=False)
        self.assertIn("<th>状态</th>", static)
        for marker in ("function bindStatus", "/api/status"):
            self.assertNotIn(marker, static)
        self.assertIn("`<td>${esc(r.status||'—')}</td>`", static)   # 静态页只显示文本

    def test_status_options_have_one_source(self):
        web = report.render_web([make_record()], "T", "")
        self.assertIn("const STATUS = " + report._embed_json(list(db.STATUS_LABELS)) + ";", web)
        self.assertIn(f"const NOTE_MAX = {db.NOTE_MAX};", web)

    def test_empty_row_spans_every_column(self):
        """表头加一列，空表和正文行就得跟着加一列——数字从模板里读，别写死。"""
        web = report.render_web([make_record()], "T", "")
        static = report._filter_html(report.STATIC_SUB_LINE, [], web=False)
        span = re.search(r'colspan="\$\{WEB\?(\d+):(\d+)\}"', report.FILTER_TEMPLATE)
        self.assertTrue(span, "空表那行必须写清网页版/静态页各跨几列")
        for html, expect in ((web, int(span.group(1))), (static, int(span.group(2)))):
            head = re.search(r"<thead><tr>(.*?)</tr></thead>", html, re.S).group(1)
            self.assertEqual(expect, head.count("<th>"), "跨列数和表头列数对不上")
        self.assertIn(f"cell.colSpan = {span.group(1)};", report.DETAIL_JS)


class TestIndustryFilter(unittest.TestCase):
    """清噪音岗位靠行业筛：控件、一级归并、未标注桶、撤销和回填都得在两种页里齐活。"""

    def test_both_variants_wire_the_industry_select(self):
        web = report.render_web([make_record()], "T", "")
        static = report._filter_html(report.STATIC_SUB_LINE, [make_record()], web=False)
        for html in (web, static):
            self.assertIn('<select id="f-industry"></select>', html)
            self.assertIn("e => { st.industry=e.target.value;", html)
            # 建完控件后要把已选值回填，否则"刷新数据"会悄悄清掉筛选
            self.assertIn("document.getElementById('f-industry').value = st.industry;", html)
            self.assertIn("else if (k==='industry'){ st.industry='';", html)
            self.assertIn("(st.industry?1:0)", html)   # 活动条件计数别漏它

    def test_industry_groups_by_first_level_and_keeps_unlabeled_bucket(self):
        web = report.render_web([make_record()], "T", "")
        self.assertIn("const indKey = r => String(r.industry || '').split('-')[0].trim();", web)
        self.assertIn("st.industry === '__none__' ? k : k !== st.industry", web)
        self.assertIn('${esc(k || \'未标注\')}', web)


class TestJobAnalysis(TempDbCase):
    """匹配度是外部模型算好后回写的本地标注，和 status/note 同一类：
    采集路径写不到它，重新采集也冲不掉它，接口侧还得把字段和取值收紧。"""

    def one_job(self, pjid="j1"):
        db.save_jobs([make_record(platform_job_id=pjid)])
        return pjid

    def test_old_database_gets_the_analysis_table(self):
        """升级前的库（只有 jobs + job_details）重新 init_db 就能多出分析表。"""
        import sqlite3
        old = "".join(line for line in db.SCHEMA.split(
            "CREATE TABLE IF NOT EXISTS job_analysis")[0])
        db.DB_PATH.unlink()
        conn = sqlite3.connect(db.DB_PATH)
        try:
            conn.executescript(old)
            conn.execute("INSERT INTO jobs(platform,platform_job_id,title,fetched_at) "
                         "VALUES('job51','j1','老职位','2026-09-16 10:00:00')")
            conn.commit()
        finally:
            conn.close()   # Windows 上不关掉句，tearDown 删不掉临时目录
        db.init_db()
        self.assertEqual(db.analysis_keys(), [])
        self.assertEqual(db.fetch_all()[0]["title"], "老职位")

    def test_same_job_overwrites_not_duplicates(self):
        pjid = self.one_job()
        db.save_analysis({"platform": "job51", "platform_job_id": pjid, "score": 62})
        saved = db.save_analysis({"platform": "job51", "platform_job_id": pjid, "score": 88.5})
        self.assertEqual(saved["score"], 88.5)
        self.assertEqual(len(db.analysis_keys()), 1)

    def test_recollect_does_not_clobber_the_score(self):
        pjid = self.one_job()
        db.save_analysis({"platform": "job51", "platform_job_id": pjid, "score": 77})
        db.save_jobs([make_record(platform_job_id=pjid)])    # 同一条再采一遍
        self.assertEqual(db.analysis_keys(), [f"job51:{pjid}"])

    def test_score_bounds_and_types(self):
        pjid = self.one_job()
        base = {"platform": "job51", "platform_job_id": pjid}
        for bad in ("abc", 101, -1):
            with self.subTest(score=bad), self.assertRaises(ValueError):
                db.save_analysis(dict(base, score=bad))
        self.assertEqual(db.analysis_keys(), [])
        # 模型没给出分数也要落一行，否则外部会把它当成"还没分析过"反复重跑
        self.assertEqual(db.save_analysis(dict(base, score=None))["score"], None)
        self.assertEqual(len(db.analysis_keys()), 1)
        self.assertEqual(db.save_analysis(dict(base, score="73"))["score"], 73.0)

    def test_unknown_job_is_not_written(self):
        self.assertIsNone(db.save_analysis({"platform": "job51",
                                            "platform_job_id": "ghost", "score": 90}))
        self.assertEqual(db.analysis_keys(), [])

    def test_only_analysis_columns_accepted(self):
        pjid = self.one_job()
        with self.assertRaises(ValueError) as ctx:
            db.save_analysis({"platform": "job51", "platform_job_id": pjid,
                              "score": 1, "prompt_tokens": 12})
        self.assertIn("白名单", str(ctx.exception))

    def test_collectors_still_cannot_reach_these_columns(self):
        """新表不能顺手放宽采集白名单——那是合规闸门，不是存储问题。"""
        for col in ("score", "reasons", "model", "analyzed_at"):
            self.assertNotIn(col, config.ALLOWED_FIELDS)
            self.assertNotIn(col, config.ALLOWED_DETAIL_FIELDS)
        with self.assertRaises(ValueError):
            db.save_jobs([dict(make_record(), score=90)])

    def test_lists_are_split_capped_and_json_serialised(self):
        pjid = self.one_job()
        saved = db.save_analysis({"platform": "job51", "platform_job_id": pjid,
                                  "score": 70, "matched": "Django, REST 、Python",
                                  "gaps": ["K8s"], "reasons": "字" * (db.REASONS_MAX + 9),
                                  "model": "qwen3.5-9b"})
        self.assertEqual(saved["platform_job_id"], pjid)
        with db._conn() as c:
            row = c.execute("SELECT * FROM job_analysis").fetchone()
        self.assertEqual(json.loads(row["matched"]), ["Django", "REST", "Python"])
        self.assertEqual(json.loads(row["gaps"]), ["K8s"])
        self.assertEqual(len(row["reasons"]), db.REASONS_MAX)
        self.assertEqual(row["model"], "qwen3.5-9b")
        with self.assertRaises(ValueError):
            db.save_analysis({"platform": "job51", "platform_job_id": pjid,
                              "matched": 42})

    def test_missing_keys_are_rejected(self):
        for body in ({}, {"platform": "job51"}, {"platform_job_id": "j1"}):
            with self.subTest(body=body), self.assertRaises(ValueError):
                db.save_analysis(body)


def post_api(path: str, body: dict, token: str = "T"):
    """不打网络地走一遍 Handler 的 POST 分发，拿回它准备发出的 JSON 和状态码。

    服务端固定用 "T"，`token` 是客户端请求头里带的那个，两者不一致就该吃 403。
    """
    payload = json.dumps(body).encode("utf-8")
    h = object.__new__(server.Handler)
    h.path = path
    h.headers = {"Content-Length": str(len(payload)), "X-Token": token}
    h.rfile = io.BytesIO(payload)
    out = {}
    h._json = lambda obj, status=200: out.update(obj=obj, status=status)
    with mock.patch.object(server.Handler, "token", "T"):
        h.do_POST()
    return out


class TestCollectApi(TempDbCase):
    """页面上的采集范围是多选：校验放在入口，别让一个空平台/混选的城市建出任务。"""

    def _post(self, body):
        task = mock.Mock()
        task.snapshot.return_value = {"id": "x", "runs_total": 2}
        mgr = mock.Mock()
        mgr.start.return_value = (task, None)
        with mock.patch.object(server.Handler, "manager", mgr):
            res = post_api("/api/collect", body)
        return res, mgr

    def test_rejects_empty_or_unknown_selection(self):
        cases = [
            ({"cities": ["东莞"], "query": "IT"}, "请至少选一个平台"),
            ({"platforms": [], "cities": ["东莞"], "query": "IT"}, "请至少选一个平台"),
            ({"platforms": ["zhitong"], "query": "IT"}, "请至少选一个城市"),
            ({"platforms": ["zhitong"], "cities": ["全国", "东莞"], "query": "IT"},
             "「全国」不能和其他城市一起选"),
            ({"platforms": ["zhitong"], "cities": ["火星"], "query": "IT"},
             "不支持的城市：火星"),
            ({"platforms": ["liepin"], "cities": ["东莞"], "query": "IT"},
             "不支持的平台：liepin"),
            ({"platforms": ["zhitong"], "cities": ["东莞"], "query": " "},
             "请输入搜索关键词"),
        ]
        for body, msg in cases:
            with self.subTest(body=body):
                res, mgr = self._post(body)
                self.assertEqual(res["status"], 400)
                self.assertEqual(res["obj"]["error"], msg)
                mgr.start.assert_not_called()

    def test_multi_selection_is_passed_through_deduped_in_order(self):
        res, mgr = self._post({"platforms": ["zhitong", "job51", "zhitong"],
                               "cities": ["东莞", "深圳"], "query": " IT ", "pages": 99})
        self.assertEqual(res["status"], 200)
        mgr.start.assert_called_once_with(["zhitong", "job51"], "IT", ["东莞", "深圳"], 10)
        self.assertEqual(res["obj"]["task"], {"id": "x", "runs_total": 2})

    def test_city_options_are_checkboxes_with_national_default(self):
        html = server.city_options_html()
        self.assertIn('data-city="全国" checked', html)
        self.assertIn('data-city="东莞"', html)
        self.assertNotIn("<option", html)


class TestContentLengthGuard(unittest.TestCase):
    """Content-Length 先验再读：-1 会让 read(-1) 一直读到连接关闭、线程挂住，
    非数字以前掉进兜底变 500。现在都当场 400，也不替陌生客户端缓冲大 body。"""

    def _post_raw(self, cl):
        h = object.__new__(server.Handler)
        h.path = "/api/status"
        h.headers = {"Content-Length": cl, "X-Token": "T"}
        h.rfile = io.BytesIO(b"{}")
        out = {}
        h._json = lambda obj, status=200: out.update(obj=obj, status=status)
        with mock.patch.object(server.Handler, "token", "T"):
            h.do_POST()
        return out

    def test_negative_non_numeric_and_oversized_are_400(self):
        for cl in ("-1", "abc", "2000000"):
            with self.subTest(cl=cl):
                res = self._post_raw(cl)
                self.assertEqual(res["status"], 400)
                self.assertFalse(res["obj"]["ok"])


class TestAnalysisApi(TempDbCase):
    def test_writes_a_score(self):
        db.save_jobs([make_record()])
        res = post_api("/api/analysis", {"platform": "job51", "platform_job_id": "j1",
                                         "score": 81, "reasons": "技术栈对得上"})
        self.assertEqual(res["status"], 200)
        self.assertTrue(res["obj"]["ok"])
        self.assertEqual(db.analysis_keys(), ["job51:j1"])

    def test_bad_token_is_403(self):
        res = post_api("/api/analysis", {"platform": "job51", "platform_job_id": "j1",
                                         "score": 81}, token="wrong")
        self.assertEqual(res["status"], 403)
        self.assertEqual(db.analysis_keys(), [])

    def test_invalid_score_is_400_and_unknown_job_is_404(self):
        self.assertEqual(post_api("/api/analysis", {"platform": "job51",
                                                    "platform_job_id": "j1",
                                                    "score": "很高"})["status"], 400)
        res = post_api("/api/analysis", {"platform": "job51",
                                         "platform_job_id": "ghost", "score": 50})
        self.assertEqual(res["status"], 404)
        self.assertEqual(db.analysis_keys(), [])

    def test_resolve_token(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertNotEqual(server.resolve_token(), server.resolve_token())
        with mock.patch.dict(os.environ, {server.TOKEN_ENV: " fixed "}):
            self.assertEqual(server.resolve_token(), "fixed")


class TestAnalysisDisplay(TempDbCase):
    """匹配度是本地算完再读出来的标注，web 页和离线静态页都只显示，不产生。"""

    def test_fetch_all_carries_the_score_columns(self):
        db.save_jobs([make_record(platform_job_id="j1"),
                      make_record(platform_job_id="j2", title="另一条")])
        db.save_analysis({"platform": "job51", "platform_job_id": "j1", "score": 86,
                          "matched": ["Python"], "gaps": ["K8s"],
                          "reasons": "技术栈吻合", "model": "qwen3.5-9b"})
        rows = {r["platform_job_id"]: r for r in db.fetch_all()}
        self.assertEqual(rows["j1"]["score"], 86)
        self.assertEqual(rows["j1"]["matched"], ["Python"])
        self.assertEqual(rows["j1"]["gaps"], ["K8s"])
        self.assertEqual(rows["j1"]["reasons"], "技术栈吻合")
        # 没跑过模型的：分数是 None（不是 0），页面据此显示占位符
        self.assertIsNone(rows["j2"]["score"])
        self.assertEqual(rows["j2"]["matched"], [])

    def test_fetch_all_works_on_a_database_without_the_analysis_table(self):
        import sqlite3
        old = db.SCHEMA.split("CREATE TABLE IF NOT EXISTS job_analysis")[0]
        db.DB_PATH.unlink()
        conn = sqlite3.connect(db.DB_PATH)
        try:
            conn.executescript(old)
            conn.execute("INSERT INTO jobs(platform,platform_job_id,title,fetched_at) "
                         "VALUES('job51','j1','老职位','2026-09-16 10:00:00')")
            conn.commit()
        finally:
            conn.close()
        rows = db.fetch_all()
        self.assertEqual(rows[0]["title"], "老职位")
        self.assertIsNone(rows[0]["score"])

    def test_score_column_in_both_pages(self):
        rec = dict(make_record(), score=86, reasons="技术栈吻合", gaps=["K8s"])
        web = report.render_web([rec], "T", "")
        static = report._filter_html(report.STATIC_SUB_LINE, [rec], web=False)
        for page in (web, static):
            self.assertIn("<th>匹配度</th>", page)
            self.assertIn('<option value="score_desc">匹配度从高到低</option>', page)
        # 没分数时用占位符，不能显示成 0 分
        self.assertIn("r.score === null || r.score === undefined", web)
        # 理由只进 title 属性，且必须转义（模型输出的文本不可信）
        self.assertIn('`<td title="${esc(why)}">${Math.round(r.score)}</td>`', web)

    def test_export_csv_is_not_widened_by_the_new_column(self):
        """导出列是显式白名单，加了分析表也不该悄悄多出列来。"""
        db.save_jobs([make_record()])
        out = Path(self._tmp.name) / "a.csv"
        db.export_csv(out)
        head = out.read_text(encoding="utf-8-sig").splitlines()[0].split(",")
        self.assertEqual(head, db.EXPORT_FIELDS)
        self.assertNotIn("score", head)


def seed_job_with_detail(pjid="j1", platform="zhitong", description="负责 Python 后端接口"):
    """造一条「采过了、正文也有了」的职位——它是模型唯一能吃的东西。"""
    db.save_jobs([make_record(platform=platform, platform_job_id=pjid)])
    if description is not None:
        db.save_details([{"platform": platform, "platform_job_id": pjid,
                          "description": description, "keyword_tags": ["ERP运维"],
                          "url": f"https://example.com/job/{pjid}"}])


class TestPendingAnalysis(TempDbCase):
    """待分析清单必须只从本地库里挑：拉一批待办绝不能顺带访问招聘平台。"""

    def test_needs_a_body_and_no_score_yet(self):
        seed_job_with_detail("j1")
        seed_job_with_detail("j2", description=None)     # 缺正文：要它就得去开详情页
        seed_job_with_detail("j3")
        db.save_analysis({"platform": "zhitong", "platform_job_id": "j3", "score": 70})
        rows = db.pending_analysis()
        self.assertEqual([r["platform_job_id"] for r in rows], ["j1"])
        self.assertEqual(rows[0]["description"], "负责 Python 后端接口")
        self.assertEqual(rows[0]["keyword_tags"], ["ERP运维"])
        # 技能仍走读取侧口径，喂给模型的和筛选台上看到的是同一套
        self.assertEqual(rows[0]["skills"], ["Python", "Django"])
        self.assertEqual(rows[0]["status"], "")

    def test_redo_platform_filter_and_limit(self):
        for i in ("1", "2", "3"):
            seed_job_with_detail(f"j{i}")
        self.assertEqual(len(db.pending_analysis(limit=2)), 2)
        self.assertEqual(len(db.pending_analysis(limit=0)), 1)   # 下限兜住，别空跑
        db.save_analysis({"platform": "zhitong", "platform_job_id": "j1", "score": 55})
        self.assertEqual(len(db.pending_analysis()), 2)
        self.assertEqual(len(db.pending_analysis(redo=True)), 3)
        seed_job_with_detail("z1", platform="job51")
        self.assertEqual({r["platform_job_id"]
                          for r in db.pending_analysis(platform="job51")}, {"z1"})

    def test_database_without_any_detail_table(self):
        import sqlite3
        old = db.SCHEMA.split("CREATE TABLE IF NOT EXISTS job_details")[0]
        db.DB_PATH.unlink()
        conn = sqlite3.connect(db.DB_PATH)
        try:
            conn.executescript(old)
            conn.execute("INSERT INTO jobs(platform,platform_job_id,title,fetched_at) "
                         "VALUES('job51','j1','老职位','2026-09-16 10:00:00')")
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(db.pending_analysis(), [])

    def test_old_database_without_the_analysis_table(self):
        """只有 jobs + job_details 的老库：正文都算没分析过，不能因为缺表就报错。"""
        import sqlite3
        old = db.SCHEMA.split("CREATE TABLE IF NOT EXISTS job_analysis")[0]
        db.DB_PATH.unlink()
        conn = sqlite3.connect(db.DB_PATH)
        try:
            conn.executescript(old)
            conn.execute("INSERT INTO jobs(platform,platform_job_id,title,fetched_at) "
                         "VALUES('zhitong','j1','IT运维','2026-09-16 10:00:00')")
            conn.execute("INSERT INTO job_details(platform,platform_job_id,description) "
                         "VALUES('zhitong','j1','正文')")
            conn.commit()
        finally:
            conn.close()
        rows = db.pending_analysis()
        self.assertEqual([r["platform_job_id"] for r in rows], ["j1"])


def get_api(path: str, token: str = "T"):
    """同 post_api：不打网络走一遍 GET 分发，拿回准备发出的 JSON 和状态码。"""
    h = object.__new__(server.Handler)
    h.path = path
    h.headers = {"X-Token": token}
    out = {}
    h._json = lambda obj, status=200: out.update(obj=obj, status=status)
    with mock.patch.object(server.Handler, "token", "T"):
        h.do_GET()
    return out


class TestPendingAnalysisApi(TempDbCase):
    """n8n 侧的只读口子：一次就给够「职位 + 正文」，省掉逐条回查。"""

    def test_returns_jobs_with_their_body(self):
        seed_job_with_detail("j1")
        seed_job_with_detail("j2", description=None)
        res = get_api("/api/pending_analysis?limit=5&platform=zhitong")
        self.assertEqual(res["status"], 200)
        jobs = res["obj"]["jobs"]
        self.assertEqual([j["platform_job_id"] for j in jobs], ["j1"])
        self.assertIn("Python", jobs[0]["description"])

    def test_bad_token_is_403(self):
        self.assertEqual(get_api("/api/pending_analysis", token="wrong")["status"], 403)

    def test_garbage_or_huge_limit_does_not_break(self):
        seed_job_with_detail("j1")
        for q in ("", "?limit=abc", "?limit=100000", "?redo=1&platform="):
            with self.subTest(query=q):
                res = get_api("/api/pending_analysis" + q)
                self.assertEqual(res["status"], 200)
                self.assertEqual(len(res["obj"]["jobs"]), 1)

    def test_redo_brings_back_analyzed_jobs(self):
        seed_job_with_detail("j1")
        db.save_analysis({"platform": "zhitong", "platform_job_id": "j1", "score": 60})
        self.assertEqual(get_api("/api/pending_analysis")["obj"]["jobs"], [])
        self.assertEqual(len(get_api("/api/pending_analysis?redo=true")["obj"]["jobs"]), 1)


class TestLocalModelAnalysis(TempDbCase):
    """本机模型这一段全是纯本地：自述在文件里、请求只发往 127.0.0.1、结果写 job_analysis。"""

    def setUp(self):
        super().setUp()
        self.profile = Path(self._tmp.name) / "profile.txt"
        self.profile.write_text("我会 Python 三年，写过爬虫和 Flask 接口；期望深圳，"
                                "月薪 15K 起；不接受长期出差。", encoding="utf-8")
        seed_job_with_detail("j1")
        self.job = db.pending_analysis()[0]

    def test_missing_profile_tells_you_what_to_write(self):
        self.profile.unlink()
        with self.assertRaises(RuntimeError) as ctx:
            analyze.load_profile(self.profile)
        self.assertIn("自述文件", str(ctx.exception))
        self.assertIn("data", str(ctx.exception))
        with self.assertRaises(RuntimeError):
            self.profile.write_text("随便", encoding="utf-8")
            analyze.load_profile(self.profile)
        self.profile.write_text("x" * (analyze.PROFILE_MAX + 500), encoding="utf-8")
        self.assertEqual(len(analyze.load_profile(self.profile)), analyze.PROFILE_MAX)

    def test_prompt_carries_the_job_and_truncates_a_long_body(self):
        job = dict(self.job, description="岗" * (config.JD_MAX_CHARS + 300))
        msgs = analyze.build_messages(job, analyze.load_profile(self.profile))
        self.assertEqual(msgs[0]["role"], "system")
        self.assertIn("90 以上", msgs[0]["content"])       # 打分细则钉在提示词里
        user = msgs[1]["content"]
        self.assertIn("Python开发工程师", user)
        self.assertIn("15-25K·14薪", user)
        self.assertIn("我会 Python 三年", user)
        self.assertIn("已截断", user)
        self.assertLess(len(user), config.JD_MAX_CHARS + 1500)

    def test_job_without_a_body_says_so(self):
        user = analyze.build_messages(dict(self.job, description=""), "我")[1]["content"]
        self.assertIn("没有正文", user)

    def test_extract_json_survives_wrapping(self):
        cases = {
            '{"score": 88}': {"score": 88},
            '```json\n{"score": 70}\n```': {"score": 70},
            '好的，结果如下：\n{"score": 65,"reasons":"技能对得上"}': {"score": 65,
                                                                       "reasons": "技能对得上"},
            '{"score": 60, "reasons": "带\\"引号\\"和{花括号}都别断"}':
                {"score": 60, "reasons": '带"引号"和{花括号}都别断'},
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw[:20]):
                self.assertEqual(analyze.extract_json(raw), expected)
        for bad in ("我觉得不太合适", '{"score": 60', 'not json: {"score": [}'):
            with self.subTest(raw=bad), self.assertRaises(ValueError):
                analyze.extract_json(bad)

    def test_parse_result_shapes_what_db_expects(self):
        got = analyze.parse_result('{"score": 82, "matched": ["Python"], "gaps": ["K8s"],'
                                   ' "reasons": "技术栈吻合"}', self.job, "qwen3.5-9b")
        self.assertEqual(got["platform"], "zhitong")
        self.assertEqual(got["platform_job_id"], "j1")
        self.assertEqual(got["score"], 82)
        self.assertEqual(got["model"], "qwen3.5-9b")
        with self.assertRaises(ValueError):
            analyze.parse_result('{"matched": []}', self.job, "m")

    def test_chat_payload_is_deterministic(self):
        sent = {}

        class Resp:
            def read(self):
                return json.dumps({"choices": [{"message": {"content": "{}"}}]}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            sent["url"] = req.full_url
            sent["body"] = json.loads(req.data.decode("utf-8"))
            return Resp()

        with mock.patch.object(analyze.urllib.request, "urlopen", fake_urlopen):
            out = analyze.post_chat([{"role": "user", "content": "hi"}],
                                    "http://127.0.0.1:1234/v1/", "qwen3.5-9b")
        self.assertEqual(out, "{}")
        self.assertEqual(sent["url"], "http://127.0.0.1:1234/v1/chat/completions")
        self.assertEqual(sent["body"]["model"], "qwen3.5-9b")
        self.assertEqual(sent["body"]["temperature"], 0)
        self.assertFalse(sent["body"]["stream"])

    def test_empty_model_output_is_a_failure(self):
        class Resp:
            def read(self):
                return json.dumps({"choices": [{"message": {"content": "   "}}]}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with mock.patch.object(analyze.urllib.request, "urlopen",
                               lambda req, timeout=None: Resp()):
            with self.assertRaises(RuntimeError):
                analyze.post_chat([{"role": "user", "content": "x"}],
                                  "http://127.0.0.1:1234/v1", "m")

    def test_scores_land_in_the_database(self):
        with mock.patch.object(analyze, "post_chat",
                               return_value='{"score": 87, "reasons": "对得上"}') as chat, \
                mock.patch("builtins.print"):
            res = analyze.analyze_jobs([self.job], profile="我会 Python",
                                       url="http://127.0.0.1:1234/v1", model="qwen3.5-9b")
        self.assertEqual({k: res[k] for k in ("analyzed", "failed")},
                         {"analyzed": 1, "failed": 0})
        self.assertGreaterEqual(res["seconds"], 0)   # 耗时是逐条记的，别信"9B 应该慢"这种猜
        self.assertEqual(chat.call_args[0][1], "http://127.0.0.1:1234/v1")
        row = {r["platform_job_id"]: r for r in db.fetch_all()}["j1"]
        self.assertEqual(row["score"], 87)
        self.assertEqual(row["model"], "qwen3.5-9b")

    def test_one_bad_answer_skips_that_job_only(self):
        answers = ['{"matched": []}', '{"score": 50}']
        with mock.patch.object(analyze, "post_chat", side_effect=answers), \
                mock.patch("builtins.print"):
            seed_job_with_detail("j2")
            res = analyze.analyze_jobs(db.pending_analysis(), profile="我", model="m")
        self.assertEqual({k: res[k] for k in ("analyzed", "failed")},
                         {"analyzed": 1, "failed": 1})
        # 到底是哪条成功的不保证（同一秒采的），但另一条必须照常跑完并落库
        self.assertEqual(len(db.analysis_keys()), 1)

    def test_three_failures_in_a_row_stop_the_whole_run(self):
        """连着失败多半是接口或模型没配好，别把二十条都耗在同一个坑里。"""
        seed_job_with_detail("j2")
        seed_job_with_detail("j3")
        with mock.patch.object(analyze, "post_chat",
                               side_effect=RuntimeError("连不上")), \
                mock.patch("builtins.print"):
            with self.assertRaises(RuntimeError) as ctx:
                analyze.analyze_jobs(db.pending_analysis(), profile="我", model="m")
        self.assertIn("停手", str(ctx.exception))
        self.assertEqual(db.analysis_keys(), [])

    def test_dry_run_never_calls_the_model_or_writes(self):
        with mock.patch.object(analyze, "post_chat",
                               side_effect=AssertionError("不该发请求")), \
                mock.patch("builtins.print") as out:
            res = analyze.analyze_jobs([self.job], profile="我", dry_run=True)
        self.assertEqual(res["analyzed"], 0)
        self.assertEqual(db.analysis_keys(), [])
        printed = "".join(str(c) for c in out.call_args_list)
        self.assertIn("预览", printed)

    def test_run_picks_the_loaded_model_and_reports(self):
        with mock.patch.object(analyze, "list_models", return_value=["qwen3.5-9b"]), \
                mock.patch.object(analyze, "post_chat",
                                  return_value='{"score": 71}') as chat, \
                mock.patch("builtins.print") as out:
            res = analyze.run(profile_path=self.profile, model="")
        self.assertEqual(res["analyzed"], 1)
        self.assertEqual(chat.call_args[0][2], "qwen3.5-9b")
        self.assertEqual(db.analysis_keys(), ["zhitong:j1"])
        # 报了平均秒数才知道一晚跑二十条到底要多久
        self.assertIn("秒/条", "".join(str(c) for c in out.call_args_list))

    def test_run_without_an_explicit_model_needs_a_loaded_one(self):
        with mock.patch.object(analyze, "list_models", return_value=[]):
            with self.assertRaises(RuntimeError) as ctx:
                analyze.resolve_model("http://127.0.0.1:1234/v1")
        self.assertIn("加载", str(ctx.exception))
        self.assertEqual(analyze.resolve_model("http://x/v1", "qwen3.5-9b"), "qwen3.5-9b")

    def test_run_with_nothing_to_do_says_so(self):
        db.DB_PATH.unlink()
        db.init_db()
        with mock.patch.object(analyze, "post_chat",
                               side_effect=AssertionError("不该发请求")), \
                mock.patch("builtins.print") as out:
            res = analyze.run(profile_path=self.profile)
        self.assertEqual(res, {"analyzed": 0, "failed": 0, "seconds": 0.0})
        self.assertIn("detail", "".join(str(c) for c in out.call_args_list))

class TrashCase(TempDbCase):
    """删除依赖 data/trash 里的批次文件；测试把回收站挪进临时目录，不碰真那个。"""

    def setUp(self):
        super().setUp()
        self.trash = Path(self._tmp.name) / "trash"
        self._orig_trash = db.TRASH_DIR
        db.TRASH_DIR = self.trash

    def tearDown(self):
        db.TRASH_DIR = self._orig_trash
        super().tearDown()


def seed_deletable(pjid="j1", platform="zhitong"):
    """采过、有正文、有匹配度、也标过状态的职位——删的时候要三张表一起走。"""
    seed_job_with_detail(pjid, platform=platform)
    db.save_analysis({"platform": platform, "platform_job_id": pjid, "score": 88,
                      "matched": ["Python"], "gaps": ["K8s"], "reasons": "对口"})
    row = next(r for r in db.fetch_all() if r["platform_job_id"] == pjid)
    db.set_status(row["id"], "想投", "已看过 JD")
    return row["id"]


class TestJobDeletion(TrashCase):
    def test_delete_takes_body_and_score_with_it(self):
        jid = seed_deletable()
        res = db.delete_jobs([jid])
        self.assertEqual(res["deleted"], 1)
        self.assertEqual(res["details"], 1)
        self.assertEqual(res["analysis"], 1)
        self.assertEqual(db.fetch_all(), [])
        self.assertEqual(db.detail_keys(), [])
        self.assertEqual(db.analysis_keys(), [])
        self.assertTrue((self.trash / res["batch"]).exists())

    def test_undo_puts_back_marks_and_score(self):
        jid = seed_deletable()
        batch = db.delete_jobs([jid])["batch"]
        res = db.restore_batch(batch)
        self.assertEqual((res["jobs"], res["details"], res["analysis"]), (1, 1, 1))
        r = db.fetch_all()[0]
        self.assertEqual(r["status"], "想投")
        self.assertEqual(r["note"], "已看过 JD")
        self.assertEqual(r["score"], 88)
        self.assertEqual(r["skills"], ["Python", "Django"])
        self.assertEqual(db.get_detail("zhitong", "j1")["description"], "负责 Python 后端接口")
        self.assertEqual(db.trash_batches(), [])      # 批次用完即废

    def test_undo_keeps_the_last_seen_date(self):
        """撤销得把 last_seen_at 一起放回来。它不在采集列里，以前撤销后只剩
        fetched_at，而页面的 seenAt 是 last_seen_at || fetched_at——第一次采到
        在 14 天前的在职职位，撤回来就会被标成「疑似已下架」。"""
        jid = seed_deletable()
        with db._conn() as c:
            c.execute("UPDATE jobs SET last_seen_at='2026-10-20 21:27:31' WHERE id=?", (jid,))
        batch = db.delete_jobs([jid])["batch"]
        db.restore_batch(batch)
        r = db.fetch_all()[0]
        self.assertEqual(r["last_seen_at"], "2026-10-20 21:27:31")
        self.assertNotEqual(r["last_seen_at"], r["fetched_at"])

    def test_a_batch_can_only_be_undoned_once(self):
        batch = db.delete_jobs([seed_deletable()])["batch"]
        self.assertIsNotNone(db.restore_batch(batch))
        self.assertIsNone(db.restore_batch(batch))

    def test_undo_keeps_detail_source_so_list_body_never_eats_quota(self):
        """job51 列表随采的正文 source='list'，不占逐条额度；撤销要是丢了这列，
        COALESCE(source,'detail') 会把它当成逐条抓的，今天 20 条额度被假占
        （2026-11-16 库副本往返实测发现）。"""
        db.save_jobs([make_record(platform="job51", platform_job_id="g1")])
        db.save_list_details([{"platform": "job51", "platform_job_id": "g1",
                               "description": "列表随采正文", "keyword_tags": [], "url": "u"}])
        self.assertEqual(db.count_details_today(), 0)
        jid = db.fetch_all()[0]["id"]
        batch = db.delete_jobs([jid])["batch"]
        db.restore_batch(batch)
        with db._conn() as c:
            src = c.execute("SELECT source FROM job_details"
                            " WHERE platform='job51' AND platform_job_id='g1'").fetchone()[0]
        self.assertEqual(src, "list")
        self.assertEqual(db.count_details_today(), 0)

    def test_undo_does_not_clobber_a_newer_mark(self):
        """撤销用的是 INSERT OR IGNORE：删后又放出来重采/又标的，以库里当下那条为准。"""
        batch = db.delete_jobs([seed_deletable()])["batch"]
        db.unblock_jobs(["zhitong:j1"])         # 屏蔽名单不放回去，这条根本采不回来
        db.save_jobs([make_record(platform="zhitong", platform_job_id="j1")])
        new = db.fetch_all()[0]
        db.set_status(new["id"], "已投")
        res = db.restore_batch(batch)
        self.assertEqual(res["jobs"], 0)
        rows = db.fetch_all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "已投")

    def test_undo_keeps_the_status_date(self):
        """删掉再撤回来，投递日期必须原样回来：漏了这列，那条从此进不了按天算的分母。"""
        jid = seed_deletable()                    # 里面已经标了「想投」
        stamp_before = db.fetch_all()[0]["status_at"]
        self.assertIsNotNone(stamp_before)
        batch = db.delete_jobs([jid])["batch"]
        db.unblock_jobs(["zhitong:j1"])
        db.restore_batch(batch)
        row = db.fetch_all()[0]
        self.assertEqual((row["status"], row["status_at"]), ("想投", stamp_before))

    def test_delete_blocks_future_collects(self):
        """删过的职位进屏蔽名单：第二天再采同一个关键词不会把它原样带回来。"""
        jid = seed_deletable()
        db.delete_jobs([jid])
        st = db.save_stats([make_record(platform="zhitong", platform_job_id="j1")])
        self.assertEqual((st["added"], st["refreshed"], st["blocked"]), (0, 0, 1))
        self.assertEqual(db.fetch_all(), [])
        self.assertEqual([(r["platform"], r["platform_job_id"]) for r in db.blocked_list()],
                         [("zhitong", "j1")])

    def test_undo_clears_the_block(self):
        """撤销一批删除要连屏蔽名单一起退，不然职位回来了却永远不再被刷新。"""
        batch = db.delete_jobs([seed_deletable()])["batch"]
        db.restore_batch(batch)
        self.assertEqual(db.blocked_list(), [])
        st = db.save_stats([make_record(platform="zhitong", platform_job_id="j1")])
        self.assertEqual((st["added"], st["refreshed"], st["blocked"]), (0, 1, 0))

    def test_unblock_accepts_a_whole_platform(self):
        jid = seed_deletable()
        db.delete_jobs([jid])
        self.assertEqual(db.unblock_jobs(["zhitong:*"]), 1)
        self.assertEqual(db.blocked_list(), [])

    def test_bad_input_never_touches_the_database(self):
        jid = seed_deletable()
        for bad in ([], ["abc"], [9999], list(range(1, db.DELETE_MAX + 2))):
            with self.assertRaises(ValueError):
                db.delete_jobs(bad)
        self.assertEqual(len(db.fetch_all()), 1)
        self.assertFalse(self.trash.exists())
        with self.assertRaises(ValueError) as ctx:
            db.delete_jobs(list(range(1, db.DELETE_MAX + 2)))
        self.assertIn(str(db.DELETE_MAX), str(ctx.exception))

    def test_batch_name_cannot_point_outside_the_trash(self):
        for bad in ("../jobs.db", "jobs.db", "", None, "20261011-225033-82bc3c.json/x"):
            with self.assertRaises(ValueError):
                db.restore_batch(bad)

    def test_batches_are_listed_with_their_titles(self):
        a = seed_deletable("j1")
        b = seed_deletable("j2")
        db.delete_jobs([a])
        db.delete_jobs([b])
        batches = db.trash_batches()
        self.assertEqual(len(batches), 2)
        self.assertEqual(sorted(x["jobs"] for x in batches), [1, 1])
        self.assertTrue(all(x["titles"] and x["deleted_at"] for x in batches))

    def test_trash_keeps_only_the_newest_batches(self):
        for _ in range(db.TRASH_KEEP + 5):
            db._write_trash({"deleted_at": "", "jobs": [], "details": [], "analysis": []})
        self.assertEqual(len(list(self.trash.glob("*.json"))), db.TRASH_KEEP)

    def test_a_broken_trash_file_does_not_hide_the_rest(self):
        b1 = db.delete_jobs([seed_deletable("j1")])["batch"]
        b2 = db.delete_jobs([seed_deletable("j2")])["batch"]
        (self.trash / b1).write_text("{不是 JSON", encoding="utf-8")
        self.assertEqual([x["batch"] for x in db.trash_batches()], [b2])


class TestDeleteApi(TrashCase):
    def test_delete_then_restore_through_the_api(self):
        jid = seed_deletable()
        res = post_api("/api/delete", {"ids": [jid]})
        self.assertEqual(res["status"], 200)
        self.assertTrue(res["obj"]["ok"])
        self.assertEqual(res["obj"]["deleted"], 1)
        self.assertEqual(db.fetch_all(), [])
        back = post_api("/api/restore", {"batch": res["obj"]["batch"]})
        self.assertEqual(back["status"], 200)
        self.assertEqual(len(db.fetch_all()), 1)
        self.assertEqual(post_api("/api/restore", {"batch": res["obj"]["batch"]})["status"], 404)

    def test_bad_token_cannot_delete_anything(self):
        jid = seed_deletable()
        res = post_api("/api/delete", {"ids": [jid]}, token="wrong")
        self.assertEqual(res["status"], 403)
        self.assertEqual(len(db.fetch_all()), 1)
        self.assertFalse(self.trash.exists())

    def test_wrong_shapes_are_400_not_500(self):
        jid = seed_deletable()
        self.assertEqual(post_api("/api/delete", {"ids": jid})["status"], 400)
        self.assertEqual(post_api("/api/delete", {"ids": [9999]})["status"], 400)
        self.assertEqual(post_api("/api/restore", {"batch": "../x"})["status"], 400)
        self.assertEqual(len(db.fetch_all()), 1)

    def test_trash_endpoint_is_tokened_too(self):
        db.delete_jobs([seed_deletable()])
        res = get_api("/api/trash")
        self.assertEqual(res["status"], 200)
        self.assertEqual(len(res["obj"]["batches"]), 1)
        self.assertEqual(get_api("/api/trash", token="wrong")["status"], 403)


class TestDeleteUi(unittest.TestCase):
    """删除只在服务版页面上有；导出的静态页连按钮都不该出现。"""

    def test_web_page_has_the_controls_static_page_has_none(self):
        rec = dict(make_record(), id=1)
        web = report.render_web([rec], "T", "")
        for needle in ("<th>删</th>", 'id="btn-del-filtered"', "撤销这次删除",
                       "async function askDelete", "async function loadTrash",
                       "'/api/delete'", "'/api/restore'", "'/api/trash'",
                       'data-del="${esc(r.id)}'):
            self.assertIn(needle, web)
        static = report._filter_html(report.STATIC_SUB_LINE, [rec], web=False)
        for needle in ("<th>删</th>", "async function askDelete", "id=\"btn-del-filtered\"",
                       "loadTrash()", "/api/delete"):
            self.assertNotIn(needle, static)

    def test_confirm_text_says_it_touches_no_platform(self):
        web = report.render_web([], "T", "")
        self.assertIn("不会访问任何招聘平台", web)
        self.assertIn("data/trash", web)


class TestDetailIdsArg(TempDbCase):
    """`detail --ids` 是"只给这几种抓正文"的口子，参数写错时最坏能把一天额度花光。"""

    def _run(self, **over):
        from types import SimpleNamespace
        args = SimpleNamespace(platform=None, ids=None, limit=None, force=False)
        for k, v in over.items():
            setattr(args, k, v)
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_detail(args)
        return buf.getvalue()

    def test_empty_ids_refuses_instead_of_falling_into_bulk(self):
        # 以前写成 `if args.ids:`，空串被当成"没给这个参数"，直接掉进自动补抓，
        # 实测一天 20 条正文额度静默花光，用户以为只是查了一下。
        db.save_jobs([make_record(platform="zhitong", platform_job_id="j1")])
        with mock.patch.object(details, "collect_details") as grab, \
                mock.patch.object(db, "jobs_missing_details",
                                  return_value=[{"id": 1}]) as pick:
            with self.assertRaises(RuntimeError) as cm:
                self._run(ids="")
            self.assertIn("空的", str(cm.exception))
            grab.assert_not_called()
            pick.assert_not_called()

    def test_non_numeric_ids_gives_a_chinese_line_not_a_traceback(self):
        with mock.patch.object(db, "jobs_by_ids") as lookup, \
                mock.patch.object(details, "collect_details") as grab:
            with self.assertRaises(RuntimeError) as cm:
                self._run(ids="562,56a")
        self.assertIn("不是行号", str(cm.exception))
        lookup.assert_not_called()
        grab.assert_not_called()

    def test_ids_that_match_nothing_says_so(self):
        # 不能落到下面那句"都已经有正文了，或今日额度用完"上，那是另一个原因
        with mock.patch.object(db, "jobs_by_ids", return_value=[]), \
                mock.patch.object(details, "collect_details") as grab:
            text = self._run(ids="99998,99999")
        self.assertIn("都找不到", text)
        grab.assert_not_called()

    def test_given_ids_fetch_only_those(self):
        db.save_jobs([make_record(platform="zhitong", platform_job_id="j1"),
                      make_record(platform="zhitong", platform_job_id="j2")])
        picked = [r for r in db.fetch_all() if r["platform_job_id"] == "j2"]
        with mock.patch.object(db, "jobs_by_ids", return_value=picked) as lookup, \
                mock.patch.object(details, "collect_details", return_value={}) as grab:
            self._run(ids="42")
        lookup.assert_called_once_with([42])
        grab.assert_called_once_with(picked, limit=None, force=False)


class TestUtf8Stdout(unittest.TestCase):
    """重定向/管道时 stdout 会退回机器代码页（这台是 cp936），标题里一个表情就能打断整条命令。"""

    def test_piped_stdout_survives_a_non_gbk_title(self):
        buf = io.BytesIO()
        fake = io.TextIOWrapper(buf, encoding="gbk", newline="")
        with mock.patch.object(sys, "stdout", fake), mock.patch.object(sys, "stderr", fake):
            cli._force_utf8_output()
            self.assertEqual(sys.stdout.encoding.lower().replace("-", ""), "utf8")
            print("🔥 IT运维")   # 不改编码的话这一句就 UnicodeEncodeError
            sys.stdout.flush()
        self.assertIn("🔥", buf.getvalue().decode("utf-8"))

    def test_a_stdout_without_reconfigure_is_left_alone(self):
        # 测试里 redirect_stdout 换进来的是 StringIO，没有 reconfigure；不许因此炸
        with redirect_stdout(io.StringIO()) as buf:
            cli._force_utf8_output()
            print("照常")
        self.assertIn("照常", buf.getvalue())


def get_api(path: str, token: str = "T"):
    """同 post_api：不打网络走一遍 GET 分发，拿回它准备发出的 JSON 和状态码。"""
    h = object.__new__(server.Handler)
    h.path = path
    h.headers = {"X-Token": token}
    out = {}
    h._json = lambda obj, status=200: out.update(obj=obj, status=status)
    with mock.patch.object(server.Handler, "token", "T"):
        h.do_GET()
    return out


class TestApiCatchAll(TempDbCase):
    """路由里冒出来的异常必须变成一条带原因的错误，不能把连接掐断让用户看 "Failed to fetch"。"""

    def test_db_error_on_get_returns_500_with_the_reason(self):
        import sqlite3
        err = sqlite3.OperationalError("no such table: job_analysis")
        with mock.patch.object(db, "fetch_all", side_effect=err):
            res = get_api("/api/jobs")
        self.assertEqual(res["status"], 500)
        self.assertFalse(res["obj"]["ok"])
        self.assertIn("no such table: job_analysis", res["obj"]["error"])

    def test_db_error_on_post_returns_500_with_the_reason(self):
        with mock.patch.object(db, "set_status", side_effect=OSError("磁盘没空间")):
            res = post_api("/api/status", {"id": 1, "status": "想投"})
        self.assertEqual(res["status"], 500)
        self.assertIn("磁盘没空间", res["obj"]["error"])

    def test_client_already_gone_gets_no_response(self):
        # 刷新/关标签时连接先没了，这时候再想发响应就是第二层异常
        sent = []
        h = object.__new__(server.Handler)
        h.path = "/api/jobs"
        h.headers = {"X-Token": "T"}
        h._json = lambda obj, status=200: sent.append(obj)
        with mock.patch.object(server.Handler, "token", "T"), \
                mock.patch.object(db, "fetch_all", side_effect=BrokenPipeError):
            self.assertIsNone(h.do_GET())
        self.assertEqual(sent, [])


class TestSalaryReadSide(TempDbCase):
    """薪资数值列是入库那一刻算的；口径升级之前入库的老行得在读取侧补回来，不改库。"""

    def _seed_old_row(self, raw, pjid="j1"):
        db.save_jobs([dict(make_record(platform="zhitong", platform_job_id=pjid,
                                       salary_raw=raw, salary_min=None,
                                       salary_max=None, salary_months=None))])

    def _stored(self, pjid="j1"):
        import sqlite3
        conn = sqlite3.connect(db.DB_PATH)
        try:
            return conn.execute("SELECT salary_min, salary_max FROM jobs"
                                " WHERE platform_job_id=?", (pjid,)).fetchone()
        finally:
            conn.close()

    def test_old_row_gets_its_range_back_on_read(self):
        self._seed_old_row("7千-1.3万")
        self.assertEqual(self._stored(), (None, None))   # 库里确实是空的
        row = db.fetch_all()[0]
        self.assertEqual((row["salary_min"], row["salary_max"]), (7.0, 13.0))
        self.assertEqual(row["salary_months"], 12)
        self.assertEqual(db.annual_wan(row), 12.0)

    def test_read_side_fallback_does_not_rewrite_the_library(self):
        self._seed_old_row("8千-1.2万")
        db.fetch_all()
        self.assertEqual(self._stored(), (None, None))

    def test_negotiable_salary_stays_empty(self):
        self._seed_old_row("面议")
        row = db.fetch_all()[0]
        self.assertIsNone(row["salary_min"])
        self.assertIsNone(db.annual_wan(row))

    def test_boss_only_k_format_is_untouched(self):
        self._seed_old_row("15-25K", pjid="b1")
        row = next(r for r in db.fetch_all() if r["platform_job_id"] == "b1")
        self.assertEqual((row["salary_min"], row["salary_max"]), (15.0, 25.0))


class TestSalaryCharts(unittest.TestCase):
    def _rec(self, pjid, lo, hi):
        return dict(make_record(platform="zhitong", platform_job_id=pjid,
                                salary_raw=f"{lo}-{hi}K", salary_min=float(lo),
                                salary_max=float(hi)))

    def test_top_bin_has_no_ceiling(self):
        # 以前最后一档是 60-100k：一条 12 万的岗位哪个档都不落，
        # 柱子加起来比总数小，读起来像"高薪岗位更少"
        stats = report.build_stats([self._rec("a", 120, 120), self._rec("b", 8, 8)])
        self.assertEqual(sum(stats["salary_hist_values"]), 2)
        self.assertEqual(stats["salary_hist_labels"][-1], "100k以上")

    def test_both_sides_use_the_two_middle_rule(self):
        four = [self._rec("a", 10, 10), self._rec("b", 12, 12),
                self._rec("c", 14, 14), self._rec("d", 100, 100)]
        # 报告侧：偶数条取中间两个的平均 = 13.0（取上中位会是 14.0）
        self.assertEqual(report.build_stats(four)["salary_median"], 13.0)
        web = report.render_web(four, "T", "")
        self.assertIn("function median(", web)
        self.assertIn("(sorted[n/2-1] + sorted[n/2]) / 2", web)
        self.assertNotIn("Math.floor(mids.length/2)", web)


class TestTrashAtomic(TrashCase):
    def test_batch_file_is_the_only_thing_left_behind(self):
        name = db._write_trash({"deleted_at": "2026-10-22 00:00:00",
                                "jobs": [make_record()], "details": [], "analysis": []},
                               self.trash)
        left = sorted(p.name for p in self.trash.iterdir())
        self.assertEqual(left, [name])   # 没有 .tmp 残留
        payload = json.loads((self.trash / name).read_text(encoding="utf-8"))
        self.assertEqual(payload["jobs"][0]["platform_job_id"], "j1")

    def test_delete_leaves_no_temp_file_and_still_offers_undo(self):
        jid = seed_deletable()
        db.delete_jobs([jid], trash_dir=self.trash)
        self.assertEqual([p.name for p in self.trash.iterdir()
                          if p.suffix != ".json"], [])
        self.assertEqual(len(db.trash_batches(self.trash)), 1)


class TestLaunchCleansDriver(unittest.TestCase):
    """两次通道都起不来时，那个 Playwright driver 子进程已经归我们管了。"""

    def _fake_pw(self, side_effect):
        # launch() 里拿的是 sync_playwright().start()，驱动要挂在 start 的返回值上
        driver = mock.Mock()
        driver.chromium.launch.side_effect = side_effect
        mgr = mock.Mock()
        mgr.start.return_value = driver
        return mock.patch.object(browser, "sync_playwright", return_value=mgr), driver

    def test_both_channels_failing_stops_the_driver(self):
        patch, driver = self._fake_pw([PlaywrightError("没有 Chrome"),
                                       PlaywrightError("没装 Chromium")])
        with patch:
            with self.assertRaises(PlaywrightError):
                browser.launch()
        driver.stop.assert_called_once()

    def test_fallback_channel_keeps_the_driver(self):
        patch, driver = self._fake_pw([PlaywrightError("没有 Chrome"), mock.Mock()])
        with patch:
            p, _br = browser.launch()
        self.assertIs(p, driver)
        driver.stop.assert_not_called()


class TestUnfilledProfile(unittest.TestCase):
    """自述文件是匹配度的尺子：【】没填完，就不能报"已就绪"，更不能拿它算分。"""

    def _profile(self, text):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = Path(tmp.name) / "profile.txt"
        p.write_text(text, encoding="utf-8")
        return p

    def test_placeholders_are_counted(self):
        self.assertEqual(analyze.placeholders("年限：【几年】，下限【例：8k】"),
                         ["【几年】", "【例：8k】"])
        self.assertEqual(analyze.placeholders("Python 三年，期望东莞，8k 起"), [])

    def test_analyze_refuses_a_template_profile(self):
        p = self._profile("总工作年限：【几年】\n能接受的月薪下限：【例：8k】\n"
                          "学历 / 专业：【例：大专 计算机应用技术】\n")
        with self.assertRaises(RuntimeError) as ctx:
            analyze.load_profile(p)
        msg = str(ctx.exception)
        self.assertIn("3 处【】没填", msg)
        self.assertIn("没刻度", msg)
        # 拦下来得同时给一条立刻能用的出路，不然他只知道跑不了
        self.assertIn("--dry-run", msg)

    def test_dry_run_still_previews_a_template(self):
        # 预览不发消息也不写库，占位没填也该让他看清提示词长什么样
        p = self._profile("总工作年限：【几年】，其余内容凑够十个字以上")
        self.assertIn("【几年】", analyze.load_profile(p, allow_placeholders=True))

    def test_filled_profile_passes(self):
        p = self._profile("总工作年限：3 年\n月薪下限：8k\n学历：大专 计算机应用技术")
        self.assertIn("8k", analyze.load_profile(p))

    def test_status_names_the_unfilled_placeholders(self):
        p = self._profile("总工作年限：【几年】\n月薪下限：【例：8k】\n学历：大专 计算机")
        buf = io.StringIO()
        with mock.patch.object(cli, "ANALYSIS_PROFILE_PATH", p), \
             mock.patch.object(cli.db, "jobs_missing_details", return_value=[]), \
             mock.patch.object(cli.db, "pending_analysis", return_value=[]), \
             redirect_stdout(buf):
            cli._analysis_check()
        text = buf.getvalue()
        self.assertIn("2 处【】没填", text)
        self.assertNotIn("已就绪", text)


class TestReportWithoutChartLibrary(unittest.TestCase):
    """统计报告的图表库是从 CDN 拉的：没网时得说清楚，不能留七块空白。"""

    def setUp(self):
        self.html = report.render(report.build_stats(
            [make_record(platform_job_id="j1", salary_raw="10-15K"),
             make_record(platform_job_id="j2", salary_raw="6千")]))

    def test_offline_notice_exists_and_comes_first(self):
        self.assertIn("图没画出来", self.html)
        # 提示必须排在第一次用 echarts 之前，否则 ReferenceError 先把整个 script 打断
        self.assertLess(self.html.find("window.echarts"), self.html.find("echarts.init"))

    def test_chart_script_braces_stay_balanced(self):
        # 这个分支是 if/else 两块，少闭合一个 } 的现场就是"页面照开、图全没了"
        self.assertEqual(self.html.count("{"), self.html.count("}"))


class TestPublishedAndPlatformState(TempDbCase):
    """平台发布时间/招聘状态三列：采集即核实、过期标志、刷新不抹平台原文。"""

    def test_expire_flag_marks_delisted_and_verified(self):
        db.save_jobs([make_record(platform="job51", platform_job_id="e1",
                                  published_raw="2026-11-17 18:05:51", _expire=True)])
        row = db.fetch_all()[0]
        self.assertTrue(row["delisted_at"])
        self.assertTrue(row["verified_at"])
        self.assertEqual(row["published_raw"], "2026-11-17 18:05:51")

    def test_recollect_without_flag_clears_delisted(self):
        db.save_jobs([make_record(platform="job51", platform_job_id="e1", _expire=True)])
        db.save_jobs([make_record(platform="job51", platform_job_id="e1")])
        row = db.fetch_all()[0]
        self.assertEqual(row["delisted_at"], "")
        self.assertTrue(row["verified_at"])

    def test_blank_published_keeps_previous_platform_text(self):
        db.save_jobs([make_record(platform="zhitong", platform_job_id="p1",
                                  published_raw="10小时前")])
        db.save_jobs([make_record(platform="zhitong", platform_job_id="p1",
                                  published_raw="")])
        self.assertEqual(db.fetch_all()[0]["published_raw"], "10小时前")

    def test_expire_is_not_a_whitelist_field_but_is_accepted(self):
        self.assertNotIn("_expire", config.ALLOWED_FIELDS)
        self.assertIn("published_raw", config.ALLOWED_FIELDS)
        db.save_jobs([make_record(_expire=False)])   # 不该抛白名单错
        self.assertEqual(db.fetch_all()[0]["delisted_at"], "")

    def test_old_db_gets_the_three_columns_without_fake_backfill(self):
        with db._conn() as c:
            c.execute("DROP TABLE jobs")
            c.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                      "platform TEXT NOT NULL, platform_job_id TEXT NOT NULL, title TEXT, "
                      "company TEXT, salary_raw TEXT, salary_min REAL, salary_max REAL, "
                      "salary_months INTEGER, city TEXT, district TEXT, experience TEXT, "
                      "education TEXT, industry TEXT, company_scale TEXT, skills TEXT, "
                      "welfare TEXT, url TEXT, fetched_at TEXT, status TEXT, status_at TEXT, "
                      "note TEXT, last_seen_at TEXT, UNIQUE(platform, platform_job_id))")
            c.execute("INSERT INTO jobs(platform, platform_job_id, title, fetched_at, "
                      "last_seen_at) VALUES('zhitong','old','老职位','2026-09-01 00:00:00',"
                      "'2026-09-01 00:00:00')")
        db.init_db()
        row = db.fetch_all()[0]
        self.assertEqual(row["published_raw"], "")
        self.assertEqual(row["verified_at"], "")
        self.assertEqual(row["delisted_at"], "")

    def test_undo_restores_verified_and_delisted(self):
        db.save_jobs([make_record(platform="zhitong", platform_job_id="u1", _expire=True)])
        jid = db.fetch_all()[0]["id"]
        res = db.delete_jobs([jid], trash_dir=Path(self._tmp.name) / "trash")
        back = db.restore_batch(res["batch"], trash_dir=Path(self._tmp.name) / "trash")
        self.assertEqual(back["jobs"], 1)
        row = db.fetch_all()[0]
        self.assertTrue(row["delisted_at"])
        self.assertTrue(row["verified_at"])


class TestZhitongStateParse(unittest.TestCase):
    """下架判据是 pcDetail 的 data-state，不是正文空不空（下架页旧正文还在）。"""

    TAG = ('<input id="other" type="hidden" data-cityid="1"\n  />\n  <input\n'
           '    id="pcDetail"\n    type="hidden"\n    data-posname="x"\n'
           '    data-state="{state}"\n  />')

    def test_state_reads_multiline_pcDetail_tag(self):
        from jobstats.collectors import zhitong
        self.assertEqual(zhitong.parse_state(self.TAG.format(state="停止招聘")), "停止招聘")
        self.assertEqual(zhitong.parse_state(self.TAG.format(state="正常招聘")), "正常招聘")

    def test_missing_state_is_none_not_delisted(self):
        from jobstats.collectors import zhitong
        self.assertIsNone(zhitong.parse_state("<div>没有主职位隐藏域</div>"))
        self.assertIsNone(zhitong.parse_state('<input id="pcDetail" data-posname="x">'))

    def test_list_record_carries_fredatestr_as_published(self):
        from jobstats.collectors import zhitong
        html = ('<li class="position pos-item PosName" data-id="1" data-posname="IT运维"'
                ' data-fredatestr="10小时前" data-cityname="东莞" data-urlcityen="dongguan"'
                ' data-positionkey="k1"></li>')
        rec = zhitong.parse_page(html)[0]
        self.assertEqual(rec["published_raw"], "10小时前")


class TestVerifyStale(TempDbCase):
    """核实疑似下架：读平台状态、共用详情页额度、认不出的状态不猜。"""

    def seed_old(self, pjid):
        db.save_jobs([make_record(platform="zhitong", platform_job_id=pjid,
                                  url=f"https://www.job5156.com/dongguan/job_{pjid}")])
        with db._conn() as c:
            c.execute("UPDATE jobs SET last_seen_at='2026-09-01 00:00:00', "
                      "verified_at=NULL WHERE platform_job_id=?", (pjid,))

    def test_delisted_and_alive_both_marked_alive_saves_body(self):
        self.seed_old("d1")
        self.seed_old("a1")

        def fake(job):
            if job["platform_job_id"] == "d1":
                return {"state": "停止招聘", "description": "", "keyword_tags": [],
                        "platform": "zhitong", "platform_job_id": "d1", "url": "u"}
            return {"state": "正常招聘", "description": "还在招的正文", "keyword_tags": [],
                    "platform": "zhitong", "platform_job_id": "a1", "url": "u"}

        with mock.patch.object(details.zhitong, "verify_detail", side_effect=fake), \
             mock.patch.object(details, "polite_sleep"), redirect_stdout(io.StringIO()):
            res = details.verify_stale(days=14)
        self.assertEqual((res["delisted"], res["alive"], res["saved"]), (1, 1, 1))
        rows = {r["platform_job_id"]: r for r in db.fetch_all()}
        self.assertTrue(rows["d1"]["delisted_at"])
        self.assertEqual(rows["a1"]["delisted_at"], "")
        self.assertTrue(rows["a1"]["verified_at"])
        self.assertIsNotNone(db.get_detail("zhitong", "a1"))
        self.assertEqual(db.count_details_today(), 1)
        self.assertEqual(db.count_delisted_today(), 1)

    def test_recently_verified_alive_rows_are_not_rechecked(self):
        self.seed_old("a2")
        with mock.patch.object(details.zhitong, "verify_detail",
                               return_value={"state": "正常招聘", "description": "",
                                             "keyword_tags": [], "platform": "zhitong",
                                             "platform_job_id": "a2", "url": "u"}), \
             mock.patch.object(details, "polite_sleep"), redirect_stdout(io.StringIO()):
            self.assertEqual(details.verify_stale(days=14)["alive"], 1)
        self.assertEqual(db.verify_candidates(14), [])

    def test_expired_state_counts_as_delisted(self):
        # 2026-11-18 真链撞见的第三种状态：页面挂"职位已过期"按钮、无投递入口
        self.seed_old("x2")
        with mock.patch.object(details.zhitong, "verify_detail",
                               return_value={"state": "已过期", "description": "",
                                             "keyword_tags": [], "platform": "zhitong",
                                             "platform_job_id": "x2", "url": "u"}), \
             mock.patch.object(details, "polite_sleep"), redirect_stdout(io.StringIO()):
            res = details.verify_stale(days=14)
        self.assertEqual(res["delisted"], 1)
        self.assertTrue(db.fetch_all()[0]["delisted_at"])

    def test_delisted_checks_share_the_detail_cap(self):
        self.seed_old("d1")
        with mock.patch.object(db, "count_details_today", return_value=19), \
             mock.patch.object(db, "count_delisted_today", return_value=1), \
             mock.patch.object(details.zhitong, "verify_detail",
                               side_effect=AssertionError("额度满了不该发请求")):
            with self.assertRaises(RuntimeError) as ctx:
                details.verify_stale(days=14)
        self.assertIn("熔断", str(ctx.exception))

    def test_unknown_or_missing_state_stops_without_marking(self):
        self.seed_old("x1")
        before = db.fetch_all()[0]["verified_at"]   # 采集入库时已写过一次
        for state in ("暂停招聘", None):
            with mock.patch.object(details.zhitong, "verify_detail",
                                   return_value={"state": state, "description": "",
                                                 "keyword_tags": [], "platform": "zhitong",
                                                 "platform_job_id": "x1", "url": "u"}), \
                 mock.patch.object(details, "polite_sleep"), \
                 redirect_stdout(io.StringIO()):
                res = details.verify_stale(days=14)
            self.assertEqual(res["checked"], 1)
            self.assertEqual((res["delisted"], res["alive"]), (0, 0))
            row = db.fetch_all()[0]
            self.assertEqual(row["verified_at"], before)
            self.assertEqual(row["delisted_at"], "")

    def test_second_verify_batch_is_rejected_while_first_runs(self):
        with details._FETCH_LOCK:
            with self.assertRaises(RuntimeError):
                details.verify_stale(days=14)
        self.assertFalse(details.fetch_in_progress())


class TestVerifyApi(TempDbCase):
    def _mgr(self, current=None):
        return mock.patch.object(server.Handler, "manager",
                                 mock.Mock(current_id=lambda: current))

    def test_verify_passes_summary_through(self):
        with self._mgr(), mock.patch.object(details, "verify_stale",
                                            return_value={"checked": 2, "delisted": 1,
                                                          "alive": 1, "saved": 1,
                                                          "failed": 0, "left": 18}) as m:
            res = post_api("/api/verify", {"limit": 5})
        self.assertEqual(res["status"], 200)
        self.assertEqual(res["obj"]["delisted"], 1)
        self.assertEqual(m.call_args.kwargs["limit"], 5)

    def test_verify_waits_for_collect_and_for_detail_lock(self):
        with self._mgr(current="t1"):
            res = post_api("/api/verify", {})
        self.assertEqual(res["status"], 409)
        with self._mgr(), details._FETCH_LOCK:
            res = post_api("/api/verify", {})
        self.assertEqual(res["status"], 409)

    def test_verify_cap_error_is_400_not_500(self):
        with self._mgr(), mock.patch.object(details, "verify_stale",
                                            side_effect=RuntimeError("额度熔断")):
            res = post_api("/api/verify", {})
        self.assertEqual(res["status"], 400)
        self.assertIn("熔断", res["obj"]["error"])


class TestPrune(TempDbCase):
    def seed(self, pjid, seen, status=None, delisted=False):
        db.save_jobs([make_record(platform="zhitong", platform_job_id=pjid,
                                  _expire=delisted)])
        with db._conn() as c:
            c.execute("UPDATE jobs SET last_seen_at=? WHERE platform_job_id=?",
                      (seen, pjid))
        if status:
            jid = [r for r in db.fetch_all() if r["platform_job_id"] == pjid][0]["id"]
            db.set_status(jid, status)

    def test_candidates_skip_annotated_and_fresh_rows(self):
        old = (datetime.now() - timedelta(days=40)).strftime("%Y-%m-%d %H:%M:%S")
        fresh = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.seed("old1", old)
        self.seed("want1", old, status="想投")
        self.seed("new1", fresh)
        self.seed("del1", fresh, delisted=True)   # 确认下架的不受天数限制
        ids = {r["platform_job_id"] for r in db.prune_candidates(30)}
        self.assertEqual(ids, {"old1", "del1"})

    def test_dry_run_does_not_touch_db_and_apply_is_undoable(self):
        from types import SimpleNamespace
        old = (datetime.now() - timedelta(days=40)).strftime("%Y-%m-%d %H:%M:%S")
        self.seed("old1", old)
        trash = Path(self._tmp.name) / "trash"
        with mock.patch.object(db, "TRASH_DIR", trash), redirect_stdout(io.StringIO()) as buf:
            cli.cmd_prune(SimpleNamespace(days=30, apply=False))
        self.assertIn("预览", buf.getvalue())
        self.assertEqual(len(db.fetch_all()), 1)
        with mock.patch.object(db, "TRASH_DIR", trash), redirect_stdout(io.StringIO()):
            cli.cmd_prune(SimpleNamespace(days=30, apply=True))
        self.assertEqual(db.fetch_all(), [])
        batch = db.trash_batches(trash_dir=trash)[0]["batch"]
        db.restore_batch(batch, trash_dir=trash)
        self.assertEqual(len(db.fetch_all()), 1)


class TestPublishedColumnUi(unittest.TestCase):
    def test_both_versions_have_the_published_column_and_delisted_filter(self):
        web = report.render_web([make_record()], "T", "")
        static = report._filter_html(report.STATIC_SUB_LINE, [make_record()], web=False)
        for html in (web, static):
            self.assertIn("<th>发布/刷新</th>", html)
            self.assertIn('value="delisted"', html)
            self.assertIn("发布/刷新", html)      # 页面 CSV 表头也带
        self.assertIn("btn-verify", web)
        self.assertNotIn("btn-verify", static)   # 核实要走本地服务，静态页没有
        self.assertIn("r.delisted_at", web)
        self.assertIn('class="delisted"', web)


if __name__ == "__main__":
    unittest.main()

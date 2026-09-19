"""核心层单元测试：薪资解析、入库去重与合规白名单、每日额度、HTML 输出加固。

运行：python -m unittest discover -s tests -v
"""
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobstats import browser, db, report

SLOT_NAMES = ("__SUB_LINE__", "__GENERATED_AT__", "__N__", "__DATA__", "__TOKEN__",
              "__CITY_OPTIONS__", "__COLLECT_PANEL__", "__COLLECT_SCRIPT__", "__BOOT_HOOK__")


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

    def test_skills_and_welfare_round_trip(self):
        db.save_jobs([make_record()])
        row = db.fetch_all()[0]
        self.assertEqual(row["skills"], ["python", "django"])
        self.assertEqual(row["welfare"], ["五险一金"])

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
        html = report.render_web([make_record()], "TOK123", '<option value="深圳">深圳</option>')
        self.assertIn("const TOKEN = 'TOK123';", html)
        self.assertIn('id="btn-collect"', html)
        self.assertIn("resumeTask();", html)
        self.assertIn('<option value="深圳">深圳</option>', html)

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


if __name__ == "__main__":
    unittest.main()

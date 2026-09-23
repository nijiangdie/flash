"""核心层单元测试：薪资解析、入库去重与合规白名单、每日额度、HTML 输出加固。

运行：python -m unittest discover -s tests -v
"""
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobstats import browser, db, report
from jobstats.collectors import boss

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


def job_item(jid: str, name: str = "IT运维工程师"):
    return {"encryptJobId": jid, "jobName": name, "salaryDesc": "6-8K",
            "cityName": "东莞", "jobExperience": "1-3年", "jobDegree": "大专"}


def job_page(code=0, items=(), has_more=True, message="Success"):
    return {"code": code, "message": message,
            "zpData": {"jobList": list(items), "hasMore": has_more}}


class TestBossHarvest(unittest.TestCase):
    """翻页逻辑离线可测：接口返回什么就怎么停，不能因为一页出错丢掉已采到的。"""

    class _Resp:
        def __init__(self, payload, status=200):
            self.payload, self.status = payload, status

        def json(self):
            return self.payload

    class _Req:
        def __init__(self, payloads, recorder):
            self.payloads, self.recorder = list(payloads), recorder

        def get(self, url, params=None, headers=None):
            self.recorder.append(dict(params))
            return TestBossHarvest._Resp(self.payloads.pop(0))

    class _Ctx:
        def __init__(self, payloads):
            self.calls = []
            self.request = TestBossHarvest._Req(payloads, self.calls)

    def setUp(self):
        patch = mock.patch.object(boss, "polite_sleep", lambda: None)
        patch.start()
        self.addCleanup(patch.stop)

    def test_pages_until_has_more_false(self):
        ctx = self._Ctx([
            job_page(items=[job_item("a"), job_item("b")], has_more=True),
            job_page(items=[job_item("c")], has_more=False),
        ])
        jobs = boss._harvest(ctx, "IT", "东莞", "101281600", 5, 200)
        self.assertEqual(sorted(jobs), ["a", "b", "c"])
        self.assertEqual([c["page"] for c in ctx.calls], [1, 2])

    def test_stops_on_risk_code_and_keeps_partial(self):
        ctx = self._Ctx([
            job_page(items=[job_item("a")], has_more=True),
            job_page(code=37, items=[], message="您的环境存在异常"),
        ])
        jobs = boss._harvest(ctx, "IT", "东莞", "101281600", 5, 200)
        self.assertEqual(list(jobs), ["a"])

    def test_dedupes_repeated_ids_and_stops(self):
        ctx = self._Ctx([
            job_page(items=[job_item("a")], has_more=True),
            job_page(items=[job_item("a")], has_more=True),
        ])
        jobs = boss._harvest(ctx, "IT", "东莞", "101281600", 5, 200)
        self.assertEqual(list(jobs), ["a"])
        self.assertEqual(len(ctx.calls), 2)  # 第二页无新增即停，不继续打接口

    def test_stops_at_daily_remaining(self):
        ctx = self._Ctx([job_page(items=[job_item("a"), job_item("b")])])
        jobs = boss._harvest(ctx, "IT", "东莞", "101281600", 5, 1)
        self.assertEqual(len(jobs), 2)  # 当页收完即停，截断在 collect() 里做
        self.assertEqual(len(ctx.calls), 1)


class TestEdgeChannel(unittest.TestCase):
    """Boss 的真实 Edge 通道：接不上时必须给出可操作的提示，而不是白跑一轮。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.auth = Path(tmp.name)
        self.patch_auth = mock.patch.object(boss, "AUTH_DIR", self.auth)
        self.patch_auth.start()

    def _logged_in(self):
        (self.auth / "boss_storage.json").write_text("{}", encoding="utf-8")

    def _alive(self, value):
        return mock.patch.object(boss, "cdp_alive", lambda port=0: value)

    def test_channel_error_without_login(self):
        with self._alive(False):
            self.assertIn("login boss --edge", boss.channel_error())

    def test_channel_error_when_window_closed(self):
        self._logged_in()
        with self._alive(False):
            self.assertIn("Edge 窗口", boss.channel_error())

    def test_channel_silent_when_ready(self):
        self._logged_in()
        with self._alive(True):
            self.assertIsNone(boss.channel_error())

    def test_edge_exe_raises_when_not_installed(self):
        with mock.patch.object(browser, "EDGE_PATHS", ("Z:/nope/msedge.exe",)):
            with self.assertRaises(RuntimeError):
                browser.edge_exe()

    def test_cdp_alive_false_without_listener(self):
        self.assertFalse(browser.cdp_alive(port=1))

    def test_attach_refuses_without_window(self):
        with mock.patch.object(browser, "cdp_alive", lambda port=0: False):
            with self.assertRaises(RuntimeError):
                with browser.attach():
                    self.fail("不该进入上下文")


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


if __name__ == "__main__":
    unittest.main()

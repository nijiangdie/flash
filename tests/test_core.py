"""核心层单元测试：薪资解析、入库去重与合规白名单、每日额度、HTML 输出加固。

运行：python -m unittest discover -s tests -v
"""
import json
import re
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from playwright.sync_api import Error as PlaywrightError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobstats import browser, config, db, report, skills
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


def zt_item(jid="12406875", poskey="TdFLo0K2", **over):
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
            '</div>'
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
        pages = [zt_page(zt_item(jid="1"), zt_item(jid="2", poskey="B")),
                 zt_page(zt_item(jid="3", poskey="C")),
                 zt_page(zt_item(jid="3", poskey="C"))]
        with mock.patch.object(self.mod, "_fetch", side_effect=pages) as fetch, \
             mock.patch.object(self.mod, "check_daily_cap", return_value=200), \
             mock.patch.object(self.mod, "polite_sleep"):
            recs = self.mod.collect("IT", "东莞", max_pages=5)
        self.assertEqual(len(recs), 3)
        self.assertEqual(fetch.call_count, 3)

    def test_fetch_error_keeps_what_was_collected(self):
        with mock.patch.object(self.mod, "_fetch",
                              side_effect=[zt_page(zt_item(jid="1")), OSError("连接重置")]), \
             mock.patch.object(self.mod, "check_daily_cap", return_value=200), \
             mock.patch.object(self.mod, "polite_sleep"):
            recs = self.mod.collect("IT", "东莞", max_pages=5)
        self.assertEqual(len(recs), 1)

    def test_page_two_uses_pn_param(self):
        self.assertIn("pn=2", self.mod._search_url("IT", "东莞", 2))
        self.assertNotIn("pn=1", self.mod._search_url("IT", "东莞", 1))
        self.assertNotIn("city=", self.mod._search_url("IT", "全国", 1))


if __name__ == "__main__":
    unittest.main()

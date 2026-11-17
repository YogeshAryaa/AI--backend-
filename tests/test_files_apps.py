import asyncio
import datetime as dt
import io
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("ADMIN_TOKEN", "t" * 32)
os.environ.setdefault("APP_SECRET_KEY", "s" * 32)

import docx  # noqa: E402
import httpx  # noqa: E402
from openpyxl import load_workbook  # noqa: E402

from app import analysis, apps, docgen, media, skill_install, skill_registry  # noqa: E402
from app.security import encrypt  # noqa: E402

MD = """# Roles
Intro **bold** and `code`.

## Candidates
| Name | City | Salary | Joined | Phone | Growth |
| --- | --- | --- | --- | --- | --- |
| Asha | Pune | ₹1,20,000 | 2024-03-01 | 09876543210 | 12% |
| =HYPERLINK("http://evil") | Indore | 85000 | 2024-04-15 | 9876543210 | 3.5% |

- one
- two
"""


class DocumentTests(unittest.TestCase):
    def test_docx_has_headings_table_and_hindi(self):
        data = docgen.build_docx("छुट्टी नीति", MD, [{"title": "HR handbook"}], accent="#16a34a")
        d = docx.Document(io.BytesIO(data))
        self.assertEqual(len(d.tables), 1)
        self.assertEqual(d.tables[0].cell(1, 0).text, "Asha")
        self.assertTrue(any("छुट्टी" in p.text for p in d.paragraphs))
        self.assertTrue(any(p.style.name == "List Bullet" for p in d.paragraphs))

    def test_xlsx_numbers_dates_text_ids_and_formula_safety(self):
        wb = load_workbook(io.BytesIO(docgen.build_xlsx("Roles", MD)))
        ws = wb["Candidates"]
        self.assertEqual(ws["C2"].value, 120000)             # ₹1,20,000 is a real number
        self.assertEqual(ws["C2"].number_format, "#,##0")
        self.assertIsInstance(ws["D2"].value, dt.datetime)    # a real date
        self.assertEqual(ws["E2"].value, "09876543210")       # phone numbers stay text
        self.assertEqual(ws["F2"].value, 0.12)                # percent
        self.assertEqual(ws["A3"].data_type, "s")             # "=HYPERLINK(..)" is data, never a formula
        self.assertEqual(ws.freeze_panes, "A2")
        self.assertTrue(ws.auto_filter.ref)

    def test_text_only_answer_goes_to_a_content_sheet(self):
        wb = load_workbook(io.BytesIO(docgen.build_xlsx("Notes", "# Notes\nJust text\n- a\n- b")))
        self.assertEqual(wb.sheetnames, ["Content"])


def sample_rows(n=200):
    import random
    random.seed(3)
    rows = []
    for i in range(n):
        rows.append({"emp_id": f"E{i:04d}", "city": random.choice(["Pune", "Indore", "Delhi"]),
                     "salary": random.gauss(60000, 9000) + (500000 if i == 5 else 0), "age": random.randint(21, 58),
                     "joined": dt.date(2024, 1, 1) + dt.timedelta(days=i * 2), "dept": None if i % 5 == 0 else random.choice(["QA", "Dev"])})
    rows.append(dict(rows[1]))
    return rows


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.p = docgen.profile_rows(sample_rows())
        self.cols = {c["name"]: c for c in self.p["columns"]}

    def test_column_types(self):
        self.assertEqual(self.cols["emp_id"]["type"], "id")
        self.assertEqual(self.cols["salary"]["type"], "number")
        self.assertEqual(self.cols["joined"]["type"], "date")
        self.assertEqual(self.cols["city"]["type"], "text")

    def test_numbers_missing_duplicates_outliers(self):
        self.assertEqual(self.p["rows"], 201)
        self.assertEqual(self.p["duplicates"], 1)
        self.assertGreaterEqual(self.cols["dept"]["missing_pct"], 19)
        st = self.cols["salary"]["stats"]
        self.assertGreaterEqual(st["outliers"], 1)
        self.assertLessEqual(st["min"], st["median"] <= st["max"] and st["median"])
        self.assertEqual(sum(b["count"] for b in st["histogram"]), st["count"])

    def test_group_summary_and_insights(self):
        g = self.p["group"]
        self.assertIn(g["by"], ("city", "dept"))
        self.assertEqual(g["value"], "salary")
        self.assertEqual(sum(r["count"] for r in g["rows"]), sum(c["count"] for c in self.cols[g["by"]]["top"]))
        text = " ".join(self.p["insights"])
        self.assertIn("duplicate", text)
        self.assertIn("extreme", text)  # one 560k salary among ~60k ones is reported even though it is a single row

    def test_messy_values(self):
        rows = [{"a": "1,200", "b": "x"}, {"a": "₹3,400", "b": "y"}, {"a": "5", "b": "x"}, {"a": None, "b": ""}]
        p = docgen.profile_rows(rows)
        c = {c["name"]: c for c in p["columns"]}
        self.assertEqual(c["a"]["type"], "number")
        self.assertEqual(c["a"]["stats"]["sum"], 4605)
        self.assertEqual(c["b"]["missing"], 1)
        self.assertEqual(docgen.profile_rows([])["rows"], 0)

    def test_reports_open_in_office_formats(self):
        rows = sample_rows()
        xl = docgen.build_analysis_xlsx(self.p, [c["name"] for c in self.p["columns"]], rows, "Test analysis")
        wb = load_workbook(io.BytesIO(xl))
        self.assertEqual(wb.sheetnames[:3], ["Summary", "Columns", "Numbers"])
        self.assertIn("Data", wb.sheetnames)
        self.assertEqual(wb["Data"].max_row, 202)
        self.assertTrue(any(wb[s]._charts for s in wb.sheetnames))
        md = docgen.analysis_markdown(self.p, "Test analysis", "- AI point")
        self.assertIn("AI commentary", md)
        d = docx.Document(io.BytesIO(docgen.build_docx("Test analysis", md.split("\n", 2)[2])))
        self.assertGreaterEqual(len(d.tables), 3)

    def test_file_loading(self):
        rows, truncated = analysis.rows_from_file("s.csv", b"region,amount\nN,10\nS,20\n")
        self.assertEqual((len(rows), truncated), (2, False))
        rows, truncated = analysis.rows_from_file("s.csv", b"a\n1\n2\n3\n", max_rows=2)
        self.assertEqual((len(rows), truncated), (2, True))
        with self.assertRaises(ValueError):
            analysis.rows_from_file("notes.pdf", b"%PDF")
        with self.assertRaises(ValueError):
            analysis.rows_from_file("e.csv", b"a,b\n")

    def test_timezone_aware_dates_from_mongodb_reach_excel(self):
        rows = [{"name": "A", "joined": dt.datetime(2024, 5, 1, 10, 30, tzinfo=dt.timezone.utc), "n": 1},
                {"name": "B", "joined": dt.datetime(2024, 5, 2, 8, 0, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30))), "n": 2}]
        p = docgen.profile_rows(rows)
        self.assertEqual({c["name"]: c["type"] for c in p["columns"]}["joined"], "date")
        wb = load_workbook(io.BytesIO(docgen.build_analysis_xlsx(p, ["name", "joined", "n"], rows, "Mongo")))
        self.assertEqual(wb["Data"]["B3"].value, dt.datetime(2024, 5, 2, 2, 30))  # 08:00 +05:30 = 02:30 UTC

    def test_flatten_mongo_documents(self):
        class ObjectId:
            def __str__(self):
                return "abc"
        row = analysis.flatten_doc({"_id": ObjectId(), "name": "A", "skills": ["x", "y"], "addr": {"city": "Pune", "geo": {"lat": 1}},
                                    "oid": ObjectId()})
        self.assertNotIn("_id", row)
        self.assertEqual(row["skills"], "x, y")
        self.assertEqual(row["addr.city"], "Pune")
        self.assertEqual(row["oid"], "abc")


class IntentTests(unittest.TestCase):
    def test_detect(self):
        cases = {
            "leave policy ki PDF bana do": "pdf", "make a word file on leave policy": "docx",
            "leave policy word mein bana do": "docx", "candidates ka excel bana do": "xlsx",
            "export employees to excel": "xlsx", "create a spreadsheet of costs": "xlsx", "docx download karo": "docx",
            "how do I make an excel sheet": None, "analyze this excel file": None, "what does the word policy mean": None,
            "keyword research": None, "ek poster ki image banao": "image",
        }
        for q, want in cases.items():
            self.assertEqual(media.detect(q), want, q)


class ChatFilesTests(unittest.IsolatedAsyncioTestCase):
    async def make(self, question, history, kind, answer=None):
        saved, styles_seen = [], []

        async def fake_chat(topic, hist, mode, ns, allow_ai, doc=False, doc_style=None):
            styles_seen.append(doc_style)
            yield {"type": "meta", "source": "database", "citations": [], "standalone_question": topic}
            yield {"type": "token", "text": answer or MD}
            yield {"type": "done"}

        async def fake_save(owner, uid, cid, k, name, mime, data, recipe=None, label=None):
            saved.append({"kind": k, "name": name, "data": data, "recipe": recipe})
            return {"id": "f1", "kind": k, "name": name, "mime": mime, "size": len(data), "url": "/files/f1"}

        with patch.object(media, "run_chat", fake_chat), patch.object(media, "save_file", fake_save):
            events = [e async for e in media._pdf_events(question, history, "ai", None, None, "o", None, None, kind=kind)]
        return events, saved, styles_seen

    async def test_excel_from_your_data(self):
        events, saved, styles = await self.make("candidates by city ka excel bana do", [], "xlsx")
        self.assertEqual([e["file"]["kind"] for e in events if e["type"] == "file"], ["xlsx"])
        self.assertIn("Excel spreadsheet", styles[0])
        self.assertIsNone(saved[0]["recipe"])  # no recipe: "another colour" must never remake it as an image
        self.assertIn("Candidates", load_workbook(io.BytesIO(saved[0]["data"])).sheetnames)
        self.assertEqual(next(e for e in events if e["type"] == "meta")["source"], "database")

    async def test_word_from_your_data(self):
        events, saved, styles = await self.make("make a word file on leave policy", [], "docx")
        self.assertEqual(saved[0]["kind"], "docx")
        self.assertIn("Word document", styles[0])
        self.assertEqual(len(docx.Document(io.BytesIO(saved[0]["data"])).tables), 1)

    async def test_excel_of_the_previous_answer(self):
        hist = [{"role": "user", "content": "list candidates"}, {"role": "assistant", "content": MD}]
        events, saved, styles = await self.make("iska excel bana do", hist, "xlsx")
        self.assertEqual(styles, [])  # nothing new was asked: the previous answer is rendered, no AI call
        self.assertEqual(saved[0]["kind"], "xlsx")
        self.assertEqual(next(e for e in events if e["type"] == "meta")["provider"], "xlsx")

    async def test_asks_for_a_topic(self):
        events, saved, _ = await self.make("excel bana do", [], "xlsx")
        self.assertEqual(saved, [])
        self.assertIn("Excel file", "".join(e.get("text", "") for e in events))


# ------------------------------------------------------------------ connected apps
def mock_client(handler):
    real = httpx.AsyncClient

    def make(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return real(*a, **kw)
    return make


def app_doc(type_, **fields):
    plain, secret = apps.split_config(type_, fields)
    return {"_id": "a1", "name": "x", "type": type_, "enabled": True, "config": plain,
            "secrets_enc": {k: encrypt(v) for k, v in secret.items()}}


FILE = {"name": "report.xlsx", "mime": "application/vnd.ms-excel", "size": 5, "data": b"12345"}


class AppSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_and_local_addresses_are_refused(self):
        for ip in ("127.0.0.1", "10.0.0.5", "192.168.1.9", "169.254.169.254", "172.16.0.1", "0.0.0.0", "::1", "::ffff:127.0.0.1", "fd00::1"):
            fake = [(2, 1, 6, "", (ip, 443))]
            with patch.object(apps.socket, "getaddrinfo", return_value=fake):
                with self.assertRaises(apps.AppError, msg=ip):
                    await apps._guard_host("evil.example.com", 443)
        with patch.object(apps.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]):
            await apps._guard_host("example.com", 443)  # public: fine
        with patch.object(apps.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 443)), (2, 1, 6, "", ("10.0.0.1", 443))]):
            with self.assertRaises(apps.AppError):  # one private answer among public ones is still refused
                await apps._guard_host("mixed.example.com", 443)

    async def test_private_allowed_only_when_switched_on(self):
        with patch.object(apps.cfg, "allow_private_app_urls", True):
            await apps._guard_host("localhost", 5678)

    def test_url_rules(self):
        bad = ["http://example.com/hook", "ftp://example.com", "https://user:pw@example.com/x", "not a url", "https:///x"]
        for u in bad:
            with self.assertRaises(apps.AppError, msg=u):
                apps._parse_url(u)
        with self.assertRaises(apps.AppError):
            apps._parse_url("https://evil.com/hooks.slack.com", apps._SLACK_HOSTS)
        with self.assertRaises(apps.AppError):
            apps._parse_url("https://hooks.slack.com.evil.com/x", apps._SLACK_HOSTS)
        apps._parse_url("https://hooks.slack.com/services/T/B/x", apps._SLACK_HOSTS)

    def test_validation_and_masking(self):
        with self.assertRaises(apps.AppError):
            apps.split_config("slack", {})
        with self.assertRaises(apps.AppError):
            apps.split_config("telegram", {"bot_token": "nope", "chat_id": "1"})
        with self.assertRaises(apps.AppError):
            apps.split_config("email", {"host": "smtp.x.com", "port": "587", "security": "none", "username": "u", "password": "p",
                                        "sender": "a@b.co", "to": "c@d.co"})
        doc = app_doc("slack", url="https://hooks.slack.com/services/T000/B000/SECRETSECRET")
        pub = apps.public(doc)
        self.assertNotIn("SECRETSECRET", str(pub))
        self.assertEqual(pub["secrets"]["url"], "https://hooks.slack.com/…")
        # editing without retyping the secret keeps it
        plain, secret = apps.split_config("webhook", {"url": ""}, {}, {"url": "https://n.example.com/h"})
        self.assertEqual(secret["url"], "https://n.example.com/h")


class AppSendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.seen = []
        self.guard = patch.object(apps, "_guard_host", AsyncMock())
        self.guard.start()

    async def asyncTearDown(self):
        self.guard.stop()

    def handler(self, status=200, body=None):
        def h(request):
            self.seen.append(request)
            return httpx.Response(status, json=body or {"ok": True})
        return h

    async def test_webhook_sends_json_secret_and_optional_file(self):
        doc = app_doc("webhook", url="https://hooks.example.com/abc", secret="s3cret", include_file=True)
        with patch.object(apps.httpx, "AsyncClient", mock_client(self.handler())):
            await apps.send(doc, "Hello", FILE)
        r = self.seen[0]
        self.assertEqual(r.headers["x-webhook-secret"], "s3cret")
        import json
        body = json.loads(r.content)
        self.assertEqual(body["text"], "Hello")
        self.assertEqual(body["file"]["name"], "report.xlsx")
        self.assertEqual(body["file"]["base64"], "MTIzNDU=")
        self.assertFalse(r.url.host != "hooks.example.com")

    async def test_slack_text_only_with_file_name(self):
        doc = app_doc("slack", url="https://hooks.slack.com/services/T/B/x")
        with patch.object(apps.httpx, "AsyncClient", mock_client(self.handler())):
            await apps.send(doc, "Report ready", FILE)
        import json
        self.assertIn("report.xlsx", json.loads(self.seen[0].content)["text"])

    async def test_discord_attaches_file(self):
        doc = app_doc("discord", url="https://discord.com/api/webhooks/1/abc")
        with patch.object(apps.httpx, "AsyncClient", mock_client(self.handler())):
            await apps.send(doc, "Report ready", FILE)
        self.assertIn(b"report.xlsx", self.seen[0].content)
        self.assertIn("multipart/form-data", self.seen[0].headers["content-type"])

    async def test_telegram_document_and_errors_never_leak_the_token(self):
        token = "123456789:ABCdefGhIJKlmNoPQRstuVWXyz012345678"
        doc = app_doc("telegram", bot_token=token, chat_id="42")
        with patch.object(apps.httpx, "AsyncClient", mock_client(self.handler())):
            await apps.send(doc, "Here", FILE)
        self.assertTrue(str(self.seen[0].url).endswith("/sendDocument"))
        with patch.object(apps.httpx, "AsyncClient", mock_client(self.handler(401, {"description": "Unauthorized"}))):
            with self.assertRaises(apps.AppError) as cm:
                await apps.send(doc, "Here", None)
        self.assertNotIn(token, str(cm.exception))

        def boom(request):
            raise httpx.ConnectError("refused", request=request)
        with patch.object(apps.httpx, "AsyncClient", mock_client(boom)):
            with self.assertRaises(apps.AppError) as cm:
                await apps.send(doc, "Here", None)
        self.assertNotIn(token, str(cm.exception))

    async def test_http_error_message_and_no_redirects(self):
        doc = app_doc("webhook", url="https://hooks.example.com/abc")
        with patch.object(apps.httpx, "AsyncClient", mock_client(self.handler(404))):
            with self.assertRaises(apps.AppError) as cm:
                await apps.send(doc, "x", None)
        self.assertIn("404", str(cm.exception))

        def redirect(request):
            return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})
        with patch.object(apps.httpx, "AsyncClient", mock_client(redirect)):
            with self.assertRaises(apps.AppError):
                await apps.send(doc, "x", None)  # a redirect is an error, never followed

    async def test_email_builds_message_with_attachment(self):
        doc = app_doc("email", host="smtp.example.com", port="587", security="starttls", username="u", password="p",
                      sender="me@example.com", to="a@example.com, b@example.com")
        sent = {}

        class FakeSMTP:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def starttls(self, **k): sent["tls"] = True
            def login(self, u, p): sent["login"] = (u, p)
            def send_message(self, m): sent["msg"] = m
        with patch.object(apps.smtplib, "SMTP", FakeSMTP):
            await apps.send(doc, "Subject line\nbody text", FILE)
        m = sent["msg"]
        self.assertTrue(sent["tls"])
        self.assertEqual(sent["login"], ("u", "p"))
        self.assertEqual(m["Subject"], "Subject line")
        self.assertEqual(m["To"], "a@example.com, b@example.com")
        self.assertEqual([p.get_filename() for p in m.iter_attachments()], ["report.xlsx"])


# ------------------------------------------------------------------ skill installation
def make_zip(entries: dict, symlink: str | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, content in entries.items():
            z.writestr(name, content)
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = (0o120777 << 16)
            z.writestr(info, "/etc/passwd")
    return buf.getvalue()


SKILL = "---\nname: my-reports\ndescription: Writes weekly reports with charts.\n---\n\n# Reports\nUse tables.\n"


class SkillInstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = patch.object(skill_registry, "SKILLS_ROOT", Path(self.tmp.name))
        self.patch.start()
        skill_registry.invalidate()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()
        skill_registry.invalidate()

    def test_install_text_and_it_becomes_active(self):
        out = skill_install.install_text(None, SKILL)
        self.assertEqual(out["id"], "community/my-reports")
        self.assertIn("community/my-reports", [s["id"] for s in skill_registry.discover_skills()])
        with self.assertRaises(skill_install.SkillInstallError):
            skill_install.install_text(None, SKILL)  # already there
        skill_install.install_text(None, SKILL, replace=True)

    def test_text_without_frontmatter_gets_one(self):
        out = skill_install.install_text("Weekly Notes!", "# How to write notes\nShort bullets.")
        self.assertEqual(out["name"], "weekly-notes")
        s = next(s for s in skill_registry.discover_skills() if s["name"] == "weekly-notes")
        self.assertIn("How to write notes", s["description"])

    def test_bad_text(self):
        for bad in ("", "x\x00y", "a" * 300_000):
            with self.assertRaises(skill_install.SkillInstallError):
                skill_install.install_text("abc", bad)
        with self.assertRaises(skill_install.SkillInstallError):
            skill_install.install_text("!", SKILL.replace("name: my-reports", "name: !"))

    def test_zip_with_folder_prefix(self):
        z = make_zip({"my-skill/SKILL.md": SKILL, "my-skill/ref/guide.md": "more", "__MACOSX/x": "junk", "other/readme.txt": "x"})
        out = skill_install.install_zip(z)
        self.assertEqual((out["id"], out["files"]), ("community/my-reports", 2))
        self.assertTrue((Path(self.tmp.name) / "community/my-reports/ref/guide.md").exists())
        self.assertFalse((Path(self.tmp.name) / "community/my-reports/readme.txt").exists())

    def test_unsafe_zips_are_refused(self):
        bad = {
            "traversal": make_zip({"../evil/SKILL.md": SKILL}),
            "absolute": make_zip({"/etc/SKILL.md": SKILL}),
            "backslash": make_zip({"a\\..\\SKILL.md": SKILL}),
            "symlink": make_zip({"s/SKILL.md": SKILL}, symlink="s/link"),
            "no skill": make_zip({"readme.md": "x"}),
            "too deep": make_zip({"a/b/c/d/SKILL.md": SKILL}),
            "too many": make_zip({f"s/f{i}.txt": "x" for i in range(301)} | {"s/SKILL.md": SKILL}),
            "not a zip": b"hello",
        }
        for label, data in bad.items():
            with self.assertRaises(skill_install.SkillInstallError, msg=label):
                skill_install.install_zip(data)
        community = Path(self.tmp.name) / "community"
        self.assertEqual(list(community.iterdir()) if community.exists() else [], [])  # nothing left behind
        with patch.object(skill_install, "MAX_UNPACKED", 1000):
            with self.assertRaises(skill_install.SkillInstallError):
                skill_install.install_zip(make_zip({"s/SKILL.md": SKILL + "x" * 5000}))

    def test_remove_only_community_skills(self):
        skill_install.install_text(None, SKILL)
        root = Path(self.tmp.name)
        (root / "gravitas").mkdir()
        (root / "gravitas/SKILL.md").write_text(SKILL)
        for bad in ("gravitas", "community", "community/../gravitas", "../etc", "community/.hidden", "community/nope", ""):
            with self.assertRaises(skill_install.SkillInstallError, msg=bad):
                skill_install.remove(bad)
        self.assertTrue((root / "gravitas/SKILL.md").exists())
        self.assertEqual(skill_install.remove("community/my-reports"), "community/my-reports")
        self.assertEqual([s["id"] for s in skill_registry.discover_skills() if s["id"].startswith("community")], [])


class StemTests(unittest.TestCase):
    def test_all_forms_of_a_word_agree(self):
        groups = [("invoice", "invoices"), ("service", "services"), ("database", "databases"), ("query", "queries"),
                  ("test", "tests", "testing", "tested"), ("dub", "dubbing", "dubbed"), ("integrate", "integrated", "integrating", "integrates"),
                  ("write", "writes", "writing"), ("webhook", "webhooks"), ("index", "indexes"), ("payment", "payments"), ("image", "images")]
        for g in groups:
            self.assertEqual(len({skill_registry._stem(w) for w in g}), 1, g)

    def test_plural_question_finds_singular_skill(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(skill_registry, "SKILLS_ROOT", Path(tmp)):
            skill_registry.invalidate()
            skill_install.install_text(None, "---\nname: invoice-writer\ndescription: Writes clear invoices with GST breakdown.\n---\n\nbody")
            plan = skill_registry.make_plan("write invoices with GST", "coding", "local")
            self.assertEqual([i["id"] for i in plan["primary"]], ["community/invoice-writer"])
        skill_registry.invalidate()


class SkillSwitchTests(unittest.TestCase):
    def test_disabled_skill_and_extra_links(self):
        q = "integrate Stripe payments webhooks"
        full = skill_registry.make_plan(q, "coding", "cloud")
        self.assertEqual(full["primary"][0]["id"], "community/stripe-best-practices")
        off = skill_registry.make_plan(q, "coding", "cloud", disabled={"community/stripe-best-practices"})
        self.assertNotIn("community/stripe-best-practices", [i["id"] for i in off["primary"] + off["linked"]])
        # a bundle can be switched off as a whole
        bundle_off = skill_registry.make_plan("write openapi spec for my endpoints", "coding", "cloud", disabled={"community/api-skill"})
        self.assertEqual([i for i in bundle_off["primary"] if "/api-skill/" in i["id"]], [])
        # a link added in the frontend brings a skill along
        link = ["community/stripe-best-practices", "community/voicestudio", "speak the receipt"]
        plan = skill_registry.make_plan(q, "coding", "cloud", extra_edges=[link])
        self.assertIn("community/voicestudio", [i["id"] for i in plan["linked"]])


if __name__ == "__main__":
    unittest.main()

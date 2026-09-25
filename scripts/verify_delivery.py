#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
方案 B 统一交付验证入口

用法（项目根目录）：
    .venv\\Scripts\\python.exe -X utf8 scripts\\verify_delivery.py --phase p1
    .venv\\Scripts\\python.exe -X utf8 scripts\\verify_delivery.py --phase p2
    .venv\\Scripts\\python.exe -X utf8 scripts\\verify_delivery.py --phase p3
    .venv\\Scripts\\python.exe -X utf8 scripts\\verify_delivery.py --phase all

设计：
- 每个用例在**独立隔离数据目录**（--data-dir 或 %TEMP%\\doc_ocr_verify_<ts>/<case>）运行，
  通过 DOC_OCR_DATA_DIR 环境变量隔离，绝不触碰真实 data/uploads
- 需要服务的用例启动独立 uvicorn 子进程（随机空闲端口），跑完即停
- 核心门禁不调用真实云端、不读真实 Key
- 结果 JSON 写 reports/acceptance/<phase>.json，任一失败退出码非零
"""
import argparse
import io
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

# Windows' GBK default cannot decode child-process UTF-8 evidence. Re-exec once
# so both parent subprocess decoding and all descendants have one encoding.
if __name__ == '__main__':
    os.environ['PYTHONUTF8'] = '1'
    if not sys.flags.utf8_mode:
        os.execv(sys.executable, [sys.executable, '-X', 'utf8', *sys.argv])
os.environ['DOC_OCR_SKIP_DOTENV'] = '1'
os.environ['LLM_API_KEY'] = ''
os.environ['LLM_BASE_URL'] = 'http://127.0.0.1:9/unreachable'

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
VENV_PY = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
PY = str(VENV_PY if VENV_PY.exists() else Path(sys.executable))
REPORTS = PROJECT_ROOT / "reports" / "acceptance"
FROZEN = REPORTS / "frozen_corpus"


# ==================== 测试基建 ====================

class Ctx:
    """单个用例的运行上下文"""

    def __init__(self, data_dir: Path, with_server: bool = True):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.port = None
        self.proc = None
        self.base = None
        if with_server:
            self.port = _free_port()
            self.base = f"http://127.0.0.1:{self.port}"

    def start_server(self, timeout: float = 90.0):
        env = dict(os.environ)
        env["DOC_OCR_DATA_DIR"] = str(self.data_dir)
        # 测试环境不读真实 .env：显式清掉云端配置，防止任何意外云端调用
        env["LLM_API_KEY"] = ""
        env["LLM_BASE_URL"] = "http://127.0.0.1:9/unreachable"
        log = open(self.data_dir / "server.log", "w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [PY, "-m", "uvicorn", "app.main:app",
             "--host", "127.0.0.1", "--port", str(self.port)],
            cwd=str(PROJECT_ROOT), env=env,
            stdout=log, stderr=subprocess.STDOUT)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"服务启动失败（退出码 {self.proc.returncode}），"
                    f"日志见 {self.data_dir / 'server.log'}")
            try:
                with urllib.request.urlopen(self.base + "/api/health", timeout=2):
                    return
            except Exception:
                time.sleep(1)
        raise RuntimeError("服务启动超时")

    def stop_server(self):
        # Windows .venv can launch a second python.exe. Stop the app-owned PID,
        # not only the launcher; otherwise restart tests race a live SQLite WAL.
        if self.proc:
            from scripts.restore_backup import stop_service
            stop_service(self.data_dir, False)
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
        self.proc = None

    def api(self, method: str, path: str, body=None, files=None, raw=False,
            timeout: float = 60.0):
        """极简 HTTP 客户端（不引第三方依赖）。返回 (status, json_or_bytes)。"""
        url = self.base + path
        headers = {}
        data = None
        if files:
            import uuid
            boundary = uuid.uuid4().hex
            parts = []
            for name, (fname, content, ctype) in files.items():
                parts.append(f"--{boundary}\r\n".encode())
                parts.append(
                    f'Content-Disposition: form-data; name="{name}"; filename="{fname}"\r\n'
                    f"Content-Type: {ctype}\r\n\r\n".encode())
                parts.append(content)
                parts.append(b"\r\n")
            parts.append(f"--{boundary}--\r\n".encode())
            data = b"".join(parts)
            headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        elif body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = resp.read()
                if raw:
                    return resp.status, payload
                return resp.status, json.loads(payload.decode("utf-8"))
        except urllib.error.HTTPError as e:
            payload = e.read()
            if raw:
                return e.code, payload
            try:
                return e.code, json.loads(payload.decode("utf-8"))
            except Exception:
                return e.code, {"code": e.code, "msg": payload[:200].decode("utf-8", "ignore")}

    def db(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.data_dir / "data" / "doc_ocr_v2.db"))

    def close(self):
        self.stop_server()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_png(path: Path, text_hint: str = "test") -> bytes:
    """生成最小合法 PNG（Pillow）。内容带随机像素避免不同用例撞哈希。"""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (240, 120), "white")
    d = ImageDraw.Draw(img)
    d.text((10, 10), f"{text_hint} {time.time_ns()}", fill="black")
    img.save(path, "PNG")
    return path.read_bytes()


def _make_multipage_pdf(path: Path, pages: int = 2, seed: str = "") -> bytes:
    """
    生成多页中文 PDF（MuPDF 内建 china-s 字体）。

    为什么自己造：现有 test_corpus 全是单页，而 T04 必须验证「逐页处理」这条路径。
    """
    import fitz
    doc = fitz.open()
    stamp = datetime.now().strftime("%H%M%S")
    for i in range(1, pages + 1):
        page = doc.new_page(width=595, height=842)
        page.insert_text((60, 90), f"测试合同 {seed} 第 {i} 页", fontsize=18,
                         fontname="china-s")
        page.insert_text((60, 150), f"合同编号：HT-{stamp}-{i}", fontsize=14,
                         fontname="china-s")
        page.insert_text((60, 190), "甲方：示例科技有限公司", fontsize=14,
                         fontname="china-s")
        page.insert_text((60, 230), "乙方：测试服务中心", fontsize=14,
                         fontname="china-s")
        if i == pages:
            page.insert_text((60, 290), "合同金额：1234.00 元", fontsize=14,
                             fontname="china-s")
            page.insert_text((60, 330), "签订日期：2026-01-15", fontsize=14,
                             fontname="china-s")
    doc.save(str(path))
    doc.close()
    return path.read_bytes()


def _render_corpus_image(path: Path, dpi: int = 200) -> bytes:
    """
    把冻结语料里的一页渲染成 PNG —— 为 OCR 提供**带真实中文文本**的图片样本。

    直接用 Pillow 画的 PNG 只有像素噪点，OCR 必然失败；
    而 T04 要的是"真实 OCR 链路能跑通"的证据，不是"失败路径也走得稳"。
    """
    import fitz
    src = sorted(FROZEN.joinpath("data_root").glob("**/*.pdf"))
    if not src:
        src = sorted((PROJECT_ROOT / "test_corpus").glob("*.pdf"))
    # 渲染第一份语料的首页即可
    doc = fitz.open(str(src[0]))
    pix = doc[0].get_pixmap(dpi=dpi)
    pix.save(str(path))
    doc.close()

    # 渲染结果是**确定性**的：同一页渲染两次字节完全相同，去重用例会误判成同一份内容。
    # 因此每次叠加一个随机角标，得到"内容不同、版面相同"的样本。
    from PIL import Image, ImageDraw
    img = Image.open(str(path)).convert("RGB")
    ImageDraw.Draw(img).text((5, 5), f"#{time.time_ns()}", fill="black")
    img.save(path, "PNG")
    return path.read_bytes()


def _wait_for(ctx: Ctx, predicate, timeout: float = 90.0, interval: float = 1.5,
              desc: str = "条件"):
    """轮询等待某个 API 判据成立（索引是后台异步任务，必须等而不是猜）"""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"等待超时：{desc}（最后一次结果 {last}）")


def _upload(ctx: Ctx, name: str, content: bytes,
            ctype: str = "application/octet-stream") -> dict:
    code, r = ctx.api("POST", "/api/file/upload",
                      files={"file": (name, content, ctype)})
    assert code == 200 and r["code"] == 200, f"上传失败: {r}"
    return r["data"]


def _recognize(ctx: Ctx, doc_id: int, force: bool = False):
    return ctx.api("POST", "/api/ocr/recognize",
                   {"doc_id": doc_id, "force": force})


def _detail(ctx: Ctx, doc_id: int) -> dict:
    code, r = ctx.api("GET", f"/api/document/{doc_id}")
    assert code == 200 and r["code"] == 200, f"取详情失败: {r}"
    return r["data"]


# ==================== T01 安装与迁移 ====================

def t01_empty_install(ctx: Ctx):
    """空库安装：全新数据目录能启动、建表到 head、列表为空"""
    ctx.start_server()
    code, h = ctx.api("GET", "/api/health")
    assert code == 200 and h["code"] == 200, f"health 异常: {h}"
    code, lst = ctx.api("GET", "/api/document/list")
    assert code == 200 and lst["data"]["total"] == 0, "空库列表应为 0"
    conn = ctx.db()
    try:
        ver = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert ver == "0002_plan_b", f"schema 版本异常: {ver}"
    assert "document_revision" in tables, "缺少 document_revision 表"
    return {"alembic": ver, "tables": sorted(tables)}


def t01_legacy_migration(ctx: Ctx):
    """旧库副本迁移：22 篇文档保留、建 legacy 快照、重复启动幂等、有迁移前备份"""
    legacy_db = FROZEN / "data_root" / "data" / "doc_ocr_v2.db"
    assert legacy_db.exists(), f"冻结语料库缺失: {legacy_db}"
    (ctx.data_dir / "data").mkdir(parents=True, exist_ok=True)
    shutil.copy2(legacy_db, ctx.data_dir / "data" / "doc_ocr_v2.db")

    ctx.start_server()
    conn = ctx.db()
    try:
        docs = conn.execute("SELECT COUNT(*) FROM document_info").fetchone()[0]
        revs = conn.execute("SELECT COUNT(*) FROM document_revision").fetchone()[0]
        cur = conn.execute(
            "SELECT COUNT(*) FROM document_info WHERE current_revision=1").fetchone()[0]
        ver = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    finally:
        conn.close()
    assert docs == 22 and revs == 22 and cur == 22, \
        f"迁移计数异常 docs={docs} revs={revs} cur={cur}"
    assert ver == "0002_plan_b"
    backups = list((ctx.data_dir / "data" / "backups").glob("pre_migration_*.db"))
    assert backups, "缺少迁移前一致性备份"

    # 重复启动幂等
    ctx.stop_server()
    ctx.start_server()
    conn = ctx.db()
    try:
        revs2 = conn.execute("SELECT COUNT(*) FROM document_revision").fetchone()[0]
    finally:
        conn.close()
    assert revs2 == 22, "重复启动不应重复建快照"

    code, lst = ctx.api("GET", "/api/document/list?page_size=100")
    assert lst["data"]["total"] == 22
    # 列表瘦身断言：不含全文与绝对路径
    item = lst["data"]["items"][0]
    assert "ocr_raw_text" not in item and "file_path" not in item, "列表响应未瘦身"
    assert "reviewed" in item and "doc_type" in item
    return {"docs": docs, "revisions": revs, "backup": backups[0].name}


def t01_unknown_structure_refused(ctx: Ctx):
    """未知结构：启动必须失败且数据库文件不被删除/破坏"""
    (ctx.data_dir / "data").mkdir(parents=True, exist_ok=True)
    db_path = ctx.data_dir / "data" / "doc_ocr_v2.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE weird_table (id INTEGER PRIMARY KEY, junk TEXT)")
    conn.execute("INSERT INTO weird_table VALUES (1, 'keepme')")
    conn.commit()
    conn.close()

    failed = False
    try:
        ctx.start_server(timeout=30)
    except RuntimeError:
        failed = True
    assert failed, "未知结构竟然启动成功"
    assert db_path.exists(), "未知结构下数据库文件被删除"
    conn = sqlite3.connect(str(db_path))
    row = conn.execute("SELECT junk FROM weird_table WHERE id=1").fetchone()
    conn.close()
    assert row and row[0] == "keepme", "未知结构下数据被改写"
    return {"refused": True, "data_intact": True}


# ==================== T02 删除/恢复/永久删除 ====================

def t02_recycle_bin(ctx: Ctx):
    """软删除排除有效入口；恢复一致；永久删除受限且不越目录；缺原件明确报错"""
    ctx.start_server()
    # 上传两份（B 用于永久删除）
    png_a = ctx.data_dir / "a.png"
    png_b = ctx.data_dir / "b.png"
    bytes_a = _make_png(png_a, "docA")
    bytes_b = _make_png(png_b, "docB")

    def upload(name, content):
        code, r = ctx.api("POST", "/api/file/upload",
                          files={"file": (name, content, "image/png")})
        assert code == 200 and r["code"] == 200, f"上传失败: {r}"
        return r["data"]["doc_id"]

    id_a = upload("a.png", bytes_a)
    id_b = upload("b.png", bytes_b)
    path_b = ctx.data_dir / "uploads"
    file_b = list(path_b.glob("*b*.png")) or list(path_b.glob("*.png"))

    # 软删除 A → 正常列表排除、回收站可见、统计排除
    code, r = ctx.api("DELETE", f"/api/document/{id_a}")
    assert r["code"] == 200
    code, lst = ctx.api("GET", "/api/document/list?page_size=100")
    ids = [i["id"] for i in lst["data"]["items"]]
    assert id_a not in ids and id_b in ids, "软删除未从正常列表排除"
    code, binlst = ctx.api("GET", "/api/document/list?in_recycle_bin=true")
    bin_ids = [i["id"] for i in binlst["data"]["items"]]
    assert id_a in bin_ids, "回收站不可见"
    code, st = ctx.api("GET", "/api/document/stats")
    assert st["data"]["total"] == 1, "统计未排除回收站"

    # 恢复 A → 回到正常列表，统计一致
    code, r = ctx.api("POST", f"/api/document/{id_a}/restore")
    assert r["code"] == 200
    code, lst = ctx.api("GET", "/api/document/list?page_size=100")
    assert id_a in [i["id"] for i in lst["data"]["items"]]

    # 未进回收站直接永久删除 → 拒绝
    code, r = ctx.api("DELETE", f"/api/document/{id_b}/permanent")
    assert r["code"] == 400, "非回收站文档竟然可永久删除"

    # 目录穿越防护：构造 file_path 在 uploads 外的记录
    conn = ctx.db()
    conn.execute(
        "INSERT INTO document_info (file_name, file_path, file_type, status, doc_type,"
        " deleted_at) VALUES ('evil.png', ?, 'image', 0, 'other', datetime('now'))",
        (str(ctx.data_dir / "data" / "doc_ocr_v2.db"),))
    evil_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()
    code, r = ctx.api("DELETE", f"/api/document/{evil_id}/permanent")
    assert r["code"] == 400, "越目录路径竟然放行永久删除"

    # 正常永久删除 B：先软删再永久删，记录与原件都不在
    ctx.api("DELETE", f"/api/document/{id_b}")
    code, r = ctx.api("DELETE", f"/api/document/{id_b}/permanent")
    assert r["code"] == 200, f"永久删除失败: {r}"
    conn = ctx.db()
    gone = conn.execute(
        "SELECT COUNT(*) FROM document_info WHERE id=?", (id_b,)).fetchone()[0]
    conn.close()
    assert gone == 0
    left = [p for p in (ctx.data_dir / "uploads").glob("*.png")]
    assert not any("b.png" == p.name for p in left)

    # 缺失原件的永久删除：明确报告（原件已不存在，仅删记录）
    conn = ctx.db()
    conn.execute(
        "INSERT INTO document_info (file_name, file_path, file_type, status, doc_type,"
        " deleted_at) VALUES ('ghost.png', ?, 'image', 0, 'other', datetime('now'))",
        (str(ctx.data_dir / "uploads" / "not_exists.png"),))
    ghost_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()
    code, r = ctx.api("DELETE", f"/api/document/{ghost_id}/permanent")
    assert r["code"] == 200 and "不存在" in r["msg"], f"缺原件处理异常: {r}"
    return {"soft_delete_ok": True, "restore_ok": True, "permanent_ok": True,
            "traversal_blocked": True, "missing_original_reported": True}


# ==================== T03 备份与恢复 ====================

def t03_backup_restore(ctx: Ctx):
    """备份-恢复往返一致；坏包/穿越包拒绝；中断可回滚；门禁语义"""
    ctx.start_server()
    png = ctx.data_dir / "c.png"
    content = _make_png(png, "docC")
    code, r = ctx.api("POST", "/api/file/upload",
                      files={"file": ("c.png", content, "image/png")})
    doc_id = r["data"]["doc_id"]

    # 1. 备份 ZIP 下载
    code, payload = ctx.api("POST", "/api/maintenance/backup", raw=True, timeout=120)
    assert code == 200 and payload[:2] == b"PK", "备份响应不是 ZIP"
    zip_path = ctx.data_dir / "backup.zip"
    zip_path.write_bytes(payload)
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
    assert "data/doc_ocr_v2.db" in names and "manifest.json" in names

    # 2. 恢复到全新目录（受控脚本，--assume-stopped）
    restore_dir = ctx.data_dir / "restored"
    r = subprocess.run(
        [PY, str(PROJECT_ROOT / "scripts" / "restore_backup.py"), str(zip_path),
         "--data-dir", str(restore_dir), "--assume-stopped"],
        capture_output=True, text=True, cwd=str(PROJECT_ROOT), timeout=180)
    assert r.returncode == 0, f"恢复失败: {r.stdout} {r.stderr}"
    conn = sqlite3.connect(str(restore_dir / "data" / "doc_ocr_v2.db"))
    cnt = conn.execute("SELECT COUNT(*) FROM document_info").fetchone()[0]
    fp = conn.execute("SELECT file_path FROM document_info WHERE id=?",
                      (doc_id,)).fetchone()[0]
    conn.close()
    assert cnt == 1, f"恢复后记录数异常: {cnt}"
    restored_file = restore_dir / "uploads" / Path(fp).name
    assert Path(fp).resolve() == restored_file.resolve(), '恢复后数据库仍指向旧数据目录'
    assert restored_file.exists() and restored_file.read_bytes() == content, \
        "原件哈希往返不一致"
    snaps = list((restore_dir / "data").glob("pre_restore_snapshot_*"))
    assert snaps, "缺少恢复前安全快照"

    # 3. 损坏 ZIP：翻转数据库成员字节 → 拒绝且目标目录数据不变
    bad_dir = ctx.data_dir / "bad_target"
    bad_dir.mkdir(parents=True)
    (bad_dir / "data").mkdir()
    shutil.copy2(restore_dir / "data" / "doc_ocr_v2.db",
                 bad_dir / "data" / "doc_ocr_v2.db")
    corrupt_zip = ctx.data_dir / "corrupt.zip"
    with zipfile.ZipFile(zip_path) as zin, \
            zipfile.ZipFile(corrupt_zip, "w") as zout:
        for n in zin.namelist():
            data = zin.read(n)
            if n == "data/doc_ocr_v2.db":
                data = b"GARBAGE" + data[100:]
            elif n == "manifest.json":
                continue  # 清单保持原样 → hash 必然不匹配
            zout.writestr(n, data)
        # 重写 manifest（含错误 hash 也没关系，校验应失败）
        with zipfile.ZipFile(zip_path) as z2:
            zout.writestr("manifest.json", z2.read("manifest.json"))
    r = subprocess.run(
        [PY, str(PROJECT_ROOT / "scripts" / "restore_backup.py"), str(corrupt_zip),
         "--data-dir", str(bad_dir), "--assume-stopped"],
        capture_output=True, text=True, cwd=str(PROJECT_ROOT), timeout=120)
    assert r.returncode != 0, "损坏包竟然恢复成功"
    conn = sqlite3.connect(str(bad_dir / "data" / "doc_ocr_v2.db"))
    cnt = conn.execute("SELECT COUNT(*) FROM document_info").fetchone()[0]
    conn.close()
    assert cnt == 1, "错误包改动了当前数据"

    # 4. 穿越 ZIP：构造 ../ 条目 → 预检拒绝
    evil_zip = ctx.data_dir / "evil.zip"
    with zipfile.ZipFile(evil_zip, "w") as z:
        z.writestr("../escape.txt", "x")
        z.writestr("backup_meta.json",
                   json.dumps({"format_version": 1, "schema_version": 2}))
        z.writestr("manifest.json", json.dumps({"files": []}))
    r = subprocess.run(
        [PY, str(PROJECT_ROOT / "scripts" / "restore_backup.py"), str(evil_zip),
         "--data-dir", str(ctx.data_dir / "evil_target"), "--assume-stopped"],
        capture_output=True, text=True, cwd=str(PROJECT_ROOT), timeout=60)
    assert r.returncode != 0, "穿越包竟然通过预检"
    assert not (ctx.data_dir / "escape.txt").exists()

    # 5. 门禁语义（进程内直接验证，比模拟并发 API 更确定）
    gate_script = (
        "from app.services.backup_gate import GATE, BackupInProgress\n"
        "import threading\n"
        "res = {}\n"
        "def writer():\n"
        "    try:\n"
        "        with GATE.write_section('t'):\n"
        "            res['write'] = 'entered'\n"
        "    except BackupInProgress:\n"
        "        res['write'] = 'rejected'\n"
        "with GATE.backup_window(timeout=5):\n"
        "    t = threading.Thread(target=writer); t.start(); t.join()\n"
        "assert res['write'] == 'rejected', res\n"
        "with GATE.write_section('t2'):\n"
        "    res['after'] = 'ok'\n"
        "print('GATE_OK')\n"
    )
    r = subprocess.run([PY, "-c", gate_script], capture_output=True, text=True,
                       cwd=str(PROJECT_ROOT), timeout=60)
    assert "GATE_OK" in r.stdout, f"门禁语义异常: {r.stdout} {r.stderr}"

    # 6. 回滚机制（直接演练 rollback：快照→破坏→回滚→字节级一致）
    rb_script = r"""
import shutil, sys
from pathlib import Path
sys.path.insert(0, r"%s")
from scripts.restore_backup import make_snapshot, switch_data, rollback
d = Path(sys.argv[1])
(d / "data").mkdir(parents=True, exist_ok=True)
(d / "uploads").mkdir(exist_ok=True)
(d / "data" / "doc_ocr_v2.db").write_bytes(b"ORIGINAL_DB")
(d / "uploads" / "f.bin").write_bytes(b"ORIGINAL_FILE")
snap = make_snapshot(d, "test")
stg = d / "data" / "restore_staging"
(stg / "data").mkdir(parents=True)
(stg / "uploads").mkdir()
(stg / "data" / "doc_ocr_v2.db").write_bytes(b"NEW_DB")
(stg / "uploads" / "f.bin").write_bytes(b"NEW_FILE")
switch_data(d, stg)
assert (d / "data" / "doc_ocr_v2.db").read_bytes() == b"NEW_DB"
rollback(d, snap)
assert (d / "data" / "doc_ocr_v2.db").read_bytes() == b"ORIGINAL_DB"
assert (d / "uploads" / "f.bin").read_bytes() == b"ORIGINAL_FILE"
print("ROLLBACK_OK")
""" % str(PROJECT_ROOT)
    rb_dir = ctx.data_dir / "rollback_demo"
    r = subprocess.run([PY, "-c", rb_script, str(rb_dir)],
                       capture_output=True, text=True, cwd=str(PROJECT_ROOT), timeout=60)
    assert "ROLLBACK_OK" in r.stdout, f"回滚演练失败: {r.stdout} {r.stderr}"

    return {"roundtrip_ok": True, "corrupt_rejected": True,
            "traversal_rejected": True, "gate_ok": True, "rollback_ok": True,
            "snapshot": snaps[0].name}


# ==================== T04 识别路径 ====================

def t04_recognition_paths(ctx: Ctx):
    """
    真实 OCR 可跑（含多页 PDF 逐页处理）；损坏/加密 PDF 明确失败且**不产出有效版本**；
    引擎来源可辨识，Mock 不被当成真实成功。
    """
    ctx.start_server()

    code, info = ctx.api("GET", "/api/ocr/engine-info")
    assert code == 200, f"引擎信息接口异常: {info}"
    engine_type = info["data"].get("engine_type")
    assert engine_type in ("rapidocr", "tesseract", "mock"), \
        f"未显式标注引擎来源: {info}"

    if engine_type == "mock":
        # Mock 仅测试环境：正式识别必须明确不可用，不允许生成"成功"占位文档
        png = ctx.data_dir / "mock.png"
        d = _upload(ctx, "mock.png", _render_corpus_image(png), "image/png")
        code, r = _recognize(ctx, d["doc_id"])
        assert r["code"] != 200, f"Mock 环境竟然产出成功结果: {r}"
        return {"engine_type": engine_type, "mock_refused": True,
                "note": "本机无真实 OCR 引擎，真实识别子用例未执行（T04 标为受阻）"}

    # ① 图片：真实的中文版面
    png = ctx.data_dir / "real.png"
    d1 = _upload(ctx, "real.png", _render_corpus_image(png), "image/png")
    code, r = _recognize(ctx, d1["doc_id"])
    assert r["code"] == 200, f"图片识别失败: {r}"
    text1 = r["data"].get("ocr_raw_text") or ""
    assert len(text1.strip()) > 10, "识别文本过短，疑似未真正走完 OCR"
    assert r["data"].get("engine_type") == engine_type, "响应引擎来源与健康检查不一致"

    # ② 多页 PDF：逐页处理，最后一页内容必须出现在结果里
    mp = ctx.data_dir / "multi.pdf"
    d2 = _upload(ctx, "multi.pdf", _make_multipage_pdf(mp, pages=3, seed="A"),
                 "application/pdf")
    code, r2 = _recognize(ctx, d2["doc_id"])
    assert r2["code"] == 200, f"多页 PDF 识别失败: {r2}"
    text2 = r2["data"].get("ocr_raw_text") or ""
    assert "3" in text2 and ("页" in text2), "多页 PDF 未处理到最后一页"

    # ③ 损坏 PDF：明确失败，且不产生有效版本（不许把部分页面当整份成功）
    bad = ctx.data_dir / "broken.pdf"
    bad.write_bytes(b"%PDF-1.4\n\x00\x01garbage-not-a-real-pdf\n")
    d3 = _upload(ctx, "broken.pdf", bad.read_bytes(), "application/pdf")
    code, r3 = _recognize(ctx, d3["doc_id"])
    assert r3["code"] != 200, f"损坏 PDF 竟然识别成功: {r3}"
    det = _detail(ctx, d3["doc_id"])
    assert det["current_revision"] is None, "失败识别竟产出了有效版本"
    assert det["last_attempt_status"] == 2 and det["last_attempt_error"], \
        "未记录最近一次失败原因"

    # ④ 加密 PDF：明确报错，同样不产出有效版本
    import fitz
    plain = ctx.data_dir / "_plain.pdf"
    _make_multipage_pdf(plain, pages=1, seed="E")
    enc = ctx.data_dir / "enc.pdf"
    src = fitz.open(str(plain))
    src.save(str(enc), encryption=fitz.PDF_ENCRYPT_AES_256,
             owner_pw="owner", user_pw="user")
    src.close()
    d4 = _upload(ctx, "enc.pdf", enc.read_bytes(), "application/pdf")
    code, r4 = _recognize(ctx, d4["doc_id"])
    assert r4["code"] != 200, f"加密 PDF 竟然识别成功: {r4}"
    det4 = _detail(ctx, d4["doc_id"])
    assert det4["current_revision"] is None, "加密 PDF 产出了有效版本"

    return {"engine_type": engine_type, "image_ok": True,
            "multipage_last_page_reached": True,
            "broken_rejected": True, "encrypted_rejected": True,
            "no_fake_version": True,
            "image_chars": len(text1), "pdf_chars": len(text2)}


# ==================== T05 编辑与版本 ====================

def t05_edit_and_versions(ctx: Ctx):
    """编辑/改字段/改类型/校对标记/重识别失败的语义；历史可读且条数与版本一致"""
    ctx.start_server()
    png = ctx.data_dir / "e.png"
    d = _upload(ctx, "e.png", _render_corpus_image(png), "image/png")
    code, rr = _recognize(ctx, d["doc_id"])
    assert rr["code"] == 200, f"识别失败: {rr}"
    did = d["doc_id"]

    det = _detail(ctx, did)
    r1 = det["current_revision"]
    raw0 = det["ocr_raw_text"]
    text0 = det["effective_text"]

    # ① 过期版本号 → 409，且不改动数据
    code, r = ctx.api("PATCH", f"/api/document/{did}",
                      {"expected_revision": r1 + 99, "effective_text": "x"})
    assert r["code"] == 409, f"过期版本号未返回 409: {r}"
    det = _detail(ctx, did)
    assert det["current_revision"] == r1 and det["effective_text"] == text0

    # ② 无实际变化 → 不创建新版本
    code, r = ctx.api("PATCH", f"/api/document/{did}",
                      {"expected_revision": r1, "effective_text": text0})
    assert r["code"] == 200 and r["data"]["revision_created"] is False, f"空版本: {r}"
    assert _detail(ctx, did)["current_revision"] == r1

    # ③ 改全文 → 新版本；OCR 原文不被覆盖；可显式标为已校对
    new_text = (text0 or "") + "\n补充说明：本合同金额以最终结算为准。"
    code, r = ctx.api("PATCH", f"/api/document/{did}",
                      {"expected_revision": r1, "effective_text": new_text,
                       "mark_reviewed": True})
    assert r["code"] == 200 and r["data"]["revision_created"] is True, f"编辑失败: {r}"
    r2 = r["data"]["new_revision"]
    det = _detail(ctx, did)
    assert det["ocr_raw_text"] == raw0, "OCR 原始文本被人工编辑覆盖"
    assert det["effective_text"] == new_text
    assert det["reviewed"] is True, "未受理显式的已校对标记"

    # ④ 改字段/改类型不改写全文
    code, r = ctx.api("PATCH", f"/api/document/{did}",
                      {"expected_revision": r2, "doc_type": "invoice",
                       "fields": {"total_amount": "100.00"}})
    assert r["code"] == 200, f"字段编辑失败: {r}"
    det = _detail(ctx, did)
    assert det["effective_text"] == new_text, "改字段竟改动了全文"
    amount_rows = [e for e in det["extracts"] if e.get("field_key") == "total_amount"]
    assert amount_rows and amount_rows[0]["field_value"] == "100.00", \
        f"字段未落库: {det['extracts']}"

    # ⑤ 金额超精度 → 明确报错而非静默舍入
    code, r = ctx.api("PATCH", f"/api/document/{did}",
                      {"expected_revision": det["current_revision"],
                       "fields": {"total_amount": "10.004"}})
    assert r["code"] == 400, f"三位小数金额竟然被接受: {r}"

    # ⑥ 内容变更后清除校对标记
    det = _detail(ctx, did)
    assert det["reviewed"] is False, "内容变更后未清除校对标记"

    # ⑦ 历史：快照条数 == 当前版本号，且首版快照保留当时的原文
    code, hist = ctx.api("GET", f"/api/document/{did}/revisions")
    assert hist["data"]["total"] == det["current_revision"], \
        f"版本快照数({hist['data']['total']}) 与当前版本({det['current_revision']}) 不一致"
    code, snap = ctx.api("GET", f"/api/document/{did}/revisions/1")
    assert snap["code"] == 200 and snap["data"]["ocr_raw_text"] == raw0, "首版快照内容不符"

    # ⑧ 重识别失败：保留旧有效结果，只记录最近一次失败
    conn = ctx.db()
    real_path = conn.execute(
        "SELECT file_path FROM document_info WHERE id=?", (did,)).fetchone()[0]
    broken = ctx.data_dir / "uploads" / "replaced_broken.pdf"
    broken.write_bytes(b"%PDF-1.4\nbroken-content")
    conn.execute("UPDATE document_info SET file_path=?, file_type='pdf' WHERE id=?",
                 (str(broken), did))
    conn.commit()
    conn.close()
    code, r = _recognize(ctx, did, force=True)
    assert r["code"] != 200, f"坏原件重识别竟然成功: {r}"
    det = _detail(ctx, did)
    assert det["effective_text"] == new_text, "重识别失败丢失了旧有效结果"
    assert det["current_revision"] >= r2
    assert det["last_attempt_status"] == 2, "未记录最近一次识别失败"

    # 还原现场
    conn = ctx.db()
    conn.execute("UPDATE document_info SET file_path=?, file_type='image' WHERE id=?",
                 (real_path, did))
    conn.commit()
    conn.close()

    return {"conflict_409": True, "no_empty_version": True, "ocr_raw_intact": True,
            "mark_reviewed": True, "review_cleared_after_change": True,
            "field_edit_keeps_text": True, "amount_precision_guarded": True,
            "history_consistent": True, "refail_keeps_old_version": True,
            "revisions": det["current_revision"]}


# ==================== T06 索引范围与补偿 ====================

def t06_index_scope_compensation(ctx: Ctx):
    """索引跟随版本；范围过滤（空列表=无文档）；删除即时生效；重启补偿；旧会��标记失效"""
    ctx.start_server()
    png = ctx.data_dir / "s.png"
    d = _upload(ctx, "s.png", _render_corpus_image(png), "image/png")
    code, rr = _recognize(ctx, d["doc_id"])
    assert rr["code"] == 200, f"识别失败: {rr}"
    did = d["doc_id"]

    def status():
        code, r = ctx.api("GET", "/api/rag/index/status")
        assert code == 200, f"索引状态接口异常: {r}"
        return r["data"]

    _wait_for(ctx, lambda: status()["not_ready"] == [], desc="首次索引完成")
    st = status()
    assert st["indexed_docs"] >= 1, f"索引未就绪: {st}"

    # ① 改版本 → 索引过期 → 该文档暂不参与问答；后台重建后回归
    det = _detail(ctx, did)
    code, r = ctx.api("PATCH", f"/api/document/{did}",
                      {"expected_revision": det["current_revision"],
                       "effective_text": (det["effective_text"] or "") + "\n追加一句用于改版本。"})
    assert r["code"] == 200, f"改版本失败: {r}"
    st = status()
    assert did in st["not_ready"] or did in st["stale_docs"], "新版本未被标记为索引过期"
    _wait_for(ctx, lambda: status()["not_ready"] == [], desc="改版本后自动重建索引")
    st2 = status()
    assert st2["indexed_docs"] >= 1, f"新版本重建索引后仍未就绪: {st2}"

    # ② 范围过滤：空列表=无文档（不得扩大）；单文档=不得混入其他文档
    code, r = ctx.api("POST", "/api/rag/search", {"query": "发票金额", "doc_ids": []})
    assert r["data"]["hits"] == [] and r["data"]["doc_scope_count"] == 0, \
        f"空列表范围被扩大: {r['data']}"
    code, r2 = ctx.api("POST", "/api/rag/search",
                       {"query": "发票金额", "doc_ids": [did], "mode": "bm25"})
    outsiders = [h for h in r2["data"]["hits"] if h["doc_id"] != did]
    assert not outsiders, f"范围限制失效，混入其他文档: {outsiders}"

    # ③ 回收站即时退出检索范围（不用旧索引兜底）
    ctx.api("DELETE", f"/api/document/{did}")
    code, r = ctx.api("POST", "/api/rag/search",
                      {"query": "发票金额", "doc_ids": [did], "mode": "bm25"})
    assert r["data"]["hits"] == [], "已删除文档仍被召回"
    ctx.api("POST", f"/api/document/{did}/restore")
    _wait_for(ctx, lambda: status()["not_ready"] == [], desc="恢复后索引补齐")

    # ④ 重启补偿：人为让索引过期后重启进程
    ctx.stop_server()
    conn = ctx.db()
    conn.execute("UPDATE doc_index_state SET indexed_revision=NULL")
    conn.commit()
    conn.close()
    ctx.start_server()
    _wait_for(ctx, lambda: status()["not_ready"] == [], timeout=150,
              desc="启动补偿重建索引")
    assert status()["all_ready"] is True, "启动补偿后仍存在未就绪文档"

    # ⑤ 旧会话：历史里指向失效文档的引用要标注出来，不带旧片段进作答
    ctx.api("DELETE", f"/api/document/{did}")
    code, ask = ctx.api("POST", "/api/rag/ask",
                        {"question": "它的金额是多少",
                         "history": [{"question": "这张单据多少钱",
                                      "top_doc_id": did,
                                      "top_file_name": "s.png",
                                      "top_chunk_text": "客户端自带的旧片段"}]})
    assert ask["code"] == 200, f"ask 异常: {ask}"
    assert did in (ask["data"].get("stale_history_docs") or []), \
        "旧会话引用未被标记为失效"
    ctx.api("POST", f"/api/document/{did}/restore")

    return {"version_invalidates_index": True, "empty_scope_not_expanded": True,
            "single_doc_scope": True, "deleted_excluded": True,
            "startup_compensation": True, "stale_history_flagged": True}


# ==================== T07 去重与批量 ====================

def t07_upload_dedup_and_batch(ctx: Ctx):
    """同内容不同文件名/并发/回收站重复的去重语义；单批失败不阻断其他，失败可重试"""
    ctx.start_server()

    def counts():
        conn = ctx.db()
        n = conn.execute("SELECT COUNT(*) FROM document_info").fetchone()[0]
        conn.close()
        files = len([p for p in (ctx.data_dir / "uploads").glob("*") if p.is_file()])
        return n, files

    content = _render_corpus_image(ctx.data_dir / "d.png")
    d1 = _upload(ctx, "dup_one.png", content, "image/png")
    n0, f0 = counts()

    # ① 同内容不同文件名 → 不新增记录、不留孤儿文件
    d2 = _upload(ctx, "dup_two.png", content, "image/png")
    assert d2["duplicate"] is True and d2["existing_doc_id"] == d1["doc_id"], \
        f"内容去重失效: {d2}"
    n, f = counts()
    assert (n, f) == (n0, f0), f"重复上传产生多余记录/孤儿文件: {(n, f)} vs {(n0, f0)}"

    # ② 并发上传同一份内容 → 最终只保留一份
    from concurrent.futures import ThreadPoolExecutor
    brand_new = _render_corpus_image(ctx.data_dir / "d2.png")
    with ThreadPoolExecutor(max_workers=5) as ex:
        results = list(ex.map(
            lambda i: _upload(ctx, f"conc_{i}.png", brand_new, "image/png"),
            range(5)))
    created = {r["doc_id"] for r in results}
    assert len(created) == 1, f"并发重复上传产生了多条记录: {created}"
    n, f = counts()
    assert n == n0 + 1, f"并发去重后记录数异常: {n}"
    assert f == f0 + 1, f"并发去重后存在孤儿文件: {f}"

    # ③ 命中回收站 → 提示恢复，仍不新增记录
    ctx.api("DELETE", f"/api/document/{d1['doc_id']}")
    d3 = _upload(ctx, "dup_three.png", content, "image/png")
    assert d3["duplicate"] is True and d3["in_recycle_bin"] is True, \
        f"回收站重复未提示: {d3}"
    n, _ = counts()
    assert n == n0 + 1, "回收站重复产生了新记录"
    ctx.api("POST", f"/api/document/{d1['doc_id']}/restore")

    # ④ 单批中的一个失败不影响其他文件，且失败项可重试
    bad = ctx.data_dir / "batch_bad.pdf"
    bad.write_bytes(b"%PDF-1.4\nnot-real")
    bad_id = _upload(ctx, "batch_bad.pdf", bad.read_bytes(), "application/pdf")["doc_id"]
    good_id = _upload(
        ctx, "batch_good.png",
        _render_corpus_image(ctx.data_dir / "g.png"), "image/png")["doc_id"]

    code, bad_r = _recognize(ctx, bad_id)
    assert bad_r["code"] != 200, "损坏文件竟然识别成功"
    code, good_r = _recognize(ctx, good_id)
    assert good_r["code"] == 200, f"同批正常文件受影响: {good_r}"

    # 失败项手动重试：换成可用原件后应能成功
    conn = ctx.db()
    stored = conn.execute(
        "SELECT file_path FROM document_info WHERE id=?", (bad_id,)).fetchone()[0]
    conn.close()
    Path(stored).write_bytes(_render_corpus_image(ctx.data_dir / "fix.png"))
    code, retry = _recognize(ctx, bad_id)
    assert retry["code"] == 200, f"失败项修复后重识别仍失败: {retry}"

    return {"same_content_diff_name_deduped": True, "concurrent_deduped": True,
            "recycle_bin_hint": True, "batch_failure_isolated": True,
            "retry_after_fix": True}


# ==================== T08 固定统计样例 ====================

def _mk_doc(ctx: Ctx, tag: str, doc_type: str, fields: dict) -> int:
    """
    造一份**真实引擎**产出的有效文档，再按样例把字段改成固定值。

    为什么不直接插库：统计口径要求「有真实引擎类型的有效版本」，
    走上传→识别→编辑才能同时验证「统计读的是有效版本快照」这条链路。
    """
    png = ctx.data_dir / f"{tag}.png"
    d = _upload(ctx, f"{tag}.png", _render_corpus_image(png), "image/png")
    did = d["doc_id"]
    code, rr = _recognize(ctx, did)
    assert rr["code"] == 200, f"{tag} 识别失败: {rr}"
    det = _detail(ctx, did)
    code, r = ctx.api("PATCH", f"/api/document/{did}", {
        "expected_revision": det["current_revision"],
        "doc_type": doc_type, "fields": fields})
    assert r["code"] == 200, f"{tag} 字段写入失败: {r}"
    return did


def _qs(params: dict) -> str:
    return "&".join(f"{k}={v}" for k, v in params.items() if v is not None)


def _an(ctx: Ctx, **params) -> dict:
    qs = _qs(params)
    code, r = ctx.api("GET", "/api/document/analytics" + (f"?{qs}" if qs else ""))
    assert code == 200 and r["code"] == 200, f"统计接口异常: {r}"
    return r["data"]


def _an_details(ctx: Ctx, **params) -> dict:
    qs = _qs(params)
    code, r = ctx.api("GET", "/api/document/analytics/details" + (f"?{qs}" if qs else ""))
    assert code == 200 and r["code"] == 200, f"统计明细接口异常: {r}"
    return r["data"]


def _amt_group(data: dict, doc_type: str, currency):
    for g in data["amounts"]:
        if g["doc_type"] == doc_type and g["currency"] == currency:
            return g
    return None


def _month_amount(group: dict, month: str):
    for m in group.get("monthly", []):
        if m["month"] == month:
            return m["amount_total"]
    return None


def t08_analytics_fixed_sample(ctx: Ctx):
    """
    第 5.4 节固定样例：数量/金额精确匹配、无币种混算、单角色排行、图与明细同口径、空态无假数据
    """
    from decimal import Decimal as D
    ctx.start_server()

    # ---------- 造样例（I1~I6、C1 有效；X1 Mock / X2 回收站 / X3 首败 不计入） ----------
    party = {"buyer_name": "甲公司", "seller_name": "乙公司"}
    ids = {}
    ids["I1"] = _mk_doc(ctx, "I1", "invoice", {**party, "invoice_date": "2026-01-10",
                                               "total_amount": "100.00", "currency": "CNY"})
    ids["I2"] = _mk_doc(ctx, "I2", "invoice", {**party, "invoice_date": "2026-01-11",
                                               "total_amount": "-20.00", "currency": "CNY"})
    ids["I3"] = _mk_doc(ctx, "I3", "invoice", {**party, "invoice_date": "2026-01-12",
                                               "total_amount": "10.00", "currency": "USD"})
    ids["I4"] = _mk_doc(ctx, "I4", "invoice", {**party, "invoice_date": "2026-02-01",
                                               "total_amount": None, "currency": "CNY"})
    ids["I5"] = _mk_doc(ctx, "I5", "invoice", {**party, "invoice_date": None,
                                               "total_amount": "30.00", "currency": "CNY"})
    ids["I6"] = _mk_doc(ctx, "I6", "invoice", {**party, "invoice_date": "2026-01-13",
                                               "total_amount": "7.00", "currency": None})
    ids["C1"] = _mk_doc(ctx, "C1", "contract", {"party_a": "甲公司", "party_b": "丙公司",
                                                "signed_date": "2026-01-15",
                                                "contract_amount": "500.00",
                                                "currency": "CNY"})

    # X1：Mock 引擎产出（把已造好的文档标成 mock 引擎，验证"非已知 Mock"这条口径）
    ids["X1"] = _mk_doc(ctx, "X1", "invoice", {**party, "invoice_date": "2026-01-16",
                                               "total_amount": "999.00", "currency": "CNY"})
    conn = ctx.db()
    conn.execute("UPDATE document_info SET engine_type='mock' WHERE id=?", (ids["X1"],))
    conn.execute("UPDATE document_revision SET engine_type='mock' WHERE doc_id=?", (ids["X1"],))
    conn.commit()
    conn.close()

    # X2：回收站
    ids["X2"] = _mk_doc(ctx, "X2", "invoice", {**party, "invoice_date": "2026-01-17",
                                               "total_amount": "888.00", "currency": "CNY"})
    ctx.api("DELETE", f"/api/document/{ids['X2']}")

    # X3：首次识别即失败（无有效版本）
    broken = ctx.data_dir / "x3.pdf"
    broken.write_bytes(b"%PDF-1.4\nbroken-content")
    ids["X3"] = _upload(ctx, "x3.pdf", broken.read_bytes(), "application/pdf")["doc_id"]
    code, rx = _recognize(ctx, ids["X3"])
    assert rx["code"] != 200, "损坏文件竟然识别成功"

    # ---------- ① 初始预期：7 份有效、6 发票 1 合同、7 份未校对 ----------
    a = _an(ctx)
    s = a["summary"]
    assert s["doc_count"] == 7, f"有效文档数应为 7，实际 {s['doc_count']}"
    assert s["unreviewed_count"] == 7, f"未校对数应为 7，实际 {s['unreviewed_count']}"
    assert s["missing_amount_count"] == 1, f"金额缺失应为 1，实际 {s['missing_amount_count']}"
    assert s["missing_date_count"] == 1, f"日期缺失应为 1，实际 {s['missing_date_count']}"
    assert s["missing_currency_count"] == 1, f"币种缺失应为 1，实际 {s['missing_currency_count']}"

    tc = {t["doc_type"]: t["doc_count"] for t in a["type_counts"]}
    assert tc.get("invoice") == 6 and tc.get("contract") == 1, f"类型分布不符: {tc}"

    # ---------- ② 金额：按类型+币种独立汇总，无混算 ----------
    cny_inv = _amt_group(a, "invoice", "CNY")
    usd_inv = _amt_group(a, "invoice", "USD")
    cny_con = _amt_group(a, "contract", "CNY")
    assert cny_inv and D(cny_inv["amount_total"]) == D("110.00"), \
        f"CNY 发票总额应为 110.00，实际 {cny_inv}"
    assert D(_month_amount(cny_inv, "2026-01")) == D("80.00"), \
        f"一月 CNY 发票应为 80.00，实际 {cny_inv['monthly']}"
    assert D(_month_amount(cny_inv, "unknown")) == D("30.00"), \
        f"日期未知 CNY 发票应为 30.00，实际 {cny_inv['monthly']}"
    assert usd_inv and D(usd_inv["amount_total"]) == D("10.00"), f"USD 发票应为 10.00: {usd_inv}"
    assert cny_con and D(cny_con["amount_total"]) == D("500.00"), f"CNY 合同应为 500.00: {cny_con}"

    # 不输出三者混加值：不存在任何跨类型/跨币种的合计字段
    mixed = [g for g in a["amounts"]
             if g["currency"] is not None
             and D(g["amount_total"]) in (D("620.00"), D("120.00"), D("610.00"))]
    assert not mixed, f"出现了跨类型/跨币种混加值: {mixed}"
    # 未知币种：只计数，不给合计
    unknown_cur = [g for g in a["amounts"] if g["currency"] is None]
    assert len(unknown_cur) == 1 and unknown_cur[0]["amount_total"] is None, \
        f"未知币种不应输出合计: {unknown_cur}"

    # ---------- ③ 日期过滤一月：有效 5 份；CNY 发票明细两条合计 80.00 ----------
    jan = dict(date_from="2026-01-01", date_to="2026-01-31")
    aj = _an(ctx, **jan)
    assert aj["summary"]["doc_count"] == 5, f"一月有效文档应为 5，实际 {aj['summary']['doc_count']}"
    assert any(w["code"] == "date_range_excludes_unknown" for w in aj["warnings"]), \
        "日期范围过滤未提示日期未知文档被排除"

    det = _an_details(ctx, doc_type="invoice", currency="CNY", require_amount="true", **jan)
    assert det["total"] == 2, f"一月 CNY 发票明细应仅 2 条，实际 {det['total']}"
    assert D(det["amount_totals"][0]["amount_total"]) == D("80.00"), \
        f"明细合计应为 80.00，实际 {det['amount_totals']}"

    # ---------- ④ 编辑 I1 = 120.00 → 一月合计 100.00 ----------
    cur_rev = _detail(ctx, ids["I1"])["current_revision"]
    code, r = ctx.api("PATCH", f"/api/document/{ids['I1']}", {
        "expected_revision": cur_rev, "fields": {"total_amount": "120.00"}})
    assert r["code"] == 200, f"编辑 I1 失败: {r}"
    det = _an_details(ctx, doc_type="invoice", currency="CNY", require_amount="true", **jan)
    assert det["total"] == 2 and D(det["amount_totals"][0]["amount_total"]) == D("100.00"), \
        f"编辑后一月合计应为 100.00，实际 {det['amount_totals']}"

    # ---------- ⑤ 删除 I2 → 120.00；恢复 → 100.00 ----------
    ctx.api("DELETE", f"/api/document/{ids['I2']}")
    det = _an_details(ctx, doc_type="invoice", currency="CNY", require_amount="true", **jan)
    assert det["total"] == 1 and D(det["amount_totals"][0]["amount_total"]) == D("120.00"), \
        f"删除 I2 后应为 120.00，实际 {det['amount_totals']}"
    ctx.api("POST", f"/api/document/{ids['I2']}/restore")
    det = _an_details(ctx, doc_type="invoice", currency="CNY", require_amount="true", **jan)
    assert det["total"] == 2 and D(det["amount_totals"][0]["amount_total"]) == D("100.00"), \
        f"恢复 I2 后应回到 100.00，实际 {det['amount_totals']}"

    # ---------- ⑥ 单角色排行：一次只选一个角色，不把双方重复累计 ----------
    # 先复原 I1（回到样例初始的 100.00），让排行同样在"初始样例"上比对 ——
    # 任务书要求每组测试复原独立样例，避免编辑用例的残留污染后续断言。
    cur_rev = _detail(ctx, ids["I1"])["current_revision"]
    code, r = ctx.api("PATCH", f"/api/document/{ids['I1']}", {
        "expected_revision": cur_rev, "fields": {"total_amount": "100.00"}})
    assert r["code"] == 200, f"复原 I1 失败: {r}"
    det = _an_details(ctx, doc_type="invoice", currency="CNY", require_amount="true", **jan)
    assert D(det["amount_totals"][0]["amount_total"]) == D("80.00"), \
        f"复原后一月合计应回到 80.00，实际 {det['amount_totals']}"

    rk = _an(ctx, party_role="buyer")["party_ranking"]
    assert rk["role"] == "buyer" and rk["doc_type"] == "invoice", f"角色未锁定: {rk}"
    cny_items = [i for i in rk["items"] if i["currency"] == "CNY"]
    assert cny_items and cny_items[0]["party"] == "甲公司", f"买方排行不符: {cny_items}"
    assert D(cny_items[0]["amount_total"]) == D("110.00"), \
        f"买方 CNY 合计应为 110.00（不是双方重复累计的 220.00）: {cny_items}"
    assert sum(D(i["amount_total"]) for i in cny_items) == D("110.00"), "买方排行出现重复累计"
    rk2 = _an(ctx, party_role="party_a")["party_ranking"]
    assert rk2["doc_type"] == "contract" and D(rk2["items"][0]["amount_total"]) == D("500.00"), \
        f"甲方合同排行不符: {rk2}"

    # ---------- ⑦ 空态：无假数据 ----------
    empty = _an(ctx, doc_type="other")
    assert empty["summary"]["doc_count"] == 0, f"空集文档数应为 0: {empty['summary']}"
    assert empty["amounts"] == [] and empty["monthly"] == [], f"空集不应有图表数据: {empty}"
    assert empty["party_ranking"]["items"] == [], "空集不应有排行假数据"

    return {
        "valid_docs": 7, "invoice": 6, "contract": 1,
        "cny_invoice_total": "110.00", "jan_cny_invoice_total": "80.00",
        "usd_invoice_total": "10.00", "cny_contract_total": "500.00",
        "edit_then_total": "100.00", "delete_then_total": "120.00",
        "restore_then_total": "100.00",
        "buyer_ranking_total": "110.00", "party_a_ranking_total": "500.00",
        "no_cross_currency_mix": True,
        "empty_state_clean": True, "excluded": {"mock": ids["X1"], "recycle": ids["X2"],
                                                "failed": ids["X3"]},
    }


# ==================== T09 PDF 报告与批量 ZIP ====================

def _pdf_text(data: bytes):
    import fitz
    doc = fitz.open(stream=data, filetype="pdf")
    try:
        return doc, "\n".join(p.get_text() for p in doc)
    except Exception:
        doc.close()
        raise


def _render_shot(data: bytes, page_index: int, out_path: Path, dpi: int = 110):
    """渲染指定页为 PNG —— 供人工做「中文无乱码 / 未裁切 / 附录正确」的视觉检查"""
    import fitz
    doc = fitz.open(stream=data, filetype="pdf")
    try:
        if page_index >= len(doc):
            return None
        pix = doc[page_index].get_pixmap(dpi=dpi)
        pix.save(str(out_path))
        return out_path
    finally:
        doc.close()


def t09_pdf_reports_and_zip(ctx: Ctx):
    """长中文跨页、特殊字符、图片/PDF 附录、ZIP 清单与部分失败"""
    ctx.start_server()
    shots = REPORTS / "p3_pdf"
    shots.mkdir(parents=True, exist_ok=True)

    # ---------- 造两份可识别文档：多页 PDF + 图片 ----------
    pdf_path = ctx.data_dir / "a.pdf"
    d1 = _upload(ctx, "a.pdf", _make_multipage_pdf(pdf_path, pages=2, seed="甲"),
                 "application/pdf")
    code, r1 = _recognize(ctx, d1["doc_id"])
    assert r1["code"] == 200, f"多页 PDF 识别失败: {r1}"
    id1 = d1["doc_id"]

    d2 = _upload(ctx, "b.png", _render_corpus_image(ctx.data_dir / "b.png"), "image/png")
    code, r2 = _recognize(ctx, d2["doc_id"])
    assert r2["code"] == 200, f"图片识别失败: {r2}"
    id2 = d2["doc_id"]

    # ---------- ① 不附原件：基础报告要素齐全 ----------
    code, data = ctx.api("GET", f"/api/document/export/report?doc_id={id1}", raw=True)
    assert code == 200 and data[:4] == b"%PDF", f"单份报告不是 PDF: {code} {data[:80]}"
    doc, txt = _pdf_text(data)
    n_base = len(doc)
    doc.close()
    for must in ("文档识别结果报告", "文档基本信息", "有效字段", "有效全文",
                 "尚未人工校对", "第 1 页 / 共", "不等同于原件"):
        assert must in txt, f"报告缺少要素「{must}」"
    assert "r1" in txt, f"报告未标注版本: {txt[:200]}"
    _render_shot(data, 0, shots / "report_basic_p1.png")
    (shots / "report_basic.pdf").write_bytes(data)

    # ---------- ② 附原件（PDF 原页顺序追加；图片适应页面） ----------
    code, data_pdf_app = ctx.api(
        "GET", f"/api/document/export/report?doc_id={id1}&attach_original=true", raw=True)
    assert code == 200 and data_pdf_app[:4] == b"%PDF", "附原件导出失败"
    doc, _ = _pdf_text(data_pdf_app)
    assert len(doc) == n_base + 2, f"PDF 原件应按 2 页追加，实际 {len(doc) - n_base} 页"
    doc.close()
    _render_shot(data_pdf_app, n_base, shots / "report_appendix_pdf_p1.png")
    _render_shot(data_pdf_app, n_base + 1, shots / "report_appendix_pdf_p2.png")
    (shots / "report_appendix_pdf.pdf").write_bytes(data_pdf_app)

    code, data_img_app = ctx.api(
        "GET", f"/api/document/export/report?doc_id={id2}&attach_original=true", raw=True)
    assert code == 200 and data_img_app[:4] == b"%PDF", "图片附原件导出失败"
    _render_shot(data_img_app, 0, shots / "report_appendix_image.png")
    with __import__('fitz').open(stream=data_img_app, filetype='pdf') as image_report:
        _render_shot(data_img_app, len(image_report) - 1, shots / "report_appendix_image_p3.png")
    (shots / "report_appendix_image.pdf").write_bytes(data_img_app)

    # ---------- ③ 原件缺失 + 要求附原件 → 该份失败，绝不静默省略 ----------
    conn = ctx.db()
    stored = conn.execute("SELECT file_path FROM document_info WHERE id=?", (id2,)).fetchone()[0]
    conn.close()
    Path(stored).unlink()
    code, r = ctx.api("GET",
                      f"/api/document/export/report?doc_id={id2}&attach_original=true", raw=True)
    assert code != 200 or r[:4] != b"%PDF", "原件缺失竟然产出了 PDF（静默省略附录）"
    try:
        msg = json.loads(r.decode("utf-8"))
    except Exception:
        msg = {}
    assert "原件" in (msg.get("msg") or ""), f"原件缺失的报错不可操作: {msg}"
    # 前端就是靠 Content-Type 判断「这是错误 JSON 还是真文件」，
    # 这里必须确认错误响应确实是 application/json —— 否则前端会把错误存成 .pdf
    import urllib.error
    import urllib.request as _ur
    req = _ur.Request(ctx.base + f"/api/document/export/report?doc_id={id2}"
                                             f"&attach_original=true")
    try:
        with _ur.urlopen(req, timeout=30) as resp:
            ct = resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        ct = e.headers.get("Content-Type", "")
    assert "application/json" in ct, f"导出错误响应不是 JSON（前端会存成假 PDF）: {ct}"

    # ---------- ④ 长中文 + 特殊字符：跨页且内容完整（转义但可读、无乱码） ----------
    det = _detail(ctx, id1)
    marker = "特殊字符 <&>\"'测试 第一百二十三章"
    long_text = "\n".join(f"第{i + 1}段：{marker}；本合同金额以最终结算为准，甲乙双方另行签署补充协议。"
                          for i in range(120))
    code, r = ctx.api("PATCH", f"/api/document/{id1}", {
        "expected_revision": det["current_revision"], "effective_text": long_text})
    assert r["code"] == 200, f"写入长文本失败: {r}"
    code, data_long = ctx.api("GET", f"/api/document/export/report?doc_id={id1}", raw=True)
    assert code == 200, "长报告导出失败"
    doc, long_txt = _pdf_text(data_long)
    assert len(doc) >= 2, f"长报告应跨页，实际 {len(doc)} 页"
    doc.close()
    assert marker in long_txt, "长报告正文缺失/乱码（特殊字符未正确转义渲染）"
    _render_shot(data_long, 0, shots / "report_long_p1.png")
    _render_shot(data_long, 1, shots / "report_long_p2.png")
    (shots / "report_long.pdf").write_bytes(data_long)

    # ---------- ⑤ 批量 ZIP：部分成功可下载，清单记录成败与原因 ----------
    code, blob = ctx.api(
        "GET", f"/api/document/export/reports?doc_ids={id1},{id2}&attach_original=true", raw=True)
    assert code == 200 and blob[:2] == b"PK", f"批量 ZIP 未产出: {code} {blob[:120]}"
    (shots / "batch_reports.zip").write_bytes(blob)
    import zipfile as zf
    with zf.ZipFile(io.BytesIO(blob)) as z:
        names = z.namelist()
        manifest = json.loads(z.read("export_manifest.json").decode("utf-8"))
        pdfs = [n for n in names if n.endswith(".pdf")]
    assert "export_manifest.json" in names, "ZIP 缺少清单"
    assert manifest["success_count"] == 1 and manifest["failed_count"] == 1, \
        f"部分成功统计不符: {manifest}"
    assert len(pdfs) == 1, f"ZIP 内 PDF 数量应为 1（失败那份不能有伪 PDF）: {pdfs}"
    failed = [i for i in manifest["items"] if i["status"] == "failed"][0]
    assert failed["doc_id"] == id2 and failed["reason"], "失败条目未记录原因"
    ok = [i for i in manifest["items"] if i["status"] == "ok"][0]
    assert ok["revision"] and ok["file"], f"成功条目缺少 revision/文件名: {ok}"
    assert f"doc_{id1}_r" in ok["file"], f"报告名未包含 doc_id: {ok['file']}"

    # ---------- ⑥ 全部失败 → 返回错误，不产出空 ZIP ----------
    code, blob2 = ctx.api(
        "GET", f"/api/document/export/reports?doc_ids={id2}&attach_original=true", raw=True)
    if blob2[:2] == b"PK":
        raise AssertionError("全部失败竟然产出了 ZIP")
    msg2 = json.loads(blob2.decode("utf-8"))
    assert msg2["code"] != 200, f"全部失败未返回错误: {msg2}"

    # ---------- ⑦ 批量上限 20 份 ----------
    code, r = ctx.api("GET", "/api/document/export/reports?doc_ids=" +
                      ",".join(str(i) for i in range(1, 23)))
    assert r["code"] == 400 and "20" in r["msg"], f"批量上限未生效: {r}"

    return {"basic_report_ok": True, "pdf_appendix_pages": 2,
            "image_appendix_ok": True, "missing_original_fails": True,
            "export_error_is_json": True,
            "long_report_pages": 2, "special_chars_rendered": True,
            "zip_partial_success": True, "manifest_complete": True,
            "all_failed_rejected": True, "batch_limit_20": True,
            "shots": str(shots)}


# ==================== T10 Excel 导出 ====================

def t10_excel_export(ctx: Ctx):
    """按类型分工作表；金额为数值、币种独立列；公式起始文本不触发公式；可再次打开"""
    ctx.start_server()
    from openpyxl import load_workbook

    d_inv = _upload(ctx, "inv.png", _render_corpus_image(ctx.data_dir / "x1.png"), "image/png")
    code, r = _recognize(ctx, d_inv["doc_id"])
    assert r["code"] == 200, f"识别失败: {r}"
    det = _detail(ctx, d_inv["doc_id"])
    code, r = ctx.api("PATCH", f"/api/document/{d_inv['doc_id']}", {
        "expected_revision": det["current_revision"],
        "doc_type": "invoice",
        "fields": {"invoice_date": "2026-03-04", "total_amount": "100.00",
                   "currency": "CNY", "buyer_name": "甲公司"},
    })
    assert r["code"] == 200, f"发票字段写入失败: {r}"
    # 公式起始文本放在「其他」类型的 notes 字段里（notes 属于 other，不属于 invoice）
    d_oth = _upload(ctx, "oth.png", _render_corpus_image(ctx.data_dir / "x2.png"), "image/png")
    code, r = _recognize(ctx, d_oth["doc_id"])
    assert r["code"] == 200, f"识别失败: {r}"
    det = _detail(ctx, d_oth["doc_id"])
    code, r = ctx.api("PATCH", f"/api/document/{d_oth['doc_id']}", {
        "expected_revision": det["current_revision"],
        "doc_type": "other",
        "effective_text": "=1+1 这是以等号开头的正文，不应被 Excel 当公式执行",
        "fields": {"notes": "=SUM(A1:A9)"},
    })
    assert r["code"] == 200, f"其他类型字段写入失败: {r}"

    d_con = _upload(ctx, "con.png", _render_corpus_image(ctx.data_dir / "x3.png"), "image/png")
    code, r = _recognize(ctx, d_con["doc_id"])
    assert r["code"] == 200, f"识别失败: {r}"
    det = _detail(ctx, d_con["doc_id"])
    code, r = ctx.api("PATCH", f"/api/document/{d_con['doc_id']}", {
        "expected_revision": det["current_revision"],
        "doc_type": "contract",
        "fields": {"signed_date": "2026-03-05", "contract_amount": "-20.00",
                   "currency": "CNY", "party_a": "甲公司", "party_b": "丙公司"},
    })
    assert r["code"] == 200, f"合同字段写入失败: {r}"

    ids = f"{d_inv['doc_id']},{d_con['doc_id']},{d_oth['doc_id']}"
    code, blob = ctx.api("GET", f"/api/document/export?doc_ids={ids}", raw=True)
    assert code == 200 and blob[:2] == b"PK", f"Excel 未产出: {code} {blob[:120]}"

    xlsx = REPORTS / "p3_excel_export.xlsx"
    xlsx.write_bytes(blob)

    wb = load_workbook(str(xlsx))          # ① 能再次打开读取
    names = wb.sheetnames
    for must in ("发票", "合同", "其他"):
        assert must in names, f"缺少工作表「{must}」: {names}"

    ws = wb["发票"]
    headers = [c.value for c in ws[1]]
    amount_col = None
    for i, h in enumerate(headers, 1):
        if h == "价税合计":
            amount_col = i
    assert amount_col, f"发票表缺少价税合计列: {headers}"
    assert "币种" in headers, f"币种未独立成列: {headers}"
    assert "校对状态" in headers and "版本" in headers, f"缺少校对状态/版本列: {headers}"

    amount_cell = ws.cell(row=2, column=amount_col)
    assert isinstance(amount_cell.value, (int, float)), \
        f"金额应为数值单元格，实际 {type(amount_cell.value)}: {amount_cell.value}"
    assert abs(float(amount_cell.value) - 100.0) < 1e-9, f"金额数值不符: {amount_cell.value}"

    ws2 = wb["合同"]
    h2 = [c.value for c in ws2[1]]
    cidx = h2.index("合同金额") + 1
    v = ws2.cell(row=2, column=cidx).value
    assert abs(float(v) + 20.0) < 1e-9, f"负数金额导出不符: {v}"

    # ② 公式起始文本：字符串单元格 + 文本格式，不会被当公式
    ws3 = wb["其他"]
    h3 = [c.value for c in ws3[1]]
    nidx = h3.index("备注") + 1
    c = ws3.cell(row=2, column=nidx)
    assert c.data_type == "s", f"公式起始文本不是字符串单元格: {c.data_type}"
    assert str(c.value).startswith("="), f"原文被改动: {c.value!r}"
    assert c.number_format == "@", f"未设置文本格式: {c.number_format}"

    return {"sheets": names, "amount_numeric": True, "negative_amount": "-20.00",
            "currency_column": True, "formula_text_safe": True,
            "reopenable": True, "xlsx": str(xlsx)}


# ==================== 用例注册与执行 ====================

def t11_privacy_and_safety(ctx):
    result = subprocess.run([PY, '-m', 'pytest', 'tests', '-q'],
                            cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=180)
    evidence = REPORTS / 'p4_pytest.txt'
    evidence.write_text(result.stdout + result.stderr, encoding='utf-8')
    assert result.returncode == 0, result.stdout[-2500:] + result.stderr[-500:]
    return {'unit_and_privacy': 'passed', 'cloud': 'mock transport only', 'evidence': str(evidence)}


def t12_integrated_restart(ctx):
    ctx.start_server()
    did = _mk_doc(ctx, 'integration', 'invoice', {
        'invoice_number': 'DEMO-20260925', 'total_amount': '120.00', 'currency': 'CNY',
        'invoice_date': '2026-09-25', 'buyer_name': '本地验收公司'})
    assert _amt_group(_an(ctx), 'invoice', 'CNY')['amount_total'] == '120.00'
    status, pdf = ctx.api('GET', f'/api/document/export/report?doc_id={did}', raw=True)
    assert status == 200 and pdf.startswith(b'%PDF')
    ctx.api('DELETE', f'/api/document/{did}')
    assert _an(ctx)['summary']['doc_count'] == 0
    assert ctx.api('POST', '/api/rag/search', {'query': 'DEMO-20260925', 'doc_ids': [did]})[1]['data']['hits'] == []
    ctx.api('POST', f'/api/document/{did}/restore')
    status, backup = ctx.api('POST', '/api/maintenance/backup', raw=True, timeout=120)
    assert status == 200 and backup.startswith(b'PK')
    before = _detail(ctx, did)
    ctx.stop_server()
    ctx.start_server()
    after = _detail(ctx, did)
    assert after['current_revision'] == before['current_revision']
    assert after['effective_text'] == before['effective_text']
    assert ctx.api('GET', f'/api/file/preview/{did}', raw=True)[0] == 200
    status, home = ctx.api('GET', '/', raw=True)
    assert status == 200 and b'/vite/assets/' in home
    assert ctx.api('GET', '/cdn')[0] == 410
    return {'document_id': did, 'restart_preserved': True, 'original_readable': True,
            'export_and_backup': True, 'cdn_retired': True}


def t13_batch_and_build(ctx):
    ctx.start_server()
    terminals = []
    for i in range(20):
        original = _render_corpus_image(ctx.data_dir / f'batch{i}.png')
        item = _upload(ctx, f'batch{i}.png', original, 'image/png')
        status, response = _recognize(ctx, item['doc_id'])
        assert status == 200 and response['code'] == 200, response
        terminals.append({'id': item['doc_id'], 'status': response['data']['status']})
    assert len({item['id'] for item in terminals}) == 20
    ctx.stop_server()
    npm = shutil.which('npm.cmd') or shutil.which('npm')
    build = subprocess.run([npm, 'run', 'build'], cwd=str(PROJECT_ROOT / 'frontend'),
                           capture_output=True, text=True, timeout=180)
    (REPORTS / 'p4_build.log').write_text(build.stdout + build.stderr, encoding='utf-8')
    assert build.returncode == 0, build.stderr[-1500:]
    return {'batch': terminals, 'build': 'p4_build.log'}


def t13_frozen_regression(ctx):
    """Optional frozen-corpus comparison; the private fixture is not published."""
    result = subprocess.run([PY, 'scripts/verify_frozen.py', str(ctx.data_dir / 'frozen_copy')],
                            cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=180)
    (REPORTS / 'p4_frozen.log').write_text(result.stdout + result.stderr, encoding='utf-8')
    assert result.returncode == 0 and 'FROZEN_OK' in result.stdout, result.stderr[-1500:]
    return {'frozen': 'final_frozen_compare.json'}

CASES = {
    "p1": [
        ("T01a", "空库安装", t01_empty_install),
        ("T01b", "旧库副本迁移+重复启动", t01_legacy_migration),
        ("T01c", "未知结构安全拒绝", t01_unknown_structure_refused),
        ("T02", "删除/恢复/永久删除/缺失原件", t02_recycle_bin),
        ("T03", "备份恢复/坏包/穿越包/门禁/回滚", t03_backup_restore),
    ],
    "p2": [
        ("T04", "图片/多页PDF/损坏/加密/引擎来源", t04_recognition_paths),
        ("T05", "编辑/校对/改类型/重识别失败/历史", t05_edit_and_versions),
        ("T06", "索引版本/范围限制/删除/重启补偿/旧会话", t06_index_scope_compensation),
        ("T07", "去重/并发/回收站/单批失败重试", t07_upload_dedup_and_batch),
    ],
    "p3": [
        ("T08", "固定统计样例/角色排行/穿透/空态", t08_analytics_fixed_sample),
        ("T09", "PDF报告/附录/ZIP清单/部分失败", t09_pdf_reports_and_zip),
        ("T10", "Excel分类/数值金额/公式防护", t10_excel_export),
    ],
    'p4': [
        ('T11', '隐私、云端模拟降级与维护安全', t11_privacy_and_safety),
        ('T12', '完整流程、重启及正式入口', t12_integrated_restart),
        ('T13a', '20份批量与前端构建', t13_batch_and_build),
        ('T13b', '冻结检索回归（需本机夹具）', t13_frozen_regression),
    ],
}


def run_phase(phase: str, work_root: Path, keep: bool) -> dict:
    results = []
    for case_id, name, fn in CASES[phase]:
        optional_fixture = {
            'T01b': [FROZEN / 'data_root' / 'data' / 'doc_ocr_v2.db'],
            'T13b': [FROZEN / 'data_root' / 'data' / 'doc_ocr_v2.db',
                     FROZEN / 'eval_set_frozen.json', REPORTS / 'baseline_rag.json'],
        }.get(case_id, [])
        if optional_fixture and any(not path.is_file() for path in optional_fixture):
            results.append({'id': case_id, 'name': name, 'pass': False, 'skip': True,
                            'reason': '公开源码不附带旧库/原件冻结夹具'})
            print(f"  ⏭ {case_id} {name}：本机冻结夹具缺失，跳过")
            continue
        case_dir = work_root / f"{phase}_{case_id}"
        ctx = Ctx(case_dir)
        t0 = time.time()
        try:
            detail = fn(ctx)
            results.append({"id": case_id, "name": name, "pass": True,
                            "seconds": round(time.time() - t0, 1), "detail": detail})
            print(f"  ✅ {case_id} {name}（{time.time() - t0:.1f}s）")
        except Exception as e:
            results.append({
                "id": case_id, "name": name, "pass": False,
                "seconds": round(time.time() - t0, 1),
                "error": f"{type(e).__name__}: {e}",
                "traceback": traceback.format_exc()[-1500:],
            })
            print(f"  ❌ {case_id} {name}: {type(e).__name__}: {e}")
        finally:
            ctx.close()
            if not keep:
                pass  # 目录保留供排查；清理由调用方/系统临时目录策略决定
    return {
        "phase": phase,
        "run_at": datetime.now().isoformat(timespec="seconds"),
        "work_root": str(work_root),
        "total": len(results),
        "passed": sum(1 for r in results if r["pass"]),
        "skipped": sum(1 for r in results if r.get("skip")),
        "cases": results,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="方案 B 交付验证")
    ap.add_argument("--phase", required=True, choices=["p1", "p2", "p3", "p4", "all"])
    ap.add_argument("--data-dir", type=Path, default=None,
                    help="隔离工作根目录（默认 %TEMP%\\doc_ocr_verify_<时间戳>）")
    ap.add_argument("--keep", action="store_true", help="保留各用例子目录供排查")
    args = ap.parse_args()

    work_root = args.data_dir or Path(tempfile.mkdtemp(prefix="doc_ocr_verify_"))
    work_root.mkdir(parents=True, exist_ok=True)
    phases = ["p1", "p2", "p3", "p4"] if args.phase == "all" else [args.phase]

    REPORTS.mkdir(parents=True, exist_ok=True)
    summaries = []
    for ph in phases:
        if not CASES[ph]:
            print(f"[{ph}] 暂无用例（该阶段未实现），跳过")
            continue
        print(f"[{ph}] 开始验证，工作目录 {work_root}")
        s = run_phase(ph, work_root, args.keep)
        summaries.append(s)
        out = REPORTS / f"{ph}.json"
        out.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[{ph}] {s['passed']}/{s['total']} 通过，跳过 {s['skipped']} → {out}")

    failed = sum(s["total"] - s["passed"] - s["skipped"] for s in summaries)
    print(f"\n总计：{sum(s['passed'] for s in summaries)}/{sum(s['total'] for s in summaries)}"
          f" 通过；跳过 {sum(s['skipped'] for s in summaries)}；失败 {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

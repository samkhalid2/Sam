#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🎛️ بوت إدارة البوتات - نسخة Subprocess مع حماية أمنية (نسخة مطورة)
✅ كل بوت = Process منفصل
✅ حماية ضد سرقة الملفات والبوتات
✅ مميزات متقدمة للمشرف
✅ استبدال الملفات وإيقاف/تشغيل البوتات
✅ إدارة متقدمة للمكتبات: فحص، معلومات، حذف شامل مع تنظيف الملفات المتبقية
✅ نسخ احتياطي لأي ملف داخل أي بوت (حتى ملفات تُنشأ أثناء التشغيل) + نسخة ZIP كاملة
✅ إدارة مشرفين إضافيين من داخل البوت
"""

import os
import sys
import subprocess
import time
import base64
import re
import shutil
import tempfile
import sqlite3
import zipfile
import site
import json
from urllib.parse import quote, unquote
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

# ═══════════════════════════════════════════════════════════
# ⚙️ الإعدادات
# ═══════════════════════════════════════════════════════════
ADMIN_TOKEN = "1665080977:AAH31rE0ra9Qf--HpYtgoxRfEzs7ilqinH8"

# المشرف الرئيسي - له صلاحيات كاملة دائماً ولا يمكن حذفه، وهو الوحيد
# القادر على إضافة/حذف مشرفين إضافيين
SUPER_ADMIN_ID = 1058616316

BOTS_ROOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bots_data")
DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bots_manager.db")
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "system.log")
MAIN_STDERR_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main_errors.log")

# متغيرات النظام
running_bots = {}
paused_bots = {}
error_logs = []
pending_requests = {}

# ═══════════════════════════════════════════════════════════
# 🔒 نظام الحماية الأمنية
# ═══════════════════════════════════════════════════════════

SECURITY_CODE = """
import os
import sys
import builtins

__BOT_DIR__ = os.path.dirname(os.path.abspath(__file__))
__BOTS_ROOT__ = os.path.dirname(__BOT_DIR__)

_original_open = builtins.open
def _secure_open(file, mode='r', *args, **kwargs):
    if isinstance(file, str):
        abs_path = os.path.abspath(file)
        if not (abs_path.startswith(__BOT_DIR__) or abs_path.startswith('/tmp') or abs_path.startswith('/data/data')):
            if 'w' in mode or 'a' in mode or '+' in mode:
                raise PermissionError(f"الكتابة ممنوعة خارج مجلد البوت: {file}")
    return _original_open(file, mode, *args, **kwargs)

builtins.open = _secure_open
print("نظام الحماية نشط")
"""

# ═══════════════════════════════════════════════════════════
# 🗄️ قاعدة البيانات SQLite
# ═══════════════════════════════════════════════════════════

def init_database():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS bots
                 (bot_num INTEGER PRIMARY KEY,
                  bot_name TEXT NOT NULL,
                  bot_code TEXT NOT NULL,
                  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS bot_imports
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  bot_num INTEGER,
                  module_name TEXT NOT NULL,
                  FOREIGN KEY (bot_num) REFERENCES bots(bot_num))''')
    # ✅ الإصلاح: المفتاح الأساسي أصبح (bot_num, module_name) بدل module_name وحده
    # حتى لا يتشارك بوتان مختلفان نفس صف الملف المساعد لمجرد تطابق الاسم
    c.execute('''CREATE TABLE IF NOT EXISTS helper_files
                 (bot_num INTEGER NOT NULL,
                  module_name TEXT NOT NULL,
                  code TEXT NOT NULL,
                  PRIMARY KEY (bot_num, module_name))''')
    c.execute('''CREATE TABLE IF NOT EXISTS admins
                 (user_id INTEGER PRIMARY KEY,
                  added_by INTEGER,
                  added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()

    # ترقية قواعد بيانات قديمة كانت تستخدم helper_files(module_name PRIMARY KEY)
    try:
        c.execute("PRAGMA table_info(helper_files)")
        cols = [row[1] for row in c.fetchall()]
        if 'bot_num' not in cols:
            print("[WARNING] ترقية جدول helper_files القديم إلى مفتاح مركّب (bot_num, module_name)")
            c.execute("ALTER TABLE helper_files RENAME TO helper_files_old")
            c.execute('''CREATE TABLE helper_files
                         (bot_num INTEGER NOT NULL,
                          module_name TEXT NOT NULL,
                          code TEXT NOT NULL,
                          PRIMARY KEY (bot_num, module_name))''')
            # نعيد ربط كل ملف مساعد قديم بكل بوت كان يستورده (أفضل تقدير ممكن من بيانات قديمة تالفة أصلاً)
            c.execute("SELECT module_name, code FROM helper_files_old")
            old_helpers = {row[0]: row[1] for row in c.fetchall()}
            c.execute("SELECT DISTINCT bot_num, module_name FROM bot_imports")
            for bot_num, module_name in c.fetchall():
                if module_name in old_helpers:
                    c.execute(
                        "INSERT OR REPLACE INTO helper_files (bot_num, module_name, code) VALUES (?, ?, ?)",
                        (bot_num, module_name, old_helpers[module_name])
                    )
            c.execute("DROP TABLE helper_files_old")
            conn.commit()
    except Exception as e:
        print(f"[ERROR] خطأ في ترقية جدول helper_files: {e}")

    conn.close()

def save_bot_to_db(bot_num, bot_name, bot_code, helper_modules=None):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    try:
        c.execute("DELETE FROM bots WHERE bot_num = ?", (bot_num,))
        c.execute("DELETE FROM bot_imports WHERE bot_num = ?", (bot_num,))
        c.execute("INSERT INTO bots (bot_num, bot_name, bot_code) VALUES (?, ?, ?)",
                  (bot_num, bot_name, bot_code))
        if helper_modules:
            for module_name in helper_modules:
                c.execute("INSERT INTO bot_imports (bot_num, module_name) VALUES (?, ?)",
                          (bot_num, module_name))
        conn.commit()
        return True
    except Exception as e:
        log_message(f"خطأ في حفظ البوت: {e}", "ERROR")
        conn.rollback()
        return False
    finally:
        conn.close()

def save_helper_file_to_db(bot_num, module_name, code):
    # ✅ الإصلاح: الحفظ الآن مرتبط برقم البوت، فملفان مختلفان بنفس الاسم
    # في بوتين مختلفين لا يتصادمان أبداً
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    try:
        c.execute("INSERT OR REPLACE INTO helper_files (bot_num, module_name, code) VALUES (?, ?, ?)",
                  (bot_num, module_name, code))
        conn.commit()
        return True
    except Exception as e:
        log_message(f"خطأ في حفظ الملف المساعد: {e}", "ERROR")
        return False
    finally:
        conn.close()

def load_bot_from_db(bot_num):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    try:
        c.execute("SELECT bot_name, bot_code FROM bots WHERE bot_num = ?", (bot_num,))
        result = c.fetchone()
        if result:
            bot_name, bot_code = result
            c.execute("SELECT module_name FROM bot_imports WHERE bot_num = ?", (bot_num,))
            imports = [row[0] for row in c.fetchall()]
            return {'name': bot_name, 'code': bot_code, 'imports': imports}
        return None
    finally:
        conn.close()

def load_helper_files_from_db(bot_num):
    # ✅ الإصلاح: نقرأ الملفات المساعدة الخاصة بهذا البوت فقط من helper_files
    # مباشرة عبر (bot_num, module_name)، بدل الاعتماد على اسم الملف وحده
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    try:
        c.execute("SELECT module_name, code FROM helper_files WHERE bot_num = ?", (bot_num,))
        return {row[0]: row[1] for row in c.fetchall()}
    finally:
        conn.close()

def load_all_bots_from_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    try:
        c.execute("SELECT bot_num, bot_name FROM bots ORDER BY bot_num")
        return {bot_num: bot_name for bot_num, bot_name in c.fetchall()}
    finally:
        conn.close()

def delete_bot_from_db(bot_num):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    try:
        c.execute("DELETE FROM bots WHERE bot_num = ?", (bot_num,))
        c.execute("DELETE FROM bot_imports WHERE bot_num = ?", (bot_num,))
        # ✅ تنظيف الملفات المساعدة الخاصة بهذا البوت أيضاً بدل تركها يتيمة في القاعدة
        c.execute("DELETE FROM helper_files WHERE bot_num = ?", (bot_num,))
        conn.commit()
        return True
    except Exception as e:
        log_message(f"خطأ في حذف البوت: {e}", "ERROR")
        return False
    finally:
        conn.close()

init_database()

# ═══════════════════════════════════════════════════════════
# 👥 إدارة المشرفين
# ═══════════════════════════════════════════════════════════

def add_admin_to_db(user_id, added_by):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    try:
        c.execute("INSERT OR IGNORE INTO admins (user_id, added_by) VALUES (?, ?)", (user_id, added_by))
        conn.commit()
        return True
    except Exception as e:
        log_message(f"خطأ في إضافة مشرف: {e}", "ERROR")
        return False
    finally:
        conn.close()

def remove_admin_from_db(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    try:
        c.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
        conn.commit()
        return True
    except Exception as e:
        log_message(f"خطأ في حذف مشرف: {e}", "ERROR")
        return False
    finally:
        conn.close()

def get_extra_admins():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    try:
        c.execute("SELECT user_id, added_by, added_at FROM admins ORDER BY added_at")
        return c.fetchall()
    finally:
        conn.close()

def get_all_admin_ids():
    ids = {SUPER_ADMIN_ID}
    for row in get_extra_admins():
        ids.add(row[0])
    return ids

def is_admin(user_id):
    return user_id in get_all_admin_ids()

def is_super_admin(user_id):
    return user_id == SUPER_ADMIN_ID

# ═══════════════════════════════════════════════════════════
# 🔧 وظائف النظام
# ═══════════════════════════════════════════════════════════

def log_message(message, level="INFO"):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"[{timestamp}] [{level}] {message}"
    error_logs.append(log_entry)
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(log_entry + "\n")
    except:
        pass
    print(log_entry)

def get_bot_directory(bot_num):
    bot_dir = os.path.join(BOTS_ROOT_DIR, f"bot_{bot_num}")
    os.makedirs(bot_dir, exist_ok=True)
    return bot_dir

def make_upload_temp_path(file_name):
    """مسار مؤقت فريد؛ يمنع تداخل رفع ملفين لهما الاسم نفسه."""
    suffix = os.path.splitext(file_name)[1]
    fd, file_path = tempfile.mkstemp(prefix="bot_upload_", suffix=suffix)
    os.close(fd)
    os.remove(file_path)
    return file_path

def _read_text_file(file_path):
    """قراءة ملف نصي بأمان أثناء فحص ملفات البوت."""
    try:
        if not os.path.isfile(file_path) or os.path.getsize(file_path) > 10 * 1024 * 1024:
            return ""
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            return f.read()
    except Exception:
        return ""

def _get_recovery_code(file_path):
    """إرجاع كود البوت الأصلي من ملف التشغيل المحمي عند الحاجة."""
    code = _read_text_file(file_path)
    if os.path.basename(file_path).endswith('_run.py'):
        marker = SECURITY_CODE + "\n"
        if marker in code:
            code = code.split(marker, 1)[1]
    return code

def restore_bots_from_storage():
    """استعادة البوتات تلقائياً إذا كانت قاعدة البيانات محذوفة أو ناقصة."""
    if not os.path.isdir(BOTS_ROOT_DIR):
        return 0

    restored = 0
    for folder_name in os.listdir(BOTS_ROOT_DIR):
        match = re.fullmatch(r'bot_(\d+)', folder_name)
        if not match:
            continue
        bot_num = int(match.group(1))
        if load_bot_from_db(bot_num):
            continue

        bot_dir = os.path.join(BOTS_ROOT_DIR, folder_name)
        py_files = []
        for file_name in os.listdir(bot_dir):
            file_path = os.path.join(bot_dir, file_name)
            if os.path.isfile(file_path) and file_name.endswith('.py'):
                py_files.append(file_path)
        if not py_files:
            continue

        # نفضل الاسم المعتاد للملف الرئيسي، ثم الملف الذي يحتوي التوكن،
        # ثم أي ملف Python غير مؤقت. هذا يمنع اختيار c.py المساعد بدلاً من
        # main.py إذا كان التوكن محفوظاً في الملف المساعد فقط.
        token_files = {
            p for p in py_files if extract_token_from_code(_read_text_file(p))
        }
        candidates = list(py_files)
        candidates.sort(key=lambda p: (
            0 if os.path.basename(p).lower() in ('main.py', 'bot.py', 'index.py') else 1,
            0 if p in token_files else 1,
            0 if not os.path.basename(p).endswith('_run.py') else 1,
            -os.path.getsize(p)
        ))
        main_path = candidates[0]
        main_code = _get_recovery_code(main_path)
        if not main_code.strip():
            continue

        helper_names = []
        for helper_path in py_files:
            if helper_path == main_path or os.path.basename(helper_path).endswith('_run.py'):
                continue
            module_name = os.path.splitext(os.path.basename(helper_path))[0]
            helper_code = _get_recovery_code(helper_path)
            if helper_code:
                encoded_helper = base64.b64encode(helper_code.encode('utf-8')).decode('ascii')
                save_helper_file_to_db(bot_num, module_name, encoded_helper)
                helper_names.append(module_name)

        bot_name = os.path.basename(main_path)
        encoded_main = base64.b64encode(main_code.encode('utf-8')).decode('ascii')
        if save_bot_to_db(bot_num, bot_name, encoded_main, helper_names):
            restored += 1
            log_message(f"تم استرجاع البوت {bot_num} من مجلد التخزين", "WARNING")
    return restored

def fix_bot_code(code, bot_num):
    bot_dir = get_bot_directory(bot_num)
    modified = code
    old_bot_pattern = r'/storage/emulated/0/[^/]+/[^/]+/bots_data/bot_\d+'
    modified = re.sub(old_bot_pattern, '', modified)

    def replace_simple_db(match):
        quote = match.group(1)
        db_file = match.group(2)
        if db_file.startswith('/'):
            return match.group(0)
        return f'{quote}{os.path.join(bot_dir, db_file)}{quote}'
    modified = re.sub(r'(["\'])(\w+\.(?:db|sqlite|sqlite3))\1', replace_simple_db, modified)

    def replace_sqlite3(match):
        db_file = match.group(1)
        if db_file.startswith('/'):
            return match.group(0)
        return f'sqlite3.connect("{os.path.join(bot_dir, db_file)}")'
    modified = re.sub(r'sqlite3\.connect\(["\']([^"\']+)["\']\)', replace_sqlite3, modified)

    def replace_aiosqlite(match):
        db_file = match.group(1)
        if db_file.startswith('/'):
            return match.group(0)
        return f'aiosqlite.connect("{os.path.join(bot_dir, db_file)}")'
    modified = re.sub(r'aiosqlite\.connect\(["\']([^"\']+)["\']\)', replace_aiosqlite, modified)

    def replace_file_open(match):
        quote = match.group(1)
        file_name = match.group(2)
        if file_name.startswith('/'):
            return match.group(0)
        return f'open({quote}{os.path.join(bot_dir, file_name)}{quote}, '
    modified = re.sub(r'open\((["\'])(\w+\.(?:txt|json|csv|log))\1\s*,', replace_file_open, modified)
    return modified

def install_package(package_name):
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", package_name, "--upgrade"],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode == 0:
            return True, f"تم تثبيت {package_name}"
        else:
            return False, f"فشل: {result.stderr[:100]}"
    except Exception as e:
        return False, str(e)

def uninstall_package(package_name):
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "uninstall", package_name, "-y"],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode == 0:
            return True, f"تم حذف {package_name}"
        else:
            return False, f"فشل عبر pip (سيتم تنظيف يدوي): {result.stderr[:150]}"
    except Exception as e:
        return False, str(e)

def get_installed_packages():
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "list", "--format=columns"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            lines = result.stdout.strip().split('\n')
            packages = []
            for line in lines[2:]:
                parts = line.split()
                if len(parts) >= 2:
                    packages.append({'name': parts[0], 'version': parts[1]})
            return packages
        return []
    except Exception as e:
        log_message(f"خطأ في جلب المكتبات: {e}", "ERROR")
        return []

def is_package_installed(package_name):
    """التحقق من تثبيت مكتبة عبر pip show، يرجع (installed, output)"""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "show", package_name],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0 and result.stdout.strip():
            return True, result.stdout.strip()
        return False, None
    except Exception as e:
        return False, str(e)

def get_site_packages_dirs():
    """كل المسارات المحتمل وجود المكتبات فيها (site-packages بكل أنواعها)"""
    dirs = set()
    try:
        dirs.update(site.getsitepackages())
    except Exception:
        pass
    try:
        user_site = site.getusersitepackages()
        if user_site:
            dirs.add(user_site)
    except Exception:
        pass
    try:
        import sysconfig
        purelib = sysconfig.get_paths().get("purelib")
        platlib = sysconfig.get_paths().get("platlib")
        if purelib:
            dirs.add(purelib)
        if platlib:
            dirs.add(platlib)
    except Exception:
        pass
    return [d for d in dirs if d and os.path.isdir(d)]

def normalize_pkg_name(name):
    """توحيد اسم المكتبة (يوتيوب-دي-ال-بي وyoutube_dl و youtube.dl تصير نفس الشيء)"""
    name = name.split('-')[0] if name.endswith('.dist-info') else name
    return re.sub(r'[-_.]+', '-', name).strip('-').lower()

def force_clean_package(package_name):
    """
    حذف شامل للمكتبة: pip uninstall أولاً، ثم تنظيف يدوي لأي مجلدات
    أو ملفات متبقية (بما فيها .dist-info و .egg-info و __pycache__)
    من كل مسارات site-packages المتاحة، وليس من مكان واحد فقط.
    """
    removed_paths = []

    # 1) حذف عادي عبر pip
    ok, msg = uninstall_package(package_name)

    # 2) تنظيف يدوي شامل من كل المسارات
    norm_target = normalize_pkg_name(package_name)

    for base_dir in get_site_packages_dirs():
        try:
            entries = os.listdir(base_dir)
        except Exception:
            continue

        for entry in entries:
            entry_path = os.path.join(base_dir, entry)

            # اسم المجلد/الملف بدون لواحق dist-info / egg-info / إصدار
            base_name = entry
            for suffix in ('.dist-info', '.egg-info', '.egg-link'):
                if base_name.endswith(suffix):
                    base_name = base_name[: -len(suffix)]
                    break

            entry_norm = normalize_pkg_name(base_name.split('-')[0])

            matches = (
                entry_norm == norm_target
                or base_name.lower().startswith(f"{norm_target}-")
                or entry.lower() == norm_target
            )

            if matches:
                try:
                    if os.path.isdir(entry_path):
                        shutil.rmtree(entry_path, ignore_errors=True)
                    else:
                        os.remove(entry_path)
                    removed_paths.append(entry_path)
                except Exception as e:
                    log_message(f"فشل حذف {entry_path}: {e}", "WARNING")

    return ok, msg, removed_paths

def validate_pip_command(cmd_text):
    """
    يتحقق أن الأمر المرسل عبارة عن أمر/سلسلة أوامر pip فقط (يسمح بدمجها مع
    grep للتصفية، أو > للحفظ بملف)، ويرفض أي تسلسل أوامر آخر (; && || إلخ)
    منعاً لتنفيذ أوامر نظام عشوائية.
    يرجع: (ok, error_or_none, cleaned_cmd, redirect_target_or_none)
    """
    cmd_text = cmd_text.strip()
    if not cmd_text:
        return False, "الأمر فارغ", cmd_text, None

    if any(bad in cmd_text for bad in (';', '&&', '||', '`', '$(', '\n', '\r')):
        return False, "غير مسموح بدمج عدة أوامر (; && || إلخ) - أمر واحد فقط بكل مرة", cmd_text, None

    redirect_target = None
    working_cmd = cmd_text
    redirect_match = re.search(r'(?<!\d)>\s*([^\s|]+)\s*$', cmd_text)
    if redirect_match:
        redirect_target = redirect_match.group(1)
        working_cmd = cmd_text[:redirect_match.start()].strip()

    segments = [s.strip() for s in working_cmd.split('|')]
    allowed_starts = (
        'pip', 'pip3',
        'python -m pip', 'python3 -m pip',
        f'{os.path.basename(sys.executable)} -m pip',
        'grep', 'findstr'
    )
    for seg in segments:
        if not seg:
            return False, "يوجد جزء فارغ في الأمر", cmd_text, None
        if not any(seg == a or seg.startswith(a + ' ') for a in allowed_starts):
            return False, (
                f"الأمر غير مسموح:\n`{seg}`\n\n"
                "مسموح فقط بأوامر pip (ويمكن دمجها مع grep للتصفية، أو > للحفظ بملف)"
            ), cmd_text, None

    return True, None, cmd_text, redirect_target


def send_admin_notification(message):
    try:
        import requests
        for admin_id in get_all_admin_ids():
            url = f"https://api.telegram.org/bot{ADMIN_TOKEN}/sendMessage"
            requests.post(url, data={
                "chat_id": admin_id,
                "text": message
            }, timeout=5)
    except:
        pass

# ═══════════════════════════════════════════════════════════
# 🗑️ وظائف حذف السجلات
# ═══════════════════════════════════════════════════════════

def get_logs_size_info():
    """جلب معلومات حجم جميع ملفات السجل"""
    info = {}

    if os.path.exists(LOG_FILE):
        info['system_log'] = os.path.getsize(LOG_FILE) / 1024
    else:
        info['system_log'] = 0

    if os.path.exists(MAIN_STDERR_FILE):
        info['main_errors'] = os.path.getsize(MAIN_STDERR_FILE) / 1024
    else:
        info['main_errors'] = 0

    all_bots = load_all_bots_from_db()
    info['bots_logs'] = {}
    total_bots_logs = 0
    for bot_num in all_bots:
        bot_dir = get_bot_directory(bot_num)
        stdout_file = os.path.join(bot_dir, f"bot_{bot_num}_output.log")
        stderr_file = os.path.join(bot_dir, f"bot_{bot_num}_errors.log")
        bot_log_size = 0
        if os.path.exists(stdout_file):
            bot_log_size += os.path.getsize(stdout_file) / 1024
        if os.path.exists(stderr_file):
            bot_log_size += os.path.getsize(stderr_file) / 1024
        info['bots_logs'][bot_num] = bot_log_size
        total_bots_logs += bot_log_size

    info['total_bots_logs'] = total_bots_logs
    info['total'] = info['system_log'] + info['main_errors'] + total_bots_logs
    return info

def clear_bot_logs(bot_num):
    bot_dir = get_bot_directory(bot_num)
    stdout_file = os.path.join(bot_dir, f"bot_{bot_num}_output.log")
    stderr_file = os.path.join(bot_dir, f"bot_{bot_num}_errors.log")
    cleared = []
    for f in [stdout_file, stderr_file]:
        if os.path.exists(f):
            try:
                open(f, 'w').close()
                cleared.append(os.path.basename(f))
            except Exception as e:
                log_message(f"خطأ في تفريغ {f}: {e}", "ERROR")
    return cleared

def clear_system_logs():
    cleared = []
    for log_path, name in [(LOG_FILE, 'system.log'), (MAIN_STDERR_FILE, 'main_errors.log')]:
        if os.path.exists(log_path):
            try:
                open(log_path, 'w').close()
                cleared.append(name)
            except Exception as e:
                log_message(f"خطأ في تفريغ {name}: {e}", "ERROR")
    error_logs.clear()
    return cleared

def clear_all_logs():
    results = []
    results.extend(clear_system_logs())
    all_bots = load_all_bots_from_db()
    for bot_num in all_bots:
        results.extend(clear_bot_logs(bot_num))
    return results

# ═══════════════════════════════════════════════════════════
# 💾 نسخ احتياطي لملفات البوتات (اي ملف داخل مجلد البوت)
# ═══════════════════════════════════════════════════════════

def get_clean_main_file(bot_num):
    """
    يرجع (اسم الملف الاصلي، محتوى الكود النظيف) للملف الرئيسي كما تم رفعه آخر مرة
    من طرف المستخدم — مفكوك من قاعدة البيانات مباشرة، وليس ملف bot_X_run.py
    المشفّر بكود الحماية والمعدّل بمسارات الملفات (هذا الأخير غير صالح للاستخدام
    خارج بيئة الاستضافة).
    """
    bot_data = load_bot_from_db(bot_num)
    if not bot_data:
        return None, None
    bot_name = bot_data['name']
    try:
        code = base64.b64decode(bot_data['code']).decode('utf-8')
    except Exception as e:
        log_message(f"فشل فك تشفير الملف الرئيسي للبوت {bot_num}: {e}", "ERROR")
        return bot_name, None
    old_paths_pattern = r'/storage/emulated/0/[^/]+/[^/]+/bots_data/bot_\d+'
    if re.search(old_paths_pattern, code):
        code = re.sub(old_paths_pattern, '', code)
    return bot_name, code

def zip_bot_directory(bot_num):
    """
    ضغط نسخة احتياطية كاملة لبوت معين في ملف ZIP واحد:
    - الملف الرئيسي بنسخته الأصلية النظيفة (كما رفعها المستخدم، مفكوكة من القاعدة)
    - كل الملفات الأخرى الموجودة فعلياً في مجلد البوت (بيانات، ملفات مساعدة، ملفات أُنشئت وقت التشغيل)
    عدا ملف التشغيل المؤقت (bot_X_run.py) لأنه مجرد نسخة مشفّرة بكود الحماية وغير صالحة للاستخدام المباشر.
    """
    bot_dir = get_bot_directory(bot_num)
    zip_path = os.path.join(tempfile.gettempdir(), f"bot_{bot_num}_backup_{int(time.time())}.zip")
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        # 1) الملف الرئيسي الأصلي (النظيف) من قاعدة البيانات
        bot_name, clean_code = get_clean_main_file(bot_num)
        if clean_code is not None:
            main_arcname = os.path.basename(bot_name) if bot_name else f"bot_{bot_num}_main.py"
            try:
                # نكتب تعريفاً داخل النسخة حتى لا نضطر لتخمين الملف الرسمي
                # عند استيراد ZIP يحتوي أكثر من ملف Python.
                manifest = {
                    'version': 1,
                    'bot_num': bot_num,
                    'bot_name': main_arcname,
                    'main_file': main_arcname
                }
                zf.writestr(
                    '__bot_manager__/manifest.json',
                    json.dumps(manifest, ensure_ascii=False, indent=2)
                )
                zf.writestr(main_arcname, clean_code)
            except Exception as e:
                log_message(f"فشل إضافة الملف الرئيسي للنسخة الاحتياطية: {e}", "WARNING")

        # 2) باقي ملفات مجلد البوت (بيانات + ملفات مساعدة + ملفات وقت التشغيل)
        for root, dirs, files in os.walk(bot_dir):
            for f in files:
                if f.endswith('_run.py'):
                    continue
                full_path = os.path.join(root, f)
                arcname = os.path.relpath(full_path, bot_dir)
                try:
                    zf.write(full_path, arcname)
                except Exception:
                    pass
    return zip_path

def list_bot_files(bot_num):
    """كل الملفات الموجودة فعلياً داخل مجلد البوت الآن (بما فيها ملفات أنشأها البوت وقت التشغيل)"""
    bot_dir = get_bot_directory(bot_num)
    if not os.path.exists(bot_dir):
        return []
    files = []
    for f in os.listdir(bot_dir):
        full_path = os.path.join(bot_dir, f)
        if os.path.isfile(full_path) and not f.endswith('_run.py'):
            files.append(f)
    return files

def safe_bot_path(bot_num, relative_path=''):
    """إرجاع مسار داخل مجلد البوت فقط، مع رفض الخروج منه."""
    bot_dir = os.path.abspath(get_bot_directory(bot_num))
    relative_path = relative_path.replace('\\', '/')
    if os.path.isabs(relative_path):
        raise ValueError("المسار غير مسموح")
    target = os.path.abspath(os.path.join(bot_dir, relative_path))
    if os.path.commonpath([bot_dir, target]) != bot_dir:
        raise ValueError("المسار خارج مجلد البوت")
    return target

def bot_directory_entries(bot_num, relative_path=''):
    """إرجاع المجلدات والملفات في مسار فرعي داخل بوت محدد."""
    current_dir = safe_bot_path(bot_num, relative_path)
    if not os.path.isdir(current_dir):
        return [], []
    directories, files = [], []
    for name in sorted(os.listdir(current_dir), key=str.lower):
        if name.endswith('_run.py'):
            continue
        full_path = os.path.join(current_dir, name)
        if os.path.isdir(full_path):
            directories.append(name)
        elif os.path.isfile(full_path):
            files.append(name)
    return directories, files

def extract_zip_safely(zip_path, destination):
    """فك ZIP بدون السماح لمساراته بالكتابة خارج مجلد الاستخراج."""
    with zipfile.ZipFile(zip_path, 'r') as archive:
        members = []
        destination = os.path.abspath(destination)
        for info in archive.infolist():
            name = info.filename.replace('\\', '/')
            if not name or name.startswith('/') or os.path.isabs(name):
                raise ValueError("النسخة تحتوي مساراً غير آمن")
            target = os.path.abspath(os.path.join(destination, name))
            if os.path.commonpath([destination, target]) != destination:
                raise ValueError("النسخة تحتوي مساراً خارجياً")
            members.append((info, name, target))
        for info, name, target in members:
            if name.endswith('/'):
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with archive.open(info, 'r') as source, open(target, 'wb') as output:
                shutil.copyfileobj(source, output)

def import_bot_from_zip(zip_path):
    """استيراد نسخة ZIP وإضافتها كبوت جديد، مع حفظ ملفات المساعدة الخاصة به."""
    extraction_dir = tempfile.mkdtemp(prefix='bot_zip_import_')
    try:
        preferred_main = None
        # النسخ الجديدة تحدد الملف الرسمي في manifest. النسخ القديمة من
        # هذا النظام كانت تضع الملف الرسمي أول ملف داخل ZIP.
        with zipfile.ZipFile(zip_path, 'r') as archive:
            names = [info.filename.replace('\\', '/') for info in archive.infolist()]
            try:
                manifest_info = archive.getinfo('__bot_manager__/manifest.json')
                manifest_data = json.loads(archive.read(manifest_info).decode('utf-8'))
                preferred_main = str(manifest_data.get('main_file', '')).replace('\\', '/')
            except (KeyError, ValueError, TypeError, json.JSONDecodeError):
                for name in names:
                    if name.lower().endswith('.py') and not name.lower().endswith('_run.py'):
                        preferred_main = name
                        break
        extract_zip_safely(zip_path, extraction_dir)
        py_files = []
        for root, _, files in os.walk(extraction_dir):
            for file_name in files:
                if file_name.endswith('.py') and not file_name.endswith('_run.py'):
                    py_files.append(os.path.join(root, file_name))
        if not py_files:
            raise ValueError("ملف ZIP لا يحتوي ملف Python للبوت")

        preferred_path = None
        if preferred_main:
            candidate = os.path.abspath(os.path.join(extraction_dir, preferred_main))
            if (os.path.commonpath([os.path.abspath(extraction_dir), candidate]) ==
                    os.path.abspath(extraction_dir) and candidate in py_files):
                preferred_path = candidate

        if preferred_path:
            main_path = preferred_path
        else:
            token_files = {p for p in py_files if extract_token_from_code(_read_text_file(p))}
            py_files.sort(key=lambda p: (
                0 if os.path.basename(p).lower() in ('main.py', 'bot.py', 'index.py') else 1,
                0 if p in token_files else 1,
                -os.path.getsize(p)
            ))
            main_path = py_files[0]
        main_code = _read_text_file(main_path)
        all_bots = load_all_bots_from_db()
        bot_num = max(all_bots.keys()) + 1 if all_bots else 1
        bot_dir = get_bot_directory(bot_num)
        if os.path.exists(bot_dir):
            shutil.rmtree(bot_dir)
        shutil.copytree(extraction_dir, bot_dir)
        shutil.rmtree(os.path.join(bot_dir, '__bot_manager__'), ignore_errors=True)

        relative_main = os.path.relpath(main_path, extraction_dir)
        bot_name = os.path.basename(relative_main)
        encoded_main = base64.b64encode(main_code.encode('utf-8')).decode('ascii')
        helper_codes = []
        for helper_path in py_files:
            if helper_path == main_path:
                continue
            relative = os.path.relpath(helper_path, extraction_dir)
            # الملفات المساعدة الحالية تحفظ باسم module فقط؛ نحفظ ملفات الجذر
            # في DB، بينما تبقى الملفات المتداخلة موجودة فعلياً داخل مجلد البوت.
            if os.path.dirname(relative):
                continue
            module_name = os.path.splitext(os.path.basename(relative))[0]
            helper_code = _read_text_file(helper_path)
            helper_codes.append((module_name, base64.b64encode(helper_code.encode('utf-8')).decode('ascii')))
        if not save_bot_to_db(bot_num, bot_name, encoded_main, [name for name, _ in helper_codes]):
            raise ValueError("فشل حفظ البوت في قاعدة البيانات")
        for module_name, encoded_code in helper_codes:
            save_helper_file_to_db(bot_num, module_name, encoded_code)
        return bot_num, bot_name
    finally:
        shutil.rmtree(extraction_dir, ignore_errors=True)

# ═══════════════════════════════════════════════════════════
# 🔄 استبدال ملف بوت موجود
# ═══════════════════════════════════════════════════════════

def replace_bot_file(bot_num, new_code, bot_name):
    """استبدال ملف البوت الرئيسي"""
    try:
        old_helpers = load_helper_files_from_db(bot_num)

        clean_code = new_code
        old_paths_pattern = r'/storage/emulated/0/[^/]+/[^/]+/bots_data/bot_\d+'
        if re.search(old_paths_pattern, clean_code):
            clean_code = re.sub(old_paths_pattern, '', clean_code)

        encoded_bot = base64.b64encode(clean_code.encode('utf-8')).decode('ascii')
        helper_names = list(old_helpers.keys())

        if save_bot_to_db(bot_num, bot_name, encoded_bot, helper_names):
            log_message(f"تم استبدال ملف البوت {bot_num}", "INFO")
            return True
        return False
    except Exception as e:
        log_message(f"خطأ في استبدال الملف: {e}", "ERROR")
        return False

# ═══════════════════════════════════════════════════════════
# ⏸️ إيقاف وتشغيل البوتات
# ═══════════════════════════════════════════════════════════

def pause_bot(bot_num):
    proc = running_bots.get(bot_num)
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=5)
            paused_bots[bot_num] = True
            running_bots.pop(bot_num, None)
            log_message(f"تم إيقاف البوت {bot_num} مؤقتاً", "INFO")
            return True
        except Exception as e:
            log_message(f"خطأ في إيقاف البوت {bot_num}: {e}", "ERROR")
            return False
    return False

def resume_bot(bot_num):
    if bot_num in paused_bots:
        success = start_bot_process(bot_num)
        if success:
            paused_bots.pop(bot_num, None)
            log_message(f"تم تشغيل البوت {bot_num}", "INFO")
            return True
    return False

def is_bot_paused(bot_num):
    return bot_num in paused_bots

# ═══════════════════════════════════════════════════════════
# 🔍 استخراج معلومات البوت
# ═══════════════════════════════════════════════════════════

def extract_token_from_code(text):
    pattern = r'(?<![A-Za-z0-9_])([0-9]{8,12}:[A-Za-z0-9_-]{30,})'
    match = re.search(pattern, text)
    if match:
        return match.group(1)
    return None

def find_bot_token(bot_num, stored_code=None):
    """البحث عن توكن البوت في الكود المخزن وفي جميع ملفات مجلده."""
    sources = []
    if stored_code:
        sources.append(stored_code)

    bot_dir = get_bot_directory(bot_num)
    for root, _, files in os.walk(bot_dir):
        for file_name in sorted(files, key=str.lower):
            # السجلات قد تحتوي توكنات مطبوعة بالخطأ، ولا تعتبر مصدراً
            # موثوقاً لتحديد توكن البوت.
            if file_name.endswith(('.log', '.out', '.err')):
                continue
            file_path = os.path.join(root, file_name)
            content = _read_text_file(file_path)
            if content:
                sources.append(content)
    for content in sources:
        token = extract_token_from_code(content)
        if token:
            return token
    return None

def build_bot_process_env(bot_num, stored_code=None):
    """إنشاء بيئة مستقلة للبوت ومنع توكن الاستضافة من الوصول إليه."""
    bot_env = os.environ.copy()
    token_keys = {
        'BOT_TOKEN', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_TOKEN',
        'FILE_TOKEN', 'BOT_API_TOKEN', 'TOKEN'
    }
    for key in token_keys:
        bot_env.pop(key, None)

    own_token = find_bot_token(bot_num, stored_code)
    if own_token:
        # بعض المشاريع تستخدم اسماً مختلفاً للتوكن؛ كل القيم هنا هي
        # نفس توكن هذا البوت وليست قيم الاستضافة.
        for key in ('BOT_TOKEN', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_TOKEN',
                    'FILE_TOKEN', 'BOT_API_TOKEN', 'TOKEN'):
            bot_env[key] = own_token
    return bot_env

def get_bot_telegram_info(token):
    try:
        import requests
        resp = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=5)
        data = resp.json()
        if data.get('ok'):
            bot = data['result']
            return bot.get('first_name', ''), bot.get('username', '')
    except:
        pass
    return None, None

def build_bot_list_line(num, file_name, status):
    if is_bot_paused(num):
        status = "موقوف مؤقتاً 🟡"

    tg_info = ""
    try:
        bot_data = load_bot_from_db(num)
        if bot_data:
            code = base64.b64decode(bot_data['code']).decode('utf-8')
            token = find_bot_token(num, code)
            if token:
                tg_name, tg_username = get_bot_telegram_info(token)
                if tg_name or tg_username:
                    tg_info = f" | {tg_name}"
                    if tg_username:
                        tg_info += f" (@{tg_username})"
    except:
        pass
    return f"{num}. {file_name}{tg_info} - {status}\n"

# ═══════════════════════════════════════════════════════════
# 🚀 تشغيل البوتات
# ═══════════════════════════════════════════════════════════

def prepare_bot(bot_num):
    bot_dir = get_bot_directory(bot_num)
    log_message(f"تحضير البوت {bot_num}", "INFO")
    bot_data = load_bot_from_db(bot_num)
    if not bot_data:
        log_message(f"البوت {bot_num} غير موجود", "ERROR")
        return None
    bot_name = bot_data['name']
    try:
        code = base64.b64decode(bot_data['code']).decode("utf-8")
    except Exception as e:
        log_message(f"فشل فك التشفير: {e}", "ERROR")
        return None
    old_paths_pattern = r'/storage/emulated/0/[^/]+/[^/]+/bots_data/bot_\d+'
    if re.search(old_paths_pattern, code):
        code = re.sub(old_paths_pattern, '', code)
    installed, failed = check_and_install_requirements(code)
    if failed:
        log_message(f"فشل تثبيت: {', '.join([p for p, _ in failed])}", "WARNING")
    helper_files = load_helper_files_from_db(bot_num)
    for module_name, encoded_code in helper_files.items():
        try:
            helper_code = base64.b64decode(encoded_code).decode("utf-8")
            helper_file = os.path.join(bot_dir, f"{module_name}.py")
            with open(helper_file, 'w', encoding='utf-8') as f:
                f.write(helper_code)
        except Exception as e:
            log_message(f"فشل حفظ {module_name}: {e}", "WARNING")
    modified_code = fix_bot_code(code, bot_num)
    protected_code = SECURITY_CODE + "\n" + modified_code
    run_file = os.path.join(bot_dir, f"bot_{bot_num}_run.py")
    try:
        with open(run_file, 'w', encoding='utf-8') as f:
            f.write(protected_code)
        log_message(f"تم تحضير {bot_name}", "INFO")
        return run_file
    except Exception as e:
        log_message(f"فشل حفظ ملف التشغيل: {e}", "ERROR")
        return None

def check_and_install_requirements(code):
    imports = []
    imports.extend(re.findall(r'^import\s+([a-zA-Z0-9_]+)', code, re.MULTILINE))
    imports.extend(re.findall(r'^from\s+([a-zA-Z0-9_]+)\s+import', code, re.MULTILINE))
    builtin_modules = {
        'os', 'sys', 'time', 'datetime', 'json', 'math', 'random', 're',
        'threading', 'base64', 'hashlib', 'collections', 'itertools',
        'functools', 'asyncio', 'typing', 'pathlib', 'subprocess', 'io',
        'sqlite3', 'csv', 'xml', 'html', 'urllib', 'http', 'email',
        'logging', 'unittest', 'pickle', 'shelve', 'types', 'copy', 'warnings'
    }
    installed_packages = []
    failed_packages = []
    if 'sqlite' in code.lower() or 'aiosqlite' in code.lower():
        try:
            __import__('aiosqlite')
        except ImportError:
            success, msg = install_package('aiosqlite')
            if success:
                installed_packages.append('aiosqlite')
            else:
                failed_packages.append(('aiosqlite', msg))
    for module in set(imports):
        if module in builtin_modules:
            continue
        try:
            __import__(module)
        except ImportError:
            success, msg = install_package(module)
            if success:
                installed_packages.append(module)
            else:
                failed_packages.append((module, msg))
    return installed_packages, failed_packages

def start_bot_process(bot_num):
    stop_bot_process(bot_num)
    run_file = prepare_bot(bot_num)
    if not run_file:
        return False
    stored_code = None
    bot_data = load_bot_from_db(bot_num)
    if bot_data:
        try:
            stored_code = base64.b64decode(bot_data['code']).decode('utf-8')
        except Exception:
            stored_code = None
    process_env = build_bot_process_env(bot_num, stored_code)
    if not any(process_env.get(key) for key in ('BOT_TOKEN', 'FILE_TOKEN', 'TELEGRAM_BOT_TOKEN')):
        log_message(f"لم يتم العثور على توكن داخل ملفات البوت {bot_num}; تم منع توكنات الاستضافة", "WARNING")
    bot_dir = get_bot_directory(bot_num)
    stdout_file = os.path.join(bot_dir, f"bot_{bot_num}_output.log")
    stderr_file = os.path.join(bot_dir, f"bot_{bot_num}_errors.log")
    try:
        with open(stdout_file, 'w') as out, open(stderr_file, 'w') as err:
            proc = subprocess.Popen(
                [sys.executable, run_file],
                cwd=bot_dir, stdout=out, stderr=err, text=True,
                env=process_env
            )
        running_bots[bot_num] = proc
        log_message(f"تم إطلاق Process للبوت {bot_num} (PID: {proc.pid})", "SUCCESS")
        time.sleep(2)
        if proc.poll() is not None:
            with open(stderr_file, 'r') as f:
                errors = f.read()
            log_message(f"البوت {bot_num} توقف فوراً:\n{errors[:500]}", "ERROR")
            send_admin_notification(f"البوت {bot_num} توقف فوراً!\n{errors[:300]}")
            return False
        return True
    except Exception as e:
        log_message(f"فشل تشغيل البوت {bot_num}: {e}", "ERROR")
        return False

def stop_bot_process(bot_num):
    proc = running_bots.pop(bot_num, None)
    if proc:
        try:
            proc.terminate()
            proc.wait(timeout=5)
            log_message(f"تم إيقاف البوت {bot_num}", "INFO")
        except subprocess.TimeoutExpired:
            proc.kill()
            log_message(f"تم قتل البوت {bot_num} بالقوة", "WARNING")
        except Exception as e:
            log_message(f"خطأ في إيقاف البوت {bot_num}: {e}", "WARNING")

# ═══════════════════════════════════════════════════════════
# 🎛️ وظائف مساعدة للتنقل
# ═══════════════════════════════════════════════════════════

async def go_back_to_main(query, context):
    context.user_data.clear()
    try:
        await query.message.delete()
    except:
        pass
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text="لوحة تحكم البوتات\n\nاختر العملية:",
        reply_markup=get_main_keyboard()
    )

def build_edit_bot_keyboard(bot_num):
    is_paused = is_bot_paused(bot_num)
    proc = running_bots.get(bot_num)
    is_running = proc and proc.poll() is None

    keyboard = [
        [InlineKeyboardButton("🔄 استبدال الملف الرئيسي", callback_data=f"replace_main_{bot_num}")],
        [InlineKeyboardButton("اضافة ملف بيانات", callback_data=f"add_data_{bot_num}")],
        [InlineKeyboardButton("اضافة ملف مساعد (.py)", callback_data=f"add_helper_{bot_num}")],
        [InlineKeyboardButton("📁 استعراض الملفات والمجلدات", callback_data=f"show_files_{bot_num}")],
        [InlineKeyboardButton("💾 نسخة احتياطية لملف", callback_data=f"backup_menu_{bot_num}")],
        [InlineKeyboardButton("حذف ملف", callback_data=f"delete_file_{bot_num}")],
        [InlineKeyboardButton("عرض اخطاء البوت", callback_data=f"show_errors_{bot_num}")],
    ]

    if is_running:
        keyboard.append([InlineKeyboardButton("⏸️ إيقاف البوت", callback_data=f"pause_bot_{bot_num}")])
    elif is_paused:
        keyboard.append([InlineKeyboardButton("▶️ تشغيل البوت", callback_data=f"resume_bot_{bot_num}")])
    else:
        keyboard.append([InlineKeyboardButton("🔄 تشغيل البوت", callback_data=f"start_bot_{bot_num}")])

    keyboard.append([InlineKeyboardButton("اعادة تشغيل البوت", callback_data=f"restart_single_{bot_num}")])
    keyboard.append([InlineKeyboardButton("رجوع", callback_data="edit")])

    if is_running:
        status = "🟢 يعمل"
    elif is_paused:
        status = "🟡 موقوف مؤقتاً"
    else:
        status = "🔴 متوقف"

    return keyboard, status

async def go_back_to_edit_bot(query, context, bot_num):
    all_bots = load_all_bots_from_db()
    bot_name = all_bots.get(bot_num, f"بوت {bot_num}")
    keyboard, status = build_edit_bot_keyboard(bot_num)

    try:
        await query.message.delete()
    except:
        pass
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=f"تعديل: {bot_name}\nالحالة: {status}\n\nاختر العملية:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def show_bot_directory_browser(query, bot_num, relative_path=''):
    """واجهة تصفح مجلد بوت واحد مع الدخول للمجلدات والرجوع منها."""
    try:
        directories, files = bot_directory_entries(bot_num, relative_path)
    except ValueError:
        await query.edit_message_text("المسار غير مسموح")
        return
    shown_path = relative_path or '/'
    text = f"📁 ملفات البوت {bot_num}\n\nالمجلد الحالي: {shown_path}\n"
    keyboard = []
    for directory in directories:
        child = os.path.join(relative_path, directory).replace('\\', '/')
        keyboard.append([InlineKeyboardButton(
            f"📂 {directory}", callback_data=f"browse_dir_{bot_num}_{quote(child, safe='')}"
        )])
    for file_name in files:
        file_path = safe_bot_path(bot_num, os.path.join(relative_path, file_name))
        size = os.path.getsize(file_path) / 1024
        text += f"\n📄 {file_name} ({size:.1f} KB)"
    if not directories and not files:
        text += "\n\nالمجلد فارغ."
    if relative_path:
        parent = os.path.dirname(relative_path).replace('\\', '/')
        keyboard.append([InlineKeyboardButton(
            "⬅️ المجلد السابق", callback_data=f"browse_dir_{bot_num}_{quote(parent, safe='')}"
        )])
    keyboard.append([InlineKeyboardButton("🗑️ حذف ملفات من هذا البوت", callback_data=f"delete_file_{bot_num}")])
    keyboard.append([InlineKeyboardButton("رجوع لتعديل البوت", callback_data=f"edit_bot_{bot_num}")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def show_delete_selection(query, context, bot_num):
    """واجهة تحديد عدة ملفات وحذفها دفعة واحدة."""
    bot_dir = get_bot_directory(bot_num)
    candidates = []
    for root, _, files in os.walk(bot_dir):
        for file_name in files:
            if file_name.endswith('_run.py'):
                continue
            candidates.append(os.path.relpath(os.path.join(root, file_name), bot_dir).replace('\\', '/'))
    candidates.sort(key=str.lower)
    selected = set(context.user_data.get('delete_selected', set()))
    context.user_data['delete_candidates'] = candidates
    context.user_data['delete_bot_num'] = bot_num
    keyboard = []
    for index, file_name in enumerate(candidates):
        mark = '✅' if index in selected else '⬜'
        keyboard.append([InlineKeyboardButton(
            f"{mark} {file_name}", callback_data=f"delete_pick_{bot_num}_{index}"
        )])
    if candidates:
        keyboard.append([InlineKeyboardButton("تحديد/إلغاء تحديد الكل", callback_data=f"delete_all_{bot_num}")])
        keyboard.append([InlineKeyboardButton("🗑️ حذف المحدد", callback_data=f"delete_done_{bot_num}")])
    keyboard.append([InlineKeyboardButton("رجوع", callback_data=f"edit_bot_{bot_num}")])
    selected_count = len(selected.intersection(range(len(candidates))))
    await query.edit_message_text(
        f"حدد الملفات المراد حذفها\n\nالمحدد: {selected_count} من {len(candidates)}",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ═══════════════════════════════════════════════════════════
# 🎛️ لوحة التحكم الرئيسية
# ═══════════════════════════════════════════════════════════

def get_main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("عرض البوتات", callback_data="list")],
        [InlineKeyboardButton("اضافة بوت", callback_data="add")],
        [InlineKeyboardButton("تعديل بوت", callback_data="edit")],
        [InlineKeyboardButton("حذف بوت", callback_data="delete")],
        [InlineKeyboardButton("تثبيت مكتبة", callback_data="install"),
         InlineKeyboardButton("المكتبات المثبتة", callback_data="list_packages")],
        [InlineKeyboardButton("🔍 فحص مكتبة", callback_data="check_package"),
         InlineKeyboardButton("ℹ️ معلومات مكتبة", callback_data="package_info")],
        [InlineKeyboardButton("حذف مكتبة (شامل)", callback_data="uninstall_package")],
        [InlineKeyboardButton("⌨️ تنفيذ أمر pip مخصص", callback_data="pip_shell")],
        [InlineKeyboardButton("اعادة تشغيل", callback_data="restart"),
         InlineKeyboardButton("اعادة تشغيل متقدمة", callback_data="force_restart")],
        [InlineKeyboardButton("الاحصائيات", callback_data="stats")],
        [InlineKeyboardButton("تحميل main.py", callback_data="download")],
        [InlineKeyboardButton("سجل التشغيل", callback_data="logs")],
        [InlineKeyboardButton("اخطاء بوت الادارة", callback_data="main_bot_errors")],
        [InlineKeyboardButton("🗑️ ادارة السجلات", callback_data="manage_logs")],
        [InlineKeyboardButton("👥 ادارة المشرفين", callback_data="manage_admins")],
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if is_admin(user_id):
        await update.message.reply_text(
            "لوحة تحكم البوتات\n\nاختر العملية:",
            reply_markup=get_main_keyboard()
        )
    else:
        keyboard = [[InlineKeyboardButton("طلب استضافة", callback_data="request_hosting")]]
        await update.message.reply_text(
            "خدمة استضافة البوتات\n\nاستضافة مجانية لبوتات Python\n\naضغط للطلب:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id

    # ── طلبات الاستضافة ──
    if query.data == "request_hosting":
        await query.answer()
        keyboard = [[InlineKeyboardButton("رجوع", callback_data="back_user")]]
        await query.edit_message_text(
            "طلب استضافة بوت\n\nارسل ملف البوت (.py)",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        context.user_data['requesting_hosting'] = True
        return

    elif query.data == "back_user":
        await query.answer()
        context.user_data.clear()
        keyboard = [[InlineKeyboardButton("طلب استضافة", callback_data="request_hosting")]]
        await query.edit_message_text(
            "خدمة استضافة البوتات\n\nاختر ما تريد:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    elif query.data == "submit_hosting_request":
        if not context.user_data.get('hosting_files'):
            await query.answer("لم يتم ارسال اي ملفات!", show_alert=True)
            return
        await query.answer()
        req_id = str(int(datetime.now().timestamp()))
        pending_requests[req_id] = {
            'user_id': user_id,
            'name': query.from_user.first_name,
            'username': query.from_user.username,
            'files': context.user_data['hosting_files'],
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        try:
            user_link = f'<a href="tg://user?id={user_id}">{query.from_user.first_name}</a>'
            for admin_id in get_all_admin_ids():
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=(
                        f"طلب استضافة جديد!\n\n"
                        f"المستخدم: {user_link}\n"
                        f"الايدي: {user_id}\n"
                        f"عدد الملفات: {len(context.user_data['hosting_files'])}"
                    ),
                    parse_mode="HTML"
                )
                for file_data in context.user_data['hosting_files']:
                    temp_path = os.path.join(tempfile.gettempdir(), f"temp_{file_data['name']}")
                    if isinstance(file_data['content'], str):
                        with open(temp_path, 'w', encoding='utf-8') as f:
                            f.write(file_data['content'])
                    else:
                        with open(temp_path, 'wb') as f:
                            f.write(file_data['content'])
                    with open(temp_path, 'rb') as f:
                        await context.bot.send_document(
                            chat_id=admin_id, document=f, filename=file_data['name']
                        )
                    os.remove(temp_path)
        except Exception as e:
            log_message(f"خطأ في إرسال للمشرف: {e}", "ERROR")
        await query.edit_message_text("تم ارسال طلبك!\n\nسيتم التواصل معك قريبا")
        context.user_data.clear()
        return

    # ── التحقق من صلاحيات المشرف ──
    if not is_admin(user_id):
        await query.answer("غير مصرح!", show_alert=True)
        return

    await query.answer()

    # ══════════════════════════════════════════════════════
    # 👥 إدارة المشرفين
    # ══════════════════════════════════════════════════════

    if query.data == "manage_admins":
        admins_list = get_extra_admins()
        text = "👥 إدارة المشرفين\n\n"
        text += f"🔑 المشرف الرئيسي: `{SUPER_ADMIN_ID}`\n\n"
        if admins_list:
            text += "المشرفون الإضافيون:\n"
            for uid, added_by, added_at in admins_list:
                text += f"• {uid}\n"
        else:
            text += "لا يوجد مشرفون إضافيون حالياً"

        keyboard = []
        if is_super_admin(user_id):
            keyboard.append([InlineKeyboardButton("➕ اضافة مشرف", callback_data="add_admin_prompt")])
            for uid, _, _ in admins_list:
                keyboard.append([InlineKeyboardButton(f"❌ حذف {uid}", callback_data=f"remove_admin_{uid}")])
        keyboard.append([InlineKeyboardButton("رجوع", callback_data="back")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

    elif query.data == "add_admin_prompt":
        if not is_super_admin(user_id):
            await query.answer("هذه الميزة للمشرف الرئيسي فقط!", show_alert=True)
            return
        keyboard = [[InlineKeyboardButton("الغاء", callback_data="manage_admins")]]
        await query.edit_message_text(
            "➕ اضافة مشرف جديد\n\nارسل ايدي المستخدم (رقم فقط):",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        context.user_data['waiting_for_admin_id'] = True

    elif query.data.startswith("remove_admin_"):
        if not is_super_admin(user_id):
            await query.answer("هذه الميزة للمشرف الرئيسي فقط!", show_alert=True)
            return
        target_id = int(query.data.split("_")[2])
        remove_admin_from_db(target_id)
        log_message(f"تم حذف المشرف {target_id} بواسطة {user_id}", "INFO")

        admins_list = get_extra_admins()
        text = "👥 إدارة المشرفين\n\n"
        text += f"🔑 المشرف الرئيسي: `{SUPER_ADMIN_ID}`\n\n"
        if admins_list:
            text += "المشرفون الإضافيون:\n"
            for uid, added_by, added_at in admins_list:
                text += f"• {uid}\n"
        else:
            text += "لا يوجد مشرفون إضافيون حالياً"
        keyboard = [[InlineKeyboardButton("➕ اضافة مشرف", callback_data="add_admin_prompt")]]
        for uid, _, _ in admins_list:
            keyboard.append([InlineKeyboardButton(f"❌ حذف {uid}", callback_data=f"remove_admin_{uid}")])
        keyboard.append([InlineKeyboardButton("رجوع", callback_data="back")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

    # ══════════════════════════════════════════════════════
    # 🗑️ إدارة السجلات
    # ══════════════════════════════════════════════════════

    elif query.data == "manage_logs":
        info = get_logs_size_info()
        all_bots = load_all_bots_from_db()

        text = "🗑️ ادارة السجلات\n\n"
        text += f"سجل النظام (system.log): {info['system_log']:.2f} KB\n"
        text += f"سجل الاخطاء (main_errors.log): {info['main_errors']:.2f} KB\n"
        text += f"سجلات البوتات: {info['total_bots_logs']:.2f} KB\n"
        text += f"الاجمالي: {info['total']:.2f} KB\n\n"
        text += "اختر ما تريد حذفه:"

        keyboard = [
            [InlineKeyboardButton("حذف سجل النظام (system.log)", callback_data="clear_system_log")],
            [InlineKeyboardButton("حذف سجل الاخطاء (main_errors.log)", callback_data="clear_main_errors")],
            [InlineKeyboardButton("حذف سجلات جميع البوتات", callback_data="clear_all_bots_logs")],
        ]

        for bot_num, bot_name in sorted(all_bots.items()):
            size = info['bots_logs'].get(bot_num, 0)
            keyboard.append([
                InlineKeyboardButton(
                    f"حذف سجلات {bot_name} ({size:.2f} KB)",
                    callback_data=f"clear_bot_log_{bot_num}"
                )
            ])

        keyboard.append([InlineKeyboardButton("حذف جميع السجلات", callback_data="clear_all_logs")])
        keyboard.append([InlineKeyboardButton("رجوع", callback_data="back")])

        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

    elif query.data == "clear_system_log":
        size_before = os.path.getsize(LOG_FILE) / 1024 if os.path.exists(LOG_FILE) else 0
        try:
            open(LOG_FILE, 'w').close()
            log_message("تم تفريغ system.log", "INFO")
            await query.edit_message_text(
                f"تم تفريغ سجل النظام\n\nالحجم المحذوف: {size_before:.2f} KB",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("رجوع لادارة السجلات", callback_data="manage_logs")],
                    [InlineKeyboardButton("رجوع للقائمة", callback_data="back")]
                ])
            )
        except Exception as e:
            await query.edit_message_text(
                f"خطأ في تفريغ السجل: {e}",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("رجوع", callback_data="manage_logs")]])
            )

    elif query.data == "clear_main_errors":
        size_before = os.path.getsize(MAIN_STDERR_FILE) / 1024 if os.path.exists(MAIN_STDERR_FILE) else 0
        try:
            open(MAIN_STDERR_FILE, 'w').close()
            error_logs.clear()
            await query.edit_message_text(
                f"تم تفريغ سجل الاخطاء\n\nالحجم المحذوف: {size_before:.2f} KB",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("رجوع لادارة السجلات", callback_data="manage_logs")],
                    [InlineKeyboardButton("رجوع للقائمة", callback_data="back")]
                ])
            )
        except Exception as e:
            await query.edit_message_text(
                f"خطأ في تفريغ السجل: {e}",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("رجوع", callback_data="manage_logs")]])
            )

    elif query.data.startswith("clear_bot_log_"):
        bot_num = int(query.data.split("_")[3])
        all_bots = load_all_bots_from_db()
        bot_name = all_bots.get(bot_num, f"بوت {bot_num}")
        size_before = get_logs_size_info()['bots_logs'].get(bot_num, 0)
        cleared = clear_bot_logs(bot_num)
        log_message(f"تم تفريغ سجلات البوت {bot_num}", "INFO")
        await query.edit_message_text(
            f"تم تفريغ سجلات البوت: {bot_name}\n\nالحجم المحذوف: {size_before:.2f} KB\nالملفات: {', '.join(cleared) if cleared else 'لا توجد ملفات'}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("رجوع لادارة السجلات", callback_data="manage_logs")],
                [InlineKeyboardButton("رجوع للقائمة", callback_data="back")]
            ])
        )

    elif query.data == "clear_all_bots_logs":
        info = get_logs_size_info()
        size_before = info['total_bots_logs']
        all_bots = load_all_bots_from_db()
        total_cleared = []
        for bot_num in all_bots:
            cleared = clear_bot_logs(bot_num)
            total_cleared.extend(cleared)
        log_message("تم تفريغ سجلات جميع البوتات", "INFO")
        await query.edit_message_text(
            f"تم تفريغ سجلات جميع البوتات\n\nالحجم المحذوف: {size_before:.2f} KB\nعدد الملفات: {len(total_cleared)}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("رجوع لادارة السجلات", callback_data="manage_logs")],
                [InlineKeyboardButton("رجوع للقائمة", callback_data="back")]
            ])
        )

    elif query.data == "clear_all_logs":
        info = get_logs_size_info()
        size_before = info['total']
        cleared = clear_all_logs()
        log_message("تم تفريغ جميع السجلات", "INFO")
        await query.edit_message_text(
            f"تم تفريغ جميع السجلات\n\nالحجم المحذوف: {size_before:.2f} KB\nعدد الملفات: {len(cleared)}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("رجوع لادارة السجلات", callback_data="manage_logs")],
                [InlineKeyboardButton("رجوع للقائمة", callback_data="back")]
            ])
        )

    # ── عرض البوتات ──
    elif query.data == "list":
        all_bots = load_all_bots_from_db()
        if not all_bots:
            keyboard = [[InlineKeyboardButton("رجوع", callback_data="back")]]
            await query.edit_message_text(
                "قائمة البوتات:\n\nلا يوجد بوتات!",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            await query.edit_message_text("قائمة البوتات:\n\nجاري جلب معلومات البوتات...")
            text = "قائمة البوتات:\n\n"
            for num, name in sorted(all_bots.items()):
                proc = running_bots.get(num)
                status = "يعمل" if (proc and proc.poll() is None) else "متوقف"
                text += build_bot_list_line(num, name, status)
            keyboard = [[InlineKeyboardButton("رجوع", callback_data="back")]]
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

    # ── إضافة بوت ──
    elif query.data == "add":
        keyboard = [[InlineKeyboardButton("الغاء", callback_data="back")]]
        await query.edit_message_text(
            "اضافة بوت جديد\n\nارسل ملف البوت الرئيسي (.py) أو نسخة احتياطية كاملة (.zip):\n"
            "يمكنك إرسال ZIP الذي تم تنزيله من النسخ الاحتياطية لإرجاع البوت وملفاته.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        context.user_data['waiting_for_main_bot'] = True
        context.user_data['helper_files_data'] = []

    # ── تعديل بوت ──
    elif query.data == "edit":
        all_bots = load_all_bots_from_db()
        if not all_bots:
            keyboard = [[InlineKeyboardButton("رجوع", callback_data="back")]]
            await query.edit_message_text("لا يوجد بوتات للتعديل!", reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            keyboard = []
            for num, name in sorted(all_bots.items()):
                keyboard.append([InlineKeyboardButton(f"{name}", callback_data=f"edit_bot_{num}")])
            keyboard.append([InlineKeyboardButton("رجوع", callback_data="back")])
            await query.edit_message_text("اختر البوت للتعديل:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif query.data.startswith("edit_bot_"):
        bot_num = int(query.data.split("_")[2])
        all_bots = load_all_bots_from_db()
        bot_name = all_bots.get(bot_num, f"بوت {bot_num}")
        keyboard, status = build_edit_bot_keyboard(bot_num)
        await query.edit_message_text(
            f"تعديل: {bot_name}\nالحالة: {status}\n\nاختر العملية:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    # ══════════════════════════════════════════════════════
    # 💾 نسخة احتياطية لملفات البوت (اي ملف حتى ملفات وقت التشغيل)
    # ══════════════════════════════════════════════════════

    elif query.data.startswith("backup_menu_"):
        bot_num = int(query.data.split("_")[2])
        files = list_bot_files(bot_num)
        keyboard = [
            [InlineKeyboardButton("📦 نسخة كاملة (ZIP) لكل الملفات", callback_data=f"backup_zip_{bot_num}")],
            [InlineKeyboardButton("📄 الملف الرئيسي (النسخة الأصلية)", callback_data=f"backup_main_{bot_num}")],
        ]
        if files:
            for f in files:
                full_path = os.path.join(get_bot_directory(bot_num), f)
                size = os.path.getsize(full_path) / 1024
                keyboard.append([InlineKeyboardButton(f"💾 {f} ({size:.1f} KB)", callback_data=f"backup_file_{bot_num}_{f}")])
        keyboard.append([InlineKeyboardButton("رجوع", callback_data=f"edit_bot_{bot_num}")])
        note = "" if files else "\n\n(لا توجد ملفات إضافية حالياً غير الملف الرئيسي)"
        await query.edit_message_text(
            f"💾 نسخة احتياطية\n\nاختر ملفاً لتحميله مباشرة (يشمل أي ملف أنشأه البوت أثناء التشغيل مثل قواعد البيانات)، أو حمّل نسخة مضغوطة كاملة.{note}",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif query.data.startswith("backup_main_"):
        bot_num = int(query.data.split("_")[2])
        bot_name, clean_code = get_clean_main_file(bot_num)
        keyboard = [[InlineKeyboardButton("رجوع", callback_data=f"backup_menu_{bot_num}")]]
        if clean_code is None:
            await query.message.reply_text("تعذر إيجاد الملف الرئيسي لهذا البوت", reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            filename = bot_name if bot_name else f"bot_{bot_num}_main.py"
            temp_path = os.path.join(tempfile.gettempdir(), f"main_original_{bot_num}_{int(time.time())}.py")
            try:
                with open(temp_path, 'w', encoding='utf-8') as f:
                    f.write(clean_code)
                with open(temp_path, 'rb') as f:
                    await query.message.reply_document(
                        document=f,
                        filename=filename,
                        caption=(
                            f"📄 الملف الرئيسي الأصلي\n\n"
                            f"{filename}\n"
                            f"هذه هي آخر نسخة رفعتها بالضبط (وليست ملف التشغيل bot_{bot_num}_run.py المشفّر بكود الحماية)\n"
                            f"الحجم: {len(clean_code.encode('utf-8')) / 1024:.2f} KB"
                        ),
                        reply_markup=InlineKeyboardMarkup(keyboard)
                    )
                os.remove(temp_path)
            except Exception as e:
                await query.message.reply_text(f"خطأ: {str(e)}", reply_markup=InlineKeyboardMarkup(keyboard))

    elif query.data.startswith("backup_zip_"):
        bot_num = int(query.data.split("_")[2])
        all_bots = load_all_bots_from_db()
        bot_name = all_bots.get(bot_num, f"بوت {bot_num}")
        await query.answer("جاري ضغط الملفات...")
        try:
            zip_path = zip_bot_directory(bot_num)
            keyboard = [[InlineKeyboardButton("رجوع", callback_data=f"backup_menu_{bot_num}")]]
            with open(zip_path, 'rb') as f:
                await query.message.reply_document(
                    document=f,
                    filename=f"bot_{bot_num}_{bot_name}_backup_{time.strftime('%Y%m%d_%H%M%S')}.zip",
                    caption=(
                        f"📦 نسخة احتياطية كاملة\n\n"
                        f"البوت: {bot_name}\n"
                        f"الحجم: {os.path.getsize(zip_path) / 1024:.2f} KB"
                    ),
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            os.remove(zip_path)
        except Exception as e:
            await query.message.reply_text(f"خطأ في إنشاء النسخة: {str(e)}")

    elif query.data.startswith("backup_file_"):
        parts = query.data.split("_", 3)
        bot_num = int(parts[2])
        file_name = parts[3]
        bot_dir = get_bot_directory(bot_num)
        file_path = os.path.join(bot_dir, file_name)
        keyboard = [[InlineKeyboardButton("رجوع", callback_data=f"backup_menu_{bot_num}")]]
        if os.path.exists(file_path):
            try:
                with open(file_path, 'rb') as f:
                    await query.message.reply_document(
                        document=f,
                        filename=file_name,
                        caption=(
                            f"💾 نسخة احتياطية للملف\n\n"
                            f"{file_name}\n"
                            f"الحجم: {os.path.getsize(file_path) / 1024:.2f} KB"
                        ),
                        reply_markup=InlineKeyboardMarkup(keyboard)
                    )
            except Exception as e:
                await query.message.reply_text(f"خطأ: {str(e)}", reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            await query.message.reply_text("الملف غير موجود", reply_markup=InlineKeyboardMarkup(keyboard))

    # ══════════════════════════════════════════════════════
    # 🔄 استبدال الملف الرئيسي
    # ══════════════════════════════════════════════════════

    elif query.data.startswith("replace_main_"):
        bot_num = int(query.data.split("_")[2])
        all_bots = load_all_bots_from_db()
        bot_name = all_bots.get(bot_num, f"بوت {bot_num}")

        keyboard = [[InlineKeyboardButton("الغاء", callback_data=f"edit_bot_{bot_num}")]]
        await query.edit_message_text(
            f"استبدال الملف الرئيسي للبوت: {bot_name}\n\n"
            "⚠️ سيتم الاحتفاظ بجميع الملفات المساعدة والبيانات\n"
            "فقط الملف الرئيسي سيتم استبداله\n\n"
            "ارسل الملف الجديد (.py):",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        context.user_data['replace_main_bot'] = bot_num
        context.user_data['replace_bot_name'] = bot_name

    # ══════════════════════════════════════════════════════
    # ⏸️ إيقاف وتشغيل البوت
    # ══════════════════════════════════════════════════════

    elif query.data.startswith("pause_bot_"):
        bot_num = int(query.data.split("_")[2])
        all_bots = load_all_bots_from_db()
        bot_name = all_bots.get(bot_num, f"بوت {bot_num}")

        success = pause_bot(bot_num)
        status = "تم إيقاف البوت مؤقتاً ✅" if success else "فشل الإيقاف ❌"

        keyboard = [[InlineKeyboardButton("رجوع", callback_data=f"edit_bot_{bot_num}")]]
        await query.edit_message_text(
            f"{status}\n\nالبوت: {bot_name}\n\n"
            "يمكنك تشغيله في أي وقت من قائمة التعديل",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif query.data.startswith("resume_bot_"):
        bot_num = int(query.data.split("_")[2])
        all_bots = load_all_bots_from_db()
        bot_name = all_bots.get(bot_num, f"بوت {bot_num}")

        success = resume_bot(bot_num)
        status = "تم تشغيل البوت ✅" if success else "فشل التشغيل ❌"

        keyboard = [[InlineKeyboardButton("رجوع", callback_data=f"edit_bot_{bot_num}")]]
        await query.edit_message_text(
            f"{status}\n\nالبوت: {bot_name}",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif query.data.startswith("start_bot_"):
        bot_num = int(query.data.split("_")[2])
        all_bots = load_all_bots_from_db()
        bot_name = all_bots.get(bot_num, f"بوت {bot_num}")

        success = start_bot_process(bot_num)
        status = "تم تشغيل البوت ✅" if success else "فشل التشغيل ❌"

        keyboard = [[InlineKeyboardButton("رجوع", callback_data=f"edit_bot_{bot_num}")]]
        await query.edit_message_text(
            f"{status}\n\nالبوت: {bot_name}",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif query.data.startswith("add_data_"):
        bot_num = int(query.data.split("_")[2])
        keyboard = [[InlineKeyboardButton("الغاء", callback_data=f"edit_bot_{bot_num}")]]
        await query.edit_message_text(
            f"اضافة ملف بيانات للبوت {bot_num}\n\nارسل الملف (db, txt, json, csv, log...):",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        context.user_data['add_data_to_bot'] = bot_num

    elif query.data.startswith("add_helper_"):
        bot_num = int(query.data.split("_")[2])
        keyboard = [[InlineKeyboardButton("الغاء", callback_data=f"edit_bot_{bot_num}")]]
        await query.edit_message_text(
            f"اضافة ملف مساعد للبوت {bot_num}\n\nارسل الملف (.py):",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        context.user_data['add_helper_to_bot'] = bot_num

    elif query.data.startswith("show_files_"):
        bot_num = int(query.data.split("_")[2])
        await show_bot_directory_browser(query, bot_num)

    elif query.data.startswith("browse_dir_"):
        parts = query.data.split("_", 3)
        bot_num = int(parts[2])
        relative_path = unquote(parts[3]) if len(parts) > 3 else ''
        await show_bot_directory_browser(query, bot_num, relative_path)

    elif query.data.startswith("delete_file_"):
        bot_num = int(query.data.split("_")[2])
        context.user_data['delete_selected'] = set()
        await show_delete_selection(query, context, bot_num)

    elif query.data.startswith("delete_pick_"):
        parts = query.data.split("_")
        bot_num, index = int(parts[2]), int(parts[3])
        if context.user_data.get('delete_bot_num') != bot_num:
            await query.answer("انتهت جلسة التحديد، افتحها من جديد", show_alert=True)
            return
        selected = context.user_data.setdefault('delete_selected', set())
        if index in selected:
            selected.remove(index)
        else:
            selected.add(index)
        await show_delete_selection(query, context, bot_num)

    elif query.data.startswith("delete_all_"):
        bot_num = int(query.data.split("_")[2])
        candidates = context.user_data.get('delete_candidates', [])
        selected = context.user_data.setdefault('delete_selected', set())
        selected.clear() if len(selected) == len(candidates) else selected.update(range(len(candidates)))
        await show_delete_selection(query, context, bot_num)

    elif query.data.startswith("delete_done_"):
        bot_num = int(query.data.split("_")[2])
        candidates = context.user_data.get('delete_candidates', [])
        selected = context.user_data.get('delete_selected', set())
        deleted, failed = [], []
        for index in sorted(selected):
            if index >= len(candidates):
                continue
            relative_path = candidates[index]
            try:
                file_path = safe_bot_path(bot_num, relative_path)
                if os.path.isfile(file_path):
                    os.remove(file_path)
                    deleted.append(relative_path)
                    if relative_path.endswith('.py') and '/' not in relative_path:
                        module_name = os.path.splitext(os.path.basename(relative_path))[0]
                        conn = sqlite3.connect(DB_FILE)
                        c = conn.cursor()
                        c.execute("DELETE FROM helper_files WHERE bot_num = ? AND module_name = ?", (bot_num, module_name))
                        c.execute("DELETE FROM bot_imports WHERE bot_num = ? AND module_name = ?", (bot_num, module_name))
                        conn.commit()
                        conn.close()
            except Exception:
                failed.append(relative_path)
        context.user_data.pop('delete_selected', None)
        context.user_data.pop('delete_candidates', None)
        context.user_data.pop('delete_bot_num', None)
        await query.edit_message_text(
            f"تم حذف {len(deleted)} ملف\n"
            f"تعذر حذف: {len(failed)} ملف",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("العودة لاستعراض الملفات", callback_data=f"show_files_{bot_num}")],
                [InlineKeyboardButton("رجوع", callback_data=f"edit_bot_{bot_num}")]
            ])
        )

    elif query.data.startswith("confirm_delete_file_"):
        parts = query.data.split("_")
        bot_num = int(parts[3])
        file_name = "_".join(parts[4:])
        try:
            file_path = safe_bot_path(bot_num, file_name)
            if os.path.exists(file_path):
                os.remove(file_path)
                status = "تم حذف الملف"
                log_message(f"تم حذف الملف {file_name} من البوت {bot_num}", "INFO")
                # ✅ إن كان هذا الملف المحذوف ملفاً مساعداً مسجلاً في القاعدة، ننظفه أيضاً
                # حتى لا يعود بالظهور تلقائياً في تحضير التشغيل القادم (prepare_bot)
                module_name = file_name[:-3] if file_name.endswith('.py') else None
                if module_name:
                    conn = sqlite3.connect(DB_FILE)
                    c = conn.cursor()
                    c.execute("DELETE FROM helper_files WHERE bot_num = ? AND module_name = ?", (bot_num, module_name))
                    c.execute("DELETE FROM bot_imports WHERE bot_num = ? AND module_name = ?", (bot_num, module_name))
                    conn.commit()
                    conn.close()
            else:
                status = "الملف غير موجود"
        except Exception as e:
            status = f"خطأ: {str(e)}"
        keyboard = [[InlineKeyboardButton("رجوع", callback_data=f"edit_bot_{bot_num}")]]
        await query.edit_message_text(
            f"{status}\n\nالملف: {file_name}",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    # ── عرض أخطاء البوت المستضاف ──
    elif query.data.startswith("show_errors_"):
        bot_num = int(query.data.split("_")[2])
        all_bots = load_all_bots_from_db()
        bot_name = all_bots.get(bot_num, f"بوت {bot_num}")
        bot_dir = get_bot_directory(bot_num)
        stdout_file = os.path.join(bot_dir, f"bot_{bot_num}_output.log")
        stderr_file = os.path.join(bot_dir, f"bot_{bot_num}_errors.log")

        log_content = "=" * 60 + "\n"
        log_content += f"سجل البوت {bot_num}: {bot_name}\n"
        log_content += f"التاريخ: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        log_content += "=" * 60 + "\n\n"
        log_content += "سجل الاخطاء (errors.log):\n" + "-" * 40 + "\n"
        if os.path.exists(stderr_file):
            with open(stderr_file, 'r', encoding='utf-8', errors='replace') as f:
                errors = f.read()
            log_content += errors if errors.strip() else "لا توجد اخطاء\n"
        else:
            log_content += "ملف الاخطاء غير موجود\n"
        log_content += "\n\nسجل المخرجات (output.log):\n" + "-" * 40 + "\n"
        if os.path.exists(stdout_file):
            with open(stdout_file, 'r', encoding='utf-8', errors='replace') as f:
                output = f.read()
            log_content += output if output.strip() else "لا توجد مخرجات\n"
        else:
            log_content += "ملف المخرجات غير موجود\n"

        temp_log = os.path.join(tempfile.gettempdir(), f"bot_{bot_num}_log_{int(time.time())}.txt")
        try:
            with open(temp_log, 'w', encoding='utf-8') as f:
                f.write(log_content)
            stderr_size = os.path.getsize(stderr_file) / 1024 if os.path.exists(stderr_file) else 0
            stdout_size = os.path.getsize(stdout_file) / 1024 if os.path.exists(stdout_file) else 0
            keyboard = [[InlineKeyboardButton("رجوع", callback_data=f"back_from_edit_{bot_num}")]]
            with open(temp_log, 'rb') as f:
                await query.message.reply_document(
                    document=f,
                    filename=f"bot_{bot_num}_{bot_name}_log_{time.strftime('%Y%m%d_%H%M%S')}.txt",
                    caption=(
                        f"سجل البوت {bot_num}: {bot_name}\n\n"
                        f"errors.log: {stderr_size:.2f} KB\n"
                        f"output.log: {stdout_size:.2f} KB\n"
                        f"الاجمالي: {(stderr_size + stdout_size):.2f} KB"
                    ),
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            os.remove(temp_log)
        except Exception as e:
            keyboard = [[InlineKeyboardButton("رجوع", callback_data=f"back_from_edit_{bot_num}")]]
            await query.message.reply_text(f"خطأ: {str(e)}", reply_markup=InlineKeyboardMarkup(keyboard))

    elif query.data.startswith("back_from_edit_"):
        bot_num = int(query.data.split("_")[3])
        await go_back_to_edit_bot(query, context, bot_num)

    elif query.data.startswith("restart_single_"):
        bot_num = int(query.data.split("_")[2])
        all_bots = load_all_bots_from_db()
        bot_name = all_bots.get(bot_num, f"بوت {bot_num}")
        stop_bot_process(bot_num)
        time.sleep(1)
        success = start_bot_process(bot_num)
        status = "تم اعادة التشغيل" if success else "فشل التشغيل"
        keyboard = [[InlineKeyboardButton("رجوع", callback_data=f"edit_bot_{bot_num}")]]
        await query.edit_message_text(
            f"{status}\n\nالبوت: {bot_name}",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    # ── حذف بوت ──
    elif query.data == "delete":
        all_bots = load_all_bots_from_db()
        if not all_bots:
            keyboard = [[InlineKeyboardButton("رجوع", callback_data="back")]]
            await query.edit_message_text("لا يوجد بوتات للحذف!", reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            keyboard = []
            for num, name in sorted(all_bots.items()):
                keyboard.append([InlineKeyboardButton(f"حذف {name}", callback_data=f"confirm_del_{num}")])
            keyboard.append([InlineKeyboardButton("رجوع", callback_data="back")])
            await query.edit_message_text("اختر البوت للحذف:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif query.data.startswith("confirm_del_"):
        bot_num = int(query.data.split("_")[2])
        all_bots = load_all_bots_from_db()
        bot_name = all_bots.get(bot_num, f"بوت {bot_num}")
        stop_bot_process(bot_num)
        delete_bot_from_db(bot_num)
        bot_dir = get_bot_directory(bot_num)
        if os.path.exists(bot_dir):
            shutil.rmtree(bot_dir)
        paused_bots.pop(bot_num, None)
        keyboard = [[InlineKeyboardButton("رجوع", callback_data="back")]]
        await query.edit_message_text(
            f"تم حذف البوت!\n\nالبوت: {bot_name}\nالرقم: {bot_num}",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        log_message(f"تم حذف البوت {bot_name} (رقم {bot_num})", "INFO")

    # ── إعادة تشغيل ──
    elif query.data == "restart":
        await query.edit_message_text("جاري اعادة تشغيل...")
        for bot_num in list(running_bots.keys()):
            stop_bot_process(bot_num)
        time.sleep(2)
        all_bots = load_all_bots_from_db()
        restarted = 0
        for bot_num in sorted(all_bots.keys()):
            if not is_bot_paused(bot_num):
                if start_bot_process(bot_num):
                    restarted += 1
                    time.sleep(1)
        keyboard = [[InlineKeyboardButton("رجوع", callback_data="back")]]
        await query.message.reply_text(
            f"تمت اعادة التشغيل!\n\nتم اعادة تشغيل {restarted} بوت",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif query.data == "force_restart":
        await query.edit_message_text("اعادة تشغيل متقدمة\n\nجاري قتل البوتات المعلقة...")
        killed = 0
        restarted = 0
        for bot_num, proc in list(running_bots.items()):
            try:
                proc.kill()
                proc.wait(timeout=2)
                killed += 1
                log_message(f"تم قتل البوت {bot_num} بالقوة", "WARNING")
            except:
                pass
            running_bots.pop(bot_num, None)
        time.sleep(3)
        all_bots = load_all_bots_from_db()
        for bot_num in sorted(all_bots.keys()):
            if not is_bot_paused(bot_num):
                if start_bot_process(bot_num):
                    restarted += 1
                    time.sleep(1.5)
        keyboard = [[InlineKeyboardButton("رجوع", callback_data="back")]]
        await query.message.reply_text(
            f"تمت اعادة التشغيل المتقدمة!\n\nتم قتل: {killed} بوت\nيعمل الان: {restarted} بوت",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    # ── تنفيذ أمر pip مخصص ──
    elif query.data == "pip_shell":
        keyboard = [[InlineKeyboardButton("رجوع", callback_data="back")]]
        await query.edit_message_text(
            "⌨️ تنفيذ أمر pip مخصص\n\n"
            "أرسل الأمر كما تكتبه بالضبط، أمثلة:\n\n"
            "`pip list`\n"
            "`pip list | grep pyrogram`\n"
            "`pip show pyrogram`\n"
            "`pip install -U pyrogram`\n"
            "`pip install --upgrade pyrogram`\n"
            "`pip uninstall pyrogram`\n"
            "`pip list --outdated`\n"
            "`pip freeze > requirements.txt`\n"
            "`pip --version`\n"
            "`python -m pip install --upgrade pip`\n\n"
            "⚠️ مسموح فقط بأوامر pip (يمكن دمجها مع grep للتصفية، أو > للحفظ "
            "بملف). لا يمكن دمج عدة أوامر ببعض بـ ; أو &&.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        context.user_data['waiting_for_pip_command'] = True

    # ── تثبيت مكتبة ──
    elif query.data == "install":
        keyboard = [[InlineKeyboardButton("رجوع", callback_data="back")]]
        await query.edit_message_text(
            "تثبيت مكتبة\n\nارسل اسم المكتبة (او عدة مكتبات مفصولة بمسافة):",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        context.user_data['waiting_for_package'] = True

    # ── فحص مكتبة (هل هي مثبتة؟) ──
    elif query.data == "check_package":
        keyboard = [[InlineKeyboardButton("رجوع", callback_data="back")]]
        await query.edit_message_text(
            "🔍 فحص مكتبة\n\nارسل اسم المكتبة للتحقق من كونها مثبتة أم لا:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        context.user_data['waiting_for_check_package'] = True

    # ── معلومات مكتبة (pip show) ──
    elif query.data == "package_info":
        keyboard = [[InlineKeyboardButton("رجوع", callback_data="back")]]
        await query.edit_message_text(
            "ℹ️ معلومات مكتبة\n\nارسل اسم المكتبة لعرض تفاصيلها (الاصدار، المسار، الاعتماديات):",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        context.user_data['waiting_for_package_info'] = True

    # ── عرض المكتبات المثبتة ──
    elif query.data == "list_packages":
        await query.edit_message_text("جاري جلب قائمة المكتبات...")
        packages = get_installed_packages()
        if not packages:
            keyboard = [[InlineKeyboardButton("رجوع", callback_data="back")]]
            await query.edit_message_text("لا توجد مكتبات مثبتة!", reply_markup=InlineKeyboardMarkup(keyboard))
            return

        content = "=" * 60 + "\n"
        content += "المكتبات المثبتة\n"
        content += f"التاريخ: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        content += f"العدد الكلي: {len(packages)} مكتبة\n"
        content += "=" * 60 + "\n\n"
        content += f"{'الاسم':<35} {'الاصدار'}\n"
        content += "-" * 55 + "\n"
        for pkg in packages:
            content += f"{pkg['name']:<35} {pkg['version']}\n"

        temp_file = os.path.join(tempfile.gettempdir(), f"packages_{int(time.time())}.txt")
        with open(temp_file, 'w', encoding='utf-8') as f:
            f.write(content)

        keyboard = [[InlineKeyboardButton("رجوع", callback_data="back_from_file")]]
        with open(temp_file, 'rb') as f:
            await query.message.reply_document(
                document=f,
                filename=f"installed_packages_{time.strftime('%Y%m%d_%H%M%S')}.txt",
                caption=(
                    f"المكتبات المثبتة\n\n"
                    f"العدد: {len(packages)} مكتبة\n"
                    f"التاريخ: {time.strftime('%Y-%m-%d %H:%M:%S')}"
                ),
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        os.remove(temp_file)

    # ── حذف مكتبة (حذف شامل) ──
    elif query.data == "uninstall_package":
        keyboard = [[InlineKeyboardButton("رجوع", callback_data="back")]]
        await query.edit_message_text(
            "🗑️ حذف مكتبة (شامل)\n\n"
            "ارسل اسم المكتبة التي تريد حذفها\n(يمكنك ارسال عدة مكتبات مفصولة بمسافة)\n\n"
            "سيتم حذفها عبر pip، ثم تنظيف أي ملفات أو مجلدات متبقية "
            "من كل مسارات المكتبات (site-packages) وليس من مكان واحد فقط.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        context.user_data['waiting_for_uninstall'] = True

    # ── تحميل main.py ──
    elif query.data == "download":
        await query.answer("جاري تحضير الملف...")
        try:
            with open(__file__, 'rb') as f:
                await query.message.reply_document(
                    document=f,
                    filename='main.py',
                    caption=(
                        f"ملف main.py\n\n"
                        f"الحجم: {os.path.getsize(__file__) / 1024:.2f} KB\n"
                        f"عدد البوتات: {len(load_all_bots_from_db())}\n"
                        f"التاريخ: {time.strftime('%Y-%m-%d %H:%M:%S')}"
                    )
                )
        except Exception as e:
            await query.message.reply_text(f"خطأ: {str(e)}")

    # ── سجل التشغيل ──
    elif query.data == "logs":
        await query.answer("جاري تحضير السجل...")
        try:
            log_content = "=" * 70 + "\n"
            log_content += "سجل نظام ادارة البوتات\n"
            log_content += f"التاريخ: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            log_content += "=" * 70 + "\n\n"
            all_bots = load_all_bots_from_db()
            log_content += "البوتات المسجلة:\n" + "-" * 70 + "\n"
            for num, name in sorted(all_bots.items()):
                proc = running_bots.get(num)
                is_paused = is_bot_paused(num)
                if is_paused:
                    status = "موقوف مؤقتاً"
                elif proc and proc.poll() is None:
                    status = f"يعمل (PID: {proc.pid})"
                else:
                    status = "متوقف"
                bot_dir = get_bot_directory(num)
                log_content += f"البوت {num}: {name} - {status}\n"
                log_content += f"  المجلد: {bot_dir}\n"
                if os.path.exists(bot_dir):
                    files = os.listdir(bot_dir)
                    bot_size = sum(os.path.getsize(os.path.join(bot_dir, f)) for f in files)
                    log_content += f"  الملفات: {len(files)} ملف ({bot_size / 1024:.2f} KB)\n"
                log_content += "\n"
            log_content += "\n" + "=" * 70 + "\nسجل الاحداث:\n" + "=" * 70 + "\n\n"
            if os.path.exists(LOG_FILE):
                with open(LOG_FILE, 'r', encoding='utf-8') as f:
                    log_content += "".join(f.readlines()[-500:])
            else:
                log_content += "لا توجد سجلات\n"
            temp_log = os.path.join(tempfile.gettempdir(), f"system_log_{int(time.time())}.txt")
            with open(temp_log, 'w', encoding='utf-8') as f:
                f.write(log_content)
            keyboard = [[InlineKeyboardButton("رجوع", callback_data="back_from_file")]]
            with open(temp_log, 'rb') as f:
                await query.message.reply_document(
                    document=f,
                    filename=f"system_log_{time.strftime('%Y%m%d_%H%M%S')}.txt",
                    caption=(
                        f"سجل النظام\n\n"
                        f"عدد البوتات: {len(all_bots)}\n"
                        f"يعمل: {sum(1 for n, p in running_bots.items() if p.poll() is None)}\n"
                        f"الحجم: {len(log_content) / 1024:.2f} KB"
                    ),
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            os.remove(temp_log)
        except Exception as e:
            await query.message.reply_text(f"خطأ: {str(e)}")

    # ── الإحصائيات ──
    elif query.data == "stats":
        all_bots = load_all_bots_from_db()
        running_count = sum(1 for p in running_bots.values() if p.poll() is None)
        paused_count = len(paused_bots)
        total_size = 0
        for num in all_bots.keys():
            bot_dir = get_bot_directory(num)
            if os.path.exists(bot_dir):
                for root, dirs, files in os.walk(bot_dir):
                    total_size += sum(os.path.getsize(os.path.join(root, f)) for f in files)
        packages = get_installed_packages()
        logs_info = get_logs_size_info()
        keyboard = [[InlineKeyboardButton("رجوع", callback_data="back")]]
        await query.edit_message_text(
            f"احصائيات النظام\n\n"
            f"عدد البوتات: {len(all_bots)}\n"
            f"🟢 يعمل: {running_count}\n"
            f"🟡 موقوف مؤقتاً: {paused_count}\n"
            f"🔴 متوقف: {len(all_bots) - running_count - paused_count}\n"
            f"مساحة البيانات: {total_size / 1024:.2f} KB\n"
            f"المكتبات المثبتة: {len(packages)}\n"
            f"عدد المشرفين: {len(get_all_admin_ids())}\n"
            f"حجم السجلات الكلي: {logs_info['total']:.2f} KB",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    # ── أخطاء بوت الإدارة الرئيسي ──
    elif query.data == "main_bot_errors":
        await query.answer("جاري تحضير السجل...")

        content = "=" * 70 + "\n"
        content += "سجل اخطاء بوت الادارة الرئيسي\n"
        content += f"التاريخ: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        content += "=" * 70 + "\n\n"

        content += "[ SYSTEM LOG ]\n" + "-" * 70 + "\n"
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, 'r', encoding='utf-8', errors='replace') as f:
                log_data = f.read().strip()
            content += log_data if log_data else "لا توجد سجلات\n"
            log_size = os.path.getsize(LOG_FILE) / 1024
        else:
            content += "ملف system.log غير موجود\n"
            log_size = 0

        content += "\n\n[ MAIN STDERR ]\n" + "-" * 70 + "\n"
        if os.path.exists(MAIN_STDERR_FILE):
            with open(MAIN_STDERR_FILE, 'r', encoding='utf-8', errors='replace') as f:
                main_err = f.read().strip()
            content += main_err if main_err else "لا توجد اخطاء\n"
            main_err_size = os.path.getsize(MAIN_STDERR_FILE) / 1024
        else:
            content += "ملف main_errors.log غير موجود\n"
            main_err_size = 0

        content += "\n\n[ ERRORS IN MEMORY ]\n" + "-" * 70 + "\n"
        if error_logs:
            content += "\n".join(error_logs[-200:])
        else:
            content += "لا توجد اخطاء في الذاكرة\n"

        temp_log = os.path.join(tempfile.gettempdir(), f"main_errors_{int(time.time())}.txt")
        try:
            with open(temp_log, 'w', encoding='utf-8') as f:
                f.write(content)
            keyboard = [
                [InlineKeyboardButton("تحديث", callback_data="main_bot_errors_refresh")],
                [InlineKeyboardButton("رجوع", callback_data="back_from_file")]
            ]
            with open(temp_log, 'rb') as f:
                await query.message.reply_document(
                    document=f,
                    filename=f"main_bot_log_{time.strftime('%Y%m%d_%H%M%S')}.txt",
                    caption=(
                        f"سجل بوت الادارة الرئيسي\n\n"
                        f"system.log: {log_size:.2f} KB\n"
                        f"main_errors.log: {main_err_size:.2f} KB\n"
                        f"احداث في الذاكرة: {len(error_logs)}\n"
                        f"الوقت: {time.strftime('%H:%M:%S')}"
                    ),
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            os.remove(temp_log)
        except Exception as e:
            await query.message.reply_text(f"خطأ: {str(e)}")

    elif query.data == "main_bot_errors_refresh":
        try:
            await query.message.delete()
        except:
            pass

        content = "=" * 70 + "\n"
        content += "سجل اخطاء بوت الادارة الرئيسي\n"
        content += f"التاريخ: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        content += "=" * 70 + "\n\n"

        content += "[ SYSTEM LOG ]\n" + "-" * 70 + "\n"
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, 'r', encoding='utf-8', errors='replace') as f:
                log_data = f.read().strip()
            content += log_data if log_data else "لا توجد سجلات\n"
            log_size = os.path.getsize(LOG_FILE) / 1024
        else:
            content += "ملف system.log غير موجود\n"
            log_size = 0

        content += "\n\n[ MAIN STDERR ]\n" + "-" * 70 + "\n"
        if os.path.exists(MAIN_STDERR_FILE):
            with open(MAIN_STDERR_FILE, 'r', encoding='utf-8', errors='replace') as f:
                main_err = f.read().strip()
            content += main_err if main_err else "لا توجد اخطاء\n"
            main_err_size = os.path.getsize(MAIN_STDERR_FILE) / 1024
        else:
            content += "ملف main_errors.log غير موجود\n"
            main_err_size = 0

        content += "\n\n[ ERRORS IN MEMORY ]\n" + "-" * 70 + "\n"
        if error_logs:
            content += "\n".join(error_logs[-200:])
        else:
            content += "لا توجد اخطاء في الذاكرة\n"

        temp_log = os.path.join(tempfile.gettempdir(), f"main_errors_{int(time.time())}.txt")
        try:
            with open(temp_log, 'w', encoding='utf-8') as f:
                f.write(content)
            keyboard = [
                [InlineKeyboardButton("تحديث", callback_data="main_bot_errors_refresh")],
                [InlineKeyboardButton("رجوع", callback_data="back_from_file")]
            ]
            with open(temp_log, 'rb') as f:
                await context.bot.send_document(
                    chat_id=query.message.chat_id,
                    document=f,
                    filename=f"main_bot_log_{time.strftime('%Y%m%d_%H%M%S')}.txt",
                    caption=(
                        f"سجل بوت الادارة الرئيسي\n\n"
                        f"system.log: {log_size:.2f} KB\n"
                        f"main_errors.log: {main_err_size:.2f} KB\n"
                        f"احداث في الذاكرة: {len(error_logs)}\n"
                        f"الوقت: {time.strftime('%H:%M:%S')}"
                    ),
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            os.remove(temp_log)
        except Exception as e:
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=f"خطأ: {str(e)}"
            )

    # ── الرجوع من الملفات ──
    elif query.data == "back_from_file":
        await go_back_to_main(query, context)

    elif query.data == "confirm_no_helpers":
        await finalize_bot_addition(query, context)

    elif query.data == "send_helpers":
        keyboard = [[InlineKeyboardButton("الغاء", callback_data="back")]]
        await query.edit_message_text(
            "ارسل الملفات المساعدة\n\nارسل ملفات .py واحدا تلو الاخر\nعند الانتهاء: /done",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        context.user_data['waiting_for_helpers'] = True

    elif query.data == "back":
        context.user_data.clear()
        await query.edit_message_text(
            "لوحة تحكم البوتات\n\nاختر العملية:",
            reply_markup=get_main_keyboard()
        )

async def finalize_bot_addition(query, context):
    try:
        bot_name = context.user_data.get('main_bot_name')
        bot_code = context.user_data.get('main_bot_code')
        helpers = context.user_data.get('helper_files_data', [])
        if not bot_name or not bot_code:
            await query.message.reply_text("خطأ: بيانات غير مكتملة!")
            return
        all_bots = load_all_bots_from_db()
        new_num = max(all_bots.keys()) + 1 if all_bots else 1
        bot_dir = get_bot_directory(new_num)
        if os.path.exists(bot_dir):
            shutil.rmtree(bot_dir)
        os.makedirs(bot_dir, exist_ok=True)
        clean_code = bot_code
        old_paths_pattern = r'/storage/emulated/0/[^/]+/[^/]+/bots_data/bot_\d+'
        if re.search(old_paths_pattern, clean_code):
            clean_code = re.sub(old_paths_pattern, '', clean_code)
        encoded_bot = base64.b64encode(clean_code.encode('utf-8')).decode('ascii')
        helper_names = []
        for helper in helpers:
            helper_code = helper['code']
            if re.search(old_paths_pattern, helper_code):
                helper_code = re.sub(old_paths_pattern, '', helper_code)
            encoded_helper = base64.b64encode(helper_code.encode('utf-8')).decode('ascii')
            # ✅ الإصلاح: نمرر رقم البوت الجديد مع اسم الملف المساعد
            # حتى لا يتصادم مع ملف بنفس الاسم تابع لبوت آخر
            save_helper_file_to_db(new_num, helper['module'], encoded_helper)
            helper_names.append(helper['module'])
        if not save_bot_to_db(new_num, bot_name, encoded_bot, helper_names):
            await query.message.reply_text("فشل حفظ البوت!")
            return
        log_message(f"تم حفظ البوت {bot_name} في DB", "INFO")
        success = start_bot_process(new_num)
        status = "يعمل" if success else "مضاف"
        helper_text = f"\nملفات مساعدة: {', '.join(helper_names)}" if helper_names else ""
        await query.message.reply_text(
            f"تمت اضافة البوت!\n\n"
            f"الاسم: {bot_name}\n"
            f"الرقم: {new_num}{helper_text}\n\n{status}"
        )
        context.user_data.clear()
    except Exception as e:
        await query.message.reply_text(f"خطأ: {str(e)}")
        log_message(f"خطأ في إضافة البوت: {e}", "ERROR")

async def file_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if context.user_data.get('requesting_hosting'):
        try:
            file = await update.message.document.get_file()
            file_name = update.message.document.file_name
            file_path = make_upload_temp_path(file_name)
            await file.download_to_drive(file_path)
            if file_name.endswith(('.txt', '.json', '.csv', '.py')):
                with open(file_path, 'r', encoding='utf-8') as f:
                    content = f.read()
            else:
                with open(file_path, 'rb') as f:
                    content = f.read()
            os.remove(file_path)
            if 'hosting_files' not in context.user_data:
                context.user_data['hosting_files'] = []
            context.user_data['hosting_files'].append({'name': file_name, 'content': content})
            keyboard = [
                [InlineKeyboardButton("ارسال الطلب", callback_data="submit_hosting_request")],
                [InlineKeyboardButton("الغاء", callback_data="back_user")]
            ]
            await update.message.reply_text(
                f"تم استلام: {file_name}\n"
                f"عدد الملفات: {len(context.user_data['hosting_files'])}\n\n"
                "ارسل المزيد او اضغط ارسال",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return
        except Exception as e:
            await update.message.reply_text(f"خطأ: {str(e)}")
            return

    if not is_admin(user_id):
        return

    try:
        file = await update.message.document.get_file()
        file_name = os.path.basename(update.message.document.file_name.replace('\\', '/'))
        if not file_name or file_name in ('.', '..'):
            await update.message.reply_text("اسم الملف غير صالح")
            return

        # استيراد نسخة بوت كاملة من ZIP
        if context.user_data.get('waiting_for_main_bot') and file_name.lower().endswith('.zip'):
            zip_path = make_upload_temp_path(file_name)
            try:
                await file.download_to_drive(zip_path)
                bot_num, bot_name = import_bot_from_zip(zip_path)
                success = start_bot_process(bot_num)
                await update.message.reply_text(
                    f"تم استيراد النسخة الاحتياطية بنجاح ✅\n\n"
                    f"البوت: {bot_name}\nالرقم: {bot_num}\n"
                    f"الحالة: {'يعمل' if success else 'تمت الإضافة دون تشغيل'}",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("رجوع", callback_data="back")]
                    ])
                )
            except Exception as e:
                await update.message.reply_text(f"فشل استيراد ملف ZIP: {e}")
            finally:
                if os.path.exists(zip_path):
                    os.remove(zip_path)
                context.user_data.clear()
            return

        # استبدال الملف الرئيسي
        if context.user_data.get('replace_main_bot'):
            bot_num = context.user_data['replace_main_bot']
            bot_name = context.user_data.get('replace_bot_name', f"بوت {bot_num}")

            if not file_name.endswith('.py'):
                await update.message.reply_text("يرجى ارسال ملف .py فقط!")
                return

            file_path = make_upload_temp_path(file_name)
            await file.download_to_drive(file_path)

            with open(file_path, 'r', encoding='utf-8') as f:
                code = f.read()
            os.remove(file_path)

            was_running = False
            proc = running_bots.get(bot_num)
            if proc and proc.poll() is None:
                stop_bot_process(bot_num)
                was_running = True
                await update.message.reply_text("جاري إيقاف البوت...")
                time.sleep(2)

            success = replace_bot_file(bot_num, code, bot_name)

            if success:
                if was_running:
                    await update.message.reply_text("جاري إعادة التشغيل...")
                    time.sleep(1)
                    start_success = start_bot_process(bot_num)
                    final_status = "تم الاستبدال وإعادة التشغيل ✅" if start_success else "تم الاستبدال لكن فشل التشغيل ⚠️"
                else:
                    final_status = "تم الاستبدال بنجاح ✅"

                keyboard = [[InlineKeyboardButton("رجوع", callback_data=f"edit_bot_{bot_num}")]]
                await update.message.reply_text(
                    f"{final_status}\n\n"
                    f"البوت: {bot_name}\n"
                    f"الملف الجديد: {file_name}\n"
                    f"الحجم: {len(code) / 1024:.2f} KB",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            else:
                keyboard = [[InlineKeyboardButton("رجوع", callback_data=f"edit_bot_{bot_num}")]]
                await update.message.reply_text(
                    "فشل استبدال الملف ❌",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )

            context.user_data.pop('replace_main_bot', None)
            context.user_data.pop('replace_bot_name', None)
            return

        if context.user_data.get('add_data_to_bot'):
            bot_num = context.user_data['add_data_to_bot']
            bot_dir = get_bot_directory(bot_num)
            file_path = make_upload_temp_path(file_name)
            await file.download_to_drive(file_path)
            dest_path = os.path.join(bot_dir, file_name)
            shutil.move(file_path, dest_path)
            keyboard = [[InlineKeyboardButton("رجوع", callback_data=f"edit_bot_{bot_num}")]]
            file_size = os.path.getsize(dest_path) / 1024
            await update.message.reply_text(
                f"تم اضافة الملف\n\nالاسم: {file_name}\n"
                f"الحجم: {file_size:.2f} KB\nالمسار: {dest_path}",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            context.user_data.pop('add_data_to_bot', None)
            log_message(f"تم إضافة ملف بيانات {file_name} للبوت {bot_num}", "INFO")
            return

        if context.user_data.get('add_helper_to_bot'):
            bot_num = context.user_data['add_helper_to_bot']
            if not file_name.endswith('.py'):
                await update.message.reply_text("يرجى ارسال ملف .py فقط!")
                return
            bot_dir = get_bot_directory(bot_num)
            file_path = make_upload_temp_path(file_name)
            await file.download_to_drive(file_path)
            with open(file_path, 'r', encoding='utf-8') as f:
                code = f.read()
            os.remove(file_path)
            helper_path = os.path.join(bot_dir, file_name)
            with open(helper_path, 'w', encoding='utf-8') as f:
                f.write(code)
            module_name = file_name.replace('.py', '')
            encoded_helper = base64.b64encode(code.encode('utf-8')).decode('ascii')
            # ✅ الإصلاح: تمرير bot_num حتى يبقى الملف المساعد خاصاً بهذا البوت فقط
            save_helper_file_to_db(bot_num, module_name, encoded_helper)
            # نحدّث أيضاً جدول bot_imports حتى تظهر في قائمة استيرادات هذا البوت (إن لم تكن موجودة أصلاً)
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute("SELECT 1 FROM bot_imports WHERE bot_num = ? AND module_name = ?", (bot_num, module_name))
            if not c.fetchone():
                c.execute("INSERT INTO bot_imports (bot_num, module_name) VALUES (?, ?)", (bot_num, module_name))
                conn.commit()
            conn.close()
            keyboard = [[InlineKeyboardButton("رجوع", callback_data=f"edit_bot_{bot_num}")]]
            await update.message.reply_text(
                f"تم اضافة الملف المساعد\n\nالاسم: {file_name}\nالمسار: {helper_path}",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            context.user_data.pop('add_helper_to_bot', None)
            log_message(f"تم إضافة ملف مساعد {file_name} للبوت {bot_num}", "INFO")
            return

        if not file_name.endswith('.py'):
            await update.message.reply_text("يرجى ارسال ملف .py فقط!")
            return

        file_path = make_upload_temp_path(file_name)
        await file.download_to_drive(file_path)
        with open(file_path, 'r', encoding='utf-8') as f:
            code = f.read()
        os.remove(file_path)

        if context.user_data.get('waiting_for_main_bot'):
            context.user_data['main_bot_name'] = file_name
            context.user_data['main_bot_code'] = code
            context.user_data['waiting_for_main_bot'] = False
            keyboard = [
                [InlineKeyboardButton("نعم، ارسل ملفات مساعدة", callback_data="send_helpers")],
                [InlineKeyboardButton("لا، لا يوجد ملفات اضافية", callback_data="confirm_no_helpers")],
                [InlineKeyboardButton("الغاء", callback_data="back")]
            ]
            await update.message.reply_text(
                f"تم استلام: {file_name}\n\nهل يحتاج البوت لملفات مساعدة؟",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

        elif context.user_data.get('waiting_for_helpers'):
            module_name = file_name.replace('.py', '')
            context.user_data['helper_files_data'].append({
                'name': file_name, 'module': module_name, 'code': code
            })
            await update.message.reply_text(
                f"تم استلام: {file_name}\n\nارسل المزيد او اكتب /done"
            )

    except Exception as e:
        await update.message.reply_text(f"خطأ: {str(e)}")

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return
    text = update.message.text

    if context.user_data.get('waiting_for_package'):
        packages = text.split()
        results = []
        msg = await update.message.reply_text("جاري التثبيت...")
        for pkg in packages:
            success, result_msg = install_package(pkg)
            results.append(f"{'تم' if success else 'فشل'}: {result_msg}")
        keyboard = [[InlineKeyboardButton("رجوع", callback_data="back")]]
        await msg.edit_text(
            "نتائج التثبيت:\n\n" + "\n".join(results),
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        context.user_data.pop('waiting_for_package', None)

    elif context.user_data.get('waiting_for_check_package'):
        pkg = text.strip()
        installed, output = is_package_installed(pkg)
        keyboard = [[InlineKeyboardButton("رجوع", callback_data="back")]]
        if installed:
            version_match = re.search(r'^Version:\s*(.+)$', output, re.MULTILINE)
            version = version_match.group(1) if version_match else "غير معروف"
            msg_text = f"✅ المكتبة مثبتة\n\nالاسم: {pkg}\nالاصدار: {version}"
        else:
            msg_text = f"❌ المكتبة غير مثبتة\n\nالاسم: {pkg}"
        await update.message.reply_text(msg_text, reply_markup=InlineKeyboardMarkup(keyboard))
        context.user_data.pop('waiting_for_check_package', None)

    elif context.user_data.get('waiting_for_package_info'):
        pkg = text.strip()
        installed, output = is_package_installed(pkg)
        keyboard = [[InlineKeyboardButton("رجوع", callback_data="back")]]
        if installed:
            await update.message.reply_text(
                f"ℹ️ معلومات المكتبة: {pkg}\n\n{output}",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            await update.message.reply_text(
                f"❌ المكتبة غير مثبتة: {pkg}",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        context.user_data.pop('waiting_for_package_info', None)

    elif context.user_data.get('waiting_for_uninstall'):
        packages = text.split()
        results = []
        msg = await update.message.reply_text("جاري حذف المكتبات وتنظيف الملفات المتبقية من كل المسارات...")
        for pkg in packages:
            success, result_msg, removed_paths = force_clean_package(pkg)
            line = f"{'✅' if success else '⚠️'} {result_msg}"
            if removed_paths:
                line += f"\n   🧹 تم تنظيف {len(removed_paths)} عنصر متبقي:"
                for p in removed_paths[:5]:
                    line += f"\n      - {os.path.basename(p)}"
                if len(removed_paths) > 5:
                    line += f"\n      ... و{len(removed_paths) - 5} عنصر آخر"
            else:
                line += "\n   🧹 لا توجد ملفات متبقية"
            results.append(line)
            log_message(f"حذف شامل للمكتبة {pkg} (عناصر متبقية محذوفة: {len(removed_paths)})", "INFO")
        keyboard = [[InlineKeyboardButton("رجوع", callback_data="back")]]
        await msg.edit_text(
            "نتائج الحذف الشامل:\n\n" + "\n\n".join(results),
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        context.user_data.pop('waiting_for_uninstall', None)

    elif context.user_data.get('waiting_for_pip_command'):
        cmd_text = text.strip()
        ok, err_msg, full_cmd, redirect_target = validate_pip_command(cmd_text)
        keyboard = [[InlineKeyboardButton("رجوع", callback_data="back")]]

        if not ok:
            await update.message.reply_text(f"❌ {err_msg}", reply_markup=InlineKeyboardMarkup(keyboard))
            context.user_data.pop('waiting_for_pip_command', None)
            return

        msg = await update.message.reply_text(f"⌛ جاري تنفيذ:\n`{full_cmd}`")
        workdir = os.path.dirname(os.path.abspath(__file__))

        try:
            result = subprocess.run(
                full_cmd, shell=True, capture_output=True, text=True,
                timeout=180, cwd=workdir
            )
            output = result.stdout.strip()
            error = result.stderr.strip()

            combined = ""
            if output:
                combined += output
            if error:
                combined += (("\n\n[stderr]\n" + error) if combined else ("[stderr]\n" + error))
            if not combined:
                combined = "(لا يوجد إخراج)"

            header = f"$ {full_cmd}\n"
            header += f"كود الخروج: {result.returncode}\n\n"

            full_text = header + combined

            if len(full_text) > 3500:
                temp_file = os.path.join(tempfile.gettempdir(), f"pip_output_{int(time.time())}.txt")
                with open(temp_file, 'w', encoding='utf-8') as f:
                    f.write(full_text)
                with open(temp_file, 'rb') as f:
                    await update.message.reply_document(
                        document=f,
                        filename="pip_output.txt",
                        caption=f"نتيجة تنفيذ:\n{full_cmd}",
                        reply_markup=InlineKeyboardMarkup(keyboard)
                    )
                os.remove(temp_file)
                try:
                    await msg.delete()
                except:
                    pass
            else:
                await msg.edit_text(full_text, reply_markup=InlineKeyboardMarkup(keyboard))

            # لو الأمر كان فيه تحويل مخرجات لملف (مثل pip freeze > requirements.txt)
            # نرسل الملف الناتج مباشرة أيضاً
            if redirect_target:
                target_path = (
                    redirect_target if os.path.isabs(redirect_target)
                    else os.path.join(workdir, redirect_target)
                )
                if os.path.exists(target_path):
                    with open(target_path, 'rb') as f:
                        await update.message.reply_document(
                            document=f,
                            filename=os.path.basename(target_path),
                            caption=f"📄 الملف الناتج: {redirect_target}"
                        )

            log_message(f"تنفيذ أمر pip: {full_cmd} (كود الخروج: {result.returncode})", "INFO")

        except subprocess.TimeoutExpired:
            await msg.edit_text(
                "⏱ انتهت المهلة الزمنية للأمر (180 ثانية)",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except Exception as e:
            await msg.edit_text(f"❌ خطأ: {str(e)}", reply_markup=InlineKeyboardMarkup(keyboard))

        context.user_data.pop('waiting_for_pip_command', None)

    elif context.user_data.get('waiting_for_admin_id'):
        if not is_super_admin(user_id):
            context.user_data.pop('waiting_for_admin_id', None)
            return
        try:
            new_admin_id = int(text.strip())
            add_admin_to_db(new_admin_id, user_id)
            log_message(f"تم اضافة مشرف جديد: {new_admin_id} بواسطة {user_id}", "INFO")
            keyboard = [[InlineKeyboardButton("رجوع لإدارة المشرفين", callback_data="manage_admins")]]
            await update.message.reply_text(
                f"✅ تم اضافة المشرف بنجاح\n\nالايدي: {new_admin_id}",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except ValueError:
            await update.message.reply_text("❌ يجب ارسال رقم ايدي صحيح فقط!")
            return
        context.user_data.pop('waiting_for_admin_id', None)

async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('waiting_for_helpers'):
        class FakeQuery:
            def __init__(self, message):
                self.message = message
        await finalize_bot_addition(FakeQuery(update.message), context)

# ═══════════════════════════════════════════════════════════
# 🚀 تشغيل النظام
# ═══════════════════════════════════════════════════════════

def run_admin_bot():
    try:
        application = Application.builder().token(ADMIN_TOKEN).build()
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("done", done_command))
        application.add_handler(CallbackQueryHandler(button_handler))
        application.add_handler(MessageHandler(filters.Document.ALL, file_handler))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def run():
            await application.initialize()
            await application.start()
            await application.updater.start_polling()
            while True:
                await asyncio.sleep(1)

        loop.run_until_complete(run())
    except Exception as e:
        log_message(f"خطأ في بوت الإدارة: {e}", "ERROR")
        time.sleep(30)
        run_admin_bot()

def main():
    try:
        _stderr_file = open(MAIN_STDERR_FILE, 'a', encoding='utf-8')
        _stderr_file.write(
            f"\n{'='*60}\n"
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] بدء تشغيل جديد\n"
            f"{'='*60}\n"
        )
        _stderr_file.flush()
        sys.stderr = _stderr_file
    except Exception as e:
        print(f"تحذير: فشل تحويل stderr: {e}")

    log_message("=" * 70, "INFO")
    log_message("نظام إدارة البوتات (نسخة مطورة)", "INFO")
    log_message(f"المجلد: {BOTS_ROOT_DIR}", "INFO")
    log_message(f"المشرف الرئيسي: {SUPER_ADMIN_ID}", "INFO")
    log_message(f"عدد المشرفين الإضافيين: {len(get_extra_admins())}", "INFO")
    log_message("=" * 70, "INFO")

    os.makedirs(BOTS_ROOT_DIR, exist_ok=True)

    restored_count = restore_bots_from_storage()
    if restored_count:
        log_message(f"تم استرجاع {restored_count} بوت من bots_data", "WARNING")

    all_bots = load_all_bots_from_db()
    for bot_num in sorted(all_bots.keys()):
        if not is_bot_paused(bot_num):
            log_message(f"تشغيل البوت {bot_num}...", "INFO")
            start_bot_process(bot_num)
            time.sleep(1)

    log_message("بدء تشغيل بوت الإدارة...", "INFO")
    run_admin_bot()

if __name__ == "__main__":
    main()

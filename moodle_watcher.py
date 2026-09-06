#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FBSU Moodle Watcher
====================
يراقب موقع المودل لجامعة فهد بن سلطان (elearning.fbsu.edu.sa) عن طريق خدمة
الويب الرسمية لتطبيق الجوال (Moodle Mobile web service)، ويكتشف أي جديد:
- مقررات جديدة تم تسجيلك فيها
- محتوى/أنشطة جديدة داخل المقررات (واجبات، ملفات، شباتر، اختبارات... أي عنصر يضيفه الدكتور)
- واجبات جديدة مع تاريخ التسليم
- إشعارات جديدة من الموقع (تشمل غالبًا رسائل التحضير/التغييب إذا كان الموقع يرسلها كإشعار)

ثم يرسل ملخص بكل ما هو جديد إلى Poke عبر الـ API الخاص به.

الحالة (اللي شفناه سابقًا) تُحفظ في ملف state.json حتى لا يتكرر إرسال نفس الإشعار.
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
import urllib.error

MOODLE_URL = (os.environ.get("MOODLE_URL") or "https://elearning.fbsu.edu.sa").rstrip("/")
MOODLE_USERNAME = os.environ.get("MOODLE_USERNAME", "")
MOODLE_PASSWORD = os.environ.get("MOODLE_PASSWORD", "")
POKE_API_KEY = os.environ.get("POKE_API_KEY", "")
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
SERVICE = os.environ.get("MOODLE_SERVICE", "moodle_mobile_app")

TIMEOUT = 30


def http_post_form(url, data):
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read().decode("utf-8", errors="replace")


def http_post_json(url, payload, headers=None):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read().decode("utf-8", errors="replace")


def get_token():
    """يسجّل الدخول عبر خدمة تطبيق الجوال الرسمية ويرجّع التوكن."""
    url = f"{MOODLE_URL}/login/token.php"
    raw = http_post_form(url, {
        "username": MOODLE_USERNAME,
        "password": MOODLE_PASSWORD,
        "service": SERVICE,
    })
    data = json.loads(raw)
    if "token" not in data:
        raise RuntimeError(f"فشل تسجيل الدخول للمودل: {data}")
    return data["token"]


def ws_call(token, wsfunction, extra_params=None):
    """ينادي أي دالة من دوال Web Service بصيغة JSON."""
    params = {
        "wstoken": token,
        "wsfunction": wsfunction,
        "moodlewsrestformat": "json",
    }
    if extra_params:
        params.update(extra_params)
    url = f"{MOODLE_URL}/webservice/rest/server.php"
    raw = http_post_form(url, params)
    data = json.loads(raw)
    if isinstance(data, dict) and data.get("exception"):
        raise RuntimeError(f"{wsfunction} -> {data.get('errorcode')}: {data.get('message')}")
    return data


def flatten_ws_params(name, values):
    """يحوّل list إلى صيغة باراميترات Moodle: name[0]=x&name[1]=y"""
    out = {}
    for i, v in enumerate(values):
        out[f"{name}[{i}]"] = v
    return out


def safe_call(label, fn):
    try:
        return fn()
    except Exception as e:
        print(f"[تنبيه] تخطّينا '{label}' لأنه غير متاح أو صار خطأ: {e}", file=sys.stderr)
        return None


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"first_run": True, "modules": {}, "assignments": {}, "notifications": {}, "attendance": {}}


def save_state(state):
    state["first_run"] = False
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def send_to_poke(message):
    if not POKE_API_KEY:
        print("[تنبيه] لا يوجد POKE_API_KEY - سيتم طباعة الرسالة فقط بدل إرسالها.")
        print(message)
        return
    url = "https://poke.com/api/v1/inbound/api-message"
    try:
        raw = http_post_json(url, {"message": message}, headers={
            "Authorization": f"Bearer {POKE_API_KEY}"
        })
        print("تم الإرسال لـ Poke:", raw)
    except urllib.error.HTTPError as e:
        print(f"[خطأ] فشل الإرسال لـ Poke ({e.code}): {e.read().decode('utf-8', 'replace')}", file=sys.stderr)
    except Exception as e:
        print(f"[خطأ] فشل الإرسال لـ Poke: {e}", file=sys.stderr)


def main():
    if not MOODLE_USERNAME or not MOODLE_PASSWORD:
        print("لازم تحط MOODLE_USERNAME و MOODLE_PASSWORD كمتغيرات بيئة (Secrets).", file=sys.stderr)
        sys.exit(1)

    state = load_state()
    is_first_run = state.get("first_run", True)

    token = get_token()

    site_info = ws_call(token, "core_webservice_get_site_info")
    userid = site_info["userid"]
    enabled_functions = {f["name"] for f in site_info.get("functions", [])}
    print(f"مسجل الدخول كـ: {site_info.get('fullname', MOODLE_USERNAME)} (userid={userid})")

    new_items = []  # كل عنصر: (نوع، نص وصفي، رابط أو None)

    # 1) المقررات المسجّل فيها
    courses = safe_call("قائمة المقررات", lambda: ws_call(
        token, "core_enrol_get_users_courses", {"userid": userid}
    )) or []
    course_names = {c["id"]: c.get("fullname", f"مقرر {c['id']}") for c in courses}

    prev_modules = state.get("modules", {})
    new_modules_state = dict(prev_modules)

    # 2) محتوى كل مقرر (أي عنصر/نشاط جديد يضيفه الدكتور = شباتر/ملفات/واجبات/اختبارات...)
    for course in courses:
        cid = str(course["id"])
        contents = safe_call(f"محتوى المقرر {course.get('fullname')}", lambda cid=course["id"]: ws_call(
            token, "core_course_get_contents", {"courseid": cid}
        ))
        if not contents:
            continue
        seen_ids = set(prev_modules.get(cid, []))
        current_ids = set()
        for section in contents:
            for module in section.get("modules", []):
                mid = str(module["id"])
                current_ids.add(mid)
                if mid not in seen_ids and not is_first_run:
                    mname = module.get("name", "بدون اسم")
                    mtype = module.get("modname", "")
                    murl = module.get("url", "")
                    new_items.append((
                        "تحديث بالمقرر",
                        f"📚 {course_names.get(course['id'], '')} — عنصر جديد ({mtype}): {mname}",
                        murl,
                    ))
        new_modules_state[cid] = sorted(current_ids)

    state["modules"] = new_modules_state

    # 3) الواجبات (تفاصيل أدق: تاريخ التسليم)
    if courses:
        assign_params = flatten_ws_params("courseids", [c["id"] for c in courses])
        assignments_resp = safe_call("الواجبات", lambda: ws_call(
            token, "mod_assign_get_assignments", assign_params
        ))
        prev_assign = state.get("assignments", {})
        new_assign_state = dict(prev_assign)
        if assignments_resp:
            for course_a in assignments_resp.get("courses", []):
                cid = str(course_a["id"])
                seen = set(prev_assign.get(cid, []))
                current = set()
                for a in course_a.get("assignments", []):
                    aid = str(a["id"])
                    current.add(aid)
                    if aid not in seen and not is_first_run:
                        due = a.get("duedate", 0)
                        due_txt = time.strftime("%Y-%m-%d %H:%M", time.localtime(due)) if due else "بدون تاريخ محدد"
                        new_items.append((
                            "واجب جديد",
                            f"📝 {course_names.get(int(cid), '')} — واجب جديد: {a.get('name')} | التسليم: {due_txt}",
                            None,
                        ))
                new_assign_state[cid] = sorted(current)
        state["assignments"] = new_assign_state

    # 4) إشعارات الموقع (قد تشمل تحضير/تغييب/رد على منتدى/تقييم درجة...)
    if "message_popup_get_popup_notifications" in enabled_functions or True:
        notif_resp = safe_call("الإشعارات", lambda: ws_call(
            token, "message_popup_get_popup_notifications", {"useridto": userid, "limit": 30}
        ))
        prev_notif = set(state.get("notifications", {}).get("ids", []))
        current_notif = set()
        if notif_resp:
            for n in notif_resp.get("notifications", []):
                nid = str(n["id"])
                current_notif.add(nid)
                if nid not in prev_notif and not is_first_run:
                    subject = n.get("subject", "")
                    text = n.get("fullmessage", "") or n.get("smallmessage", "")
                    new_items.append((
                        "إشعار",
                        f"🔔 {subject} — {text}".strip(" —"),
                        None,
                    ))
            state["notifications"] = {"ids": sorted(current_notif)}

    # 5) محاولة تغطية نظام الحضور (mod_attendance) إن كان مفعّل في هذا الموقع
    attendance_funcs = [f for f in enabled_functions if f.startswith("mod_attendance_")]
    if attendance_funcs:
        att_result = safe_call("جلسات الحضور", lambda: ws_call(
            token, "mod_attendance_get_courses_with_today_sessions"
        ) if "mod_attendance_get_courses_with_today_sessions" in enabled_functions else None)
        prev_att = set(state.get("attendance", {}).get("ids", []))
        current_att = set()
        if att_result:
            for c in att_result:
                for s in c.get("sessions", []):
                    sid = str(s.get("id"))
                    current_att.add(sid)
                    status = s.get("description", "")
                    if sid not in prev_att and not is_first_run:
                        new_items.append((
                            "تحضير",
                            f"🗓️ جلسة حضور جديدة/محدثة: {c.get('fullname', '')} {status}".strip(),
                            None,
                        ))
        state["attendance"] = {"ids": sorted(current_att)}
    else:
        print("[معلومة] دوال الحضور (mod_attendance) غير متاحة عبر هذا التوكن على هذا الموقع - تم تخطيها.")

    save_state(state)

    if is_first_run:
        send_to_poke(
            "✅ تم تفعيل مراقبة موقع مودل جامعة فهد بن سلطان بنجاح.\n"
            f"عدد المقررات المسجّل فيها: {len(courses)}.\n"
            "بترسل لك تحديث كل ما يصير شي جديد (واجب/محتوى/إشعار)."
        )
        print("أول تشغيل: تم حفظ الحالة الحالية كنقطة بداية بدون إرسال كل السجل القديم.")
        return

    if not new_items:
        print("لا يوجد جديد في هذا الفحص.")
        return

    lines = ["📢 تحديثات جديدة من مودل جامعة فهد بن سلطان:", ""]
    for _, text, url in new_items:
        line = f"- {text}"
        if url:
            line += f"\n  🔗 {url}"
        lines.append(line)
    message = "\n".join(lines)
    print(message)
    send_to_poke(message)


if __name__ == "__main__":
    main()

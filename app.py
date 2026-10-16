"""AI Learning Lab: pilot MVP. NOT for unapproved human-subject research."""
import hashlib
import hmac
import io
import secrets
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import quote

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="AI Learning Lab", page_icon="🎓", layout="wide")
st.markdown("""<style>
html, body, [data-testid='stAppViewContainer'] {direction: rtl; text-align: right;}
[data-testid='stChatMessage'] {direction: rtl; text-align: right;}
.block-container {max-width: 1100px; padding-top: 1.8rem;}
h1,h2,h3,p {text-align: right;}
</style>""", unsafe_allow_html=True)

TASK = """משימת הדגמה בלבד (יש להחליף במשימה לימודית מאושרת לפני הניסוי):
הסבירו מהם שני יתרונות ושני חסרונות של שימוש בבינה מלאכותית לצורכי למידה.
הציגו דוגמה אחת למצב שבו חשוב לבדוק תשובה שהפיק כלי AI, ונמקו את בחירתכם.
יש להגיש תשובה מנומקת במילים שלכם."""
CONSENT = """זו טיוטת נוסח הסכמה לפיילוט ואינה תחליף לאישור ועדת אתיקה.
במחקר תבצעו משימה לימודית ותענו על שאלונים קצרים לפני העבודה ואחריה. יישמרו קוד משתתף אקראי, שיוך קבוצה,
תשובות לשאלונים, התוצר שהגשתם, חותמות זמן, אירועי שימוש ותוכן השיחה עם מודל ה-AI.
אין להזין שמות, פרטי קשר, מספרי זהות או מידע רגיש לתיבת הצ'אט. ייתכן שתוכן חופשי שתקלידו יכיל פרטים מזהים,
ולכן הנתונים מקודדים לפי מזהה ולא מובטחת אנונימיות מוחלטת. המידע נשלח גם לספק מודל ה-AI ולספק אחסון הנתונים.
ההשתתפות מרצון וניתן להפסיק בכל שלב. לפני גיוס משתתפים אמיתיים יש להשלים ולמסור פרטי חוקרים,
פרטי ספקים, מדיניות מחיקה ושמירה, מענה לבקשות משתתפים ואישור מוסדי מתאים.
"""
PRE = {
    "confidence_task": "אני בטוח/ה ביכולתי לבצע את המשימה בהצלחה.",
    "confidence_independent": "אני בטוח/ה שאוכל להתמודד עם משימה דומה ללא AI.",
    "ai_familiarity": "אני מכיר/ה היטב כלי AI לצורכי למידה.",
}
POST = {
    "confidence_task": "אני בטוח/ה ביכולתי לבצע את המשימה בהצלחה.",
    "confidence_independent": "אני בטוח/ה שאוכל להתמודד עם משימה דומה ללא AI.",
    "engagement": "הייתי מעורב/ת באופן פעיל בתהליך הלמידה.",
    "critical_check": "בחנתי באופן ביקורתי מידע שקיבלתי מה-AI.",
}
SYSTEM_A = """You are a general-purpose academic AI assistant. Answer the user's questions clearly and accurately.
Do not introduce mandatory pedagogical stages or forced reflection. Respond in Hebrew unless asked otherwise.
Do not fabricate citations."""
SYSTEM_B = """You are a structured pedagogical AI learning guide in a research experiment. Respond in Hebrew.
Follow this sequence with supportive, short prompts: (1) independent first attempt, (2) clarify understanding,
(3) focused questions, (4) critically evaluate and verify AI information, (5) draft own answer, (6) reflect.
At the current stage, encourage student thinking, ask one meaningful question, and adapt help to the reply.
Avoid immediately delivering a full completed submission; help the student construct it independently.
Do not fabricate citations. Current pedagogical stage: {stage}."""
STAGES = ["ניסיון עצמאי", "הבנת הבעיה", "שאלות ממוקדות", "בדיקה ואימות", "כתיבה עצמאית", "רפלקציה"]


def setting(name):
    try:
        return str(st.secrets.get(name, "")).strip()
    except (FileNotFoundError, KeyError):
        return ""


def configured():
    return all(setting(k) and not setting(k).startswith("REPLACE") and "YOUR-" not in setting(k)
               for k in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "GROQ_API_KEY", "ADMIN_PASSWORD", "GROUP_A_TOKEN", "GROUP_B_TOKEN"))


def db(table, method="GET", params=None, data=None, prefer="return=representation"):
    url = setting("SUPABASE_URL").rstrip("/") + "/rest/v1/" + table
    key = setting("SUPABASE_SERVICE_ROLE_KEY")
    headers = {"apikey": key, "Authorization": "Bearer " + key,
               "Content-Type": "application/json", "Prefer": prefer}
    res = requests.request(method, url, params=params, json=data, headers=headers, timeout=18)
    if not res.ok:
        raise RuntimeError(f"Database returned HTTP {res.status_code}. Check setup and connectivity.")
    return res.json() if res.content else []


def event(kind, detail=None):
    db("events", "POST", data={"id": str(uuid.uuid4()), "participant_id": st.session_state.pid,
                                "group_id": st.session_state.group, "event_type": kind, "detail": detail or {}})


def init_session(pid, group):
    st.session_state.pid = pid
    st.session_state.group = group
    st.session_state.setdefault("stage_index", 0)
    st.session_state.setdefault("messages", None)


def restore_messages():
    records = db("messages", params={"select": "role,content", "participant_id": "eq." + st.session_state.pid,
                                      "order": "created_at.asc", "limit": "500"})
    st.session_state.messages = records
    stages = db("events", params={"select": "detail", "participant_id": "eq." + st.session_state.pid,
                                      "event_type": "eq.stage_changed", "order": "occurred_at.desc", "limit": "1"})
    if stages:
        st.session_state.stage_index = min(5, max(0, int(stages[0]["detail"].get("to_index", 0))))


def participant_state():
    pid = st.session_state.pid
    a = db("answers", params={"select": "phase,question_id", "participant_id": "eq." + pid})
    sub = db("submissions", params={"select": "id", "participant_id": "eq." + pid, "limit": "1"})
    participant = db("participants", params={"select": "completed_at,withdrawn_at", "id": "eq." + pid, "limit": "1"})
    if not participant or participant[0].get("withdrawn_at"):
        return "withdrawn"
    phases = {x["phase"] for x in a}
    if "pre" not in phases:
        return "pre"
    if not sub:
        return "task"
    if "post" not in phases:
        return "post"
    return "done"


def survey(phase, items):
    st.subheader("שאלון לפני המשימה" if phase == "pre" else "שאלון לאחר המשימה")
    st.caption("טיוטת שאלון לא מאומת — יש לאשר פריטים ונוסח לפני מחקר אמיתי. 1 = כלל לא מסכים/ה; 5 = מסכים/ה מאוד.")
    with st.form("survey_" + phase):
        vals = {k: st.radio(q, [1, 2, 3, 4, 5], index=None, horizontal=True, key=phase+k)
                for k, q in items.items()}
        go = st.form_submit_button("שמירת תשובות והמשך", type="primary")
    if go:
        if any(v is None for v in vals.values()):
            st.warning("יש לענות על כל השאלות.")
            return
        # Single bulk DB request with unique participant-phase-question keys.
        db("answers", "POST", data=[{"id": str(uuid.uuid4()), "participant_id": st.session_state.pid,
                                       "phase": phase, "question_id": q, "value": int(v)} for q, v in vals.items()])
        event("survey_completed", {"phase": phase})
        if phase == "post":
            db("participants", "PATCH", params={"id": "eq." + st.session_state.pid},
               data={"completed_at": datetime.now(timezone.utc).isoformat()})
            event("experiment_completed")
        st.rerun()


def ai_reply(user_text, history):
    stage = STAGES[st.session_state.stage_index]
    prompt = SYSTEM_A if st.session_state.group == "A" else SYSTEM_B.format(stage=stage)
    messages = [{"role": "system", "content": prompt}]
    messages += [{"role": item["role"], "content": item["content"]} for item in history[-24:]]
    started = time.monotonic()
    try:
        r = requests.post("https://api.groq.com/openai/v1/chat/completions", timeout=45,
             headers={"Authorization": "Bearer " + setting("GROQ_API_KEY"), "Content-Type": "application/json"},
             json={"model": setting("GROQ_MODEL") or "llama-3.3-70b-versatile",
                   "messages": messages, "temperature": 0.3, "max_tokens": 700})
        if not r.ok:
            return None, round(time.monotonic()-started, 2), r.status_code
        return r.json()["choices"][0]["message"]["content"], round(time.monotonic()-started, 2), None
    except (requests.RequestException, KeyError, IndexError, ValueError):
        return None, round(time.monotonic()-started, 2), "request_failure"


def chat_and_task():
    st.subheader("משימת למידה — גרסת הדגמה")
    st.info(TASK)
    st.caption("אין לשלוח מידע אישי בצ'אט. התכתבויות נשמרות לצורכי הדגמה/מחקר בהתאם להסכמה.")
    st.write("**קבוצת העבודה:**", "שימוש חופשי" if st.session_state.group == "A" else "סוכן פדגוגי מובנה")
    if st.session_state.messages is None:
        restore_messages()
    if st.session_state.group == "B":
        st.progress((st.session_state.stage_index+1)/6,
                    text=f"שלב {st.session_state.stage_index+1} מתוך 6 — {STAGES[st.session_state.stage_index]}")
        if st.session_state.stage_index < 5:
            if st.button("סיימתי שלב — מעבר לשלב הבא"):
                st.session_state.stage_index += 1
                event("stage_changed", {"to_index": st.session_state.stage_index,
                                        "stage": STAGES[st.session_state.stage_index]})
                st.rerun()
    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.write(msg["content"])
    if text := st.chat_input("כתוב/כתבי שאלה למודל AI", max_chars=4000):
        # Save before model request to prevent loss when remote model is unavailable.
        db("messages", "POST", data={"id": str(uuid.uuid4()), "participant_id": st.session_state.pid,
                                       "role": "user", "content": text, "stage": STAGES[st.session_state.stage_index] if st.session_state.group == "B" else None})
        event("user_message", {"chars": len(text)})
        st.session_state.messages.append({"role": "user", "content": text})
        with st.spinner("ה-AI מכין תשובה..."):
            result, secs, err = ai_reply(text, st.session_state.messages)
        if err:
            event("ai_error", {"status": str(err), "elapsed_seconds": secs})
            st.error("לא התקבלה תשובה מהמודל. ההודעה שלך נשמרה; נסה/י שוב בהודעה נוספת.")
        else:
            db("messages", "POST", data={"id": str(uuid.uuid4()), "participant_id": st.session_state.pid,
                                           "role": "assistant", "content": result,
                                           "stage": STAGES[st.session_state.stage_index] if st.session_state.group == "B" else None,
                                           "model": setting("GROQ_MODEL") or "llama-3.3-70b-versatile"})
            event("ai_response", {"chars": len(result), "latency_seconds": secs})
            st.session_state.messages.append({"role": "assistant", "content": result})
        st.rerun()
    st.divider()
    st.subheader("הגשת תשובה סופית")
    with st.form("submission"):
        answer = st.text_area("כתבו כאן את התוצר הסופי במילים שלכם", height=180, max_chars=12000)
        sent = st.form_submit_button("הגש/י והמשך לשאלון המסכם", type="primary")
    if sent:
        if len(answer.strip()) < 30:
            st.warning("יש לכתוב תשובה באורך של 30 תווים לפחות.")
        else:
            db("submissions", "POST", data={"id": str(uuid.uuid4()), "participant_id": st.session_state.pid,
                                               "content": answer.strip()})
            event("task_submitted", {"chars": len(answer.strip())})
            st.rerun()


def admin():
    st.subheader("דשבורד חוקר")
    if not st.session_state.get("admin_ok"):
        pwd = st.text_input("סיסמת חוקר", type="password")
        if st.button("כניסה לחוקר"):
            if hmac.compare_digest(pwd, setting("ADMIN_PASSWORD")):
                st.session_state.admin_ok = True
                st.rerun()
            else:
                st.error("סיסמה שגויה")
        return
    if st.button("יציאה מניהול"):
        st.session_state.admin_ok = False
        st.rerun()
    st.warning("הורידו קבצים רק למחשב מחקרי מאובטח. הם עשויים להכיל טקסט חופשי ומידע מזהה שהוקלד בשיחות.")
    tables = ["participants", "events", "messages", "answers", "submissions"]
    all_data = {}
    for table in tables:
        # Supabase REST defaults to 1,000 rows max; page with offset for complete export.
        out = []
        offset = 0
        while True:
            page = db(table, params={"select": "*", "limit": "500", "offset": str(offset)})
            out.extend(page)
            if len(page) < 500:
                break
            offset += 500
        all_data[table] = pd.DataFrame(out)
    people = all_data["participants"]
    a, b, c = st.columns(3)
    a.metric("משתתפים", len(people))
    b.metric("קבוצה A", int((people.get("group_id", pd.Series(dtype=str)) == "A").sum()))
    c.metric("קבוצה B", int((people.get("group_id", pd.Series(dtype=str)) == "B").sum()))
    st.write("**משתתפים**")
    if not people.empty:
        st.dataframe(people.drop(columns=["resume_hash"], errors="ignore"), use_container_width=True)
    for table in tables:
        df = all_data[table]
        if table == "participants":
            df = df.drop(columns=["resume_hash"], errors="ignore")
        st.download_button(f"הורדת {table}.csv", data=df.to_csv(index=False).encode("utf-8-sig"),
                           file_name=f"{table}.csv", mime="text/csv", key="dl_"+table)
    bundle = io.BytesIO()
    with pd.ExcelWriter(bundle, engine="openpyxl") as writer:
        for table in tables:
            df = all_data[table].drop(columns=["resume_hash"], errors="ignore")
            # Prevent formula injection in spreadsheets from user-supplied text.
            df = df.map(lambda v: "'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@")) else v)
            df.to_excel(writer, sheet_name=table, index=False)
    st.download_button("הורדת קובץ Excel מאוחד", bundle.getvalue(), "ai_research_export.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def main():
    st.title("🎓 AI Learning Lab")
    st.caption("פלטפורמת ניסוי — שתי קבוצות | מודל AI משותף | תיעוד נתונים")
    st.error("גרסת פיילוט טכנית בלבד. אין לגייס משתתפים לפני אישור אתיקה, נוסח הסכמה, מדיניות מידע ומשימה מאושרת.")
    if not configured():
        st.warning("נדרשת הגדרה של Supabase, Groq, סיסמת חוקר ואסימוני קבוצות ב-Streamlit Secrets.")
        st.code('SUPABASE_URL = "..."\nSUPABASE_SERVICE_ROLE_KEY = "..."\nGROQ_API_KEY = "..."\nGROQ_MODEL = "llama-3.3-70b-versatile"\nADMIN_PASSWORD = "..."\nGROUP_A_TOKEN = "..."\nGROUP_B_TOKEN = "..."', language="toml")
        st.stop()
    if st.query_params.get("admin") == "1":
        admin()
        return
    if setting("EXPERIMENT_ENABLED").lower() != "true":
        st.info("ההרשמה כבויה כברירת מחדל. החוקר יוכל לאפשר בדיקות עם משתתפי דמה באמצעות EXPERIMENT_ENABLED = true ב-Secrets, לאחר בדיקת נהלי המחקר.")
        return
    token = st.query_params.get("invite", "")
    expected = {"A": setting("GROUP_A_TOKEN"), "B": setting("GROUP_B_TOKEN")}
    group = next((k for k, v in expected.items() if token and hmac.compare_digest(token, v)), None)
    if not group:
        st.info("כניסה לניסוי דורשת קישור הזמנה תקף שקיבלתם מהחוקר.")
        return
    st.caption(f"קבוצה {group}")
    if st.session_state.get("pid") and st.session_state.get("group") != group:
        st.error("המפגש הפעיל שייך לקבוצה אחרת. פתחו חלון גלישה חדש עם הקישור המתאים.")
        return
    if not st.session_state.get("pid"):
        with st.expander("כבר השתתפתי — חזרה למפגש", expanded=False):
            code = st.text_input("קוד חזרה שקיבלתם", type="password").strip()
            if st.button("שחזור מפגש") and code:
                hashed = hashlib.sha256(code.encode()).hexdigest()
                found = db("participants", params={"select": "id,group_id", "resume_hash": "eq."+hashed, "limit": "1"})
                if found and found[0]["group_id"] == group:
                    init_session(found[0]["id"], group)
                    event("session_restored")
                    st.rerun()
                else:
                    st.error("קוד חזרה לא נמצא בקבוצה זו.")
        st.subheader("הסכמה להשתתפות")
        st.write(CONSENT)
        agree = st.checkbox("קראתי והבנתי את המידע ואני מסכים/ה להשתתף")
        if st.button("מאשר/ת ומתחיל/ה", disabled=not agree, type="primary"):
            pid = str(uuid.uuid4())
            recovery = secrets.token_urlsafe(18)
            db("participants", "POST", data={"id": pid, "group_id": group,
                                             "resume_hash": hashlib.sha256(recovery.encode()).hexdigest(),
                                             "consent_version": setting("CONSENT_VERSION") or "pilot-draft-v1"})
            init_session(pid, group)
            st.session_state.recovery_code = recovery
            event("consent_granted", {"version": setting("CONSENT_VERSION") or "pilot-draft-v1"})
            st.rerun()
        return
    st.success("המפגש זוהה. קוד המשתתף שלך: " + st.session_state.pid[:8])
    if st.session_state.get("recovery_code"):
        st.warning("שמרו את קוד החזרה. הוא יוצג פעם אחת בלבד: " + st.session_state.recovery_code)
        if st.button("שמרתי את קוד החזרה"):
            st.session_state.pop("recovery_code", None)
            st.rerun()
    phase = participant_state()
    if phase == "pre":
        survey("pre", PRE)
    elif phase == "task":
        chat_and_task()
    elif phase == "post":
        survey("post", POST)
    elif phase == "done":
        st.balloons()
        st.success("תודה! השלמת את הפעילות.")
    else:
        st.info("המפגש אינו זמין.")


if __name__ == "__main__":
    main()

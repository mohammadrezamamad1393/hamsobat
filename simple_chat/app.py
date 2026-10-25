import os, re, sqlite3, secrets, json, urllib.request, urllib.error
from datetime import datetime
from pathlib import Path
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_from_directory, abort
from flask_socketio import SocketIO, emit, join_room
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash

BASE = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get('DATABASE_PATH', str(BASE / 'chat.db')))
UPLOAD_DIR = Path(os.environ.get('UPLOAD_DIR', str(BASE / 'uploads')))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
MAX_FILE_BYTES = 12 * 1024 * 1024
ALLOWED_EXT = {'png','jpg','jpeg','gif','webp','pdf','txt','zip','docx','xlsx','mp3','mp4'}
ADMIN_NAME = 'محمدرضا'
ADMIN_PASSWORD = os.environ.get('CHAT_ADMIN_PASSWORD', '13931393')
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY') or secrets.token_hex(32)
app.config['MAX_CONTENT_LENGTH'] = MAX_FILE_BYTES
socketio = SocketIO(app, cors_allowed_origins=[])
active_sids = {}


def db():
    con = sqlite3.connect(DB_PATH, timeout=20)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.execute('''CREATE TABLE IF NOT EXISTS members (
            name TEXT PRIMARY KEY, password_hash TEXT, first_seen TEXT NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0,
            banned INTEGER NOT NULL DEFAULT 0)''')
        con.execute('''CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)''')
        con.execute('''CREATE TABLE IF NOT EXISTS rooms (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL)''')
        con.execute('''CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, body TEXT NOT NULL DEFAULT '',
            file_name TEXT, stored_name TEXT, created_at TEXT NOT NULL,
            reply_to INTEGER, edited INTEGER NOT NULL DEFAULT 0, pinned INTEGER NOT NULL DEFAULT 0,
            room_id INTEGER NOT NULL DEFAULT 1)''')
        mcols = {r['name'] for r in con.execute('PRAGMA table_info(members)')}
        if 'password_hash' not in mcols: con.execute('ALTER TABLE members ADD COLUMN password_hash TEXT')
        if 'is_admin' not in mcols: con.execute('ALTER TABLE members ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0')
        if 'banned' not in mcols: con.execute('ALTER TABLE members ADD COLUMN banned INTEGER NOT NULL DEFAULT 0')
        cols = {r['name'] for r in con.execute('PRAGMA table_info(messages)')}
        for col, definition in [('reply_to','INTEGER'), ('edited','INTEGER NOT NULL DEFAULT 0'), ('pinned','INTEGER NOT NULL DEFAULT 0'), ('room_id','INTEGER NOT NULL DEFAULT 1')]:
            if col not in cols: con.execute(f'ALTER TABLE messages ADD COLUMN {col} {definition}')
        con.execute('INSERT OR IGNORE INTO rooms(id,name,created_at) VALUES(1,?,?)', ('گپ عمومی', timestamp()))
        con.execute('UPDATE members SET is_admin=1 WHERE name=?', (ADMIN_NAME,))


def timestamp(): return datetime.now().astimezone().strftime('%Y-%m-%d %H:%M')
def room_channel(room_id): return f'room_{int(room_id)}'
def clean_name(raw):
    name = re.sub(r'\s+', ' ', (raw or '').strip())
    if len(name) < 2 or len(name) > 24 or any(ord(c) < 32 for c in name): return None
    return name

def member(name):
    if not name: return None
    with db() as con:
        row = con.execute('SELECT name, is_admin, banned FROM members WHERE name=?', (name,)).fetchone()
        return dict(row) if row else None

def logged_in():
    m = member(session.get('name'))
    return bool(m and not m['banned'])
def is_admin():
    m = member(session.get('name'))
    return bool(m and m['is_admin'] and not m['banned'])
def get_room(room_id):
    with db() as con: return con.execute('SELECT * FROM rooms WHERE id=?', (room_id,)).fetchone()
def member_names():
    with db() as con: return [r['name'] for r in con.execute('SELECT name FROM members WHERE banned=0 ORDER BY first_seen')]
def message_payload(row):
    d = dict(row)
    d['url'] = url_for('download', message_id=d['id']) if d.get('stored_name') else None
    d.pop('stored_name', None)
    return d
def get_setting(key, default=''):
    with db() as con: row = con.execute('SELECT value FROM app_settings WHERE key=?', (key,)).fetchone()
    return row['value'] if row else default
def set_setting(key, value):
    with db() as con: con.execute('INSERT INTO app_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))
def call_openai(prompt):
    api_key = get_setting('openai_api_key') or os.environ.get('OPENAI_API_KEY', '')
    if not api_key: raise RuntimeError('ادمین هنوز کلید API را در پنل مدیریت وارد نکرده است.')
    payload = json.dumps({'model': os.environ.get('OPENAI_MODEL', 'gpt-4.1-mini'), 'input': prompt}).encode('utf-8')
    req = urllib.request.Request('https://api.openai.com/v1/responses', data=payload, headers={'Authorization': 'Bearer ' + api_key, 'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(req, timeout=60) as resp: result = json.loads(resp.read().decode('utf-8'))
    answer = result.get('output_text')
    if not answer:
        parts = []
        for item in result.get('output', []):
            for content in item.get('content', []):
                if content.get('type') == 'output_text': parts.append(content.get('text', ''))
        answer = '\n'.join(parts)
    return answer or 'پاسخی دریافت نشد.'
def save_ai_message(answer, room_id):
    created = timestamp()
    with db() as con:
        cur = con.execute('INSERT INTO messages(name,body,created_at,room_id) VALUES(?,?,?,?)', ('هوش مصنوعی', answer[:4000], created, room_id))
        row = con.execute('SELECT m.*, p.name AS reply_name, p.body AS reply_body FROM messages m LEFT JOIN messages p ON p.id=m.reply_to WHERE m.id=?', (cur.lastrowid,)).fetchone()
    return message_payload(row)
def remove_message_file(row):
    if row and row['stored_name']:
        try: (UPLOAD_DIR / row['stored_name']).unlink(missing_ok=True)
        except OSError: pass


@app.route('/', methods=['GET', 'POST'])
def index():
    if logged_in(): return redirect(url_for('chat'))
    if request.method == 'POST':
        name = clean_name(request.form.get('name')); password = request.form.get('password') or ''
        if not name or len(password) < 4 or len(password) > 128:
            return render_template('index.html', error='نام معتبر و رمز حداقل ۴ نویسه‌ای وارد کن.', name=request.form.get('name',''))
        admin_login = name == ADMIN_NAME and secrets.compare_digest(password, ADMIN_PASSWORD)
        if name == ADMIN_NAME and not admin_login:
            return render_template('index.html', error='رمز ادمین اشتباه است.', name=name)
        with db() as con:
            row = con.execute('SELECT * FROM members WHERE name=?', (name,)).fetchone()
            if row and row['banned'] and not admin_login:
                return render_template('index.html', error='این حساب توسط ادمین مسدود شده است.', name=name)
            if row:
                if admin_login:
                    con.execute('UPDATE members SET password_hash=?, is_admin=1, banned=0 WHERE name=?', (generate_password_hash(password), name))
                elif not row['password_hash']:
                    con.execute('UPDATE members SET password_hash=? WHERE name=?', (generate_password_hash(password), name))
                elif not check_password_hash(row['password_hash'], password):
                    return render_template('index.html', error='نام یا رمز عبور اشتباه است.', name=name)
            else:
                if not admin_login:
                    con.execute('INSERT INTO members(name,password_hash,first_seen,is_admin,banned) VALUES(?,?,?,0,0)', (name, generate_password_hash(password), timestamp()))
                else:
                    con.execute('INSERT INTO members(name,password_hash,first_seen,is_admin,banned) VALUES(?,?,?,1,0)', (name, generate_password_hash(password), timestamp()))
        session.clear(); session['name'] = name; session.permanent = True
        return redirect(url_for('chat'))
    return render_template('index.html', error=None, name='')

@app.route('/chat')
def chat():
    if not logged_in(): session.clear(); return redirect(url_for('index'))
    try: room_id = int(request.args.get('room', '1'))
    except ValueError: room_id = 1
    room = get_room(room_id)
    if not room:
        with db() as con: room = con.execute('SELECT * FROM rooms ORDER BY id LIMIT 1').fetchone()
        if not room: abort(404)
        room_id = room['id']
    with db() as con:
        rows = con.execute('''SELECT m.*, p.name AS reply_name, p.body AS reply_body FROM messages m LEFT JOIN messages p ON p.id=m.reply_to WHERE m.room_id=? ORDER BY m.id''', (room_id,)).fetchall()
        people = [r['name'] for r in con.execute('SELECT name FROM members WHERE banned=0 ORDER BY first_seen')]
        rooms = con.execute('SELECT * FROM rooms ORDER BY id').fetchall()
    return render_template('chat.html', name=session['name'], history=[message_payload(r) for r in rows], people=people, admin=is_admin(), rooms=[dict(r) for r in rooms], room=dict(room), room_id=room_id)

@app.route('/leave')
def leave(): session.clear(); return redirect(url_for('index'))

@socketio.on('connect')
def connected(auth=None):
    if not logged_in(): return False
    try: room_id = int(request.args.get('room', '1'))
    except (ValueError, TypeError): room_id = 1
    if not get_room(room_id): return False
    active_sids[request.sid] = {'name': session['name'], 'room_id': room_id}
    join_room(room_channel(room_id))
    emit('presence', {'people': sorted({x['name'] for x in active_sids.values() if x['room_id'] == room_id})}, to=room_channel(room_id))

@socketio.on('disconnect')
def disconnected(reason=None):
    old = active_sids.pop(request.sid, None)
    room_ids = {x['room_id'] for x in active_sids.values()}
    if old: room_ids.add(old['room_id'])
    for rid in room_ids:
        emit('presence', {'people': sorted({x['name'] for x in active_sids.values() if x['room_id'] == rid})}, to=room_channel(rid))

@socketio.on('send_message')
def send_message(data):
    if not logged_in(): return
    data = data or {}; body = (data.get('body') or '').strip()[:4000]
    if not body: return
    try: room_id = int(data.get('room_id', 1))
    except (ValueError, TypeError): return
    if not get_room(room_id): return
    try: reply_to = int(data.get('reply_to')) if data.get('reply_to') else None
    except (ValueError, TypeError): reply_to = None
    created = timestamp()
    with db() as con:
        if reply_to and not con.execute('SELECT id FROM messages WHERE id=? AND room_id=?', (reply_to, room_id)).fetchone(): reply_to = None
        cur = con.execute('INSERT INTO messages(name,body,created_at,reply_to,room_id) VALUES(?,?,?,?,?)', (session['name'], body, created, reply_to, room_id))
        row = con.execute('SELECT m.*, p.name AS reply_name, p.body AS reply_body FROM messages m LEFT JOIN messages p ON p.id=m.reply_to WHERE m.id=?', (cur.lastrowid,)).fetchone()
    socketio.emit('new_message', message_payload(row), to=room_channel(room_id))
    if body.lower().startswith('@ai') and (len(body) == 3 or body[3].isspace() or body[3] in ':،,'):
        prompt = body[3:].lstrip(' :،,\t\n')[:3500] or 'کاربر فقط @ai فرستاده است؛ از او بخواه سؤالش را بنویسد.'
        try: answer = call_openai('تو دستیار هوش مصنوعی گروه دوستانه هم‌صحبت هستی. به زبان پیام کاربر و با لحنی دوستانه پاسخ بده.\nنام فرستنده: ' + session['name'] + '\nپیام: ' + prompt)
        except Exception as exc: answer = 'فعلاً نتونستم به هوش مصنوعی وصل بشم. ' + str(exc)
        socketio.emit('new_message', save_ai_message(answer, room_id), to=room_channel(room_id))

@app.route('/upload', methods=['POST'])
def upload():
    if not logged_in(): abort(403)
    f = request.files.get('file')
    if not f or not f.filename: return jsonify(error='فایلی انتخاب نشده.'), 400
    original = secure_filename(f.filename)
    if not original or '.' not in original or original.rsplit('.',1)[1].lower() not in ALLOWED_EXT: return jsonify(error='این نوع فایل مجاز نیست.'), 400
    try: room_id = int(request.form.get('room_id', '1'))
    except ValueError: return jsonify(error='گفت‌وگوی نامعتبر.'), 400
    if not get_room(room_id): return jsonify(error='این گفت‌وگو وجود ندارد.'), 404
    stored = secrets.token_hex(16) + '_' + original; f.save(UPLOAD_DIR / stored); created = timestamp()
    with db() as con:
        cur = con.execute('INSERT INTO messages(name,body,file_name,stored_name,created_at,room_id) VALUES(?,?,?,?,?,?)', (session['name'], '', original, stored, created, room_id))
        row = con.execute('SELECT * FROM messages WHERE id=?', (cur.lastrowid,)).fetchone()
    socketio.emit('new_message', message_payload(row), to=room_channel(room_id))
    return jsonify(ok=True)

@app.route('/file/<int:message_id>')
def download(message_id):
    if not logged_in(): abort(403)
    with db() as con: row = con.execute('SELECT file_name,stored_name FROM messages WHERE id=?', (message_id,)).fetchone()
    if not row or not row['stored_name']: abort(404)
    return send_from_directory(UPLOAD_DIR, row['stored_name'], as_attachment=True, download_name=row['file_name'])

@socketio.on('edit_message')
def edit_message(data):
    if not logged_in(): return
    try: mid = int((data or {}).get('id'))
    except (ValueError, TypeError): return
    body = ((data or {}).get('body') or '').strip()[:4000]
    if not body: return
    with db() as con:
        row = con.execute('SELECT * FROM messages WHERE id=?', (mid,)).fetchone()
        if not row or (row['name'] != session['name'] and not is_admin()) or not row['body']: return
        con.execute('UPDATE messages SET body=?, edited=1 WHERE id=?', (body, mid))
        updated = con.execute('SELECT m.*, p.name AS reply_name, p.body AS reply_body FROM messages m LEFT JOIN messages p ON p.id=m.reply_to WHERE m.id=?', (mid,)).fetchone()
    socketio.emit('message_updated', message_payload(updated), to=room_channel(row['room_id']))

@socketio.on('delete_message')
def delete_message(data):
    if not logged_in(): return
    try: mid = int((data or {}).get('id'))
    except (ValueError, TypeError): return
    with db() as con:
        row = con.execute('SELECT * FROM messages WHERE id=?', (mid,)).fetchone()
        if not row or (row['name'] != session['name'] and not is_admin()): return
        remove_message_file(row); con.execute('DELETE FROM messages WHERE id=?', (mid,)); con.execute('UPDATE messages SET reply_to=NULL WHERE reply_to=?', (mid,))
    socketio.emit('message_deleted', {'id': mid}, to=room_channel(row['room_id']))

@socketio.on('pin_message')
def pin_message(data):
    if not logged_in() or not is_admin(): return
    try: mid = int((data or {}).get('id'))
    except (ValueError, TypeError): return
    with db() as con:
        row = con.execute('SELECT id,room_id FROM messages WHERE id=?', (mid,)).fetchone()
        if not row: return
        con.execute('UPDATE messages SET pinned=0 WHERE room_id=?', (row['room_id'],)); con.execute('UPDATE messages SET pinned=1 WHERE id=?', (mid,))
        pinned = con.execute('SELECT id,name,body,file_name,created_at,pinned,room_id FROM messages WHERE id=?', (mid,)).fetchone()
    socketio.emit('pin_updated', dict(pinned), to=room_channel(row['room_id']))

@app.route('/admin')
def admin():
    if not is_admin(): abort(403)
    with db() as con:
        count = con.execute('SELECT COUNT(*) FROM messages').fetchone()[0]
        users = [dict(r) for r in con.execute('SELECT name,first_seen,is_admin,banned FROM members ORDER BY is_admin DESC,first_seen')]
        rooms = [dict(r) for r in con.execute('SELECT r.id,r.name,r.created_at,(SELECT COUNT(*) FROM messages m WHERE m.room_id=r.id) AS message_count FROM rooms r ORDER BY id')]
        recent = [dict(r) for r in con.execute('SELECT id,name,body,file_name,created_at,room_id FROM messages ORDER BY id DESC LIMIT 100')]
    return render_template('admin.html', name=session['name'], message_count=count, ai_ready=bool(get_setting('openai_api_key') or os.environ.get('OPENAI_API_KEY')), users=users, rooms=rooms, recent=recent)

@app.route('/admin/api-key', methods=['POST'])
def admin_api_key():
    if not is_admin(): abort(403)
    data = request.get_json(silent=True) or {}; key = (data.get('api_key') or '').strip()
    if key:
        set_setting('openai_api_key', key); return jsonify(ok=True, configured=True, message='کلید API ذخیره شد.')
    if data.get('clear'):
        set_setting('openai_api_key', ''); return jsonify(ok=True, configured=False, message='کلید ذخیره‌شده پاک شد.')
    return jsonify(error='کلید API را وارد کن.'), 400

@app.route('/admin/ai', methods=['POST'])
def admin_ai():
    if not is_admin(): abort(403)
    data = request.get_json(silent=True) or {}; prompt = (data.get('prompt') or '').strip()[:6000]
    if not prompt: return jsonify(error='پیام خالی است.'), 400
    try: return jsonify(answer=call_openai(prompt))
    except urllib.error.HTTPError as e: return jsonify(error=f'خطای API ({e.code}): {e.read().decode("utf-8", errors="replace")[:1000]}'), 502
    except Exception as e: return jsonify(error='ارتباط با سرویس هوش مصنوعی برقرار نشد: ' + str(e)), 502

@app.route('/admin/users', methods=['POST'])
def admin_add_user():
    if not is_admin(): abort(403)
    data = request.get_json(silent=True) or {}; name = clean_name(data.get('name')); password = data.get('password') or ''
    if not name or len(password) < 4 or len(password) > 128: return jsonify(error='نام معتبر و رمز حداقل ۴ نویسه‌ای وارد کن.'), 400
    if name == ADMIN_NAME: return jsonify(error='این نام مخصوص حساب ادمین است.'), 400
    try:
        with db() as con: con.execute('INSERT INTO members(name,password_hash,first_seen,is_admin,banned) VALUES(?,?,?,0,0)', (name,generate_password_hash(password),timestamp()))
    except sqlite3.IntegrityError: return jsonify(error='این نام کاربری قبلاً ثبت شده است.'), 409
    return jsonify(ok=True,message='کاربر ساخته شد. صفحه را تازه کن تا در فهرست دیده شود.')

@app.route('/admin/users/<path:username>/action', methods=['POST'])
def admin_user_action(username):
    if not is_admin(): abort(403)
    data = request.get_json(silent=True) or {}; action = data.get('action')
    if username == ADMIN_NAME: return jsonify(error='حساب اصلی ادمین قابل حذف یا مسدودکردن نیست.'), 400
    with db() as con:
        row = con.execute('SELECT * FROM members WHERE name=?', (username,)).fetchone()
        if not row: return jsonify(error='کاربر پیدا نشد.'), 404
        if action == 'ban': con.execute('UPDATE members SET banned=1 WHERE name=?', (username,))
        elif action == 'unban': con.execute('UPDATE members SET banned=0 WHERE name=?', (username,))
        elif action == 'delete': con.execute('DELETE FROM members WHERE name=?', (username,))
        elif action == 'reset_password':
            password = data.get('password') or ''
            if len(password) < 4 or len(password) > 128: return jsonify(error='رمز جدید باید حداقل ۴ نویسه باشد.'), 400
            con.execute('UPDATE members SET password_hash=? WHERE name=?', (generate_password_hash(password), username))
        else: return jsonify(error='عملیات نامعتبر است.'), 400
    if action == 'ban':
        for sid, info in list(active_sids.items()):
            if info['name'] == username: socketio.server.disconnect(sid)
    return jsonify(ok=True,message='عملیات انجام شد. صفحه را تازه کن.')

@app.route('/admin/rooms', methods=['POST'])
def admin_add_room():
    if not is_admin(): abort(403)
    data = request.get_json(silent=True) or {}; name = re.sub(r'\s+',' ',(data.get('name') or '').strip())
    if len(name) < 2 or len(name) > 40: return jsonify(error='نام گفت‌وگو باید بین ۲ تا ۴۰ نویسه باشد.'), 400
    try:
        with db() as con: con.execute('INSERT INTO rooms(name,created_at) VALUES(?,?)', (name,timestamp()))
    except sqlite3.IntegrityError: return jsonify(error='گفت‌وگویی با این نام وجود دارد.'), 409
    return jsonify(ok=True,message='گفت‌وگو ساخته شد. صفحه را تازه کن.')

@app.route('/admin/rooms/<int:room_id>/delete', methods=['POST'])
def admin_delete_room(room_id):
    if not is_admin(): abort(403)
    with db() as con:
        row = con.execute('SELECT * FROM rooms WHERE id=?', (room_id,)).fetchone()
        if not row: return jsonify(error='گفت‌وگو پیدا نشد.'), 404
        count = con.execute('SELECT COUNT(*) FROM rooms').fetchone()[0]
        if count <= 1: return jsonify(error='باید دست‌کم یک گفت‌وگو باقی بماند.'), 400
        messages = con.execute('SELECT * FROM messages WHERE room_id=?', (room_id,)).fetchall()
        for msg in messages: remove_message_file(msg)
        con.execute('DELETE FROM messages WHERE room_id=?', (room_id,)); con.execute('DELETE FROM rooms WHERE id=?', (room_id,))
    return jsonify(ok=True,message='گفت‌وگو و پیام‌های داخل آن حذف شدند.')

@app.route('/admin/rooms/<int:room_id>/clear', methods=['POST'])
def admin_clear_room(room_id):
    if not is_admin(): abort(403)
    with db() as con:
        if not con.execute('SELECT id FROM rooms WHERE id=?',(room_id,)).fetchone(): return jsonify(error='گفت‌وگو پیدا نشد.'),404
        messages = con.execute('SELECT * FROM messages WHERE room_id=?',(room_id,)).fetchall()
        for msg in messages: remove_message_file(msg)
        con.execute('DELETE FROM messages WHERE room_id=?',(room_id,))
    socketio.emit('room_cleared', {'room_id':room_id}, to=room_channel(room_id))
    return jsonify(ok=True,message='پیام‌های گفت‌وگو پاک شدند.')

@app.route('/admin/messages/<int:message_id>/delete', methods=['POST'])
def admin_delete_message(message_id):
    if not is_admin(): abort(403)
    with db() as con:
        row = con.execute('SELECT * FROM messages WHERE id=?',(message_id,)).fetchone()
        if not row: return jsonify(error='پیام پیدا نشد.'),404
        remove_message_file(row); con.execute('DELETE FROM messages WHERE id=?',(message_id,)); con.execute('UPDATE messages SET reply_to=NULL WHERE reply_to=?',(message_id,))
    socketio.emit('message_deleted',{'id':message_id},to=room_channel(row['room_id']))
    return jsonify(ok=True,message='پیام حذف شد.')

@app.errorhandler(413)
def too_large(_): return 'حجم فایل باید کمتر از ۱۲ مگابایت باشد.', 413

init_db()
if __name__ == '__main__': socketio.run(app, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)

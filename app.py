"""Senim 39 — Telegram course + private teacher dashboard, Python 3.12 stdlib."""
import csv
from contextlib import contextmanager
import hashlib
import hmac
import html
import io
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LESSONS = json.loads((ROOT / 'lessons.json').read_text(encoding='utf-8'))
SCHOOL = 'Іле ауданы №39 гимназия'
FULL_SCHOOL = '«39 гимназия мектепке дейінгі шағын орталығымен» МКМ'
TZ = timezone(timedelta(hours=5))
MENU = [['📚 Сабақтар', '📍 Менің нәтижем'], ['👩‍🏫 Мұғалім кабинеті'], ['▶️ Жалғастыру', '👤 Профиль'], ['🔒 Құпиялық', '🆘 Қолдау']]
PRIVACY = ('Бұл — 6-сыныпқа арналған эмоциялық сауаттылық оқу практикумы. Диагноз қоймайды. '
 'Мұғалім сіз енгізген аты-жөнді, сыныпты, Telegram ID нөмірін және жіберген жауабыңызды көреді. '
 'Жеке сырды жазу міндетті емес: ойдан алынған кейіпкер туралы жауап беруге немесе /skip арқылы өткізуге болады. '
 'Фото/құжат жіберсеңіз, бөгде адамның жеке мәліметін қоспаңыз. Деректер осы оқу жылының жұмысы үшін '
 'серверде сақталады (әдепкі мерзім — 365 күн), Telegram да хабарламаларды өз жүйесінде өңдейді. '
 'Өз деректеріңізді /delete_me арқылы өшіре аласыз; мұғалімге бұрын жеткен Telegram хабарламалары бөлек қалады. '
 'Ботқа енгізген аты-жөніңізді мұғалім сынып тізімімен тексереді. Қатысу алдында мектептің келісім тәртібі түсіндірілуі тиіс.')
SUPPORT = ('Қиналсаңыз, жауапты тоқтатып, сенімді ересекпен, сынып жетекшісімен немесе мектеп психологымен сөйлесіңіз. '
 'Өзіңізге не басқа адамға қазір қауіп төніп тұрса, бот жауабын күтпей, қасыңыздағы ересектен немесе жергілікті жедел қызметтен көмек сұраңыз. '
 'Бұл арна үнемі бақыланбайды және шұғыл көмек қызметі емес.')

SCHEMA = '''
CREATE TABLE IF NOT EXISTS students(chat INTEGER PRIMARY KEY,name TEXT NOT NULL,class_name TEXT NOT NULL,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS states(chat INTEGER PRIMARY KEY,stage TEXT NOT NULL,payload TEXT NOT NULL DEFAULT '{}',updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS answers(id INTEGER PRIMARY KEY,chat INTEGER NOT NULL,lesson INTEGER NOT NULL,task INTEGER NOT NULL,attempt INTEGER NOT NULL,kind TEXT NOT NULL,body TEXT NOT NULL,file_id TEXT,message_id INTEGER,created REAL NOT NULL,feedback TEXT NOT NULL DEFAULT '',feedback_at REAL,UNIQUE(chat,lesson,task,attempt));
CREATE TABLE IF NOT EXISTS events(update_id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
CREATE TABLE IF NOT EXISTS outbox(id INTEGER PRIMARY KEY,method TEXT NOT NULL,payload TEXT NOT NULL,created REAL NOT NULL,owner INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY,expires REAL NOT NULL,csrf TEXT NOT NULL,teacher INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS teacher_links(token TEXT PRIMARY KEY,teacher INTEGER NOT NULL,expires REAL NOT NULL);
'''

def connect(path):
    db = sqlite3.connect(str(path), timeout=15)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA secure_delete=ON')
    db.executescript(SCHEMA)
    if 'teacher' not in {r[1] for r in db.execute('PRAGMA table_info(sessions)')}:
        db.execute('ALTER TABLE sessions ADD COLUMN teacher INTEGER NOT NULL DEFAULT 0')
        db.execute('DELETE FROM sessions')
    db.commit()
    return db


@contextmanager
def opened(path):
    db=connect(path)
    try:
        with db:
            yield db
    finally:
        db.close()


def stamp(t):
    return datetime.fromtimestamp(t, TZ).strftime('%d.%m.%Y %H:%M')


def secure_equal(a,b):
    return hmac.compare_digest(str(a).encode('utf-8'),str(b).encode('utf-8'))


def scrub_csv(value):
    value = str(value if value is not None else '')
    return "'" + value if value.lstrip().startswith(('=', '+', '-', '@')) else value


class Telegram:
    def __init__(self, token):
        self.token = token

    def __call__(self, method, **payload):
        # Never put tokens or full urllib exceptions into logs.
        req = urllib.request.Request('https://api.telegram.org/bot' + self.token + '/' + method,
            data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                data = json.load(r)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f'Telegram HTTP {exc.code}') from None
        except (OSError, ValueError):
            raise RuntimeError('Telegram байланыс қатесі') from None
        if not data.get('ok'):
            raise RuntimeError('Telegram сұрауы қабылданбады')
        return data['result']

    def upload(self, method, chat, path, field='photo', caption=''):
        boundary = 'Senim' + secrets.token_hex(12)
        parts = []
        for k, v in {'chat_id': str(chat), 'caption': caption, 'protect_content': 'true'}.items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
        content_type = 'image/png' if path.suffix == '.png' else 'application/octet-stream'
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{path.name}"\r\nContent-Type: {content_type}\r\n\r\n'.encode() + path.read_bytes() + b'\r\n')
        parts.append(f'--{boundary}--\r\n'.encode())
        req = urllib.request.Request('https://api.telegram.org/bot' + self.token + '/' + method,
            data=b''.join(parts), headers={'Content-Type': 'multipart/form-data; boundary=' + boundary})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                result = json.load(r)
        except (OSError, ValueError):
            raise RuntimeError('Сурет жіберілмеді') from None
        if not result.get('ok'):
            raise RuntimeError('Сурет қабылданбады')
        return result['result']


class Course:
    def __init__(self, db, api, admins, join_code, base_url=''):
        self.db, self.api, self.admins = db, api, set(admins)
        self.join_code, self.base_url = join_code, base_url

    def enqueue(self, method, **payload):
        self.db.execute('INSERT INTO outbox(method,payload,created,owner) VALUES (?,?,?,?)',
                        (method, json.dumps(payload, ensure_ascii=False), time.time(), getattr(self,'owner',0)))

    def send(self, chat, text, keyboard=None, inline=None):
        for offset in range(0, len(text), 3600):
            markup = {'inline_keyboard': inline} if inline is not None else {'keyboard': keyboard or MENU, 'resize_keyboard': True}
            self.enqueue('sendMessage', chat_id=chat, text=text[offset:offset+3600], reply_markup=markup, protect_content=True)

    def state(self, chat, stage, payload=None):
        self.db.execute('INSERT OR REPLACE INTO states VALUES (?,?,?,?)', (chat, stage, json.dumps(payload or {}), time.time()))

    def get_state(self, chat):
        r = self.db.execute('SELECT stage,payload FROM states WHERE chat=?', (chat,)).fetchone()
        return (r['stage'], json.loads(r['payload'])) if r else ('', {})

    def profile(self, chat):
        return self.db.execute('SELECT * FROM students WHERE chat=?', (chat,)).fetchone()

    def process(self, update):
        """Persist response, next state, offset and notification together before sending."""
        uid = update['update_id']
        msg=update.get('message') or update.get('callback_query',{}).get('message',{})
        self.owner=msg.get('chat',{}).get('id',0)
        try:
            with self.db:
                if self.db.execute('SELECT 1 FROM events WHERE update_id=?', (uid,)).fetchone():
                    return
                self.handle(update)
                self.db.execute('INSERT INTO events VALUES (?)', (uid,))
                self.db.execute("INSERT OR REPLACE INTO meta VALUES ('offset',?)", (str(uid + 1),))
        except Exception:
            self.db.rollback()
            raise

    def flush(self):
        for _ in range(30):
            item = self.db.execute('SELECT * FROM outbox ORDER BY id LIMIT 1').fetchone()
            if not item:
                return
            payload = json.loads(item['payload'])
            try:
                if item['method'] == '_localphoto':
                    self.api.upload('sendPhoto', payload['chat_id'], ROOT / payload['path'], caption=payload.get('caption', ''))
                else:
                    self.api(item['method'], **payload)
            except RuntimeError as exc:
                # A deleted/blocked destination must not block delivery to everyone else.
                if 'HTTP 400' in str(exc) or 'HTTP 403' in str(exc):
                    with self.db:
                        self.db.execute('DELETE FROM outbox WHERE id=?', (item['id'],))
                    print('Хабарлама жеткізілмеді: чат қолжетімсіз. Жауап кабинетте сақталған.', flush=True)
                    continue
                break
            with self.db:
                self.db.execute('DELETE FROM outbox WHERE id=?', (item['id'],))

    def lesson_menu(self, chat, page=0):
        page = max(0, min(4, page))
        rows = [[{'text': l['title'], 'callback_data': f'lesson:{l["number"]}'}] for l in LESSONS[page*8:(page+1)*8]]
        nav = []
        if page: nav.append({'text': '← Артқа', 'callback_data': f'page:{page-1}'})
        if page < 4: nav.append({'text': 'Келесі →', 'callback_data': f'page:{page+1}'})
        if nav: rows.append(nav)
        self.send(chat, f'📚 ЭМОЦИЯЛАР ӘЛЕМІ\n{SCHOOL}\n\n34 сабақ • 136 тапсырма\nСабақты таңдаңыз. {page+1}/5 бет', inline=rows)

    def show_task(self, chat, n, t):
        lesson = LESSONS[n-1]
        if t >= len(lesson['tasks']):
            self.state(chat, '')
            self.send(chat, f'✨ {n}-сабақ аяқталды!\nЖауаптарыңыз мұғалімге жіберілді. Мұғалім пікір қалдырса, боттан аласыз.\n\nКелесі сабақты «📚 Сабақтар» мәзірінен таңдаңыз.')
            return
        task = lesson['tasks'][t]
        if t==0:
            for image in lesson['images']:self.enqueue('_localphoto',chat_id=chat,path=image,caption=lesson['title'])
            if lesson.get('visual_notes'):self.send(chat,'\n'.join(lesson['visual_notes']))
        self.state(chat, 'answer', {'lesson': n, 'task': t})
        self.send(chat, f'✦ {lesson["title"]}\n━━━━━━━━━━━━\n🧩 {t+1}/4 · {task["stage"]}\n\n{task["prompt"]}\n\n✍️ Жауапты мәтін, сурет, дауыс немесе құжат түрінде жіберіңіз.\n/sheet — суретті парақ\n/criteria — тексеру өлшемдері\n/skip — өткізу\n/cancel — мәзір', keyboard=[['📄 Суретті парақ', '💡 Өлшемдер'], ['⏭ Өткізу', '🏠 Мәзір']])

    def next_pending(self, chat, n):
        done = {r[0] for r in self.db.execute('SELECT DISTINCT task FROM answers WHERE chat=? AND lesson=?', (chat,n))}
        return next((t for t in range(4) if t not in done),4)

    def handle(self, update):
        callback = update.get('callback_query')
        if callback:
            msg = callback.get('message', {})
            if msg.get('chat', {}).get('type') != 'private': return
            chat = msg['chat']['id']
            if callback['from']['id'] != chat: return
            self.enqueue('answerCallbackQuery', callback_query_id=callback['id'])
            if not self.profile(chat):
                self.send(chat, 'Алдымен /start арқылы тіркеліңіз.');return
            value = callback.get('data','')
            if value.startswith('page:'):
                try: self.lesson_menu(chat,int(value.split(':')[1]))
                except ValueError: pass
            elif value.startswith('lesson:'):
                try: n=int(value.split(':')[1])
                except ValueError:return
                if n not in range(1,35):return
                lesson=LESSONS[n-1]
                self.send(chat, f'🌿 {lesson["title"]}\n\n{lesson["section"]}\n\n🎯 {lesson["goal"]}\n\nЖұптық және топтық әрекетті сыныпта орындап, өз қорытындыңызды жеке жіберіңіз. Үйде орындасаңыз, кейіпкерге сүйенген нұсқа қолдануға болады.')
                t=self.next_pending(chat,n)
                if t==4:
                    self.state(chat,'confirm_repeat',{'lesson':n})
                    self.send(chat,'Бұл сабақ орындалған. Жаңа әрекет бастау үшін /repeat жіберіңіз. Бұрынғы жауаптар мұғалімде сақталады.')
                else:self.show_task(chat,n,t)
            return
        m=update.get('message',{})
        if m.get('chat',{}).get('type')!='private':return
        chat=m['chat']['id'];text=m.get('text','').strip();stage,payload=self.get_state(chat)
        if text=='/id':self.send(chat,f'Сіздің Telegram ID: {chat}');return
        if text in ['/privacy','🔒 Құпиялық']:self.send(chat,PRIVACY);return
        if text in ['/help','🆘 Қолдау']:self.send(chat,SUPPORT);return
        if text in ['/teacher','👩‍🏫 Мұғалім кабинеті']:
            if chat not in self.admins:
                self.send(chat,'Бұл бөлім тек жауапты мұғалімнің Telegram аккаунтына ашылады.');return
            if not self.base_url.startswith('https://'):
                self.send(chat,'Сервердегі BASE_URL өрісіне сайттың https:// сілтемесін енгізіңіз. /inbox арқылы жауаптарды қазір де көре аласыз.');return
            secret=secrets.token_urlsafe(32)
            self.db.execute('DELETE FROM teacher_links WHERE teacher=? OR expires<?',(chat,time.time()))
            self.db.execute('INSERT INTO teacher_links VALUES (?,?,?)',(hashlib.sha256(secret.encode()).hexdigest(),chat,time.time()+300))
            self.send(chat,'👩‍🏫 Мұғалім кабинеті\n\nҚұпиясөз қажет емес. Төмендегі жеке батырмамен кіріңіз. Сілтеме 5 минут жарамды, бір рет қолданылады. Оны басқаға жібермеңіз.',inline=[[{'text':'Кабинетті ашу ↗','url':self.base_url+'/teacher/entry?token='+secret}]])
            return
        if text.startswith(('/inbox','/feedback','/export','/roster')):
            if chat not in self.admins:self.send(chat,'Бұл команда тек мұғалімге қолжетімді.');return
            if text.startswith('/feedback'):
                bits=text.split(maxsplit=2)
                if len(bits)!=3 or not bits[1].isdigit():self.send(chat,'Үлгі: /feedback 12 Жауабыңдағы мысалды нақтыла.');return
                a=self.db.execute('SELECT * FROM answers WHERE id=?',(int(bits[1]),)).fetchone()
                if not a:self.send(chat,'Жауап табылмады.');return
                feedback=bits[2][:2500]
                self.db.execute('UPDATE answers SET feedback=?,feedback_at=? WHERE id=?',(feedback,time.time(),a['id']))
                self.send(a['chat'],f'💬 Мұғалім пікірі\n{a["lesson"]}-сабақ · {a["task"]+1}-тапсырма\n\n{feedback}')
                self.send(chat,'Пікір оқушыға жіберу кезегіне қосылды.');return
            if text.startswith('/export'):self.send(chat,self.base_url+'/teacher/export.csv — кабинетке кіріп жүктеңіз.');return
            if text.startswith('/roster'):
                rows=self.db.execute('SELECT name,class_name,chat FROM students ORDER BY class_name,name').fetchall()
                self.send(chat,'👥 Оқушылар\n'+'\n'.join(f'{r["name"]} · {r["class_name"]} · ID {r["chat"]}' for r in rows) if rows else 'Оқушылар әлі тіркелмеген.');return
            rows=self.db.execute('SELECT a.*,s.name,s.class_name FROM answers a JOIN students s ON s.chat=a.chat ORDER BY a.id DESC LIMIT 10').fetchall()
            if not rows:self.send(chat,'Жауаптар әлі жоқ.')
            for a in rows:self.send(chat,self.answer_text(a))
            return
        if text=='/delete_me':
            self.state(chat,'delete_confirm');self.send(chat,'Сервердегі профиліңіз бен барлық жауабыңыз өшіріледі. Растау үшін /confirm_delete жіберіңіз. Болдырмау: /cancel.');return
        if text=='/confirm_delete' and stage=='delete_confirm':
            self.db.execute('DELETE FROM answers WHERE chat=?',(chat,));self.db.execute('DELETE FROM students WHERE chat=?',(chat,));self.db.execute('DELETE FROM states WHERE chat=?',(chat,))
            # Clear pending answer notifications, including any queued copy of student media.
            self.db.execute('DELETE FROM outbox WHERE owner=?',(chat,))
            self.send(chat,'Сервердегі деректеріңіз өшірілді. Telegram-дағы бұрын жеткен көшірмелерді өшіру үшін мұғалімге жүгініңіз.');return
        if text=='/start':
            if self.profile(chat):self.state(chat,'');self.send(chat,f'🌿 СЕНІМ 39\n{SCHOOL}\n\nЭмоцияңды таны. Ойыңды жеткіз. Өзіңді дамыт.\n\n📚 34 сабақ · 🧩 136 тапсырма\nЖалғастыру үшін мәзірді таңдаңыз.');return
            self.state(chat,'consent');self.send(chat,'🌿 СЕНІМ 39\n'+SCHOOL+'\n\n'+PRIVACY,keyboard=[['✅ Түсіндім, жалғастыру'],['🆘 Қолдау']]);return
        if text in ['/cancel','🏠 Мәзір']:
            self.state(chat,'');self.send(chat,'🏠 Басты мәзір. Тапсырманы кейін сол жерден жалғастыра аласыз.');return
        if not self.profile(chat):
            if stage in ['consent','join'] and text=='✅ Түсіндім, жалғастыру':
                self.state(chat,'name');self.send(chat,'👤 Аты-жөніңізді толық жазыңыз. Мұғалім сізді осы есіммен көреді. Код қажет емес.',keyboard=[['🏠 Мәзір']]);return
            if stage=='join':
                self.state(chat,'name');self.send(chat,'Код енді қажет емес. Аты-жөніңізді толық жазыңыз.');return
            if stage=='name':
                if not 4<=len(text)<=100 or len(text.split())<2:self.send(chat,'Аты-жөніңізді кемінде екі сөзбен, 100 таңбадан асырмай жазыңыз.');return
                self.state(chat,'class',{'name':text});self.send(chat,'🏫 Сыныбыңызды жазыңыз. Мысалы: 6А немесе 6 «Ә».');return
            if stage=='class':
                if not re.fullmatch(r'6\s*[«"\']?\s*[A-Za-zА-Яа-яӘәҒғҚқҢңӨөҰұҮүҺһІі]?\s*[»"\']?',text):self.send(chat,'Бұл курс 6-сыныпқа арналған. Үлгі: 6А, 6Ә немесе 6.');return
                self.db.execute('INSERT INTO students VALUES (?,?,?,?)',(chat,payload['name'],text,time.time()));self.state(chat,'')
                self.send(chat,f'✨ Қош келдіңіз, {payload["name"]}!\nСынып: {text}\n\nМұғалім сіздің аты-жөніңіз бен жауаптарыңызды көреді. Сабақты мәзірден таңдаңыз.');return
            self.send(chat,'Курсты бастау үшін /start жіберіңіз.');return
        if text in ['📚 Сабақтар','/lessons']:self.lesson_menu(chat);return
        if text in ['👤 Профиль','/profile']:
            p=self.profile(chat);self.send(chat,f'👤 {p["name"]}\n🏫 {p["class_name"]}\n{SCHOOL}\n\nАты-жөніңіз қате болса, мұғалімге хабарласыңыз.');return
        if text in ['📍 Менің нәтижем','/progress']:
            count=self.db.execute('SELECT COUNT(*) FROM (SELECT DISTINCT lesson,task FROM answers WHERE chat=?)',(chat,)).fetchone()[0]
            skipped=self.db.execute("SELECT COUNT(*) FROM answers a WHERE chat=? AND kind='skip' AND id=(SELECT MAX(id) FROM answers b WHERE b.chat=a.chat AND b.lesson=a.lesson AND b.task=a.task)",(chat,)).fetchone()[0]
            self.send(chat,f'📍 Оқу жолым\n\n{count}/136 тапсырма қаралды\nОның ішінде {skipped} тапсырма өткізіліп жіберілген.\nБұл көрсеткіш баға немесе психологиялық қорытынды емес.');return
        if text in ['▶️ Жалғастыру','/continue']:
            for n in range(1,35):
                t=self.next_pending(chat,n)
                if t<4:self.show_task(chat,n,t);return
            self.send(chat,'✨ Барлық 34 сабақ қаралды. Қайталау үшін сабақты таңдаңыз.');return
        if text=='/repeat' and stage=='confirm_repeat':self.show_task(chat,payload['lesson'],0);return
        if stage=='answer':
            n,t=payload['lesson'],payload['task'];lesson=LESSONS[n-1];task=lesson['tasks'][t]
            if text in ['/sheet','📄 Суретті парақ']:
                for path in lesson['images']:self.enqueue('_localphoto',chat_id=chat,path=path,caption=lesson['title'])
                self.send(chat,'Суретті парақты қағазда орындап, фотосын немесе мәтіндік қорытындысын жіберіңіз.');return
            if text in ['/criteria','💡 Өлшемдер']:
                bullets=re.findall(r'•\s*(.*?)\s*[—–-]\s*1\s*;',task['rubric'])
                self.send(chat,'💡 Тексеру өлшемдері\n'+'\n'.join('• '+b for b in bullets)+'\n\nМұғалім жауапқа мазмұндық пікір береді. Балл автоматты қойылмайды.');return
            kind='text';body=text;file_id=None
            if text in ['/skip','⏭ Өткізу']:kind='skip';body='Оқушы тапсырманы өткізіп жіберуді таңдады.'
            elif text.startswith('/'):
                self.send(chat,'Бұл команда табылмады. /cancel — мәзір; /skip — өткізу.');return
            elif m.get('photo'):kind='photo';file_id=m['photo'][-1]['file_id'];body=m.get('caption','')
            elif m.get('document'):kind='document';file_id=m['document']['file_id'];body=m.get('caption','')
            elif m.get('voice'):kind='voice';file_id=m['voice']['file_id'];body=m.get('caption','')
            elif not body:self.send(chat,'Мәтін, фото, дауыс не құжат жіберіңіз.');return
            if len(body)>3000:self.send(chat,'Жауапты 3000 таңбаға дейін қысқартыңыз.');return
            attempt=self.db.execute('SELECT COALESCE(MAX(attempt),0)+1 FROM answers WHERE chat=? AND lesson=? AND task=?',(chat,n,t)).fetchone()[0]
            cursor=self.db.execute('INSERT INTO answers(chat,lesson,task,attempt,kind,body,file_id,message_id,created) VALUES (?,?,?,?,?,?,?,?,?)',(chat,n,t,attempt,kind,body,file_id,m.get('message_id'),time.time()))
            a=self.db.execute('SELECT a.*,s.name,s.class_name FROM answers a JOIN students s ON s.chat=a.chat WHERE a.id=?',(cursor.lastrowid,)).fetchone()
            for admin in self.admins:
                self.send(admin,self.answer_text(a))
                if file_id:
                    method={'photo':'sendPhoto','document':'sendDocument','voice':'sendVoice'}[kind]
                    self.enqueue(method,chat_id=admin,**{kind:file_id},caption=f'Жауап №{a["id"]} · {a["name"]} · {a["class_name"]}',protect_content=True)
            self.send(chat,f'✅ Жауап №{cursor.lastrowid} сақталды. Мұғалімге жеткізу кезегіне қосылды.')
            self.show_task(chat,n,t+1);return
        self.send(chat,'Мәзірден сабақты таңдаңыз немесе /continue арқылы жалғастырыңыз.')

    @staticmethod
    def answer_text(a):
        return (f'📩 Жауап №{a["id"]}\n👤 {a["name"]} · {a["class_name"]}\n🆔 {a["chat"]}\n'
                f'📚 {a["lesson"]}-сабақ · {a["task"]+1}-тапсырма · {a["attempt"]}-әрекет\n🕒 {stamp(a["created"])}\n'
                f'Түрі: {a["kind"]}\n\n{a["body"] or "Тіркеме жіберілген"}\n\n'
                f'Пікір: /feedback {a["id"]} пікіріңіз')


def run_poll(path,api,admins,join,base):
    db=connect(path);course=Course(db,api,admins,join,base)
    try:
        api('deleteWebhook',drop_pending_updates=False)
        api('setMyName',name='Сенім 39 | Эмоциялар әлемі')
        api('setMyDescription',description='Іле ауданы №39 гимназия. 6-сыныпқа арналған 34 сабақ, 136 тапсырма. Мәтін, сурет және дауыс арқылы жауап беріңіз. Мұғалім кері байланыс береді.')
        api('setMyShortDescription',short_description='№39 гимназия • 34 сабақ • Эмоциялық сауаттылық')
        api('setMyCommands',commands=[{'command':'start','description':'Бастау және тіркелу'},{'command':'lessons','description':'34 сабақ'},{'command':'continue','description':'Жалғастыру'},{'command':'progress','description':'Менің нәтижем'},{'command':'privacy','description':'Құпиялық'},{'command':'help','description':'Қолдау'},{'command':'teacher','description':'Мұғалім бөлімі'},{'command':'id','description':'Telegram ID'}])
    except RuntimeError:
        print('Telegram баптауын тексеріңіз. Токенді журналға жазбаңыз.',flush=True)
    while True:
        try:
            course.flush()
            r=db.execute("SELECT value FROM meta WHERE key='offset'").fetchone()
            updates=api('getUpdates',offset=int(r[0]) if r else 0,timeout=20,allowed_updates=['message','callback_query'])
            for u in updates:course.process(u)
            course.flush()
            with db:
                cutoff=time.time()-int(os.getenv('RETENTION_DAYS','365'))*86400
                db.execute('DELETE FROM outbox WHERE created<?',(cutoff,))
                db.execute('DELETE FROM answers WHERE created<?',(cutoff,))
                db.execute('DELETE FROM students WHERE created<? AND chat NOT IN (SELECT chat FROM answers)',(cutoff,))
                db.execute('DELETE FROM sessions WHERE expires<?',(time.time(),))
                db.execute('DELETE FROM teacher_links WHERE expires<?',(time.time(),))
                db.execute('DELETE FROM states WHERE updated<?',(time.time()-86400,))
                db.execute('DELETE FROM events WHERE update_id < (SELECT COALESCE(MAX(update_id),0)-10000 FROM events)')
        except Exception:
            print('Telegram байланысы уақытша үзілді. Қайта қосылуда.',flush=True)
            time.sleep(5)


def make_handler(db_path,admins,secure=True):
    admins=set(admins)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def reply(self,status,body,content_type='text/html; charset=utf-8',headers=None):
            data=body.encode() if isinstance(body,str) else body
            self.send_response(status);self.send_header('Content-Type',content_type)
            self.send_header('Content-Length',str(len(data)));self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff');self.send_header('X-Frame-Options','DENY')
            self.send_header('Referrer-Policy','no-referrer')
            self.send_header('Content-Security-Policy',"default-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; form-action 'self'")
            for k,v in (headers or {}).items():self.send_header(k,v)
            self.end_headers();self.wfile.write(data)
        def session(self,db):
            cookie=SimpleCookie()
            try:cookie.load(self.headers.get('Cookie',''))
            except Exception:return None
            token=cookie.get('senim_session')
            if not token:return None
            row=db.execute('SELECT * FROM sessions WHERE token=? AND expires>?',(hashlib.sha256(token.value.encode()).hexdigest(),time.time())).fetchone()
            return row if row and row['teacher'] in admins else None

        def redirect(self,path,headers=None):self.reply(303,'',headers={'Location':path,**(headers or {})})
        def page(self,title,body):
            return (ROOT/'templates/layout.html').read_text(encoding='utf-8').replace('{{title}}',html.escape(title)).replace('{{body}}',body)
        def do_GET(self):
            url=urllib.parse.urlparse(self.path);path=url.path
            if path=='/health':self.reply(200,'ok','text/plain');return
            if path in ['/style.css','/avatar.png']:
                f=ROOT/('static/style.css' if path.endswith('css') else 'assets/avatar.png')
                if not f.exists():self.reply(404,'Файл табылмады');return
                self.reply(200,f.read_bytes(),'text/css' if path.endswith('css') else 'image/png');return
            if path=='/':
                self.reply(200,self.page('Сенім 39', '<section class="hero"><span class="eyebrow">ІЛЕ АУДАНЫ №39 ГИМНАЗИЯ</span><h1>Сезіміңді таны.<br>Өзіңді дамыт.</h1><p>6-сыныпқа арналған эмоциялық сауаттылық курсы</p><div class="pills"><span>34 сабақ</span><span>136 тапсырма</span><span>Мұғалім қолдауы</span></div><a class="button" href="/teacher">Мұғалім кабинетіне кіру →</a></section><section class="features"><article><b>01 · Таны</b><p>Эмоциялар мен күшті жақтарыңды зертте.</p></article><article><b>02 · Орында</b><p>Мәтін, сурет, дауыс арқылы ойыңды жеткіз.</p></article><article><b>03 · Дамы</b><p>Мұғалім пікірін оқып, жауабыңды толықтыр.</p></article></section>'));return
            if path=='/login':
                self.reply(200,self.page('Кабинетке кіру','<section class="login"><span class="eyebrow">МҰҒАЛІМГЕ АРНАЛҒАН</span><h1>Telegram арқылы кіріңіз</h1><p>Боттағы «👩‍🏫 Мұғалім кабинеті» батырмасын басыңыз немесе /teacher жіберіңіз. Бот сізді Telegram аккаунтыңыз арқылы таниды. Құпиясөз теру қажет емес.</p></section>'));return
            if path=='/teacher/entry':
                secret=urllib.parse.parse_qs(url.query).get('token',[''])[0]
                with opened(db_path) as db:
                    row=db.execute('SELECT * FROM teacher_links WHERE token=? AND expires>?',(hashlib.sha256(secret.encode()).hexdigest(),time.time())).fetchone()
                    if not row or row['teacher'] not in admins:
                        self.reply(403,self.page('Сілтеме ескірген','<section class="login"><h1>Жаңа сілтеме алыңыз</h1><p>Ботта /teacher жіберіп, жаңа батырмамен кіріңіз.</p></section>'));return
                self.reply(200,self.page('Мұғалім кабинеті','<section class="login"><span class="eyebrow">№39 ГИМНАЗИЯ</span><h1>Кабинетіңіз дайын</h1><p>Код немесе құпиясөз қажет емес. Батырманы басыңыз.</p><form action="/login" method="post"><input type="hidden" name="token" value="'+html.escape(secret,quote=True)+'"><button>Кабинетті ашу →</button></form></section>'));return
            if path.startswith('/teacher'):
                with opened(db_path) as db:
                    session=self.session(db)
                    if not session:self.redirect('/login');return
                    if path=='/teacher/export.csv':
                        rows=db.execute('SELECT s.name,s.class_name,a.* FROM answers a JOIN students s ON s.chat=a.chat ORDER BY a.id').fetchall()
                        buf=io.StringIO();writer=csv.writer(buf);writer.writerow(['Жауап ID','Аты-жөні','Сынып','Telegram ID','Сабақ','Тапсырма','Әрекет','Түрі','Жауап','Тіркеме file_id','Уақыт','Мұғалім пікірі'])
                        for a in rows:writer.writerow([scrub_csv(x) for x in [a['id'],a['name'],a['class_name'],a['chat'],a['lesson'],a['task']+1,a['attempt'],a['kind'],a['body'],a['file_id'],stamp(a['created']),a['feedback']]])
                        self.reply(200,('\ufeff'+buf.getvalue()).encode('utf-8'),'text/csv; charset=utf-8',{'Content-Disposition':'attachment; filename="Senim39_answers.csv"'});return
                    params=urllib.parse.parse_qs(url.query)
                    q=params.get('q',[''])[0][:100];grade=params.get('class',[''])[0][:20];lesson=params.get('lesson',[''])[0]
                    where=['1=1'];args=[]
                    if q:where.append('(s.name LIKE ? OR CAST(a.chat AS TEXT)=?)');args.extend(['%'+q+'%',q])
                    if grade:where.append('s.class_name=?');args.append(grade)
                    if lesson.isdigit() and int(lesson) in range(1,35):where.append('a.lesson=?');args.append(int(lesson))
                    condition=' AND '.join(where)
                    offset=max(0,int(params.get('offset',['0'])[0])) if params.get('offset',['0'])[0].isdigit() else 0
                    rows=db.execute('SELECT a.*,s.name,s.class_name FROM answers a JOIN students s ON s.chat=a.chat WHERE '+condition+' ORDER BY a.id DESC LIMIT 50 OFFSET ?',args+[offset]).fetchall()
                    stats=[db.execute('SELECT COUNT(*) FROM students').fetchone()[0], db.execute('SELECT COUNT(*) FROM answers').fetchone()[0],db.execute("SELECT COUNT(*) FROM answers WHERE feedback='' AND kind!='skip'").fetchone()[0]]
                    classes=[r[0] for r in db.execute('SELECT DISTINCT class_name FROM students ORDER BY class_name')]
                    esc=lambda v:html.escape(str(v),quote=True)
                    options=''.join('<option value="'+esc(c)+'"'+(' selected' if c==grade else '')+'>'+esc(c)+'</option>' for c in classes)
                    loptions=''.join(f'<option value="{l["number"]}"'+(' selected' if str(l['number'])==lesson else '')+'>'+esc(l['title'])+'</option>' for l in LESSONS)
                    body=f'<div class="pagehead"><div><span class="eyebrow">МҰҒАЛІМ КАБИНЕТІ</span><h1>Әр жауаптың артында — оқушы.</h1><p>Іле ауданы №39 гимназия · Эмоциялар әлемі · 6-сынып</p></div><a class="button secondary" href="/teacher/export.csv">CSV жүктеу ↓</a></div><div class="stats"><article><span>Тіркелген оқушы</span><strong>{stats[0]}</strong></article><article><span>Жауаптар мен өткізу белгілері</span><strong>{stats[1]}</strong></article><article><span>Пікір күтіп тұр</span><strong>{stats[2]}</strong></article></div>'
                    body+=f'<form class="filters" method="get" action="/teacher"><label>Оқушыны іздеу<input name="q" value="{esc(q)}" placeholder="Аты-жөні немесе Telegram ID"></label><label>Сынып<select name="class"><option value="">Барлық сынып</option>{options}</select></label><label>Сабақ<select name="lesson"><option value="">Барлық сабақ</option>{loptions}</select></label><button>Іздеу</button></form>'
                    body+='<section class="answer-list">'
                    if not rows:body+='<article class="empty"><h2>Бұл жерде жауаптар көрінеді</h2><p>Оқушы ботта тіркеліп, тапсырма жібергенде аты-жөнімен бірге осы тізімге түседі.</p></article>'
                    for a in rows:
                        task=LESSONS[a['lesson']-1]['tasks'][a['task']]
                        tag='Өткізілген' if a['kind']=='skip' else ('Пікір берілді' if a['feedback'] else 'Жаңа жауап')
                        media='<p class="attachment">📎 '+esc({'photo':'Фото','voice':'Дауыстық жауап','document':'Құжат'}.get(a['kind'],a['kind']))+' · Тіркеме мұғалімнің Telegram чатына жеткізіледі. file_id CSV есебінде сақталады.</p>' if a['file_id'] else ''
                        body+=f'<article class="answer"><div class="answer-head"><div><h2>{esc(a["name"])}</h2><span>{esc(a["class_name"])} · ID {a["chat"]} · {stamp(a["created"])}</span></div><span class="badge">{tag}</span></div><h3>{esc(LESSONS[a["lesson"]-1]["title"])}</h3><details><summary>{a["task"]+1}-тапсырма · Шартын көру</summary><p>{esc(task["prompt"])}</p><p>{esc(task["rubric"])}</p></details><div class="response">{esc(a["body"] or "Тіркеме жіберілген")}</div>{media}<div class="meta">Жауап №{a["id"]} · {a["attempt"]}-әрекет</div><form method="post" action="/teacher/feedback"><input type="hidden" name="csrf" value="{esc(session["csrf"])}"><input type="hidden" name="answer_id" value="{a["id"]}"><label>Мұғалімнің кері байланысы<textarea name="feedback" required maxlength="2500" placeholder="Нақты қолдау мен келесі қадамды жазыңыз">{esc(a["feedback"])}</textarea></label><button>Оқушыға жіберу ↗</button></form></article>'
                    body+='</section>'
                    more=urllib.parse.urlencode({'q':q,'class':grade,'lesson':lesson,'offset':offset+50})
                    if len(rows)==50:body+=f'<a class="button secondary" href="/teacher?{esc(more)}">Алдыңғы 50 жауап →</a>'
                    body+=f'<form class="logout" method="post" action="/logout"><input type="hidden" name="csrf" value="{esc(session["csrf"])}"><button class="secondary">Кабинеттен шығу</button></form>'
                    self.reply(200,self.page('Мұғалім кабинеті',body));return
            self.reply(404,'Бет табылмады')
        def do_POST(self):
            try:length=int(self.headers.get('Content-Length','0'))
            except ValueError:self.reply(400,'Сұрау қате');return
            if not 0<length<=16000:self.reply(413,'Сұрау тым үлкен');return
            body=urllib.parse.parse_qs(self.rfile.read(length).decode('utf-8',errors='replace'))
            val=lambda k:body.get(k,[''])[0]
            if self.path=='/login':
                secret=val('token')
                hashed=hashlib.sha256(secret.encode()).hexdigest()
                with opened(db_path) as db:
                    db.execute('BEGIN IMMEDIATE')
                    link=db.execute('SELECT * FROM teacher_links WHERE token=? AND expires>?',(hashed,time.time())).fetchone()
                    if not link or link['teacher'] not in admins:
                        self.reply(403,'Сілтеме ескірген немесе қолданылған. Ботта /teacher жіберіңіз.');return
                    db.execute('DELETE FROM teacher_links WHERE token=?',(hashed,))
                    token=secrets.token_urlsafe(32)
                    db.execute('INSERT INTO sessions VALUES (?,?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),time.time()+8*3600,secrets.token_urlsafe(24),link['teacher']))
                cookie='senim_session='+token+'; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800'+('; Secure' if secure else '')
                self.redirect('/teacher',{'Set-Cookie':cookie});return
            with opened(db_path) as db:
                session=self.session(db)
                if not session:self.reply(401,'Қайта кіріңіз');return
                if not secure_equal(val('csrf'),session['csrf']):self.reply(403,'Сұрауды растау мүмкін емес');return
                if self.path=='/logout':db.execute('DELETE FROM sessions WHERE token=?',(session['token'],));self.redirect('/login',{'Set-Cookie':'senim_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict'});return
                if self.path=='/teacher/feedback':
                    ident=val('answer_id');feedback=val('feedback').strip()
                    if not ident.isdigit() or not 1<=len(feedback)<=2500:self.reply(400,'Пікірді тексеріңіз');return
                    a=db.execute('SELECT * FROM answers WHERE id=?',(int(ident),)).fetchone()
                    if not a:self.reply(404,'Жауап табылмады');return
                    db.execute('UPDATE answers SET feedback=?,feedback_at=? WHERE id=?',(feedback,time.time(),a['id']))
                    course=Course(db,None,[], '');course.owner=a['chat']
                    course.send(a['chat'],f'💬 Мұғалім пікірі\n{a["lesson"]}-сабақ · {a["task"]+1}-тапсырма\n\n{feedback}')
                    self.redirect('/teacher');return
            self.reply(404,'Бет табылмады')
    return Handler


def main():
    os.umask(0o077)
    token=os.getenv('BOT_TOKEN','').strip()
    join='';admins={int(v.strip()) for v in os.getenv('TEACHER_CHAT_IDS','').split(',') if v.strip().isdigit()}
    if not token or not admins:
        raise SystemExit('BOT_TOKEN және TEACHER_CHAT_IDS мәндерін баптаңыз. README_KZ.html қараңыз.')
    path=Path(os.getenv('DB_PATH',str(ROOT/'data'/'senim39.sqlite3')));path.parent.mkdir(parents=True,exist_ok=True)
    connect(path).close();api=Telegram(token)
    threading.Thread(target=run_poll,args=(path,api,admins,join,(os.getenv('BASE_URL') or os.getenv('RENDER_EXTERNAL_URL','')).rstrip('/')),daemon=True).start()
    from waitress import serve
    from wsgi_adapter import application
    print('Сенім 39 қосылды. Мұғалім кабинеті: /teacher',flush=True)
    serve(application(make_handler(path,admins,os.getenv('COOKIE_SECURE','1')!='0')),
          host='0.0.0.0',port=int(os.getenv('PORT','10000')),threads=8,
          max_request_body_size=16384)

if __name__=='__main__':main()

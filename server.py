from flask import Flask, jsonify, send_from_directory, request
from pathlib import Path
from datetime import datetime, timezone, timedelta
import os, requests, json, sqlite3, hashlib, hmac, secrets, re, logging
from functools import wraps
from parse_data import extract

BASE=Path(__file__).resolve().parent
app=Flask(__name__,static_folder=str(BASE),static_url_path='')
app.config['MAX_CONTENT_LENGTH']=40*1024
API='https://api.kcisa.kr/openapi/service/rest/meta/KSClost'
DATABASE_URL=os.getenv('DATABASE_URL','').strip()
IS_RENDER=bool(os.getenv('RENDER'))
MAX_POSTS=500
POST_RETENTION_DAYS=90
CATEGORIES=['지갑','휴대전화','가방','전자기기','의류','열쇠','귀금속','서류','기타']

def connect():
    if DATABASE_URL:
        import psycopg
        return psycopg.connect(DATABASE_URL)
    if IS_RENDER:
        raise RuntimeError('DATABASE_URL_NOT_CONFIGURED')
    return sqlite3.connect(str(BASE/'posts.sqlite3'))

def sql(statement):
    return statement.replace('?', '%s') if DATABASE_URL else statement

def setup_db():
    with connect() as con:
        con.execute('''CREATE TABLE IF NOT EXISTS posts (id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL, category TEXT NOT NULL, place TEXT NOT NULL, region TEXT NOT NULL, detail TEXT NOT NULL, event_date TEXT, lat REAL, lng REAL, created_at TEXT NOT NULL, secret_salt TEXT NOT NULL, secret_hash TEXT NOT NULL)''')
        con.commit()

def db_route(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            setup_db()
            return fn(*args, **kwargs)
        except Exception as exc:
            app.logger.exception('Community database request failed')
            if str(exc) == 'DATABASE_URL_NOT_CONFIGURED':
                return jsonify(error='게시판 데이터베이스가 연결되지 않았습니다. Render Environment에 DATABASE_URL을 설정하세요.'),503
            return jsonify(error='게시판 데이터베이스에 연결할 수 없습니다. Render Logs 및 Neon 연결 상태를 확인하세요.'),503
    return wrapper

@app.route('/api/health/db')
def db_health():
    try:
        setup_db()
        with connect() as con:
            con.execute('SELECT 1').fetchone()
        return jsonify(ok=True, database='postgresql' if DATABASE_URL else 'sqlite')
    except Exception:
        app.logger.exception('Database health check failed')
        return jsonify(ok=False,error='데이터베이스 연결 또는 초기화 실패'),503

@app.route('/')
def index():return send_from_directory(BASE,'index.html')

@app.route('/api/items')
def items():return jsonify(json.loads((BASE/'data.json').read_text(encoding='utf-8')))

@app.route('/api/refresh',methods=['POST'])
def refresh():
    # Public refresh disabled to avoid abuse and ephemeral file writes in production.
    return jsonify(error='공개 서버에서는 API 새로고침을 비활성화했습니다. 관리자 전용 갱신 작업을 별도로 구성하세요.'),403

def prune_posts(con):
    """Remove expired posts and cap retained community posts; KCISA data is untouched."""
    cutoff=(datetime.now(timezone.utc)-timedelta(days=POST_RETENTION_DAYS)).isoformat()
    con.execute(sql('DELETE FROM posts WHERE created_at < ?'), (cutoff,))
    # Deterministic order with ID tie breaker; portable SQLite/PostgreSQL SQL.
    con.execute(sql('DELETE FROM posts WHERE id NOT IN (SELECT id FROM posts ORDER BY created_at DESC, id DESC LIMIT ?)'),(MAX_POSTS,))


def public_post(row):
    keys=['id','kind','title','category','place','region','detail','event_date','lat','lng','created_at']
    return dict(zip(keys,row[:len(keys)]))

@app.route('/api/posts',methods=['GET'])
@db_route
def list_posts():
    with connect() as con:
        prune_posts(con)
        con.commit()
        rows=con.execute('SELECT id,kind,title,category,place,region,detail,event_date,lat,lng,created_at FROM posts ORDER BY created_at DESC, id DESC LIMIT 500').fetchall()
    return jsonify([public_post(r) for r in rows])

@app.route('/api/posts',methods=['POST'])
@db_route
def add_post():
    d=request.get_json(silent=True) or {}
    kind=str(d.get('kind',''))
    if kind not in ['분실','습득']:return jsonify(error='분실 또는 습득을 선택하세요.'),400
    category=str(d.get('category',''))
    if category not in CATEGORIES:return jsonify(error='올바른 카테고리를 선택하세요.'),400
    fields={k:str(d.get(k,'')).strip() for k in ['title','place','region','detail','event_date','password']}
    if not fields['title'] or not fields['place'] or not fields['region'] or not fields['password']:return jsonify(error='제목, 장소, 지역, 삭제 비밀번호는 필수입니다.'),400
    if any(len(fields[k])>maxlen for k,maxlen in [('title',100),('place',120),('region',80),('detail',1000),('password',100)]):return jsonify(error='입력 길이를 줄여주세요.'),400
    if len(fields['password'])<6:return jsonify(error='삭제 비밀번호는 6자 이상이어야 합니다.'),400
    try:
        date=fields['event_date']
        if date:datetime.strptime(date,'%Y-%m-%d')
        lat=float(d['lat']) if d.get('lat') is not None else None
        lng=float(d['lng']) if d.get('lng') is not None else None
        if (lat is None)!=(lng is None) or (lat is not None and not (-90<=lat<=90 and -180<=lng<=180)):raise ValueError()
    except (ValueError,TypeError):return jsonify(error='날짜 또는 좌표가 올바르지 않습니다.'),400
    salt=secrets.token_hex(16)
    hashed=hashlib.pbkdf2_hmac('sha256',fields['password'].encode(),bytes.fromhex(salt),210000).hex()
    uid=secrets.token_hex(12)
    values=(uid,kind,fields['title'],category,fields['place'],fields['region'],fields['detail'],date,lat,lng,datetime.now(timezone.utc).isoformat(),salt,hashed)
    with connect() as con:
        con.execute(sql('INSERT INTO posts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)'),values)
        prune_posts(con)
        con.commit()
    return jsonify(id=uid),201

@app.route('/api/posts/<uid>',methods=['DELETE'])
@db_route
def delete_post(uid):
    d=request.get_json(silent=True) or {}
    password=str(d.get('password',''))
    with connect() as con:
        row=con.execute(sql('SELECT secret_salt,secret_hash FROM posts WHERE id=?'),(uid,)).fetchone()
        if not row:return jsonify(error='게시글이 없습니다.'),404
        candidate=hashlib.pbkdf2_hmac('sha256',password.encode(),bytes.fromhex(row[0]),210000).hex()
        if not hmac.compare_digest(candidate,row[1]):return jsonify(error='삭제 비밀번호가 다릅니다.'),403
        con.execute(sql('DELETE FROM posts WHERE id=?'),(uid,))
        con.commit()
    return jsonify(ok=True)

@app.route('/api/chat',methods=['POST'])
def chat():
    d=request.get_json(silent=True) or {}
    message=str(d.get('message','')).strip()[:500]
    if not message:return jsonify(error='메시지를 입력하세요.'),400
    # Optional real LLM query expansion, never pass personal post data to third party.
    terms=[]; ai=False
    key=os.getenv('OPENAI_API_KEY','').strip()
    if key:
        try:
            r=requests.post('https://api.openai.com/v1/chat/completions',headers={'Authorization':'Bearer '+key},json={'model':os.getenv('OPENAI_MODEL','gpt-4o-mini'),'messages':[{'role':'system','content':'한국어 유실물 검색어 추출기. 사용자 문장에서 물품명, 장소, 색상 등 검색 키워드 1~5개만 쉼표로 구분하여 출력. 추가 설명 금지.'},{'role':'user','content':message}],'max_tokens':100,'temperature':0},timeout=12)
            r.raise_for_status()
            terms=[t.strip() for t in r.json()['choices'][0]['message']['content'].split(',') if t.strip()][:5]
            ai=True
        except Exception:pass
    if not terms:
        stop={'어제','오늘','제가','나는','저는','잃어버렸어','잃어버렸어요','찾아줘','찾고','싶어요','분실했어요','분실','습득','물건','혹시','있나요','어디','근처','에서','했는데','했어요','거기','좀','찾아','주세요'}
        words=re.findall(r'[가-힣A-Za-z0-9]+',message)
        # Extract searchable noun-like phrases, including Korean postpositions.
        suffixes=('에서는','에서도','에서','으로','에게','까지','부터','하고','이나','거나','을','를','은','는','이','가','에','도','와','과')
        terms=[]
        for word in words:
            if word in stop or len(word)<2:continue
            candidate=word
            for suffix in suffixes:
                if candidate.endswith(suffix) and len(candidate)>len(suffix)+1:
                    candidate=candidate[:-len(suffix)];break
            if candidate in stop or len(candidate)<2:continue
            if candidate not in terms:terms.append(candidate)
        terms=terms[:8]
    return jsonify(terms=terms,ai=ai,reply=('AI가 추출한' if ai else '문장에서 찾은')+' 검색어: '+(', '.join(terms) if terms else '검색어를 찾지 못했어요. 물품명과 장소를 적어주세요.')+' · 아래 검색 결과를 확인하세요.')

if __name__=='__main__':
    app.run(host='127.0.0.1',port=5000,debug=False)

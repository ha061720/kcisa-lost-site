from flask import Flask, jsonify, send_from_directory, request
from pathlib import Path
from datetime import datetime, timezone, timedelta
import os, requests, json, sqlite3, hashlib, hmac, secrets, re, logging
from functools import wraps
from parse_data import extract

BASE=Path(__file__).resolve().parent
app=Flask(__name__,static_folder=str(BASE),static_url_path='')
app.config['MAX_CONTENT_LENGTH']=500*1024
API='https://api.kcisa.kr/openapi/service/rest/meta/KSClost'
DATABASE_URL=os.getenv('DATABASE_URL','').strip()
IS_RENDER=bool(os.getenv('RENDER'))
MAX_POSTS=500
POST_RETENTION_DAYS=90
CATEGORIES=['지갑','휴대전화','가방','전자기기','의류','열쇠','귀금속','서류','기타','미아']

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
        # Add optional image column to existing SQLite/Neon databases without losing posts.
        if DATABASE_URL:
            con.execute('ALTER TABLE posts ADD COLUMN IF NOT EXISTS photo TEXT')
            con.execute('ALTER TABLE posts ADD COLUMN IF NOT EXISTS concert_id TEXT')
        else:
            cols=[row[1] for row in con.execute('PRAGMA table_info(posts)').fetchall()]
            if 'photo' not in cols:con.execute('ALTER TABLE posts ADD COLUMN photo TEXT')
            if 'concert_id' not in cols:con.execute('ALTER TABLE posts ADD COLUMN concert_id TEXT')
        con.execute('''CREATE TABLE IF NOT EXISTS concerts (id TEXT PRIMARY KEY, title TEXT NOT NULL, venue TEXT NOT NULL, date TEXT NOT NULL, end_date TEXT NOT NULL, time TEXT, description TEXT, created_at TEXT NOT NULL, secret_salt TEXT NOT NULL, secret_hash TEXT NOT NULL)''')
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
    keys=['id','kind','title','category','place','region','detail','event_date','lat','lng','created_at','photo','concert_id']
    return dict(zip(keys,row[:len(keys)]))

@app.route('/api/posts',methods=['GET'])
@db_route
def list_posts():
    with connect() as con:
        prune_posts(con)
        con.commit()
        rows=con.execute('SELECT id,kind,title,category,place,region,detail,event_date,lat,lng,created_at,photo,concert_id FROM posts ORDER BY created_at DESC, id DESC LIMIT 500').fetchall()
    return jsonify([public_post(r) for r in rows])

@app.route('/api/posts',methods=['POST'])
@db_route
def add_post():
    d=request.get_json(silent=True) or {}
    kind=str(d.get('kind',''))
    if kind not in ['분실','습득']:return jsonify(error='분실 또는 습득을 선택하세요.'),400
    category=str(d.get('category',''))
    if category not in CATEGORIES or (category=='미아' and kind!='분실'):return jsonify(error='올바른 카테고리를 선택하세요. 미아는 분실 카테고리에서만 선택할 수 있습니다.'),400
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
    photo=d.get('photo') or None
    if photo is not None:
        if not isinstance(photo,str) or len(photo)>300000 or not re.fullmatch(r'data:image/(?:jpeg|png|webp);base64,[A-Za-z0-9+/]+={0,2}',photo):
            return jsonify(error='사진 형식이 올바르지 않거나 너무 큽니다.'),400
    concert_id=str(d.get('concert_id','')).strip()[:100]
    if concert_id and not valid_concert_id(concert_id):return jsonify(error='선택한 공연을 찾을 수 없습니다.'),400
    salt=secrets.token_hex(16)
    hashed=hashlib.pbkdf2_hmac('sha256',fields['password'].encode(),bytes.fromhex(salt),210000).hex()
    uid=secrets.token_hex(12)
    values=(uid,kind,fields['title'],category,fields['place'],fields['region'],fields['detail'],date,lat,lng,datetime.now(timezone.utc).isoformat(),salt,hashed,photo,concert_id)
    with connect() as con:
        con.execute(sql('INSERT INTO posts (id,kind,title,category,place,region,detail,event_date,lat,lng,created_at,secret_salt,secret_hash,photo,concert_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)'),values)
        prune_posts(con)
        con.commit()
    return jsonify(id=uid),201

@app.route('/api/posts/<uid>',methods=['PUT'])
@db_route
def edit_post(uid):
    d=request.get_json(silent=True) or {}
    password=str(d.get('password',''))
    with connect() as con:
        row=con.execute(sql('SELECT secret_salt,secret_hash FROM posts WHERE id=?'),(uid,)).fetchone()
        if not row:return jsonify(error='게시글이 없습니다.'),404
        candidate=hashlib.pbkdf2_hmac('sha256',password.encode(),bytes.fromhex(row[0]),210000).hex()
        if not hmac.compare_digest(candidate,row[1]):return jsonify(error='비밀번호가 다릅니다.'),403
        kind=str(d.get('kind','')).strip()
        category=str(d.get('category','')).strip()
        if kind not in ('분실','습득') or category not in CATEGORIES or (category=='미아' and kind!='분실'):
            return jsonify(error='게시물 종류 또는 카테고리를 확인하세요.'),400
        vals={k:str(d.get(k,'')).strip() for k in ('title','place','region','detail','event_date')}
        if not vals['title'] or not vals['place'] or not vals['region']:return jsonify(error='제목, 지역, 장소는 필수입니다.'),400
        if any(len(vals[k])>n for k,n in [('title',100),('place',120),('region',80),('detail',1000)]):return jsonify(error='입력 글자 수를 줄여주세요.'),400
        try:
            if vals['event_date']:datetime.strptime(vals['event_date'],'%Y-%m-%d')
            lat=float(d['lat']) if d.get('lat') is not None else None
            lng=float(d['lng']) if d.get('lng') is not None else None
            if (lat is None)!=(lng is None) or (lat is not None and not (-90<=lat<=90 and -180<=lng<=180)):raise ValueError()
        except (ValueError,TypeError):return jsonify(error='날짜 또는 좌표를 확인하세요.'),400
        photo=d.get('photo') or None
        if photo is not None and (not isinstance(photo,str) or len(photo)>300000 or not re.fullmatch(r'data:image/(?:jpeg|png|webp);base64,[A-Za-z0-9+/]+={0,2}',photo)):
            return jsonify(error='사진 형식 또는 크기가 올바르지 않습니다.'),400
        concert_id=str(d.get('concert_id','')).strip()[:100]
        if concert_id and not valid_concert_id(concert_id):return jsonify(error='선택한 공연을 찾을 수 없습니다.'),400
        con.execute(sql('UPDATE posts SET kind=?,title=?,category=?,place=?,region=?,detail=?,event_date=?,lat=?,lng=?,photo=?,concert_id=? WHERE id=?'),
            (kind,vals['title'],category,vals['place'],vals['region'],vals['detail'],vals['event_date'],lat,lng,photo,concert_id,uid))
        con.commit()
    return jsonify(ok=True,id=uid)

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


def official_concerts():
    try:return json.loads((BASE/'events.json').read_text(encoding='utf-8')).get('events',[])
    except Exception:return []

def valid_concert_id(uid):
    if any(str(x.get('id'))==uid for x in official_concerts()):return True
    with connect() as con:
        return con.execute(sql('SELECT 1 FROM concerts WHERE id=?'),(uid,)).fetchone() is not None

@app.route('/api/concerts',methods=['GET'])
@db_route
def list_concerts():
    with connect() as con:
        rows=con.execute('SELECT id,title,venue,date,end_date,time,description,created_at FROM concerts ORDER BY date DESC, created_at DESC LIMIT 200').fetchall()
    return jsonify([dict(id=r[0],title=r[1],venue=r[2],date=r[3],end_date=r[4],time=r[5],description=r[6],created_at=r[7],source='사용자 등록 (미검증)',verified=False) for r in rows])

@app.route('/api/concerts',methods=['POST'])
@db_route
def add_concert():
    d=request.get_json(silent=True) or {}
    fields={k:str(d.get(k,'')).strip() for k in ['title','venue','date','end_date','time','description','password']}
    if not all(fields[k] for k in ['title','venue','date','password']):return jsonify(error='공연명, 공연장, 시작일, 삭제 비밀번호를 입력하세요.'),400
    if len(fields['password'])<6:return jsonify(error='비밀번호는 6자 이상이어야 합니다.'),400
    for k,n in [('title',120),('venue',100),('time',120),('description',700),('password',100)]:
        if len(fields[k])>n:return jsonify(error='입력 길이를 줄여주세요.'),400
    try:
        start=datetime.strptime(fields['date'],'%Y-%m-%d').date()
        end=datetime.strptime(fields['end_date'] or fields['date'],'%Y-%m-%d').date()
        if end<start:return jsonify(error='종료일은 시작일보다 빠를 수 없습니다.'),400
    except ValueError:return jsonify(error='공연 날짜가 올바르지 않습니다.'),400
    uid='user-'+secrets.token_hex(10)
    salt=secrets.token_hex(16)
    hashed=hashlib.pbkdf2_hmac('sha256',fields['password'].encode(),bytes.fromhex(salt),210000).hex()
    with connect() as con:
        con.execute(sql('INSERT INTO concerts (id,title,venue,date,end_date,time,description,created_at,secret_salt,secret_hash) VALUES (?,?,?,?,?,?,?,?,?,?)'),(uid,fields['title'],fields['venue'],fields['date'],fields['end_date'] or fields['date'],fields['time'],fields['description'],datetime.now(timezone.utc).isoformat(),salt,hashed))
        con.commit()
    return jsonify(id=uid),201

@app.route('/api/concerts/<uid>',methods=['DELETE'])
@db_route
def delete_concert(uid):
    d=request.get_json(silent=True) or {}
    with connect() as con:
        row=con.execute(sql('SELECT secret_salt,secret_hash FROM concerts WHERE id=?'),(uid,)).fetchone()
        if not row:return jsonify(error='공연 게시물이 없습니다.'),404
        candidate=hashlib.pbkdf2_hmac('sha256',str(d.get('password','')).encode(),bytes.fromhex(row[0]),210000).hex()
        if not hmac.compare_digest(candidate,row[1]):return jsonify(error='삭제 비밀번호가 다릅니다.'),403
        # Retain references in lost-property posts as text IDs; no cascading delete of posts.
        con.execute(sql('DELETE FROM concerts WHERE id=?'),(uid,))
        con.commit()
    return jsonify(ok=True)


# 공연별 유실물 추적: 개인 입력은 저장하지 않고 요청 시에만 비교합니다.
TRACKING_SYNONYMS={
 '지갑':['지갑','카드지갑','반지갑','장지갑','머니클립','wallet'],
 '휴대폰':['휴대폰','핸드폰','스마트폰','아이폰','갤럭시','전화기'],
 '가방':['가방','백팩','크로스백','숄더백','에코백'],
 '굿즈':['굿즈','응원봉','포토카드','슬로건'],
 '열쇠':['열쇠','키링','차키','자동차키'],
 '전자기기':['전자기기','이어폰','에어팟','버즈','워치','시계'],
 '기타':[]
}

def tracking_terms(category, feature):
    return TRACKING_SYNONYMS.get(category, [category]) + [x for x in re.findall(r'[가-힣A-Za-z0-9]+',feature.lower()) if len(x)>=2][:4]

def tracking_score(text, category, feature, venue, place, record_date, lost_date, concert_id='', record_concert=''):
    hay=str(text or '').lower()
    terms=tracking_terms(category,feature)
    category_hit=any(t.lower() in hay for t in terms[:max(1,len(terms)-4)])
    feature_terms=[t for t in re.findall(r'[가-힣A-Za-z0-9]+',feature.lower()) if len(t)>=2][:4]
    feature_hit=bool(feature_terms) and any(t in hay for t in feature_terms)
    venue_hit=bool(venue) and venue.lower() in hay
    place_hit=bool(place) and any(t in hay for t in [place.lower(),place.lower().replace('공연장 ','')])
    concert_hit=bool(concert_id and record_concert and concert_id==record_concert)
    date_hit=bool(record_date and lost_date and record_date>=lost_date and record_date<= (datetime.strptime(lost_date,'%Y-%m-%d')+timedelta(days=30)).strftime('%Y-%m-%d'))
    # 날짜가 분실 전이면 제외, 단 기록 날짜가 없으면 날짜 점수는 부여하지 않음
    if record_date and lost_date and record_date<lost_date:return None
    if not category_hit:return None
    score=30+(25 if (venue_hit or place_hit) else 0)+(20 if date_hit else 0)+(15 if feature_hit else 0)+(10 if concert_hit else 0)
    reasons=['물품 종류 유사']
    if venue_hit or place_hit:reasons.append('장소 관련')
    if date_hit:reasons.append('습득 날짜 범위 일치')
    if feature_hit:reasons.append('특징 유사')
    if concert_hit:reasons.append('동일 공연 등록')
    return {'score':score,'reasons':reasons}

@app.route('/api/tracking/search',methods=['POST'])
@db_route
def tracking_search():
    d=request.get_json(silent=True) or {}
    category=str(d.get('category','')).strip()
    concert_id=str(d.get('concert_id','')).strip()[:100]
    lost_date=str(d.get('lost_date','')).strip()
    place=str(d.get('place','')).strip()[:100]
    feature=str(d.get('feature','')).strip()[:100]
    if category not in TRACKING_SYNONYMS:return jsonify(error='물품 종류를 선택하세요.'),400
    if not concert_id or not valid_concert_id(concert_id):return jsonify(error='공연을 선택하세요.'),400
    try:datetime.strptime(lost_date,'%Y-%m-%d')
    except ValueError:return jsonify(error='관람 날짜를 정확히 입력하세요.'),400
    concert=next((e for e in official_concerts() if str(e.get('id'))==concert_id),None)
    if concert is None:
        with connect() as con:
            row=con.execute(sql('SELECT title,venue,date,end_date FROM concerts WHERE id=?'),(concert_id,)).fetchone()
        if not row:return jsonify(error='공연 정보를 찾을 수 없습니다.'),404
        concert={'id':concert_id,'title':row[0],'venue':row[1],'date':row[2],'end_date':row[3]}
    if not (concert['date']<=lost_date<=(concert.get('end_date') or concert['date'])):
        return jsonify(error='관람 날짜가 선택한 공연 기간에 포함되지 않습니다.'),400
    venue=str(concert.get('venue',''))
    results=[]
    with connect() as con:
        rows=con.execute("SELECT id,kind,title,category,place,region,detail,event_date,concert_id FROM posts WHERE kind='습득' AND category!='미아' ORDER BY created_at DESC LIMIT 500").fetchall()
    for r in rows:
        uid,kind,title,cat,found_place,region,detail,date,related=r
        text=' '.join([title,cat,found_place,region,detail])
        match=tracking_score(text,category,feature,venue,place,date,lost_date,concert_id,related)
        if match:results.append(dict(source='community',id=uid,title=title,place=found_place,date=date,**match))
    try:
        records=json.loads((BASE/'data.json').read_text(encoding='utf-8')).get('records',[])
    except Exception:records=[]
    for item in records[:3000]:
        title=str(item.get('name',''))
        found_place=str(item.get('place',''))
        date=str(item.get('date',''))
        text=' '.join(str(item.get(k,'') or '') for k in ['name','place','feature','note','center'])
        match=tracking_score(text,category,feature,venue,place,date,lost_date)
        if match:results.append(dict(source='kcisa',id=str(item.get('id','')),title=title,place=found_place,date=date,**match))
    results.sort(key=lambda x:(x['score'],x['date'] or ''),reverse=True)
    return jsonify(concert={'id':concert_id,'title':concert['title'],'venue':venue},candidates=results[:20],total_matches=len(results),guidance=['공연장 출입구 및 마지막 이동 경로를 우선 확인하세요.','공연장 내 분실물은 공연 주최·주관사에 보관 여부를 문의하세요.','공공 유실물은 경찰청 LOST112에서도 확인하세요.'],note='일치 점수는 검색 우선순위이며 실제 소유권이나 보관 여부를 보증하지 않습니다.')

@app.route('/api/ai/status')
def ai_status():
    return jsonify(configured=bool(os.getenv('OPENAI_API_KEY','').strip()), model=os.getenv('OPENAI_MODEL','gpt-4o-mini'), note='API 키가 설정되어도 요금·권한·네트워크 문제로 호출이 실패할 수 있습니다.')

@app.route('/api/chat',methods=['POST'])
def chat():
    d=request.get_json(silent=True) or {}
    message=str(d.get('message','')).strip()[:500]
    if not message:return jsonify(error='메시지를 입력하세요.'),400
    # Optional real LLM query expansion, never pass personal post data to third party.
    terms=[]; ai=False; ai_error=False
    key=os.getenv('OPENAI_API_KEY','').strip()
    if key:
        try:
            r=requests.post('https://api.openai.com/v1/chat/completions',headers={'Authorization':'Bearer '+key},json={'model':os.getenv('OPENAI_MODEL','gpt-4o-mini'),'messages':[{'role':'system','content':'한국어 유실물 검색어 추출기. 사용자 문장에서 물품명, 장소, 색상 등 검색 키워드 1~5개만 쉼표로 구분하여 출력. 추가 설명 금지.'},{'role':'user','content':message}],'max_tokens':100,'temperature':0},timeout=12)
            r.raise_for_status()
            terms=[t.strip() for t in r.json()['choices'][0]['message']['content'].split(',') if t.strip()][:5]
            ai=True
        except Exception:
            ai_error=True
            app.logger.warning('OpenAI chat request failed',exc_info=True)
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
    return jsonify(terms=terms,ai=ai,ai_configured=bool(key),ai_error=ai_error,reply=('AI가 추출한' if ai else '문장에서 찾은')+' 검색어: '+(', '.join(terms) if terms else '검색어를 찾지 못했어요. 물품명과 장소를 적어주세요.')+' · 아래 검색 결과를 확인하세요.')

if __name__=='__main__':
    app.run(host='127.0.0.1',port=5000,debug=False)

from flask import Flask, jsonify, send_from_directory
from pathlib import Path
import os, requests, json
from parse_data import extract

BASE=Path(__file__).resolve().parent
app=Flask(__name__,static_folder=str(BASE),static_url_path='')
API='https://api.kcisa.kr/openapi/service/rest/meta/KSClost'

@app.route('/')
def index(): return send_from_directory(BASE,'index.html')

@app.route('/api/items')
def items(): return jsonify(json.loads((BASE/'data.json').read_text(encoding='utf-8')))

@app.route('/api/refresh',methods=['POST'])
def refresh():
    key=os.environ.get('KCISA_SERVICE_KEY')
    if not key:return jsonify(error='KCISA_SERVICE_KEY 환경변수를 설정해 주세요.'),400
    try:
        response=requests.get(API,params={'serviceKey':key,'numOfRows':100,'pageNo':1},timeout=25)
        response.raise_for_status()
        data=extract(response.content)
        if not data['sourceCount'] or not data['records']:
            return jsonify(error='API 응답에서 유실물 표를 찾지 못했습니다. 기존 데이터는 유지합니다.'),502
        (BASE/'data.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
        return jsonify(data)
    except Exception as e:return jsonify(error=str(e)),502

if __name__=='__main__':app.run(host='127.0.0.1',port=5000,debug=False)

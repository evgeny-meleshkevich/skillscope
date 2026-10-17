import hashlib
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone, timedelta
import httpx
from . import db
from .extract import normalize

SOURCES = {
 'arbeitnow': {'name':'Arbeitnow','market':'Европа · преимущественно Германия','url':'https://www.arbeitnow.com','note':'Ограниченный срез публичного API. Зарплата не является структурированным полем источника.'},
 'remotive': {'name':'Remotive','market':'Международная удаленная работа','url':'https://remotive.com','note':'Публичная подборка с задержкой 24 часа. Географические ограничения указаны в вакансиях.'},
 'hh': {'name':'HeadHunter','market':'Настраиваемый регион HH','url':'https://hh.ru','note':'Официальный API. Может потребоваться токен; лимит и неполнота выдачи показываются явно.'},
}
lock=threading.Lock()
stop=threading.Event()

class SourceError(Exception): pass

def request(client,url,params=None):
    for attempt in range(3):
        response=client.get(url,params=params)
        if response.status_code==429 or response.status_code>=500:
            if attempt<2:
                time.sleep(min(4*(attempt+1),12)); continue
        if response.status_code in (401,403):
            raise SourceError(f'HTTP {response.status_code}: источник отклонил доступ. Для HH проверьте токен приложения; обход защиты не выполняется.')
        if response.status_code!=200: raise SourceError(f'HTTP {response.status_code}: не удалось получить данные')
        try: return response.json()
        except ValueError: raise SourceError('Источник вернул не JSON')
    raise SourceError('Источник временно недоступен')

def save_raw(source, payload):
    data=json.dumps(payload,ensure_ascii=False,sort_keys=True).encode()
    digest=hashlib.sha256(data).hexdigest()
    path=db.DATA / 'raw' / source / (digest+'.json')
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    path.write_bytes(data); os.chmod(path,0o600)
    return digest,str(path.relative_to(db.DATA))

def begin(source,scope):
    with db.connect() as con:
        return con.execute("INSERT INTO runs(source,started,status,scope) VALUES(?,?,'running',?)",
                           (source,db.now(),json.dumps(scope))).lastrowid

def ingest(source,items,run_id,payload,status='success',message=''):
    normalized=[]
    for item in items:
        if not isinstance(item,dict): raise ValueError('Запись вакансии должна быть объектом')
        record=normalize(item,source)
        if record: normalized.append(record)
    raw_hash, raw_path=save_raw(source,payload)
    stamp=db.now()
    # The new snapshot becomes visible atomically; the preceding snapshot survives any failure.
    with db.connect() as con:
        for record in normalized:
            job=source+':'+record['external_id']
            con.execute('INSERT INTO jobs VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen',
                        (job,source,record['external_id'],stamp,stamp))
            body=json.dumps(record,ensure_ascii=False,sort_keys=True)
            digest=hashlib.sha256(body.encode()).hexdigest()
            con.execute('INSERT OR IGNORE INTO versions(job,hash,payload,created) VALUES(?,?,?,?)',(job,digest,body,stamp))
            version=con.execute('SELECT id FROM versions WHERE job=? AND hash=?',(job,digest)).fetchone()['id']
            con.execute('INSERT OR REPLACE INTO run_items VALUES(?,?,?)',(run_id,job,version))
            for e in record['evidence']:
                con.execute('INSERT OR IGNORE INTO skill_evidence VALUES(?,?,?,?,?)',(version,e['skill'],e['kind'],e['quote'],e['method']))
        accepted=con.execute('SELECT count(*) FROM run_items WHERE run=?',(run_id,)).fetchone()[0]
        con.execute('UPDATE runs SET finished=?,status=?,message=?,received=?,accepted=?,raw_hash=?,raw_path=? WHERE id=?',
                    (stamp,status,message,len(items),accepted,raw_hash,raw_path,run_id))
    return accepted

def fail(run_id,message):
    with db.connect() as con:
        con.execute("UPDATE runs SET finished=?,status='error',message=? WHERE id=?",(db.now(),message[:600],run_id))

def collect_source(source,force=False):
    cfg=db.settings()
    scope={'pages':cfg['arbeitnow_pages']} if source=='arbeitnow' else {'area':cfg['hh_area'],'limit':cfg['hh_limit'],'auth_configured':bool(os.environ.get('HH_TOKEN'))} if source=='hh' else {'feed':'public'}
    with db.connect() as con:
        last=con.execute("SELECT * FROM runs WHERE source=? AND status!='running' ORDER BY id DESC LIMIT 1",(source,)).fetchone()
    # One retrieval per source per 6 hours, including manual actions; respect public API quotas.
    if last and not force and json.loads(last['scope'])==scope and (datetime.now(timezone.utc)-datetime.fromisoformat(last['started'])).total_seconds()<21600:
        return {'source':source,'status':'cached','message':'Повторная загрузка доступна через 6 часов. Используется сохраненный срез.'}
    run_id=begin(source,scope)
    headers={'User-Agent':os.environ.get('HH_USER_AGENT','SkillScope/1.0')}
    if source=='hh' and os.environ.get('HH_TOKEN'): headers['Authorization']='Bearer '+os.environ['HH_TOKEN']
    try:
        items=[]; payload={'responses':[],'retrieved_at':db.now(),'scope':scope}; partial=False
        with httpx.Client(timeout=40,headers=headers,follow_redirects=True) as client:
            if source=='remotive':
                result=request(client,'https://remotive.com/api/remote-jobs')
                if not isinstance(result.get('jobs'),list): raise SourceError('В ответе нет списка jobs')
                items=result['jobs']; payload['responses'].append(result)
                message='Получен публичный поток Remotive; это ограниченная подборка, а не весь рынок.'
            elif source=='arbeitnow':
                for page in range(1,cfg['arbeitnow_pages']+1):
                    result=request(client,'https://www.arbeitnow.com/api/job-board-api',{'page':page})
                    if not isinstance(result.get('data'),list): raise SourceError('В ответе нет списка data')
                    payload['responses'].append(result); items.extend(result['data'])
                    has_next=bool((result.get('links') or {}).get('next'))
                    if not has_next: break
                    if page==cfg['arbeitnow_pages']: partial=True
                    time.sleep(.6)
                message='Срез Arbeitnow: '+str(len(payload['responses']))+' стр. Полнота рынка не гарантируется.'
            else:
                seen=set()
                # Target queries, bounded acquisition and explicit partial scope, without pretending to cover the market.
                for query in ['NAME:("data analyst" OR "аналитик данных")','NAME:("data engineer" OR "инженер данных")','NAME:("python")']:
                    result=request(client,'https://api.hh.ru/vacancies',{'text':query,'area':cfg['hh_area'],'per_page':min(cfg['hh_limit'],100)})
                    if not isinstance(result.get('items'),list): raise SourceError('В ответе нет списка items')
                    payload['responses'].append(result)
                    if result.get('found',0)>len(result['items']): partial=True
                    for summary in result['items']:
                        if summary['id'] in seen: continue
                        seen.add(summary['id'])
                        if len(items)>=cfg['hh_limit']: partial=True; break
                        time.sleep(.3)
                        try:
                            detail=request(client,'https://api.hh.ru/vacancies/'+str(summary['id']))
                            items.append(detail)
                        except SourceError:
                            items.append(summary); partial=True
                    if len(items)>=cfg['hh_limit']: break
                    time.sleep(.6)
                payload['details']=items
                message='HH: три целевых запроса, ограниченная выборка. Не найденные объявления не считаются закрытыми.'
        accepted=ingest(source,items,run_id,payload,'partial' if partial else 'success',message)
        return {'source':source,'status':'partial' if partial else 'success','accepted':accepted}
    except Exception as exc:
        # Do not include HTTP headers or credentials in persisted error messages.
        message=str(exc) if isinstance(exc,(SourceError,ValueError)) else 'Сетевая ошибка или ошибка обработки. Предыдущие данные сохранены.'
        logging.warning('Collector %s failed: %s',source,type(exc).__name__)
        fail(run_id,message)
        return {'source':source,'status':'error','message':message}

def collect(sources,force=False):
    if not lock.acquire(False): return False
    return collect_locked(sources,force)

def collect_locked(sources,force=False):
    try:
        return [collect_source(source,force) for source in sources]
    finally: lock.release()

def scheduler():
    while not stop.wait(20):
        cfg=db.settings()
        if not cfg['scheduler']: continue
        due=[]
        with db.connect() as con:
            for source in cfg['enabled_sources']:
                last=con.execute('SELECT started,status FROM runs WHERE source=? ORDER BY id DESC LIMIT 1',(source,)).fetchone()
                delay=6 if last and last['status']=='error' else cfg['interval_hours']
                if not last or (datetime.now(timezone.utc)-datetime.fromisoformat(last['started'])).total_seconds()>=delay*3600: due.append(source)
        if due: collect(due)

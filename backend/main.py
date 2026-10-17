import csv
import html
import io
import json
import os
import threading
from contextlib import asynccontextmanager
from urllib.parse import urlparse
from fastapi import FastAPI,HTTPException,Request,BackgroundTasks
from fastapi.responses import FileResponse,HTMLResponse,Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel,Field
from . import db,collectors,analysis
from .extract import ROLES,SKILLS,curriculum_levels

@asynccontextmanager
async def lifespan(app):
    db.init()
    from scripts.reprocess import reprocess
    reprocess()
    collectors.stop.clear()
    thread=None
    if os.environ.get('SKILLSCOPE_TEST')!='1':
        thread=threading.Thread(target=collectors.scheduler,daemon=True); thread.start()
    yield
    collectors.stop.set()
    if thread: thread.join(timeout=1)

app=FastAPI(title='SkillScope',lifespan=lifespan)

@app.middleware('http')
async def local_mutations(request:Request,call_next):
    if request.method in ('POST','PUT','PATCH','DELETE'):
        origin=request.headers.get('origin')
        target=urlparse(origin) if origin else None
        if request.headers.get('x-skillscope-app')!='1' or (target and (target.netloc!=request.headers.get('host') or target.scheme!='http')):
            return Response('Изменения разрешены только из локального приложения',status_code=403)
    response=await call_next(request)
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['Referrer-Policy']='strict-origin-when-cross-origin'
    return response

def filters(source='arbeitnow',role='',level='',remote='',days=365,skill='',kind='all',search=''):
    sources=list(dict.fromkeys(source.split(',')))
    if not sources or any(name not in collectors.SOURCES for name in sources) or role and role not in ROLES or kind!='all' and kind not in analysis.KINDS:
        raise HTTPException(422,'Неизвестный источник, роль или вид требования')
    return dict(source=','.join(sources),role=role,level=level,remote=remote,days=days,skill=skill,kind=kind,search=search)

@app.get('/api/health')
def health(): return {'status':'ok','app':'SkillScope','version':'1.0.0'}

@app.get('/api/meta')
def meta():
    return {'sources':collectors.SOURCES,'roles':ROLES,'skills':list(SKILLS),'kinds':analysis.KINDS,
            'hh_token':bool(os.environ.get('HH_TOKEN')),'settings':db.settings()}

@app.get('/api/status')
def status():
    with db.connect() as con:
        runs=[dict(r) for r in con.execute('SELECT id,source,started,finished,status,message,received,accepted,raw_hash,scope FROM runs ORDER BY id DESC LIMIT 40')]
        versions=con.execute('SELECT count(*) FROM versions').fetchone()[0]
    return {'running':collectors.lock.locked(),'runs':runs,'versions':versions}

class CollectRequest(BaseModel):
    sources:list[str]=Field(default_factory=lambda:['arbeitnow','remotive'],min_length=1,max_length=3)

@app.post('/api/collect',status_code=202)
def collect(body:CollectRequest,background:BackgroundTasks):
    if any(s not in collectors.SOURCES for s in body.sources): raise HTTPException(422,'Неизвестный источник')
    if not collectors.lock.acquire(False): raise HTTPException(409,'Сбор уже выполняется')
    background.add_task(collectors.collect_locked,list(dict.fromkeys(body.sources)))
    return {'message':'Сбор запущен. Статус обновится автоматически.'}

class Settings(BaseModel):
    scheduler:bool=True
    interval_hours:int=Field(default=24,ge=6,le=168)
    enabled_sources:list[str]=Field(default_factory=lambda:['arbeitnow','remotive'],max_length=3)
    hh_area:str=Field(default='113',pattern=r'^\d{1,8}$')
    hh_limit:int=Field(default=100,ge=10,le=300)
    arbeitnow_pages:int=Field(default=2,ge=1,le=5)

@app.put('/api/settings')
def settings(body:Settings):
    if any(s not in collectors.SOURCES for s in body.enabled_sources): raise HTTPException(422,'Неизвестный источник')
    db.save_settings(body.model_dump()); return db.settings()

@app.get('/api/summary')
def summary(source:str='arbeitnow',role:str='',level:str='',remote:str='',days:int=365,skill:str='',kind:str='all',search:str=''):
    return analysis.summary(filters(source,role,level,remote,days,skill,kind,search))

@app.get('/api/jobs')
def jobs(source:str='arbeitnow',role:str='',level:str='',remote:str='',days:int=365,skill:str='',kind:str='all',search:str='',offset:int=0,limit:int=30,sort:str='newest'):
    if offset<0 or not 1<=limit<=100: raise HTTPException(422,'Некорректная страница')
    records,run=analysis.select_jobs(**filters(source,role,level,remote,days,skill,kind,search))
    if sort not in analysis.SORTS: raise HTTPException(422,'Неизвестная сортировка')
    records=analysis.sort_jobs(records,sort)
    return {'total':len(records),'items':[{k:v for k,v in j.items() if k not in ('description','evidence')} for j in records[offset:offset+limit]],'run_id':run['id'] if run else None}

@app.get('/api/matrix')
def matrix(source:str='arbeitnow',role:str='',level:str='',remote:str='',days:int=365,skill:str='',kind:str='all',search:str=''):
    f=filters(source,role,level,remote,days,skill,kind,search)
    records,_=analysis.select_jobs(**f)
    from collections import Counter
    counts=Counter()
    for j in records: counts.update({e['skill'] for e in j['selected_evidence']})
    names=[s for s,_ in counts.most_common(10)]
    roles=[];values=[]
    for k,name in ROLES.items():
        group=[j for j in records if j['role']==k]
        if not group:continue
        index=len(roles);roles.append({'role':k,'name':name,'n':len(group)})
        for i,s in enumerate(names):
            n=sum(any(e['skill']==s for e in j['selected_evidence']) for j in group)
            values.append([index,i,round(n/len(group)*100)])
    return {'roles':roles,'skills':names,'values':values}

@app.get('/api/jobs/{job_id:path}')
def job(job_id:str):
    with db.connect() as con:
        row=con.execute('SELECT v.payload,j.first_seen,j.last_seen FROM versions v JOIN jobs j ON j.id=v.job WHERE j.id=? ORDER BY v.id DESC LIMIT 1',(job_id,)).fetchone()
        versions=con.execute('SELECT id,created,hash FROM versions WHERE job=? ORDER BY id DESC',(job_id,)).fetchall()
    if not row: raise HTTPException(404,'Вакансия не найдена')
    return {**json.loads(row['payload']),'first_seen':row['first_seen'],'last_seen':row['last_seen'], 'versions':[dict(v) for v in versions]}

class AuditRequest(BaseModel):
    title:str=Field(default='Программа обучения',min_length=1,max_length=160)
    text:str=Field(default='',max_length=80000)
    levels:dict|None=None
    filters:dict=Field(default_factory=dict)
    parent:int|None=None

def valid_levels(values):
    if not isinstance(values,dict): raise HTTPException(422,'Ожидается справочник покрытия')
    out={}
    for s,v in values.items():
        if s not in SKILLS or not isinstance(v,dict) or type(v.get('level')) is not int or v['level'] not in (0,1,2,3):
            raise HTTPException(422,'Некорректный уровень покрытия')
        out[s]={'level':v['level'],'quote':str(v.get('quote',''))[:420]}
    return out

@app.post('/api/audit')
def audit(body:AuditRequest):
    levels=valid_levels(body.levels) if body.levels is not None else curriculum_levels(body.text)
    try: f=filters(**body.filters)
    except TypeError: raise HTTPException(422,'Неизвестные фильтры')
    return {'levels':levels,**analysis.audit(f,levels)}

@app.post('/api/curricula')
def save_curriculum(body:AuditRequest):
    levels=valid_levels(body.levels) if body.levels is not None else curriculum_levels(body.text)
    with db.connect() as con:
        if body.parent and not con.execute('SELECT 1 FROM curricula WHERE id=?',(body.parent,)).fetchone(): raise HTTPException(404,'Предыдущая версия не найдена')
        cid=con.execute('INSERT INTO curricula(title,text,levels,created,parent) VALUES(?,?,?,?,?)',
                        (body.title,body.text,json.dumps(levels,ensure_ascii=False),db.now(),body.parent)).lastrowid
    return {'id':cid}

@app.get('/api/curricula')
def curricula():
    with db.connect() as con:
        rows=[dict(r) for r in con.execute('SELECT * FROM curricula ORDER BY id DESC LIMIT 100')]
    for r in rows:r['levels']=json.loads(r['levels'])
    return rows

class ImportRequest(BaseModel):
    source:str
    data:dict|list

@app.post('/api/import')
def import_data(body:ImportRequest):
    if body.source not in collectors.SOURCES: raise HTTPException(422,'Неизвестный источник')
    items=body.data if isinstance(body.data,list) else body.data.get({'hh':'items','remotive':'jobs','arbeitnow':'data'}[body.source])
    if not isinstance(items,list) or not items: raise HTTPException(422,'JSON должен содержать непустой список вакансий')
    if len(items)>5000 or len(json.dumps(body.data))>20_000_000: raise HTTPException(413,'Импорт ограничен 5 000 записями / 20 МБ')
    if not collectors.lock.acquire(False): raise HTTPException(409,'Сбор уже выполняется')
    run_id=collectors.begin(body.source,{'mode':'user_import'})
    try:
        count=collectors.ingest(body.source,items,run_id,body.data,'partial','Импорт пользовательского JSON; происхождение не проверено сетевым сборщиком.')
        return {'accepted':count,'run_id':run_id}
    except Exception:
        collectors.fail(run_id,'Ошибка формата импортируемых данных')
        raise HTTPException(422,'Не удалось разобрать JSON. Предыдущие данные сохранены.')
    finally:collectors.lock.release()

def csv_safe(v):
    value='' if v is None else str(v)
    return "'"+value if value.startswith(('=','+','-','@','\t','\r')) else value

@app.get('/api/vacancies.csv')
def export_vacancies(source:str='arbeitnow',role:str='',level:str='',remote:str='',days:int=365,skill:str='',kind:str='all',search:str=''):
    records,_=analysis.select_jobs(**filters(source,role,level,remote,days,skill,kind,search))
    buf=io.StringIO(); writer=csv.writer(buf);writer.writerow(['source','id','title','company','role','location','published','salary_from','salary_to','currency','period','gross','skills','url'])
    for j in records:
        writer.writerow([csv_safe(v) for v in [j['source'],j['external_id'],j['title'],j['company'],ROLES[j['role']],j['location'],j['published'],j['salary']['from'],j['salary']['to'],j['salary']['currency'],j['salary']['period'],j['salary']['gross'],', '.join(sorted({e['skill'] for e in j['selected_evidence']})),j['url']]])
    return Response('\ufeff'+buf.getvalue(),media_type='text/csv',headers={'Content-Disposition':'attachment; filename="skillscope-vacancies.csv"'})

@app.get('/api/export.csv')
def export_statistics(source:str='arbeitnow',role:str='',level:str='',remote:str='',days:int=365,skill:str='',kind:str='all',search:str=''):
    f=filters(source,role,level,remote,days,skill,kind,search)
    data=analysis.summary(f)
    records,_=analysis.select_jobs(**f,run_id=data['run']['id']) if data['run'] else ([],None)
    from collections import Counter
    from datetime import datetime
    from zoneinfo import ZoneInfo
    buf=io.StringIO(); writer=csv.writer(buf,delimiter=';',lineterminator='\r\n')
    writer.writerow(['Раздел','Показатель','Категория','Значение','Единица','База расчёта (объявлений)','Пояснение'])
    def row(section,metric,category,value,unit='',base='',note=''):
        if isinstance(value,float):value=str(round(value,2)).replace('.',',')
        writer.writerow([csv_safe(v) for v in (section,metric,category,value,unit,base,note)])
    def stamp(value):
        if not value:return 'Нет данных'
        return datetime.fromisoformat(value.replace('Z','+00:00')).astimezone(ZoneInfo('Europe/Minsk')).strftime('%d.%m.%Y %H:%M')
    n=data['n']
    row('Сводка','Вакансии в выборке','Все выбранные источники',n,'объявлений',n)
    for name,count in [('Вакансии с найденными навыками',data['with_skills']),('Вакансии с раскрытой зарплатой',data['salary_disclosed'])]:
        row('Сводка',name,'Количество',count,'объявлений',n)
        row('Сводка',name,'Доля',round(count/n*100,2) if n else None,'%',n,'Нет базы расчёта' if not n else '')
    row('Сводка','Различных найденных навыков','Словарь и правила',len(data['skills']),'навыков')
    names=', '.join(collectors.SOURCES[key]['name'] for key in f['source'].split(','))
    for label,value in [('Источники',names),('Направление',ROLES.get(role,'Все IT-направления')),('Грейд',{'junior':'Junior / стажёр','middle':'Middle','senior':'Senior / Lead','unknown':'Не указан'}.get(level,'Все')),
                        ('Период публикации',str(days)+' дней' if days else 'Весь срез'),('Формат','Удалённо' if remote else 'Любой'),('Разметка',analysis.KINDS.get(kind,'Все упоминания')),('Навык',skill or 'Все'),('Поиск',search or 'Не задан')]:
        row('Фильтры',label,'',value)
    window=data['publication_window']
    row('Охват','Загружено до ограничения по дням','При остальных выбранных фильтрах',window['total'],'объявлений')
    row('Охват','Самая ранняя публикация','Минск',stamp(window['oldest']))
    row('Охват','Самая поздняя публикация','Минск',stamp(window['newest']))
    row('Охват','Без даты публикации','До ограничения по дням',window['missing_dates'],'объявлений',note='Исключены при ограничении по дням' if days else 'Включены во весь срез')
    run=data['run'] or {}
    runs=run.get('runs') or ([run] if run else [])
    counts=Counter(j['source'] for j in records)
    for key in f['source'].split(','):
        name=collectors.SOURCES[key]['name'];r=next((x for x in runs if x['source']==key),None)
        row('Источники','Вакансии в выборке',name,counts[key],'объявлений',n)
        row('Источники','Дата среза',name,stamp(r['finished']) if r else 'Нет загруженного среза',note=r['message'] if r else 'Данные не подменяются другой площадкой')
    for skill_row in data['skills']:
        name=skill_row['skill']
        row('Навыки','Вакансии с навыком',name,skill_row['count'],'объявлений',n)
        row('Навыки','Доля вакансий с навыком',name,skill_row['share'],'%',n,'Один навык учитывается один раз на объявление; сумма долей может превышать 100%')
        row('Навыки','Как обязательное требование',name,skill_row['required'],'объявлений',n,'Контекстные правила по всему тексту; требуют проверки')
    for item in data['roles']:
        row('Направления','Вакансии',item['name'],item['count'],'объявлений',n)
        row('Направления','Доля',item['name'],round(item['count']/n*100,2),'%',n)
    for key,count in data['levels'].items():
        row('Грейды','Вакансии',{'junior':'Junior / стажёр','middle':'Middle','senior':'Senior / Lead','unknown':'Не указан'}.get(key,key),count,'объявлений',n,'Определяется по заголовку')
    for section,field in [('Работодатели','company'),('Регионы','location')]:
        for name,count in Counter(j[field] or 'Не указан' for j in records).most_common():
            row(section,'Вакансии',name,count,'объявлений',n)
            row(section,'Доля',name,round(count/n*100,2),'%',n)
    periods={'hour':'час','month':'месяц','year':'год'};gross={'gross':'до налогов','net':'на руки','unknown':'налоги не указаны'}
    for group in data['salary_groups']:
        label=f"{group['currency']} / {periods[group['period']]} / {gross[group['gross']]}"
        row('Зарплаты','Полные сопоставимые вилки',label,group['n'],'объявлений',group['n'])
        row('Зарплаты','Медиана середины вилки',label,group['median'],group['currency'],group['n'],'Не рассчитывается: меньше 30 сопоставимых вилок' if group['median'] is None else 'Опубликованная вилка, не фактический доход')
    if not data['salary_groups']:row('Зарплаты','Сопоставимые вилки','',0,'объявлений',note='Нет полных вилок с известной валютой и периодом; медиана не рассчитывается')
    for point in data['history']:row('История','Вакансии в дневном срезе',point['date'],point['n'],'объявлений',note='Последний сопоставимый срез дня; пропуски не заменяются нулём')
    row('Методика','Навыки','',data['method'])
    row('Методика','Ограничения','', 'Ограниченная выборка, не весь рынок. Совпадения между площадками могут повторяться. Период ограничивает возраст публикации относительно даты сбора каждой площадки. Валюты, периоды и налоги не смешиваются.')
    return Response('\ufeff'+buf.getvalue(),media_type='text/csv; charset=utf-8',headers={'Content-Disposition':'attachment; filename="skillscope-statistics.csv"','Cache-Control':'no-store'})

@app.get('/api/chart.svg')
def export_chart(source:str='arbeitnow',role:str='',level:str='',remote:str='',days:int=365,skill:str='',kind:str='all',search:str=''):
    data=analysis.summary(filters(source,role,level,remote,days,skill,kind,search))
    items=data['skills'][:15];height=max(240,150+len(items)*31)
    esc=lambda v:html.escape(str(v))
    marks=[]
    for i,s in enumerate(items):
        y=92+i*31;width=s['share']*5.8
        marks.append(f'<text x="30" y="{y+14}" class="label">{esc(s["skill"])}</text><rect x="205" y="{y}" width="{width}" height="18" rx="3" fill="#128b7a"/><text x="{215+width}" y="{y+14}" class="label">{s["share"]}% · {s["count"]}</text>')
    grid=''.join(f'<line x1="{205+t*5.8}" x2="{205+t*5.8}" y1="82" y2="{height-55}" stroke="#dce6eb"/><text x="{205+t*5.8}" y="{height-30}" text-anchor="middle" class="label">{t}%</text>' for t in range(0,101,20))
    source_names=', '.join(collectors.SOURCES[name]['name'] for name in source.split(','))
    caption=f'{source_names} · N={data["n"]} · {analysis.KINDS.get(kind,"Все упоминания")} · '+str((data['run'] or {}).get('finished','Нет среза'))
    svg=f'''<svg xmlns="http://www.w3.org/2000/svg" width="960" height="{height}" viewBox="0 0 960 {height}" role="img"><title>SkillScope — доли навыков</title><style>text{{font-family:Arial,sans-serif;fill:#1a3547}}.label{{font-size:12px}}</style><rect width="960" height="{height}" fill="#ffffff"/><text x="30" y="35" font-size="23" font-weight="600">SkillScope · навыки в выбранном срезе</text><text x="30" y="61" class="label">{esc(caption)}</text>{grid}{''.join(marks)}<text x="930" y="{height-8}" text-anchor="end" class="label">Доля уникальных вакансий, % · Количество рядом с полосой</text></svg>'''
    return Response(svg,media_type='image/svg+xml',headers={'Content-Disposition':'attachment; filename="skillscope-skills.svg"'})

class ReportRequest(BaseModel):
    title:str=Field(default='SkillScope — отчет',max_length=160)
    filters:dict=Field(default_factory=dict)
    levels:dict|None=None

@app.post('/api/reports')
def save_report(body:ReportRequest):
    try:f=filters(**body.filters)
    except TypeError:raise HTTPException(422,'Неизвестные фильтры')
    snapshot=analysis.summary(f)
    records,_=analysis.select_jobs(**f,run_id=snapshot['run']['id']) if snapshot['run'] else ([],None)
    snapshot['evidence_jobs']=[{k:j[k] for k in ('id','title','company','url','selected_evidence')} for j in records]
    if body.levels is not None:snapshot['audit']=analysis.audit_snapshot(snapshot,valid_levels(body.levels))
    with db.connect() as con:
        rid=con.execute('INSERT INTO reports(title,created,filters,snapshot) VALUES(?,?,?,?)',
                        (body.title,db.now(),json.dumps(f),json.dumps(snapshot,ensure_ascii=False))).lastrowid
    return {'id':rid,'url':f'/api/reports/{rid}/html'}

@app.get('/api/reports')
def reports():
    with db.connect() as con:return [dict(r) for r in con.execute('SELECT id,title,created FROM reports ORDER BY id DESC LIMIT 50')]

@app.get('/api/reports/{rid}/html',response_class=HTMLResponse)
def report_html(rid:int):
    with db.connect() as con:r=con.execute('SELECT * FROM reports WHERE id=?',(rid,)).fetchone()
    if not r:raise HTTPException(404,'Отчет не найден')
    s=json.loads(r['snapshot']); esc=lambda v:html.escape(str(v))
    rows=''.join(f'<tr><td>{esc(x["skill"])}</td><td>{x["count"]}</td><td><div class="bar" style="width:{x["share"]}%"></div>{x["share"]}%</td></tr>' for x in s['skills'])
    links=''.join(f'<li><a href="{esc(j["url"])}">{esc(j["title"])}</a> · {esc(j["company"])}</li>' for j in s['evidence_jobs'])
    run=s.get('run') or {}
    provenance=''
    if run.get('runs'):
        provenance='<h2>Срезы источников</h2><ul>'+''.join(f'<li>{esc(collectors.SOURCES[x["source"]]["name"])}: {esc(x["finished"])} · {esc(x["status"])} · {esc(x["message"])}</li>' for x in run['runs'])+'</ul>'
        if run.get('missing_sources'):provenance+='<p>Нет загруженного среза: '+esc(', '.join(run['missing_sources']))+'</p>'
        provenance+='<p>Даты и охват площадок различаются; совпадения между площадками могут повторяться.</p>'
    coverage=''
    if s.get('audit'):
        a=s['audit'];coverage=f'<h2>Аудит программы: {esc(a["coverage"])}%</h2><p>Взвешенное покрытие навыков практикой. Не вероятность трудоустройства.</p>'
        coverage+='<ul>'+''.join(f'<li>{esc(x["skill"])}: уровень {x["level"]}, спрос {x["share"]}%</li>' for x in a['items'])+'</ul>'
    return f'''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(r['title'])}</title><style>body{{font:15px/1.6 system-ui;max-width:950px;margin:40px auto;padding:24px;color:#193247}}h1{{font-size:32px}}table{{width:100%;border-collapse:collapse}}td,th{{padding:9px;text-align:left;border-bottom:1px solid #d7e1e9}}.bar{{height:5px;background:#008e85;margin-bottom:4px}}pre{{white-space:pre-wrap;overflow-wrap:anywhere}}a{{color:#00786e}}@media print{{body{{margin:0}}a{{color:inherit}}}}</style><h1>{esc(r['title'])}</h1><p>Создан: {esc(r['created'])} · источник: {esc(json.loads(r['filters'])['source'])} · N = {s['n']}</p><h2>Фильтры</h2><pre>{esc(json.dumps(json.loads(r['filters']),ensure_ascii=False,indent=2))}</pre><p>Сбор: {esc((s['run'] or {}).get('finished','Нет данных'))}. {esc((s['run'] or {}).get('message',''))}</p>{provenance}<h2>Навыки</h2><table><tr><th>Навык</th><th>Вакансии</th><th>Доля</th></tr>{rows}</table>{coverage}<h2>Методика</h2><p>{esc(s['method'])}</p><p>Зарплаты: только полные вилки с явно заданным периодом оплаты, раздельно по валюте и gross/net. Агрегаты скрыты при N &lt; 30. Исчезновение объявления не подтверждает найм. История начинается с первого собственного сбора.</p><h2>Вакансии-основания</h2><p>Данные предоставлены {esc(json.loads(r['filters'])['source'])}; ссылки ведут к первоисточнику.</p><ul>{links}</ul></html>'''

dist=db.ROOT / 'frontend' / 'dist'
if dist.exists():
    app.mount('/assets',StaticFiles(directory=dist/'assets'),name='assets')

@app.get('/{path:path}')
def frontend(path:str):
    if path.startswith('api/'): raise HTTPException(404,'Метод API не найден')
    if not (dist/'index.html').exists():raise HTTPException(503,'Сначала выполните сборку интерфейса: ./start.sh')
    return FileResponse(dist/'index.html')

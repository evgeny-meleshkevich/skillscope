import json
import statistics
from collections import Counter,defaultdict
from datetime import datetime,timezone,timedelta
from . import db
from .extract import ROLES,SKILLS

KINDS={'required':'Требование (правила)','optional':'Будет плюсом','mention':'Упоминание в тексте','tagged':'Тег источника'}

def select_jobs(source='arbeitnow',role='',level='',remote='',days=365,skill='',kind='all',run_id=None,search=''):
    sources=list(dict.fromkeys(source.split(',')))
    if len(sources)>1:
        records=[]; runs=[]
        for name in sources:
            pinned=run_id.get(name) if isinstance(run_id,dict) else None
            if isinstance(run_id,dict) and name not in run_id: continue
            items,run=select_jobs(name,role,level,remote,days,skill,kind,pinned,search)
            records.extend(items)
            if run: runs.append(run)
        if not runs: return [],None
        return records,{'id':{r['source']:r['id'] for r in runs},'source':source,
                        'finished':max(r['finished'] for r in runs),'status':'partial',
                        'message':'Объединены последние доступные срезы; даты и охват площадок различаются.',
                        'runs':runs,'missing_sources':[name for name in sources if name not in {r['source'] for r in runs}]}
    if isinstance(run_id,dict):
        if source not in run_id: return [],None
        run_id=run_id[source]
    with db.connect() as con:
        if run_id is None:
            row=con.execute("SELECT * FROM runs WHERE source=? AND status IN ('success','partial') ORDER BY id DESC LIMIT 1",(source,)).fetchone()
        else:
            row=con.execute("SELECT * FROM runs WHERE id=? AND source=? AND status IN ('success','partial')",(run_id,source)).fetchone()
        if not row: return [],None
        run=dict(row)
        rows=con.execute('SELECT v.payload,j.id,j.first_seen,j.last_seen FROM run_items r JOIN versions v ON v.id=r.version JOIN jobs j ON j.id=r.job WHERE r.run=?',(run['id'],)).fetchall()
    reference=datetime.fromisoformat(run['finished'])
    records=[]
    for r in rows:
        j=json.loads(r['payload']); j.update({'id':r['id'],'first_seen':r['first_seen'],'last_seen':r['last_seen']})
        if j['archived']: continue
        if role and j['role']!=role: continue
        if level and j['level']!=level: continue
        if remote=='yes' and not j['remote']: continue
        if search and search.lower() not in (j['title']+' '+j['company']+' '+j['location']).lower(): continue
        if days:
            try:
                published=datetime.fromisoformat(j['published'].replace('Z','+00:00'))
                if published.tzinfo is None: published=published.replace(tzinfo=timezone.utc)
                if not timedelta(0)<=reference-published<=timedelta(days=days): continue
            except (ValueError,TypeError): continue
        j['selected_evidence']=[e for e in j['evidence'] if kind=='all' or e['kind']==kind]
        if skill and not any(e['skill']==skill for e in j['selected_evidence']): continue
        records.append(j)
    return records,run

def summary(filters):
    jobs,run=select_jobs(**filters)
    all_jobs,_=select_jobs(**{**filters,'days':0},run_id=run['id']) if run else ([],None)
    dates=[]
    for job in all_jobs:
        try:
            value=datetime.fromisoformat((job['published'] or '').replace('Z','+00:00'))
            dates.append(value.replace(tzinfo=value.tzinfo or timezone.utc).astimezone(timezone.utc))
        except (ValueError,TypeError): pass
    publication_window={'total':len(all_jobs),'dated':len(dates),'missing_dates':len(all_jobs)-len(dates),
                        'oldest':min(dates).isoformat() if dates else None,'newest':max(dates).isoformat() if dates else None}
    n=len(jobs); counts=Counter(); required=Counter(); roles=Counter(); levels=Counter(); locations=Counter(); employers=Counter()
    for j in jobs:
        counts.update({e['skill'] for e in j['selected_evidence']})
        required.update({e['skill'] for e in j['evidence'] if e['kind']=='required'})
        roles[j['role']]+=1; levels[j['level']]+=1; locations[j['location'] or 'Не указан']+=1; employers[j['company']]+=1
    skills=[{'skill':s,'count':c,'share':round(c/n*100,2),'required':required[s]} for s,c in counts.most_common()]
    # Every salary cohort is separate; the frontend chooses exactly one cohort.
    salary=defaultdict(list); raw_salary=0
    for j in jobs:
        s=j['salary']; a,b=s['from'],s['to']
        if s['raw'] or a is not None or b is not None: raw_salary+=1
        if a is not None and b is not None and s['currency'] and s['period']!='unknown':
            salary[(s['currency'],s['period'],s['gross'])].append({'id':j['id'],'title':j['title'],'company':j['company'],'from':a,'to':b,'mid':(a+b)/2,'level':j['level']})
    groups=[]
    for (currency,period,gross),vals in salary.items():
        groups.append({'key':currency+'|'+period+'|'+gross,'currency':currency,'period':period,'gross':gross,
                       'n':len(vals),'median':statistics.median([v['mid'] for v in vals]) if len(vals)>=30 else None,
                       'items':sorted(vals,key=lambda v:v['mid'])})
    history=[]
    if run and ',' not in filters.get('source',''):
        with db.connect() as con:
            runs=con.execute("SELECT * FROM runs WHERE source=? AND status IN ('success','partial') AND id<=? ORDER BY id",(run['source'],run['id'])).fetchall()
        daily={}
        for r in runs:
            if r['scope']!=run['scope']: continue
            day=r['finished'][:10]; daily[day]=r
        for day,r in list(daily.items())[-90:]:
            f={**filters,'run_id':r['id']}; old,_=select_jobs(**f)
            old_counts=Counter()
            for j in old: old_counts.update({e['skill'] for e in j['selected_evidence']})
            history.append({'date':day,'n':len(old),'status':r['status'],'shares':{s:round(c/max(1,len(old))*100,2) for s,c in old_counts.items()}})
    return {'n':n,'run':run,'publication_window':publication_window,'skills':skills,'roles':[{'role':k,'name':ROLES[k],'count':v} for k,v in roles.most_common()],
            'levels':dict(levels),'locations':locations.most_common(10),'employers':employers.most_common(8),
            'salary_groups':groups,'salary_disclosed':raw_salary,'detailed':sum(j['detailed'] for j in jobs),
            'with_skills':sum(bool(j['selected_evidence']) for j in jobs),'history':history,
            'method':'rules-1.2: словарь и контекстные правила; автоматическая разметка требует проверки. Доля = уникальные вакансии с навыком / все вакансии текущего фильтра. Теги площадки не считаются обязательными требованиями.'}

def audit(filters,levels):
    return audit_snapshot(summary(filters),levels)

def audit_snapshot(data,levels):
    skills=data['skills']; total=sum(s['count'] for s in skills)
    covered=sum(s['count'] for s in skills if levels.get(s['skill'],{}).get('level',0)>=2)
    result=[]
    for s in skills:
        value=levels.get(s['skill'],{}); level=value.get('level',0)
        result.append({**s,'level':level,'quote':value.get('quote',''),
                       'priority':s['share'] if level<2 else 0,
                       'module':f"{s['skill']}: практический проект, проверяемый результат и разбор ошибок"})
    return {'coverage':round(covered/total*100,1) if total else None,'items':result,'n':data['n'],
            'weight':total,'run_id':data['run']['id'] if data['run'] else None}


SORTS={'newest':'Сначала новые','oldest':'Сначала старые','title':'По названию А–Я','company':'По компании А–Я','skills':'Больше найденных навыков'}

def sort_jobs(records,sort='newest'):
    def timestamp(j):
        try:
            value=datetime.fromisoformat((j.get('published') or '').replace('Z','+00:00'))
            return value.replace(tzinfo=value.tzinfo or timezone.utc).timestamp()
        except (ValueError,TypeError): return float('-inf')
    if sort in ('newest','oldest'):
        dated=[j for j in records if timestamp(j)!=float('-inf')]
        missing=[j for j in records if timestamp(j)==float('-inf')]
        return sorted(dated,key=lambda j:(timestamp(j),j['id']),reverse=sort=='newest')+sorted(missing,key=lambda j:j['id'])
    if sort=='skills':return sorted(records,key=lambda j:(-len({e['skill'] for e in j['selected_evidence']}),j['title'].casefold(),j['id']))
    return sorted(records,key=lambda j:(j[sort].casefold(),j['id']))

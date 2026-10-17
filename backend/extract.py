import re
from datetime import datetime, timezone
from urllib.parse import urlparse
from bs4 import BeautifulSoup

EXTRACT_VERSION = 'rules-1.2'
SKILLS = {
 'Python': ['python','питон'], 'SQL': ['sql'], 'PostgreSQL': ['postgresql','postgres'],
 'Docker': ['docker'], 'Kubernetes': ['kubernetes','k8s'], 'Kafka': ['kafka'],
 'Airflow': ['airflow'], 'Spark': ['apache spark','pyspark','spark'], 'dbt': ['dbt'],
 'Tableau': ['tableau'], 'Power BI': ['power bi','powerbi'], 'Excel': ['excel'],
 'Статистика': ['statistics','statistical','статистика','статистики','статистический','статистические'], 'A/B-тесты': ['a/b','ab testing','a/b testing'],
 'Git': ['git','github','gitlab'], 'Linux': ['linux'], 'AWS': ['aws','amazon web services'],
 'Azure': ['azure'], 'GCP': ['gcp','google cloud'], 'JavaScript': ['javascript','js'],
 'TypeScript': ['typescript'], 'React': ['react','react.js','reactjs'], 'Vue': ['vue','vue.js'],
 'Angular': ['angular'], 'Node.js': ['node.js','nodejs'], 'Java': ['java'], 'Go': ['golang','go language'],
 'C#': ['c#','csharp'], 'C++': ['c++'], 'PHP': ['php'], 'Ruby': ['ruby','rails'],
 'FastAPI': ['fastapi'], 'Django': ['django'], 'Тестирование': ['pytest','unit tests','unit testing','юнит-тест'],
 'CI/CD': ['ci/cd','continuous integration','continuous delivery'], 'REST API': ['rest api','restful'],
 'Machine Learning': ['machine learning','машинное обучение'], 'LLM': ['llm','large language model','большие языковые модели'],
 'PyTorch': ['pytorch'], 'TensorFlow': ['tensorflow'], 'Pandas': ['pandas'], 'NumPy': ['numpy'],
 'ETL': ['etl','elt'], 'Snowflake': ['snowflake'], 'Databricks': ['databricks'], 'Redis': ['redis'],
 'MongoDB': ['mongodb','mongo db'], 'Figma': ['figma'], 'HTML': ['html'], 'CSS': ['css'],
}
PATTERNS = {s: re.compile(r'(?<![\w])(?:'+'|'.join(re.escape(a) for a in aliases)+r')(?![\w])', re.I)
            for s, aliases in SKILLS.items()}
ROLES = {'analyst':'Data Analyst','engineer':'Data Engineer','python':'Python Developer',
         'frontend':'Frontend','backend':'Backend / Fullstack','ai':'AI / ML','other':'Другие IT'}

def clean(html):
    soup = BeautifulSoup(html or '', 'html.parser')
    for tag in soup(['script','style']): tag.decompose()
    for tag in soup.find_all(['p','li','h1','h2','h3','h4','br']): tag.insert_before('\n')
    text = soup.get_text(' ', strip=False)
    text = re.sub(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', '[контакт скрыт]', text)
    text = re.sub(r'(?<!\w)\+?\d[\d ()-]{9,}\d(?!\w)', '[контакт скрыт]', text)
    return '\n'.join(re.sub(r'[ \t]+',' ',line).strip() for line in text.splitlines() if line.strip())[:80000]

def excluded_title(title):
    return bool(re.search(r'business development|account executive|\bsales\b|marketing|copywriter|recruiter|vertrieb|customer success',title,re.I))

def role_for(title, category=''):
    t=title.lower()
    if excluded_title(t): return None
    if re.search(r'data.?engineer|analytics engineer|инженер.*данн',t): return 'engineer'
    if re.search(r'data.?analy|analyst.*data|аналитик.*данн|bi.?analy|business intelligence',t): return 'analyst'
    if re.search(r'machine.?learning|\bml\b|\bai\b|data.?scien|искусствен|нейросет',t): return 'ai'
    if re.search(r'python|django|fastapi',t): return 'python'
    if re.search(r'frontend|front.end|react|vue|angular',t): return 'frontend'
    if re.search(r'backend|back.end|full.?stack|software.*(engineer|develop)|web.*develop|entwickler|softwareentwick|разработчик|программист',t): return 'backend'
    if re.search(r'devops|sre\b|qa.?engineer|test.*engineer|cloud.*engineer|software|cyber|security.*engineer|it.?support',t) or category.lower() in ['software development','data','devops / sysadmin']: return 'other'
    return None

def level_for(title):
    if re.search(r'\b(senior|lead|principal|staff|head)\b',title,re.I): return 'senior'
    if re.search(r'\b(junior|intern|trainee)\b',title,re.I): return 'junior'
    if re.search(r'\b(middle|mid.level)\b',title,re.I): return 'middle'
    return 'unknown'

def evidence(text, tags=()):
    found=[]; context='mention'
    for line in text.splitlines():
        if re.search(r'^(requirements|qualifications|what you bring|your profile|dein profil|qualifikationen|требования|необходимые навыки)\b',line,re.I): context='required'
        elif re.search(r'^(nice.to.have|bonus|preferred|wünschenswert|желательно|будет плюсом)\b',line,re.I): context='optional'
        elif re.search(r'^(responsibilities|benefits|about us|what we offer|deine aufgaben|unser angebot|обязанности|мы предлагаем|о компании)\b',line,re.I): context='mention'
        for sentence in re.split(r'(?<=[.!?;])\s+',line):
            for skill, pattern in PATTERNS.items():
                match=pattern.search(sentence)
                if not match: continue
                before=sentence[max(0,match.start()-45):match.start()]
                if re.search(r'(?:no|without|не требуется|не нужен|без)\s+(?:\w+\s+){0,3}$',before,re.I): continue
                kind=context
                if re.search(r'nice.to.have|bonus|preferred|a plus|wünschenswert|von vorteil|будет плюсом|желательно',sentence,re.I): kind='optional'
                elif re.search(r'\bmust\b|required|proficien|experience with|experience in|strong.*knowledge|kenntnisse|erfahrung mit|обязател|опыт работы с|знание',sentence,re.I): kind='required'
                # A sentence can exceed the display limit; keep a snippet containing the match.
                start=max(0,match.start()-140)
                quote=sentence[start:start+420]
                found.append({'skill':skill,'kind':kind,'quote':quote,'method':EXTRACT_VERSION})
    for tag in tags or []:
        name=tag.get('name','') if isinstance(tag,dict) else str(tag)
        for skill, pattern in PATTERNS.items():
            if pattern.search(name): found.append({'skill':skill,'kind':'tagged','quote':name[:420],'method':'source-tag'})
    return list({(i['skill'],i['kind'],i['quote']):i for i in found}.values())

def parse_salary(text):
    result={'from':None,'to':None,'currency':None,'gross':'unknown','period':'unknown','raw':text or ''}
    if not text or re.search(r'\bOTE\b|commission|equity',text,re.I): return result
    currency=next((c for c,p in [('USD',r'\$|\bUSD\b'),('EUR',r'€|\bEUR\b'),('GBP',r'£|\bGBP\b'),('RUR',r'₽|руб')]
                   if re.search(p,text,re.I)),None)
    if not currency: return result
    nums=re.findall(r'(?<!\w)(\d+(?:[,.]\d+)?(?:,\d{3})?)\s*([kK]?)',text)
    vals=[]
    for num,k in nums:
        if num.count(',')==1 and len(num.split(',')[1])==3: num=num.replace(',','')
        else: num=num.replace(',','.')
        vals.append(float(num)*(1000 if k else 1))
    if len(vals)!=2 or vals[0]<=0 or vals[1]<vals[0]: return result
    result.update({'from':vals[0],'to':vals[1],'currency':currency})
    if re.search(r'hour|/hr|/h\b|час',text,re.I): result['period']='hour'
    elif re.search(r'year|annual|annum|год',text,re.I): result['period']='year'
    elif re.search(r'month|/mo\b|месяц',text,re.I): result['period']='month'
    if re.search(r'\bgross\b|до налог',text,re.I): result['gross']='gross'
    elif re.search(r'\bnet\b|на руки',text,re.I): result['gross']='net'
    return result

def safe_url(url, source):
    hosts={'hh':('hh.ru','hh.kz','hh.uz','hh.by','rabota.by'), 'remotive':('remotive.com',),'arbeitnow':('arbeitnow.com','www.arbeitnow.com')}
    p=urlparse(url or '')
    return url if p.scheme=='https' and any(p.hostname==h or (p.hostname or '').endswith('.'+h) for h in hosts[source]) else ''

def normalize(item, source):
    if source=='hh':
        title=item.get('name',''); external=str(item.get('id','')); category=''
        text=clean(item.get('description') or '\n'.join(v for v in (item.get('snippet') or {}).values() if isinstance(v,str)))
        salary=item.get('salary') or {}; sr=item.get('salary_range') or {}
        salary={'from':salary.get('from',sr.get('from')), 'to':salary.get('to',sr.get('to')),
                'currency':salary.get('currency',sr.get('currency')),'gross':'gross' if salary.get('gross',sr.get('gross')) is True else 'net' if salary.get('gross',sr.get('gross')) is False else 'unknown',
                'period':'month' if salary else 'unknown','raw':''}
        location=(item.get('area') or {}).get('name',''); company=(item.get('employer') or {}).get('name','')
        published=item.get('published_at',''); tags=item.get('key_skills',[])
        exp=(item.get('experience') or {}).get('id','unknown'); remote=(item.get('schedule') or {}).get('id')=='remote'
        url=item.get('alternate_url',''); detailed=bool(item.get('description'))
    else:
        title=item.get('title',''); external=str(item.get('id') if source=='remotive' else item.get('slug',''))
        category=item.get('category',''); text=clean(item.get('description','')); company=item.get('company_name','')
        location=item.get('candidate_required_location','') if source=='remotive' else item.get('location','')
        if source=='remotive': published=item.get('publication_date','')
        else: published=datetime.fromtimestamp(float(item.get('created_at',0)),timezone.utc).isoformat()
        salary=parse_salary(item.get('salary',''))
        tags=item.get('tags',[]); exp='unknown'; remote=True if source=='remotive' else bool(item.get('remote'))
        url=item.get('url',''); detailed=True
    role=role_for(title,category)
    if not external or not title or not role: return None
    # Unspecified annual compensation remains unknown; do not convert or infer a tax regime.
    for k in ['from','to']:
        v=salary[k]
        if v is not None and (not isinstance(v,(int,float)) or v<=0): salary[k]=None
    if salary['from'] is not None and salary['to'] is not None and salary['from']>salary['to']:
        salary['from']=salary['to']=None
    return {'external_id':external,'source':source,'title':title,'company':company,'location':location,
            'role':role,'level':level_for(title),'experience':exp,'remote':remote,'published':published,
            'salary':salary,'description':text,'url':safe_url(url,source),'detailed':detailed,
            'archived':bool(item.get('archived',False)), 'evidence':evidence(text,tags),'extraction_version':EXTRACT_VERSION}

def curriculum_levels(text):
    out={}; practice=re.compile(r'практик|проект|лаборатор|задани|exercise|hands.on|project|practice|workshop',re.I)
    for line in text.splitlines():
        for skill, pattern in PATTERNS.items():
            if pattern.search(line):
                value=2 if practice.search(line) else 1
                old=out.get(skill,{}).get('level',0)
                if value>=old: out[skill]={'level':value,'quote':line[:420]}
    return out

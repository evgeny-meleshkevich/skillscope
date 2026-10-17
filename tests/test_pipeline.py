import json
import statistics
import httpx
import pytest
from fastapi.testclient import TestClient
from backend import db,analysis,collectors
from backend.extract import evidence,normalize,parse_salary,curriculum_levels,clean,role_for
from backend.main import app,filters

HEADERS={'X-SkillScope-App':'1'}

def test_exact_skills_not_substrings():
    assert not evidence('JavaScript PostgreSQL airflows dockerized') or {e['skill'] for e in evidence('JavaScript PostgreSQL airflows dockerized')}=={'JavaScript','PostgreSQL'}
    assert 'Java' not in {e['skill'] for e in evidence('JavaScript')}
    assert 'SQL' not in {e['skill'] for e in evidence('PostgreSQL')}

def test_context_and_negation():
    e=evidence('Requirements\nMust know Python.\nNice to have\nSQL is a plus.\nNo Docker experience required.\nResponsibilities\nMonitor Kafka.')
    assert any(x['skill']=='Python' and x['kind']=='required' for x in e)
    assert any(x['skill']=='SQL' and x['kind']=='optional' for x in e)
    assert not any(x['skill']=='Docker' for x in e)
    assert any(x['skill']=='Kafka' and x['kind']=='mention' for x in e)

def test_tags_are_not_automatically_required():
    assert evidence('', ['Python'])[0]['kind']=='tagged'

def test_citation_contains_skill_even_for_long_sentence():
    e=evidence('Words '*200+'Python '+'Words '*200)
    assert 'Python' in e[0]['quote'] and len(e[0]['quote'])<=420

def test_contacts_and_html_cleaned():
    txt=clean('<script>steal()</script><p>SQL mail test@example.org +375 29 123-45-67</p>')
    assert 'steal' not in txt and 'test@example' not in txt and '123-45-67' not in txt and 'SQL' in txt

def test_role_uses_title_not_broad_tags():
    assert role_for('Freelance Copywriter','Writing') is None
    assert role_for('Business Development Manager - AI Data Solutions') is None
    assert role_for('Senior Data Engineer')=='engineer'
    assert role_for('Python backend developer')=='python'

@pytest.mark.parametrize('text,expected',[
 ('$90 - $150 /hour',(90,150,'USD','hour')),
 ('$90k - $105k',(90000,105000,'USD','unknown')),
 ('EUR 50,000 - 70,000 per year',(50000,70000,'EUR','year')),
 ('$35,3k - $52k per year',(35300,52000,'USD','year')),
 ('OTE $25k - $35k',(None,None,None,'unknown')),
 ('$90 per hour',(None,None,None,'unknown')),
 ('$150 - $90 /hour',(None,None,None,'unknown')),
])
def test_conservative_salary_parser(text,expected):
    s=parse_salary(text)
    assert (s['from'],s['to'],s['currency'],s['period'])==expected

def test_ingest_deduplicates_and_keeps_versions(hh_job,insert):
    j=hh_job();r1=insert([j,j]);r2=insert([j])
    with db.connect() as con:
        assert con.execute('SELECT count(*) FROM jobs').fetchone()[0]==1
        assert con.execute('SELECT count(*) FROM versions').fetchone()[0]==1
        assert con.execute('SELECT accepted FROM runs WHERE id=?',(r1,)).fetchone()[0]==1
    j['description']='Must know Python and Kafka';insert([j])
    with db.connect() as con: assert con.execute('SELECT count(*) FROM versions').fetchone()[0]==2
    assert len(analysis.select_jobs(source='hh',days=0)[0])==1

def test_failed_fetch_does_not_replace_snapshot(hh_job,insert):
    r=insert([hh_job()]);failed=collectors.begin('hh',{});collectors.fail(failed,'403')
    jobs,run=analysis.select_jobs(source='hh',days=0)
    assert len(jobs)==1 and run['id']==r
    assert jobs[0]['archived'] is False

def test_missing_job_not_claimed_closed(hh_job,insert):
    insert([hh_job(),hh_job(id='124')]);insert([hh_job()])
    assert len(analysis.select_jobs(source='hh',days=0)[0])==1
    with db.connect() as con: assert con.execute('SELECT count(*) FROM jobs').fetchone()[0]==2

def test_share_is_distinct_jobs_and_denominator_includes_no_skills(hh_job,insert):
    insert([hh_job(description='Python. Python. Python'),hh_job('124',description='Nothing recognized')])
    s=analysis.summary(filters(source='hh',days=0))
    assert s['skills'][0]['share']==50 and s['skills'][0]['count']==1 and s['n']==2

def test_salary_cohorts_and_small_sample(hh_job,insert):
    salaries=[{'from':100,'to':200,'currency':'RUR','gross':True},
              {'from':200,'to':300,'currency':'RUR','gross':False},
              {'from':50,'to':80,'currency':'USD','gross':True},
              {'from':100,'to':None,'currency':'RUR','gross':True}]
    insert([hh_job(str(i),salary=s) for i,s in enumerate(salaries)])
    s=analysis.summary(filters(source='hh',days=0))
    assert len(s['salary_groups'])==3 and all(g['n']==1 and g['median'] is None for g in s['salary_groups'])
    assert s['salary_disclosed']==4

def test_median_only_with_sufficient_n(hh_job,insert):
    insert([hh_job(str(i),salary={'from':100+i,'to':200+i,'currency':'RUR','gross':True}) for i in range(30)])
    s=analysis.summary(filters(source='hh',days=0))
    assert s['salary_groups'][0]['median']==statistics.median([150+i for i in range(30)])

def test_same_day_only_one_history_point(hh_job,insert):
    insert([hh_job()]);insert([hh_job(),hh_job('124')])
    s=analysis.summary(filters(source='hh',days=0))
    assert len(s['history'])==1 and s['history'][0]['n']==2

def test_history_scopes_not_mixed(hh_job,insert):
    insert([hh_job()],scope={'area':'113'});insert([hh_job('124')],scope={'area':'16'})
    assert len(analysis.summary(filters(source='hh',days=0))['history'])==1

def test_curriculum_requires_practice_and_recalculates(hh_job,insert):
    insert([hh_job(description='Python and SQL'),hh_job('124',description='SQL')])
    levels=curriculum_levels('Python — обзор\nSQL — практический проект')
    assert levels['Python']['level']==1 and levels['SQL']['level']==2
    a=analysis.audit(filters(source='hh',days=0),levels)
    assert a['coverage']==66.7
    levels['Python']['level']=2
    assert analysis.audit(filters(source='hh',days=0),levels)['coverage']==100

def test_http_retry_and_access_error(monkeypatch):
    monkeypatch.setattr(collectors.time,'sleep',lambda x:None)
    calls=[]
    def respond(request):
        calls.append(1)
        return httpx.Response(429 if len(calls)<2 else 200,json={'jobs':[]})
    with httpx.Client(transport=httpx.MockTransport(respond)) as c:
        assert collectors.request(c,'https://example.org')=={'jobs':[]} and len(calls)==2
    with httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(403))) as c:
        with pytest.raises(collectors.SourceError):collectors.request(c,'https://example.org')

def test_cooldown_avoids_second_network_fetch(hh_job,insert,monkeypatch):
    insert([hh_job()],scope={'area':'113','limit':100,'auth_configured':False})
    monkeypatch.setattr(collectors,'request',lambda *a,**k:pytest.fail('Network should not be used'))
    assert collectors.collect_source('hh')['status']=='cached'

def test_russian_curriculum_statistics():
    levels=curriculum_levels('Статистика: обзор\nСтатистические методы — практический проект')
    assert levels['Статистика']['level']==2

def test_german_requirements():
    e=evidence('Dein Profil\nKenntnisse in Python\nDocker ist von Vorteil\nDeine Aufgaben\nDevelop SQL reports')
    assert any(v['skill']=='Python' and v['kind']=='required' for v in e)
    assert any(v['skill']=='Docker' and v['kind']=='optional' for v in e)

def test_report_reads_one_frozen_run(hh_job,insert,monkeypatch):
    insert([hh_job(description='SQL')])
    original=analysis.summary
    def concurrent_update(f):
        snap=original(f)
        insert([hh_job('124',description='Kafka')])
        return snap
    monkeypatch.setattr(analysis,'summary',concurrent_update)
    with TestClient(app) as c:
        r=c.post('/api/reports',headers=HEADERS,json={'filters':{'source':'hh','days':0},'levels':{'SQL':{'level':2}}}).json()
        page=c.get(r['url']).text
        assert 'SQL' in page and 'Kafka' not in page and '100.0%' in page

def test_api_filters_matrix_and_detail(hh_job,insert):
    insert([hh_job(),hh_job('124',title='Data Engineer')])
    with TestClient(app) as client:
        assert client.get('/api/health').json()['status']=='ok'
        assert client.get('/api/summary?source=hh&role=python&days=0').json()['n']==1
        matrix=client.get('/api/matrix?source=hh&days=0').json()
        assert len(matrix['roles'])==2 and matrix['values']
        detail=client.get('/api/jobs/hh%3A123').json()
        assert detail['title']=='Python Developer' and len(detail['versions'])==1
        assert client.get('/api/jobs?offset=-1').status_code==422

def test_mutations_origin_and_headers():
    with TestClient(app) as client:
        assert client.post('/api/audit',json={'text':'SQL'}).status_code==403
        assert client.post('/api/audit',headers={**HEADERS,'Origin':'http://evil.example'},json={}).status_code==403
        assert client.post('/api/audit',headers=HEADERS,json={'text':'SQL'}).status_code==200

def test_program_versions_report_snapshot_csv(hh_job,insert):
    insert([hh_job(description='Required SQL and Python')])
    with TestClient(app) as client:
        body={'title':'Тестовая программа','text':'SQL — проект','filters':{'source':'hh','days':0}}
        a=client.post('/api/audit',headers=HEADERS,json=body).json();assert a['coverage']==50
        body['levels']=a['levels']
        saved=client.post('/api/curricula',headers=HEADERS,json=body).json()
        body['parent']=saved['id'];body['levels']['Python']={'level':2,'quote':'Проверено пользователем'}
        assert client.post('/api/curricula',headers=HEADERS,json=body).status_code==200
        assert len(client.get('/api/curricula').json())==2
        r=client.post('/api/reports',headers=HEADERS,json={'title':'<script>alert(1)</script>','filters':{'source':'hh','days':0},'levels':a['levels']}).json()
        insert([hh_job('125',description='Kafka')])
        out=client.get(r['url']).text
        assert 'SQL' in out and 'Kafka' not in out and '<script>alert' not in out
        assert '&lt;script&gt;' in out
        exported=client.get('/api/export.csv?source=hh&days=0')
        assert exported.status_code==200 and 'Kafka' in exported.text

def test_invalid_import_is_atomic_and_unsafe_url_removed(hh_job,insert):
    original=insert([hh_job()])
    with TestClient(app) as c:
        assert c.post('/api/import',headers=HEADERS,json={'source':'hh','data':{'items':[{},'bad']}}).status_code==422
        assert c.get('/api/summary?source=hh&days=0').json()['run']['id']==original
        bad=hh_job('126');bad['alternate_url']='javascript:alert(1)'
        assert c.post('/api/import',headers=HEADERS,json={'source':'hh','data':{'items':[bad]}}).json()['accepted']==1
        assert c.get('/api/jobs/hh%3A126').json()['url']==''

def test_csv_formula_injection_prevented(hh_job,insert):
    j=hh_job();j['employer']['name']='=HYPERLINK("bad")';insert([j])
    with TestClient(app) as c:
        assert "'=HYPERLINK" in c.get('/api/export.csv?source=hh&days=0').text

def test_reprocessing_preserves_retrieval_time(hh_job,insert):
    from scripts.reprocess import reprocess
    rid=insert([hh_job()])
    with db.connect() as con:
        stamp=con.execute('SELECT finished FROM runs WHERE id=?',(rid,)).fetchone()[0]
        row=con.execute('SELECT id,payload FROM versions').fetchone()
        record=json.loads(row['payload']);record['extraction_version']='old'
        import hashlib
        body=json.dumps(record,ensure_ascii=False,sort_keys=True)
        con.execute('UPDATE versions SET payload=?,hash=? WHERE id=?',(body,hashlib.sha256(body.encode()).hexdigest(),row['id']))
    assert reprocess()==1
    with db.connect() as con:
        assert con.execute('SELECT finished FROM runs WHERE id=?',(rid,)).fetchone()[0]==stamp
    assert analysis.select_jobs(source='hh',days=0)[0][0]['extraction_version']=='rules-1.2'

def test_collection_reservation_prevents_false_acceptance():
    collectors.lock.acquire()
    try:
        with TestClient(app) as c:
            assert c.post('/api/collect',headers=HEADERS,json={'sources':['hh']}).status_code==409
    finally:collectors.lock.release()

def test_downloadable_chart_uses_real_filtered_counts(hh_job,insert):
    from xml.etree import ElementTree
    insert([hh_job(description='Python SQL'),hh_job('124',description='SQL')])
    with TestClient(app) as c:
        r=c.get('/api/chart.svg?source=hh&days=0')
        assert r.status_code==200 and 'attachment;' in r.headers['content-disposition']
        root=ElementTree.fromstring(r.text)
        assert root.tag.endswith('svg') and '50.0% · 1' in r.text and '100.0% · 2' in r.text


def test_multi_source_counts_exports_and_pinned_report(hh_job,insert,monkeypatch):
    insert([hh_job('123')])
    insert([{'id':'123','title':'Data Engineer','company_name':'Remote Co','candidate_required_location':'Worldwide','publication_date':'2026-10-02T12:00:00','url':'https://remotive.com/remote-jobs/software-dev/test-123','description':'Python SQL','category':'Software Development','salary':''}],source='remotive')
    f=filters(source='hh,remotive,hh',days=0)
    assert f['source']=='hh,remotive'
    data=analysis.summary(f)
    assert data['n']==2 and len(data['run']['runs'])==2 and data['history']==[]
    assert len({j['id'] for j in analysis.select_jobs(**f)[0]})==2
    original=analysis.summary
    def collect_after_summary(value):
        snapshot=original(value)
        insert([hh_job('999')])
        return snapshot
    with TestClient(app) as c:
        assert c.get('/api/jobs?source=hh,remotive&days=0').json()['total']==2
        assert c.get('/api/vacancies.csv?source=hh,remotive&days=0').text.count('\n')==3
        assert 'HeadHunter, Remotive' in c.get('/api/chart.svg?source=hh,remotive&days=0').text
        monkeypatch.setattr(analysis,'summary',collect_after_summary)
        report=c.post('/api/reports',headers=HEADERS,json={'filters':f}).json()
        with db.connect() as con:
            saved=json.loads(con.execute('SELECT snapshot FROM reports WHERE id=?',(report['id'],)).fetchone()[0])
        assert saved['n']==2 and {j['company'] for j in saved['evidence_jobs']}=={'Example Employer','Remote Co'}
        assert all(j['id'].endswith(':123') for j in saved['evidence_jobs'])
        assert c.get(report['url']).status_code==200


def test_multi_source_missing_and_failed_keep_provenance(hh_job,insert):
    insert([hh_job()])
    failed=collectors.begin('hh',{});collectors.fail(failed,'403')
    data=analysis.summary(filters(source='hh,remotive',days=0))
    assert data['n']==1 and data['run']['missing_sources']==['remotive']
    assert data['run']['runs'][0]['status']=='success'
    with TestClient(app) as c:
        assert c.get('/api/summary?source=hh,bogus').status_code==422
        assert c.get('/api/summary?source=').status_code==422


def test_sorting_is_before_pagination_and_dates_are_chronological(hh_job,insert):
    insert([hh_job('1',title='Z Python Developer',description='Python SQL Docker'),hh_job('2',title='A Python Developer',description='Python'),hh_job('3',title='B Python Developer',description='Python SQL')])
    with TestClient(app) as c:
        assert c.get('/api/jobs?source=hh&days=0&sort=title&limit=1&offset=1').json()['items'][0]['external_id']=='3'
        assert [j['external_id'] for j in c.get('/api/jobs?source=hh&days=0&sort=skills').json()['items']]==['1','3','2']
        assert c.get('/api/jobs?sort=bogus').status_code==422
    jobs=[{'id':'a','published':'2026-10-01T10:00:00+03:00'},{'id':'b','published':'2026-10-01T08:00:00+00:00'},{'id':'c','published':''}]
    assert [j['id'] for j in analysis.sort_jobs(jobs,'newest')]==['b','a','c']
    assert [j['id'] for j in analysis.sort_jobs(jobs,'oldest')]==['a','b','c']


def test_publication_period_exact_boundary_and_missing_dates(hh_job,insert):
    dates=['2026-10-05T09:00:00+00:00','2026-10-04T10:00:00+00:00','2026-10-04T09:59:59+00:00','2026-09-01T00:00:00+00:00','','2026-10-06T10:00:00+00:00']
    items=[]
    for i,published in enumerate(dates):
        item=hh_job(str(i));item['published_at']=published;items.append(item)
    rid=insert(items)
    with db.connect() as con:con.execute('UPDATE runs SET finished=? WHERE id=?',('2026-10-05T10:00:00+00:00',rid))
    assert len(analysis.select_jobs(source='hh',days=1)[0])==2
    assert len(analysis.select_jobs(source='hh',days=3)[0])==3
    assert len(analysis.select_jobs(source='hh',days=30)[0])==3
    assert len(analysis.select_jobs(source='hh',days=90)[0])==4
    assert len(analysis.select_jobs(source='hh',days=0)[0])==6
    data=analysis.summary(filters(source='hh',days=1))
    assert data['n']==2 and data['publication_window']['total']==6
    assert data['publication_window']['missing_dates']==1
    assert data['publication_window']['oldest']=='2026-09-01T00:00:00+00:00'


def test_multi_source_period_uses_each_snapshot_date(hh_job,insert):
    item=hh_job();item['published_at']='2026-10-04T09:00:00+00:00';rid=insert([item])
    remote=insert([{'id':'55','title':'Data Engineer','company_name':'Remote Co','publication_date':'2026-10-04T09:00:00','description':'Python SQL','url':'https://remotive.com/remote-jobs/software-dev/test-55','category':'Software Development'}],source='remotive')
    with db.connect() as con:
        con.execute('UPDATE runs SET finished=? WHERE id=?',('2026-10-05T10:00:00+00:00',rid))
        con.execute('UPDATE runs SET finished=? WHERE id=?',('2026-10-04T10:00:00+00:00',remote))
    with TestClient(app) as c:
        short=c.get('/api/summary?source=hh,remotive&days=1').json()
        assert short['n']==1 and short['publication_window']['total']==2
        assert c.get('/api/summary?source=hh,remotive&days=3').json()['n']==2
        assert c.get('/api/jobs?source=hh,remotive&days=1').json()['total']==1


def statistics_rows(response):
    import csv,io
    assert response.status_code==200
    assert response.content.startswith(b'\xef\xbb\xbf')
    assert 'text/csv' in response.headers['content-type']
    assert 'attachment;' in response.headers['content-disposition']
    rows=list(csv.DictReader(io.StringIO(response.content.decode('utf-8-sig')),delimiter=';'))
    assert rows and all(len(r)==7 and None not in r for r in rows)
    return rows


def test_statistics_csv_matches_filtered_dashboard_and_has_readable_numbers(hh_job,insert):
    insert([hh_job('1',description='Python SQL'),hh_job('2',description='SQL'),hh_job('3',description='Nothing recognized')])
    with TestClient(app) as c:
        response=c.get('/api/export.csv?source=hh&days=0')
        rows=statistics_rows(response)
        dashboard=c.get('/api/summary?source=hh&days=0').json()
        headline=next(r for r in rows if r['Показатель']=='Вакансии в выборке')
        assert int(headline['Значение'])==dashboard['n']==3
        python=next(r for r in rows if r['Раздел']=='Навыки' and r['Категория']=='Python' and r['Показатель']=='Доля вакансий с навыком')
        assert python['Значение']=='33,33' and python['Единица']=='%' and python['База расчёта (объявлений)']=='3'
        assert sum(int(r['Значение']) for r in rows if r['Раздел']=='Направления' and r['Показатель']=='Вакансии')==3
        assert all('https://' not in r['Значение'] for r in rows)
        filtered=statistics_rows(c.get('/api/export.csv?source=hh&days=0&skill=Python'))
        assert next(r for r in filtered if r['Показатель']=='Вакансии в выборке')['Значение']=='1'
        assert next(r for r in filtered if r['Показатель']=='Навык')['Значение']=='Python'


def test_statistics_csv_empty_and_small_salary_group_do_not_invent_metrics(hh_job,insert):
    with TestClient(app) as c:
        empty=statistics_rows(c.get('/api/export.csv?source=hh&days=0'))
        assert next(r for r in empty if r['Показатель']=='Вакансии в выборке')['Значение']=='0'
        assert all(r['Значение']=='' for r in empty if r['Раздел']=='Сводка' and r['Категория']=='Доля')
        insert([hh_job(salary={'from':100,'to':200,'currency':'RUR','gross':True})])
        rows=statistics_rows(c.get('/api/export.csv?source=hh&days=0'))
        median=next(r for r in rows if r['Показатель']=='Медиана середины вилки')
        assert median['Значение']=='' and median['База расчёта (объявлений)']=='1'
        assert 'меньше 30' in median['Пояснение']


def test_statistics_csv_pins_source_snapshots_while_collecting(hh_job,insert,monkeypatch):
    insert([hh_job('1')])
    original=analysis.summary
    def update_after_summary(f):
        data=original(f);insert([hh_job('2'),hh_job('3')]);return data
    monkeypatch.setattr(analysis,'summary',update_after_summary)
    with TestClient(app) as c:
        rows=statistics_rows(c.get('/api/export.csv?source=hh&days=0'))
        assert next(r for r in rows if r['Показатель']=='Вакансии в выборке')['Значение']=='1'
        assert next(r for r in rows if r['Раздел']=='Источники' and r['Показатель']=='Вакансии в выборке')['Значение']=='1'
        assert next(r for r in rows if r['Раздел']=='Работодатели' and r['Показатель']=='Вакансии')['Значение']=='1'

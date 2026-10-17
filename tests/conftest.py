import os
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
os.environ['SKILLSCOPE_TEST']='1'
import pytest
from backend import db,collectors

@pytest.fixture(autouse=True)
def isolated_db(tmp_path,monkeypatch):
    monkeypatch.setattr(db,'DATA',tmp_path/'data')
    db.init()
    yield
    collectors.stop.set()

@pytest.fixture
def hh_job():
    def build(id='123',title='Python Developer',description='<h2>Requirements</h2><p>Required: Python, SQL and Docker.</p>',salary=None):
        return {'id':id,'name':title,'description':description,'salary':salary,
                'employer':{'name':'Example Employer'},'area':{'name':'Москва'},
                'published_at':'2026-10-01T12:00:00+03:00','alternate_url':'https://hh.ru/vacancy/'+id,
                'experience':{'id':'between1And3'},'key_skills':[]}
    return build

@pytest.fixture
def insert():
    def do(items,source='hh',scope=None):
        rid=collectors.begin(source,scope or {'test':True})
        collectors.ingest(source,items,rid,{'items':items})
        return rid
    return do

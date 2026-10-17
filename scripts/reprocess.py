"""Recompute stored extraction without making new network observations."""
import hashlib
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend import db
from backend.extract import evidence,EXTRACT_VERSION,excluded_title

def reprocess():
    changed=0
    with db.connect() as con:
        rows=con.execute('SELECT DISTINCT v.* FROM versions v JOIN run_items r ON r.version=v.id').fetchall()
        for row in rows:
            record=json.loads(row['payload'])
            if record.get('extraction_version')==EXTRACT_VERSION:continue
            if excluded_title(record['title']):
                con.execute('DELETE FROM run_items WHERE job=?',(row['job'],))
                changed+=1
                continue
            tags=list({e['quote'] for e in record['evidence'] if e['kind']=='tagged'})
            record['evidence']=evidence(record['description'],tags)
            record['extraction_version']=EXTRACT_VERSION
            body=json.dumps(record,ensure_ascii=False,sort_keys=True);digest=hashlib.sha256(body.encode()).hexdigest()
            con.execute('INSERT OR IGNORE INTO versions(job,hash,payload,created) VALUES(?,?,?,?)',(row['job'],digest,body,db.now()))
            vid=con.execute('SELECT id FROM versions WHERE job=? AND hash=?',(row['job'],digest)).fetchone()[0]
            for e in record['evidence']:
                con.execute('INSERT OR IGNORE INTO skill_evidence VALUES(?,?,?,?,?)',(vid,e['skill'],e['kind'],e['quote'],e['method']))
            con.execute('UPDATE run_items SET version=? WHERE version=?',(vid,row['id']))
            changed+=1
        con.execute("UPDATE runs SET accepted=(SELECT count(*) FROM run_items WHERE run_items.run=runs.id) WHERE status IN ('success','partial')")
    return changed

if __name__=='__main__':print('Пересчитано версий:',reprocess())

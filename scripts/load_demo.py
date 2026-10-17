"""Load fictional vacancies into an isolated database without network requests."""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import db, collectors

def main():
    db.init()
    db.save_settings({'scheduler': False, 'demo_dataset': True})
    with db.connect() as con:
        if con.execute('SELECT count(*) FROM jobs').fetchone()[0]:
            print('Using existing demo database; no records overwritten.')
            return
    payload = json.loads((db.ROOT / 'examples/vacancies.synthetic.json').read_text())
    for item in payload['data']:
        item['created_at'] = int(datetime.now(timezone.utc).timestamp())
    run = collectors.begin('arbeitnow', {'mode': 'synthetic_demo'})
    count = collectors.ingest('arbeitnow', payload['data'], run, payload, 'partial',
        'Вымышленные демонстрационные вакансии. Формат Arbeitnow; не данные площадки. Дата публикации задана при загрузке примера.')
    print(f'Loaded {count} fictional vacancies. Automatic collection is disabled.')

if __name__ == '__main__':
    main()

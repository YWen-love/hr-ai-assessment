import sqlite3, json, os
db = os.path.join('app', 'data', 'job_configs.db')
conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row
rows = conn.execute('SELECT job_key, job_name, config_json FROM job_config_table WHERE deleted_at IS NULL').fetchall()
for r in rows:
    cfg = json.loads(r['config_json'])
    rid = cfg.get('rag_doc_id', '(MISSING)')
    print(f"{r['job_key']} | rag_doc_id={rid}")
conn.close()

# 同时查 rag_docs.db 里有什么
print("\n--- rag_docs.db ---")
db2 = os.path.join('app', 'data', 'rag_docs.db')
if os.path.exists(db2):
    conn2 = sqlite3.connect(db2)
    conn2.row_factory = sqlite3.Row
    docs = conn2.execute('SELECT rag_doc_id, job_key, title FROM rag_docs').fetchall()
    for d in docs:
        cnt = conn2.execute('SELECT COUNT(*) as c FROM rag_chunks WHERE rag_doc_id=?', (d['rag_doc_id'],)).fetchone()
        print(f"  rag_doc_id={d['rag_doc_id']} | job_key={d['job_key']} | chunks={cnt['c']}")
    conn2.close()
else:
    print("  rag_docs.db not found")

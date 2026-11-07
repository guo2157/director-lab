import sqlite3, json
conn = sqlite3.connect('/home/ubuntu/director-lab/backend/data/director_lab.db')
conn.row_factory = sqlite3.Row

# 1. 查 wf_image_t2i_qwen21_turbo
print('=== wf_481ab4b94004 ===')
cur = conn.execute("SELECT * FROM workflow WHERE id = 'wf_481ab4b94004'")
for row in cur:
    d = dict(row)
    print(json.dumps(d, indent=2, ensure_ascii=False, default=str))

# 2. 查 wf_2babab605508
print('=== wf_2babab605508 ===')
cur2 = conn.execute("SELECT * FROM workflow WHERE id = 'wf_2babab605508'")
for row in cur2:
    d2 = dict(row)
    print(json.dumps(d2, indent=2, ensure_ascii=False, default=str))

# 3. 查所有 name 包含 'wf_image_t2i' 的记录
print('=== 所有 name 包含 wf_image_t2i 的记录 ===')
cur3 = conn.execute("SELECT id, name, workflow_type, capabilities FROM workflow WHERE name LIKE '%wf_image_t2i%'")
for row in cur3:
    d3 = dict(row)
    print(json.dumps(d3, indent=2, ensure_ascii=False, default=str))
    print('---')

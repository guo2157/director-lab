import sqlite3
conn = sqlite3.connect('data/director_lab.db')
c = conn.cursor()
print("=== workflow 表结构 ===")
for row in c.execute("PRAGMA table_info(workflow)"):
    print(row)
print()
print("=== 现有工作流记录 ===")
for row in c.execute("SELECT id, name, description, workflow_type, is_builtin, is_default, version FROM workflow ORDER BY name"):
    print(row)
print()
print("=== workflow_version 表结构 ===")
for row in c.execute("PRAGMA table_info(workflow_version)"):
    print(row)
conn.close()

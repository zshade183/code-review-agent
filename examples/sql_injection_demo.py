import sqlite3

def unsafe_login(username, password):
    conn = sqlite3.connect("test.db")
    cursor = conn.cursor()

    # 【高危漏洞】直接字符串拼接用户输入，没有任何过滤
    sql = f"SELECT * FROM users WHERE username = '{username}' AND password = '{password}'"
    print("执行的SQL语句：", sql)  

    cursor.execute(sql)
    result = cursor.fetchone()

    if result:
        print("✅ 登录成功！")
    else:
        print("❌ 登录失败")

    conn.close()


# 初始化测试数据（运行一次即可）
def init_db():
    conn = sqlite3.connect("test.db")
    cursor = conn.cursor()
    cursor.execute("CREATE TABLE IF NOT EXISTS users (username TEXT, password TEXT)")
    cursor.execute("INSERT OR IGNORE INTO users VALUES ('admin', '123456')")
    conn.commit()
    conn.close()

# 初始化数据库
init_db()
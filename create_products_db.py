import sqlite3

conn = sqlite3.connect('products.db')
c = conn.cursor()

c.execute('''CREATE TABLE IF NOT EXISTS products
             (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, description TEXT, price REAL)''')

# Добавление примерных товаров
products = [
    ('iPhone 14 Pro', 'Новый iPhone 14 Pro с улучшенной камерой и процессором.', 99999),
    ('Samsung Galaxy S22 Ultra', 'Флагманский смартфон Samsung с большим экраном и мощным процессором.', 89999),
    ('MacBook Air M2', 'Легкий ноутбук Apple с чипом M2.', 149999),
]

c.executemany("INSERT OR IGNORE INTO products (name, description, price) VALUES (?, ?, ?)", products)

conn.commit()
conn.close()

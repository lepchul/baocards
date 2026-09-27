"""Transactional game engine. No Telegram dependency."""
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager, closing
from pathlib import Path


class GameError(Exception):
    def __init__(self, key, **values):
        self.key, self.values = key, values
        super().__init__(key)


def clean(value):
    return str(value).replace('\u2014', '-').replace('\u2013', '-')


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        with closing(self.connect()) as c, c:
            c.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS users(
              id INTEGER PRIMARY KEY, username TEXT, name TEXT NOT NULL,
              coins INTEGER NOT NULL DEFAULT 0 CHECK(coins>=0),
              points INTEGER NOT NULL DEFAULT 0 CHECK(points>=0),
              earned INTEGER NOT NULL DEFAULT 0, opened INTEGER NOT NULL DEFAULT 0,
              sold INTEGER NOT NULL DEFAULT 0, sent INTEGER NOT NULL DEFAULT 0,
              received INTEGER NOT NULL DEFAULT 0, level INTEGER NOT NULL DEFAULT 0 CHECK(level BETWEEN 0 AND 6),
              last_open REAL NOT NULL DEFAULT 0, banned INTEGER NOT NULL DEFAULT 0,
              joined REAL NOT NULL, seen REAL NOT NULL);
            CREATE UNIQUE INDEX IF NOT EXISTS users_username ON users(username) WHERE username IS NOT NULL;
            CREATE TABLE IF NOT EXISTS rarities(
              id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, weight INTEGER NOT NULL CHECK(weight>0));
            CREATE TABLE IF NOT EXISTS cards(
              id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
              rarity INTEGER NOT NULL REFERENCES rarities(id), price INTEGER NOT NULL CHECK(price>=0),
              points INTEGER NOT NULL CHECK(points>=0), photo TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS items(
              id TEXT PRIMARY KEY, owner INTEGER NOT NULL REFERENCES users(id),
              card INTEGER NOT NULL REFERENCES cards(id), name TEXT NOT NULL, rarity TEXT NOT NULL,
              price INTEGER NOT NULL, points INTEGER NOT NULL, photo TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'owned', favorite INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS item_owner ON items(owner,status);
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS audit(
              id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, actor INTEGER NOT NULL,
              action TEXT NOT NULL, target INTEGER, item TEXT, detail TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS audit_actor ON audit(actor,id);
            CREATE TABLE IF NOT EXISTS operations(key TEXT PRIMARY KEY,at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS pending(
              token TEXT PRIMARY KEY, actor INTEGER NOT NULL, action TEXT NOT NULL,
              payload TEXT NOT NULL, expires REAL NOT NULL);
            ''')
            c.execute("INSERT OR IGNORE INTO settings VALUES('triggers',?)", (json.dumps(['бао','бао карта','дай бао','открыть бао','bao']),))
            c.execute("INSERT OR IGNORE INTO settings VALUES('upgrade_cost','100')")
            if not c.execute('SELECT 1 FROM rarities').fetchone():
                c.executemany('INSERT INTO rarities(name,weight) VALUES(?,?)', [('Обычная',70),('Редкая',23),('Легендарная',7)])

    def connect(self):
        c = sqlite3.connect(self.path, timeout=15)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA foreign_keys=ON')
        c.execute('PRAGMA busy_timeout=15000')
        return c

    @contextmanager
    def tx(self):
        c = self.connect()
        try:
            c.execute('BEGIN IMMEDIATE')
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

    def rows(self, sql, args=()):
        with closing(self.connect()) as c, c:
            return [dict(r) for r in c.execute(sql, args)]

    def one(self, sql, args=()):
        rows = self.rows(sql, args)
        return rows[0] if rows else None

    @staticmethod
    def log(c, actor, action, target=None, item=None, **detail):
        c.execute('INSERT INTO audit(at,actor,action,target,item,detail) VALUES(?,?,?,?,?,?)',
                  (time.time(),actor,action,target,item,json.dumps(detail,ensure_ascii=False)))

    def touch(self, uid, username, name):
        username = username.lower() if username else None
        with self.tx() as c:
            old = c.execute('SELECT username,name FROM users WHERE id=?',(uid,)).fetchone()
            if username:
                c.execute('UPDATE users SET username=NULL WHERE username=? AND id<>?',(username,uid))
            c.execute('''INSERT INTO users(id,username,name,joined,seen) VALUES(?,?,?,?,?)
              ON CONFLICT(id) DO UPDATE SET username=excluded.username,name=excluded.name,seen=excluded.seen''',
                      (uid,username,clean(name)[:128],time.time(),time.time()))
            if old is None:
                self.log(c,uid,'join',username=username,name=clean(name))
            elif old['username'] != username or old['name'] != clean(name)[:128]:
                self.log(c,uid,'identity',username=username,name=clean(name))

    @staticmethod
    def user(c, uid):
        row = c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
        if not row:
            raise GameError('unknown_user')
        if row['banned']:
            raise GameError('banned')
        return row

    def setting(self, key, default=None):
        r = self.one('SELECT value FROM settings WHERE key=?',(key,))
        return json.loads(r['value']) if r else default

    def set_setting(self, actor, key, value):
        with self.tx() as c:
            c.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',(key,json.dumps(value,ensure_ascii=False)))
            self.log(c,actor,'setting',key=key,value=value)

    @staticmethod
    def once(c, key):
        try:
            c.execute('INSERT INTO operations VALUES(?,?)',(key,time.time()))
        except sqlite3.IntegrityError:
            raise GameError('already_done')

    def draw(self, uid, key, now=None):
        now = time.time() if now is None else now
        with self.tx() as c:
            u = self.user(c,uid)
            self.once(c,key)
            wait = u['last_open'] + (120-u['level']*15)*60 - now
            if wait>0:
                raise GameError('cooldown',minutes=int(wait//60)+1)
            rs = c.execute('SELECT * FROM rarities WHERE id IN (SELECT rarity FROM cards WHERE active=1)').fetchall()
            if not rs:
                raise GameError('no_cards')
            pick = secrets.randbelow(sum(r['weight'] for r in rs))
            for rarity in rs:
                pick -= rarity['weight']
                if pick<0:
                    break
            cards = c.execute('SELECT * FROM cards WHERE rarity=? AND active=1',(rarity['id'],)).fetchall()
            card = secrets.choice(cards)
            while True:
                iid = str(100000000000 + secrets.randbelow(900000000000))
                if not c.execute('SELECT 1 FROM items WHERE id=?',(iid,)).fetchone():
                    break
            c.execute('INSERT INTO items(id,owner,card,name,rarity,price,points,photo,created) VALUES(?,?,?,?,?,?,?,?,?)',
                      (iid,uid,card['id'],card['name'],rarity['name'],card['price'],card['points'],card['photo'],now))
            c.execute('UPDATE users SET last_open=?,points=points+?,earned=earned+?,opened=opened+1 WHERE id=?',
                      (now,card['points'],card['points'],uid))
            self.log(c,uid,'draw',item=iid,chat_operation=key,card=card['id'],points=card['points'])
            return dict(c.execute('SELECT * FROM items WHERE id=?',(iid,)).fetchone())

    def owned(self, uid, iid):
        r = self.one("SELECT * FROM items WHERE id=? AND owner=? AND status='owned'",(iid,uid))
        if not r:
            raise GameError('not_owned')
        return r

    def pending(self, uid, action, payload):
        token = secrets.token_hex(12)
        with self.tx() as c:
            self.user(c,uid)
            c.execute('DELETE FROM pending WHERE expires<?',(time.time(),))
            c.execute('INSERT INTO pending VALUES(?,?,?,?,?)',(token,uid,action,json.dumps(payload),time.time()+300))
        return token

    def confirm(self, uid, token):
        with self.tx() as c:
            u = self.user(c,uid)
            p = c.execute('SELECT * FROM pending WHERE token=? AND actor=?',(token,uid)).fetchone()
            if not p or p['expires'] < time.time():
                raise GameError('expired')
            data = json.loads(p['payload'])
            action = p['action']
            if action in ('sell','send'):
                item = c.execute("SELECT * FROM items WHERE id=? AND owner=? AND status='owned'",(data['item'],uid)).fetchone()
                if not item:
                    raise GameError('not_owned')
                if item['favorite']:
                    raise GameError('favorite_locked')
                if action=='sell':
                    c.execute("UPDATE items SET status='sold' WHERE id=?",(item['id'],))
                    c.execute('UPDATE users SET coins=coins+?,sold=sold+1 WHERE id=?',(item['price'],uid))
                    self.log(c,uid,'sell',item=item['id'],coins=item['price'])
                    result = dict(amount=item['price'],id=item['id'])
                else:
                    recipient = self.user(c,data['target'])
                    if uid==recipient['id']:
                        raise GameError('self_send')
                    c.execute('UPDATE items SET owner=? WHERE id=?',(recipient['id'],item['id']))
                    c.execute('UPDATE users SET sent=sent+1 WHERE id=?',(uid,))
                    c.execute('UPDATE users SET received=received+1 WHERE id=?',(recipient['id'],))
                    self.log(c,uid,'send',target=recipient['id'],item=item['id'])
                    result = dict(id=item['id'],target=recipient['id'],name=item['name'])
            elif action=='upgrade':
                cost = json.loads(c.execute("SELECT value FROM settings WHERE key='upgrade_cost'").fetchone()[0])
                if u['level']>=6:
                    raise GameError('max_upgrade')
                if data['level'] != u['level'] or data['cost'] != cost:
                    raise GameError('expired')
                if u['points']<cost:
                    raise GameError('not_enough',amount=cost)
                c.execute('UPDATE users SET points=points-?,level=level+1 WHERE id=?',(cost,uid))
                self.log(c,uid,'upgrade',cost=cost,level=u['level']+1)
                result = dict(minutes=120-(u['level']+1)*15)
            else:
                raise GameError('expired')
            if action in ('sell','send'):
                c.execute("DELETE FROM pending WHERE json_extract(payload,'$.item')=?",(data['item'],))
            else:
                c.execute('DELETE FROM pending WHERE token=?',(token,))
            return action, result

    def favorite(self, uid, iid):
        with self.tx() as c:
            self.user(c,uid)
            item=c.execute("SELECT * FROM items WHERE id=? AND owner=? AND status='owned'",(iid,uid)).fetchone()
            if not item:
                raise GameError('not_owned')
            value=1-item['favorite']
            c.execute('UPDATE items SET favorite=? WHERE id=?',(value,iid))
            self.log(c,uid,'favorite',item=iid,value=value)
            return value

    def admin_card(self, actor, data, cid=None):
        with self.tx() as c:
            if not c.execute('SELECT 1 FROM rarities WHERE id=?',(data['rarity'],)).fetchone():
                raise GameError('bad_rarity')
            if cid:
                if not c.execute('SELECT 1 FROM cards WHERE id=?',(cid,)).fetchone():
                    raise GameError('bad_card')
                c.execute('UPDATE cards SET name=?,rarity=?,price=?,points=?,photo=?,active=? WHERE id=?',
                          (data['name'],data['rarity'],data['price'],data['points'],data['photo'],data['active'],cid))
            else:
                cid=c.execute('INSERT INTO cards(name,rarity,price,points,photo,active) VALUES(?,?,?,?,?,?)',
                              (data['name'],data['rarity'],data['price'],data['points'],data['photo'],data['active'])).lastrowid
            self.log(c,actor,'admin_card',card=cid,data=data)
            return cid

    def backup(self, destination):
        with closing(self.connect()) as src, closing(sqlite3.connect(destination)) as dest, dest:
            src.backup(dest)

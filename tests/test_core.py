import concurrent.futures
import tempfile
import unittest
from pathlib import Path

from core import Store, GameError


class GameTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=Store(Path(self.tmp.name)/'game.sqlite3')
        self.db.touch(1,'alice','Alice')
        self.db.touch(2,'bob','Bob')
        self.card={'name':'Bao','rarity':1,'price':50,'points':100,'photo':'telegram-photo-id','active':1}
        self.cid=self.db.admin_card(99,self.card)

    def tearDown(self): self.tmp.cleanup()

    def draw(self,key='first',now=10000): return self.db.draw(1,key,now)

    def assertError(self,key,fn,*args):
        with self.assertRaises(GameError) as e: fn(*args)
        self.assertEqual(e.exception.key,key)

    def user(self,uid=1): return self.db.one('SELECT * FROM users WHERE id=?',(uid,))

    def test_draw_timer_global_and_unique_id(self):
        a=self.draw()
        self.assertTrue(a['id'].isdigit())
        self.assertEqual(len(a['id']),12)
        self.assertError('cooldown',self.db.draw,1,'other-chat',10001)
        b=self.draw('later',17200)
        self.assertNotEqual(a['id'],b['id'])
        self.assertEqual(self.user()['earned'],200)

    def test_retry_idempotent(self):
        self.draw()
        self.assertError('already_done',self.db.draw,1,'first',19000)
        self.assertEqual(self.user()['opened'],1)

    def test_no_cards_does_not_consume_timer(self):
        empty=Store(Path(self.tmp.name)/'empty.sqlite3'); empty.touch(1,None,'x')
        self.assertError('no_cards',empty.draw,1,'first',10000)
        self.assertEqual(empty.one('SELECT last_open FROM users')['last_open'],0)

    def test_sell_once_and_snapshot_price(self):
        item=self.draw()
        new=dict(self.card,price=999); self.db.admin_card(99,new,self.cid)
        t=self.db.pending(1,'sell',{'item':item['id']})
        _,r=self.db.confirm(1,t)
        self.assertEqual(r['amount'],50)
        self.assertEqual(self.user()['coins'],50)
        self.assertError('expired',self.db.confirm,1,t)
        self.assertEqual(self.user()['coins'],50)

    def test_send_retains_id_and_does_not_mint_points(self):
        item=self.draw()
        t=self.db.pending(1,'send',{'item':item['id'],'target':2})
        self.db.confirm(1,t)
        self.assertEqual(self.db.owned(2,item['id'])['id'],item['id'])
        self.assertEqual(self.user(2)['earned'],0)
        self.assertEqual(self.user(2)['points'],0)
        self.assertError('not_owned',self.db.owned,1,item['id'])

    def test_wrong_actor_cannot_confirm(self):
        item=self.draw(); t=self.db.pending(1,'sell',{'item':item['id']})
        self.assertError('expired',self.db.confirm,2,t)
        self.assertEqual(self.db.owned(1,item['id'])['owner'],1)

    def test_favorite_protects_existing_confirmation(self):
        item=self.draw(); t=self.db.pending(1,'sell',{'item':item['id']})
        self.db.favorite(1,item['id'])
        self.assertError('favorite_locked',self.db.confirm,1,t)

    def test_ban_applies_at_confirmation(self):
        item=self.draw(); t=self.db.pending(1,'send',{'item':item['id'],'target':2})
        with self.db.tx() as c: c.execute('UPDATE users SET banned=1 WHERE id=2')
        self.assertError('banned',self.db.confirm,1,t)
        self.assertEqual(self.db.owned(1,item['id'])['owner'],1)

    def test_upgrade_floor_and_earned_unchanged(self):
        self.draw()
        with self.db.tx() as c: c.execute('UPDATE users SET points=1000 WHERE id=1')
        for level in range(6):
            t=self.db.pending(1,'upgrade',{'cost':100,'level':level})
            _,result=self.db.confirm(1,t)
        self.assertEqual(result['minutes'],30)
        self.assertEqual(self.user()['points'],400)
        self.assertEqual(self.user()['earned'],100)
        self.assertError('cooldown',self.db.draw,1,'early',11799)
        self.db.draw(1,'ready',11800)
        t=self.db.pending(1,'upgrade',{'cost':100,'level':6})
        self.assertError('max_upgrade',self.db.confirm,1,t)

    def test_upgrade_stale_price_and_insufficient_balance(self):
        t=self.db.pending(1,'upgrade',{'cost':100,'level':0})
        self.assertError('not_enough',self.db.confirm,1,t)
        self.db.set_setting(99,'upgrade_cost',200)
        self.assertError('expired',self.db.confirm,1,t)
        self.assertEqual(self.user()['level'],0)

    def test_concurrent_draws_only_one_wins(self):
        def attempt(n):
            try: return self.draw('parallel-'+str(n))['id']
            except GameError: return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(attempt,range(16)))
        self.assertEqual(sum(r is not None for r in results),1)
        self.assertEqual(self.user()['earned'],100)

    def test_concurrent_sell_and_send_only_one_wins(self):
        item=self.draw()
        a=self.db.pending(1,'sell',{'item':item['id']})
        b=self.db.pending(1,'send',{'item':item['id'],'target':2})
        def attempt(t):
            try: return self.db.confirm(1,t)[0]
            except GameError: return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(attempt,[a,b]))
        self.assertEqual(sum(r is not None for r in results),1)
        self.assertEqual(self.user()['sold']+self.user()['sent'],1)

    def test_username_reassignment_uses_numeric_identity(self):
        item=self.draw(); t=self.db.pending(1,'send',{'item':item['id'],'target':2})
        self.db.touch(3,'bob','Different person')
        self.assertIsNone(self.user(2)['username'])
        self.db.confirm(1,t)
        self.assertEqual(self.db.owned(2,item['id'])['owner'],2)

    def test_backup_and_restart_preserve_game(self):
        item=self.draw(); path=Path(self.tmp.name)/'backup.sqlite3'; self.db.backup(path)
        restored=Store(path)
        self.assertEqual(restored.owned(1,item['id'])['price'],50)
        self.assertError('cooldown',restored.draw,1,'restart',10001)
        self.assertEqual(restored.one("SELECT COUNT(*) n FROM audit WHERE action='draw'")['n'],1)

    def test_expired_confirmation(self):
        item=self.draw(); t=self.db.pending(1,'sell',{'item':item['id']})
        with self.db.tx() as c: c.execute('UPDATE pending SET expires=0 WHERE token=?',(t,))
        self.assertError('expired',self.db.confirm,1,t)
        self.assertEqual(self.user()['coins'],0)


if __name__=='__main__': unittest.main()

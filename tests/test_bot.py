"""Offline dispatcher integration tests with a fake Telegram transport."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.types import Message, Update, UserProfilePhotos, PhotoSize

BOOT=tempfile.TemporaryDirectory()
os.environ['DATABASE_PATH']=str(Path(BOOT.name)/'bootstrap.sqlite3')
os.environ['CONFIG_PATH']=str(Path(BOOT.name)/'config.json')
Path(os.environ['CONFIG_PATH']).write_text(json.dumps({'admin_ids':[99]}))
import bot as app
from core import Store


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__(); self.calls=[]; self.counter=1000

    async def close(self): pass

    async def make_request(self,bot,method,timeout=None):
        self.calls.append(method)
        if method.__api_method__=='getUserProfilePhotos':
            return UserProfilePhotos(total_count=1,photos=[[PhotoSize(file_id='avatar',file_unique_id='avatar-unique',width=100,height=100)]])
        if method.__api_method__=='answerCallbackQuery': return True
        self.counter+=1
        return Message(message_id=self.counter,date=datetime.now(timezone.utc),chat={'id':getattr(method,'chat_id',1),'type':'private'},
                       from_user={'id':123456,'is_bot':True,'first_name':'Bao'},text=getattr(method,'text',None))

    async def stream_content(self,*args,**kwargs):
        yield b''


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        app.DB=Store(Path(self.tmp.name)/'db.sqlite3')
        app.SESSIONS.clear(); app.ADMINS={99}; app.BOT_USERNAME='BaoTestBot'
        self.session=FakeSession()
        self.bot=Bot('123456:ABCDEFfake_token_for_offline_tests',session=self.session)
        self.sequence=0

    async def asyncTearDown(self):
        await self.bot.session.close(); self.tmp.cleanup()

    async def message(self,text=None,uid=1,chat=None,photo=False,username=None):
        self.sequence+=1
        data={'message_id':self.sequence,'date':1,'chat':{'id':chat or uid,'type':'group' if chat and chat<0 else 'private'},
              'from':{'id':uid,'is_bot':False,'first_name':f'User{uid}','username':username or f'user{uid}'}}
        if text is not None: data['text']=text
        if photo: data['photo']=[{'file_id':'test_photo','file_unique_id':'unique_photo','width':100,'height':100}]
        await app.dp.feed_update(self.bot,Update.model_validate({'update_id':self.sequence,'message':data}))

    async def callback(self,action,uid=1,owner=None,chat=None):
        self.sequence+=1
        await app.dp.feed_update(self.bot,Update.model_validate({'update_id':self.sequence,'callback_query':{
            'id':str(self.sequence),'chat_instance':'test','from':{'id':uid,'is_bot':False,'first_name':f'User{uid}','username':f'user{uid}'},
            'data':f'{owner or uid}|{action}',
            'message':{'message_id':100,'date':1,'chat':{'id':chat or uid,'type':'group' if chat and chat<0 else 'private'},
                       'from':{'id':123456,'is_bot':True,'first_name':'Bao'},'text':'menu'}}}))

    def add_card(self):
        app.DB.admin_card(99,dict(name='Bao',rarity=1,price=50,points=20,photo='test_photo',active=1))

    def texts(self):
        return '\n'.join(str(getattr(m,'text','') or getattr(m,'caption','') or '') for m in self.session.calls)

    async def test_start_and_buttons(self):
        await self.message('/start')
        method=self.session.calls[-1]
        self.assertIn('Bao Cards',method.text)
        self.assertTrue(any(b.url and 'startgroup' in b.url for row in method.reply_markup.inline_keyboard for b in row))
        await self.callback('help')
        self.assertIn('/buyer',self.texts())

    async def test_add_card_wizard_and_drop_photo(self):
        await self.message('/admin',uid=99)
        await self.callback('a:add',uid=99)
        for text in ('Моя карта','1','50','20'):
            await self.message(text,uid=99)
        await self.message(uid=99,photo=True)
        self.assertEqual(app.DB.one('SELECT COUNT(*) n FROM cards')['n'],1)
        self.assertNotIn(99,app.SESSIONS)
        await self.message('бао')
        method=self.session.calls[-1]
        self.assertEqual(method.__api_method__,'sendPhoto')
        self.assertIn('✨ Вы получили карточку "Моя карта" !',method.caption)
        self.assertIn('Очков: + 20',method.caption)

    async def test_admin_denied_and_group_denied(self):
        await self.message('/admin')
        self.assertIn('только администраторам',self.texts())
        await self.callback('a:add')
        self.assertNotIn(1,app.SESSIONS)
        await self.message('/admin',uid=99,chat=-100)
        self.assertNotIn(99,app.SESSIONS)

    async def test_foreign_button_cannot_open(self):
        self.add_card()
        await self.callback('draw',uid=2,owner=1)
        self.assertEqual(app.DB.one('SELECT COUNT(*) n FROM items')['n'],0)
        self.assertIn('другого игрока',self.texts())

    async def test_group_phrase_and_global_cooldown(self):
        self.add_card()
        await self.message('БАО',chat=-100)
        await self.message('бао')
        self.assertEqual(app.DB.one('SELECT COUNT(*) n FROM items')['n'],1)
        self.assertIn('Следующая карта',self.texts())

    async def test_buyer_sale_through_callback(self):
        self.add_card(); await self.message('бао')
        item=app.DB.one('SELECT * FROM items')
        await self.message('/buyer')
        await self.callback('sell:'+item['id'])
        token=app.DB.one('SELECT token FROM pending')['token']
        await self.callback('confirm:'+token)
        self.assertEqual(app.DB.one('SELECT coins FROM users WHERE id=1')['coins'],50)
        await self.callback('confirm:'+token)
        self.assertEqual(app.DB.one('SELECT coins FROM users WHERE id=1')['coins'],50)

    async def test_username_transfer_and_profile_avatar(self):
        self.add_card(); await self.message('/start',uid=2); await self.message('бао')
        item=app.DB.one('SELECT * FROM items')
        await self.message('/send @user2 '+item['id'])
        token=app.DB.one('SELECT token FROM pending')['token']; await self.callback('confirm:'+token)
        self.assertEqual(app.DB.owned(2,item['id'])['owner'],2)
        await self.message('/profile',uid=2)
        self.assertEqual(self.session.calls[-1].photo,'avatar')
        self.assertIn('Получено: 1',self.session.calls[-1].caption)

    async def test_templates_and_triggers(self):
        await self.message('/admin',uid=99)
        await self.callback('a:text:welcome',uid=99)
        await self.message('Привет \u2014 Бао',uid=99)
        await self.message('/start')
        self.assertEqual(self.session.calls[-1].text,'Привет - Бао')
        await self.callback('a:triggers',uid=99)
        await self.message('раз\nдва\nтри\nчетыре\nпять',uid=99)
        self.add_card(); await self.message('ПЯТЬ')
        self.assertEqual(app.DB.one('SELECT COUNT(*) n FROM items')['n'],1)

    async def test_invalid_template_stays_editable(self):
        await self.message('/admin',uid=99)
        await self.callback('a:text:drop',uid=99)
        await self.message('{name.__class__}',uid=99)
        self.assertIn(99,app.SESSIONS)
        self.assertIsNone(app.DB.setting('text:drop'))

    async def test_export_and_backup(self):
        await self.message('/start')
        await self.callback('a:logs',uid=99)
        self.assertEqual(self.session.calls[-1].__api_method__,'sendDocument')
        self.assertIn(b'join',self.session.calls[-1].document.data)
        await self.callback('a:backup',uid=99)
        self.assertTrue(self.session.calls[-1].document.data.startswith(b'SQLite format 3'))


if __name__=='__main__': unittest.main()

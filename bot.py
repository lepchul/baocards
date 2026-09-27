import asyncio
import csv
import io
import json
import logging
import math
import os
import string
import tempfile
import time
from pathlib import Path
from datetime import datetime, timezone
from functools import wraps
from contextlib import closing

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile, BotCommand
from aiogram.exceptions import TelegramAPIError
from dotenv import load_dotenv

from core import Store, GameError, clean
from texts import DEFAULTS

load_dotenv()
CONFIG = json.loads(Path(os.getenv('CONFIG_PATH','config.json')).read_text(encoding='utf-8'))
ADMINS = set(CONFIG['admin_ids'])
if not ADMINS or any(type(x) is not int or x<=0 for x in ADMINS):
    raise RuntimeError('Укажи числовые admin_ids в config.json')
DB = Store(os.getenv('DATABASE_PATH','data/bao.sqlite3'))
dp = Dispatcher()
SESSIONS = {}
BOT_USERNAME = ''
LOG = logging.getLogger('bao')


def tr(key, **values):
    template = DB.setting('text:'+key, DEFAULTS.get(key,key))
    result=clean(template.format(**values))
    limit=1024 if key in ('drop','card','profile') else 4096
    if len(result.encode('utf-16-le'))//2>limit:
        result=clean(DEFAULTS.get(key,key).format(**values))
    return result


def kb(uid, rows):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=clean(label)[:64], **({'url':action[4:]} if action.startswith('url:') else {'callback_data':f'{uid}|{action}'}))
        for label,action in row] for row in rows])


def menu(uid):
    return kb(uid,[[(tr('btn_draw'),'draw'),(tr('btn_cards'),'cards:0')],
                   [(tr('btn_profile'),'profile'),(tr('btn_buyer'),'buyer:0')],
                   [(tr('btn_upgrade'),'upgrade'),(tr('btn_top'),'top')],
                   [(tr('btn_help'),'help'),(tr('btn_group'),f'url:https://t.me/{BOT_USERNAME}?startgroup=true')]])


def confirm_keyboard(uid,token):
    return kb(uid,[[(tr('btn_confirm'),'confirm:'+token),(tr('btn_cancel'),'cancel:'+token)]])


def guard(fn):
    @wraps(fn)
    async def wrapped(event, *args, **kwargs):
        user = event.from_user
        if not user or user.is_bot:
            return
        msg=event.message if isinstance(event,CallbackQuery) else event
        if not isinstance(msg,Message):
            return
        # Anonymous group senders cannot own cards.
        if isinstance(event,Message):
            if event.sender_chat:
                return
            content=(event.text or '').strip()
            if event.chat.type!='private' and not content.startswith('/') and content.casefold() not in DB.setting('triggers'):
                return
        try:
            DB.touch(user.id,user.username,user.full_name)
            await fn(event,*args,**kwargs)
        except GameError as exc:
            if isinstance(event,CallbackQuery):
                await event.answer(tr(exc.key,**exc.values)[:200],show_alert=True)
            else:
                await msg.answer(tr(exc.key,**exc.values))
        except (ValueError,KeyError,IndexError):
            await msg.answer(tr('syntax'))
        except TelegramAPIError:
            LOG.warning('Telegram delivery failed for user %s',user.id)
        except Exception:
            LOG.exception('Handler failure for user %s',user.id)
            await msg.answer(tr('error'))
    return wrapped


def player(uid):
    with closing(DB.connect()) as c:
        return dict(DB.user(c,uid))


def label(user):
    return '@'+user['username'] if user['username'] else user['name']


async def show_card(msg, item, newly=False):
    caption=tr('drop' if newly else 'card',**item)
    try:
        await msg.answer_photo(item['photo'],caption=caption)
    except TelegramAPIError:
        await msg.answer(caption+'\n\n'+tr('photo_error'))


async def draw(msg,uid,key):
    item=DB.draw(uid,key)
    await show_card(msg,item,True)


async def inventory(msg,uid,mode='cards',page=0):
    page=max(0,page)
    where="owner=? AND status='owned'" + (' AND favorite=0' if mode=='buyer' else '')
    items=DB.rows('SELECT * FROM items WHERE '+where+' ORDER BY created DESC,id LIMIT 9 OFFSET ?',(uid,page*8))
    rows=[]
    for i in items[:8]:
        rows.append([(f'{i["name"][:28]} | {i["price"]} 🪙' if mode=='buyer' else f'{"🔒 " if i["favorite"] else ""}{i["name"][:30]}',
                      ('sell:' if mode=='buyer' else 'card:')+i['id'])])
    nav=[]
    if page: nav.append((tr('btn_prev'),f'{mode}:{page-1}'))
    if len(items)>8: nav.append((tr('btn_next'),f'{mode}:{page+1}'))
    if nav: rows.append(nav)
    body='\n'.join(f'{i["name"]} | {i["rarity"]} | {i["price"]} 🪙\nID {i["id"]}' for i in items[:8]) or tr('empty')
    await msg.answer(tr('buyer' if mode=='buyer' else 'inventory',page=page+1,rows=body),reply_markup=kb(uid,rows) if rows else None)


async def profile(msg,uid):
    u=player(uid)
    u['name']=label(u)
    u['count']=DB.one("SELECT COUNT(*) n FROM items WHERE owner=? AND status='owned'",(uid,))['n']
    u['interval']=120-u['level']*15
    u['wait']=max(0,math.ceil((u['last_open']+u['interval']*60-time.time())/60))
    caption=tr('profile',**u)
    try:
        photos=await msg.bot.get_user_profile_photos(uid,limit=1)
        if photos.total_count:
            await msg.answer_photo(photos.photos[0][-1].file_id,caption=caption)
            return
    except TelegramAPIError:
        pass
    await msg.answer(caption)


async def upgrade(msg,uid):
    u=player(uid)
    if u['level']>=6: raise GameError('max_upgrade')
    cost=DB.setting('upgrade_cost')
    token=DB.pending(uid,'upgrade',{'cost':cost,'level':u['level']})
    await msg.answer(tr('upgrade',interval=120-u['level']*15,next=105-u['level']*15,cost=cost,points=u['points']),reply_markup=confirm_keyboard(uid,token))


async def sell(msg,uid,iid):
    item=DB.owned(uid,iid)
    if item['favorite']: raise GameError('favorite_locked')
    token=DB.pending(uid,'sell',{'item':iid})
    await msg.answer(tr('sell_confirm',**item),reply_markup=confirm_keyboard(uid,token))


async def top(msg):
    users=DB.rows('SELECT * FROM users WHERE banned=0 ORDER BY earned DESC,id LIMIT 20')
    body='\n'.join(f'{n}. {label(u)} - {u["earned"]} 🚩' for n,u in enumerate(users,1))
    await msg.answer(tr('top',rows=body or tr('empty')))


async def history(msg,uid):
    events=DB.rows('SELECT * FROM audit WHERE actor=? OR target=? ORDER BY id DESC LIMIT 15',(uid,uid))
    body='\n'.join(f'{datetime.fromtimestamp(e["at"],timezone.utc):%d.%m %H:%M} | {e["action"]} | {e["item"] or ""}' for e in events)
    await msg.answer(tr('history',rows=body or tr('empty')))


def admin_allowed(msg,uid):
    if uid not in ADMINS or msg.chat.type!='private':
        raise GameError('private_admin')


async def admin_menu(msg,uid):
    admin_allowed(msg,uid)
    SESSIONS.pop(uid,None)
    stats=DB.one("SELECT COUNT(*) users,SUM(opened) opened,SUM(coins) coins FROM users")
    await msg.answer(f'🐼 Bao Cards | Админ-панель\n\nИгроков: {stats["users"]}\nОткрытий: {stats["opened"] or 0}\nМонет в обороте: {stats["coins"] or 0}',reply_markup=kb(uid,[
        [('🎴 Карты','a:cards:0'),('➕ Добавить карту','a:add')],
        [('🎮 Редкости и шансы','a:rarities'),('💬 Тексты и кнопки','a:texts:0')],
        [('🗣 5 фраз открытия','a:triggers'),('⚡ Цена ускорения','a:cost')],
        [('👤 Найти игрока','a:user'),('📊 Экспорт игроков','a:users')],
        [('📜 Все операции CSV','a:logs'),('💾 Резервная копия','a:backup')]]))


def session(uid,kind,**data):
    SESSIONS[uid]={'kind':kind,'expires':time.time()+1800,**data}


async def export(msg,kind,uid=None):
    if kind=='users':
        rows=DB.rows('SELECT * FROM users ORDER BY id')
    elif uid:
        rows=DB.rows('SELECT * FROM audit WHERE actor=? OR target=? ORDER BY id',(uid,uid))
    else:
        rows=DB.rows('SELECT * FROM audit ORDER BY id')
    out=io.StringIO()
    if rows:
        writer=csv.DictWriter(out,fieldnames=rows[0].keys())
        writer.writeheader()
        for row in rows:
            # Prevent formulas when administrators open the CSV in a spreadsheet.
            writer.writerow({k:("'"+v if isinstance(v,str) and v.startswith(('=','+','-','@','\t','\r')) else v) for k,v in row.items()})
    await msg.answer_document(BufferedInputFile(out.getvalue().encode('utf-8-sig'),filename=kind+'.csv'))


async def admin_action(msg,uid,action):
    admin_allowed(msg,uid)
    parts=action.split(':')
    op=parts[1]
    if op=='home': return await admin_menu(msg,uid)
    if op=='cards':
        page=max(0,int(parts[2])); cards=DB.rows('SELECT * FROM cards ORDER BY id DESC LIMIT 9 OFFSET ?',(page*8,))
        rows=[[(f'#{c["id"]} {c["name"][:30]} {"✅" if c["active"] else "⏸"}',f'a:edit:{c["id"]}')] for c in cards[:8]]
        nav=[]
        if page: nav.append(('⬅',f'a:cards:{page-1}'))
        if len(cards)>8: nav.append(('➡',f'a:cards:{page+1}'))
        if nav: rows.append(nav)
        rows.append([('➕ Добавить','a:add'),('В меню','a:home')])
        await msg.answer('Каталог карт. Старые экземпляры сохраняют исходную цену, очки и изображение.',reply_markup=kb(uid,rows))
    elif op=='add':
        session(uid,'add',step=0,data={})
        await msg.answer('Добавление карты, 1/5. Отправь название (до 80 символов).\n/cancel - отмена')
    elif op=='edit':
        cid=int(parts[2]); card=DB.one('SELECT * FROM cards WHERE id=?',(cid,))
        if not card: raise GameError('bad_card')
        await show_admin_card(msg,uid,card)
    elif op=='field':
        cid=int(parts[2]); field=parts[3]
        if field not in ('name','rarity','price','points','photo','active'): raise ValueError()
        session(uid,'field',cid=cid,field=field)
        hints={'name':'новое название','rarity':'ID редкости из раздела «Редкости»','price':'стоимость, целое число 0..1000000000','points':'очки, целое число 0..1000000000','photo':'новую картинку как фото','active':'1 - включить выпадение, 0 - выключить'}
        await msg.answer('Отправь '+hints[field]+'.\n/cancel - отмена')
    elif op=='rarities':
        rs=DB.rows('SELECT * FROM rarities ORDER BY id')
        await msg.answer('Вес - относительный шанс редкости среди редкостей с активными картами. Внутри редкости карты равновероятны.\n\n'+'\n'.join(f'ID {r["id"]}: {r["name"]} | вес {r["weight"]}' for r in rs),reply_markup=kb(uid,[[('➕ Добавить','a:rarity:new')]]+[[(r['name'],f'a:rarity:{r["id"]}')] for r in rs]))
    elif op=='rarity':
        session(uid,'rarity',rid=parts[2])
        await msg.answer('Отправь название и вес на двух строках. Пример:\nЛегендарная\n7\n\nВес: 1..1000000. /cancel - отмена')
    elif op=='texts':
        page=max(0,int(parts[2])); keys=list(DEFAULTS)[page*8:page*8+8]
        rows=[[(k,f'a:text:{k}')] for k in keys]
        nav=[]
        if page: nav.append(('⬅',f'a:texts:{page-1}'))
        if (page+1)*8<len(DEFAULTS): nav.append(('➡',f'a:texts:{page+1}'))
        if nav: rows.append(nav)
        await msg.answer('Тексты сообщений и кнопок. Выбери ключ. Шаблоны используют {переменные}.',reply_markup=kb(uid,rows))
    elif op=='text':
        key=parts[2]
        if key not in DEFAULTS: raise ValueError()
        session(uid,'text',key=key)
        await msg.answer(f'Ключ: {key}\n\nТекущий текст:\n{DB.setting("text:"+key,DEFAULTS[key])}\n\nПришли новый текст. Сохрани переменные в фигурных скобках. /reset - исходный текст, /cancel - отмена.')
    elif op=='triggers':
        session(uid,'triggers')
        await msg.answer('Сейчас: '+', '.join(DB.setting('triggers'))+'\n\nОтправь ровно 5 разных фраз, каждую с новой строки. Регистр не важен. /cancel - отмена')
    elif op=='cost':
        session(uid,'cost')
        await msg.answer(f'Сейчас шаг ускорения стоит {DB.setting("upgrade_cost")} очков.\nОтправь новую цену (1..1000000000). /cancel - отмена')
    elif op=='user':
        session(uid,'user')
        await msg.answer('Отправь Telegram ID или @username игрока. /cancel - отмена')
    elif op=='inspect':
        await admin_user(msg,uid,int(parts[2]))
    elif op=='ban':
        target=int(parts[2]); value=int(parts[3])
        if target in ADMINS: raise ValueError()
        with DB.tx() as c:
            c.execute('UPDATE users SET banned=? WHERE id=?',(value,target))
            DB.log(c,uid,'admin_ban',target=target,value=value)
        await admin_user(msg,uid,target)
    elif op=='balance':
        session(uid,'balance',target=int(parts[2]))
        await msg.answer('Отправь изменение монет и доступных очков через пробел, например: 100 -20.\nРейтинг earned не меняется. Затем будет запрошена причина. /cancel - отмена')
    elif op=='userlogs':
        await export(msg,'audit',int(parts[2]))
    elif op in ('users','logs'):
        await export(msg,'users' if op=='users' else 'audit')
    elif op=='backup':
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'bao.sqlite3'; DB.backup(path)
            await msg.answer_document(BufferedInputFile(path.read_bytes(),filename='bao-backup.sqlite3'))


async def show_admin_card(msg,uid,c):
    await msg.answer(f'Карта каталога #{c["id"]}\n{c["name"]}\nРедкость ID: {c["rarity"]}\nЦена: {c["price"]}\nОчки: {c["points"]}\nАктивна: {c["active"]}',reply_markup=kb(uid,[[(name,f'a:field:{c["id"]}:{field}')] for name,field in [('Название','name'),('Редкость','rarity'),('Стоимость','price'),('Очки','points'),('Картинка','photo'),('Включить/выключить','active')]]))
    try: await msg.answer_photo(c['photo'])
    except TelegramAPIError: await msg.answer('Не удалось загрузить фото. Замени картинку.')


async def admin_user(msg,uid,target):
    u=DB.one('SELECT * FROM users WHERE id=?',(target,))
    if not u: raise GameError('unknown_user')
    await msg.answer(f'{label(u)}\nTelegram ID: {target}\nМонет: {u["coins"]}\nОчков: {u["points"]}\nВсего очков: {u["earned"]}\nБлокировка: {u["banned"]}',reply_markup=kb(uid,[
        [('Снять блокировку' if u['banned'] else 'Заблокировать',f'a:ban:{target}:{1-u["banned"]}')],
        [('Изменить баланс',f'a:balance:{target}'),('Операции CSV',f'a:userlogs:{target}')]]))


def number(text,low=0,high=1000000000):
    n=int(text)
    if not low<=n<=high: raise ValueError('Число вне допустимого диапазона.')
    return n


def fields(template):
    result=[]
    for _,field,spec,conversion in string.Formatter().parse(template):
        if field is not None:
            if not field.isidentifier() or spec or conversion:
                raise ValueError('Допустимы только простые переменные {name}.')
            result.append(field)
    return set(result)


async def admin_input(msg,uid,s):
    admin_allowed(msg,uid)
    text=clean(msg.text or '').strip()
    kind=s['kind']
    try:
        if kind=='add':
            step=s['step']; data=s['data']
            if step==0:
                if not 1<=len(text)<=80: raise ValueError('Название: 1..80 символов.')
                data['name']=text
                rs=DB.rows('SELECT * FROM rarities')
                prompt='2/5. Отправь ID редкости:\n'+'\n'.join(f'{r["id"]} - {r["name"]}' for r in rs)
            elif step==1:
                rid=number(text,1)
                if not DB.one('SELECT 1 FROM rarities WHERE id=?',(rid,)): raise ValueError('Нет такой редкости.')
                data['rarity']=rid; prompt='3/5. Стоимость карты в монетах (0..1000000000).'
            elif step==2:
                data['price']=number(text); prompt='4/5. Очки за получение (0..1000000000).'
            elif step==3:
                data['points']=number(text); prompt='5/5. Отправь картинку как фото, не как файл.'
            else:
                if not msg.photo: raise ValueError('Нужно отправить фото.')
                data['photo']=msg.photo[-1].file_id; data['active']=1
                cid=DB.admin_card(uid,data)
                SESSIONS.pop(uid,None)
                await msg.answer(f'✅ Карта #{cid} добавлена и участвует в выпадении.')
                return await admin_action(msg,uid,f'a:edit:{cid}')
            s['step']+=1
            return await msg.answer(prompt)
        elif kind=='field':
            c=DB.one('SELECT * FROM cards WHERE id=?',(s['cid'],))
            if not c: raise ValueError('Нет такой карты.')
            f=s['field']
            if f=='photo':
                if not msg.photo: raise ValueError('Отправь фото.')
                value=msg.photo[-1].file_id
            elif f=='name':
                if not 1<=len(text)<=80: raise ValueError('Название: 1..80 символов.')
                value=text
            else: value=number(text,1 if f=='rarity' else 0,1 if f=='active' else 1000000000)
            c[f]=value; DB.admin_card(uid,c,c['id'])
        elif kind=='rarity':
            lines=text.splitlines()
            if len(lines)!=2 or not 1<=len(lines[0])<=48: raise ValueError('Нужны 2 строки. Название до 48 символов.')
            weight=number(lines[1],1,1000000)
            with DB.tx() as c:
                if s['rid']=='new':
                    count=c.execute('SELECT COUNT(*) FROM rarities').fetchone()[0]
                    if count>=30: raise ValueError('Максимум 30 редкостей.')
                    rid=c.execute('INSERT INTO rarities(name,weight) VALUES(?,?)',(lines[0],weight)).lastrowid
                else:
                    rid=int(s['rid']); c.execute('UPDATE rarities SET name=?,weight=? WHERE id=?',(lines[0],weight,rid))
                DB.log(c,uid,'admin_rarity',rarity=rid,name=lines[0],weight=weight)
        elif kind=='text':
            key=s['key']; default=DEFAULTS[key]
            if text=='/reset': text=default
            required=fields(default)
            if fields(text)!=required: raise ValueError('Переменные должны совпадать: '+', '.join(sorted(required)))
            limit=60 if key.startswith('btn_') else (650 if key in ('drop','card','profile') else 1800)
            # Templates containing dynamic lists need room for appended entries.
            if not 1<=len(text)<=limit: raise ValueError(f'Длина: 1..{limit} символов.')
            DB.set_setting(uid,'text:'+key,text)
        elif kind=='triggers':
            phrases=[x.strip().casefold() for x in text.splitlines()]
            if len(phrases)!=5 or len(set(phrases))!=5 or any(not 1<=len(x)<=60 or x.startswith('/') for x in phrases):
                raise ValueError('Нужны 5 уникальных фраз длиной 1..60, без / в начале.')
            DB.set_setting(uid,'triggers',phrases)
        elif kind=='cost': DB.set_setting(uid,'upgrade_cost',number(text,1))
        elif kind=='user':
            u=DB.one('SELECT * FROM users WHERE username=?',(text.lstrip('@').lower(),)) if text.startswith('@') else DB.one('SELECT * FROM users WHERE id=?',(int(text),))
            if not u: raise ValueError('Игрок не найден.')
            SESSIONS.pop(uid,None)
            return await admin_user(msg,uid,u['id'])
        elif kind=='balance':
            bits=text.split()
            if len(bits)!=2: raise ValueError('Нужны два числа.')
            s['coins'],s['points']=[number(x,-1000000000) for x in bits]
            s['kind']='balance_reason'
            return await msg.answer('Отправь причину корректировки (до 300 символов). /cancel - отмена')
        elif kind=='balance_reason':
            if not 1<=len(text)<=300: raise ValueError('Причина: 1..300 символов.')
            with DB.tx() as c:
                u=c.execute('SELECT * FROM users WHERE id=?',(s['target'],)).fetchone()
                if not u or u['coins']+s['coins']<0 or u['points']+s['points']<0: raise ValueError('Баланс не может быть отрицательным.')
                DB.once(c,f'admin-balance:{msg.chat.id}:{msg.message_id}')
                c.execute('UPDATE users SET coins=coins+?,points=points+? WHERE id=?',(s['coins'],s['points'],s['target']))
                DB.log(c,uid,'admin_balance',target=s['target'],coins=s['coins'],points=s['points'],reason=text)
        SESSIONS.pop(uid,None)
        await msg.answer('✅ Сохранено. /admin - панель')
    except (ValueError,GameError) as exc:
        await msg.answer('Ошибка: '+(tr(exc.key,**exc.values) if isinstance(exc,GameError) else str(exc))+'\nПопробуй снова или /cancel.')


@dp.message()
@guard
async def on_message(msg: Message):
    uid=msg.from_user.id
    text=(msg.text or '').strip()
    bits=text.split()
    command=bits[0].split('@')[0].lower() if bits and bits[0].startswith('/') else ''
    if command and '@' in bits[0] and bits[0].split('@',1)[1].lower()!=BOT_USERNAME.lower(): return
    if command=='/cancel':
        SESSIONS.pop(uid,None)
        return await msg.answer(tr('cancelled'))
    if command=='/admin': return await admin_menu(msg,uid)
    if command=='/id': return await msg.answer(tr('id',id=uid))
    s=SESSIONS.get(uid)
    if s and s['expires']<time.time(): SESSIONS.pop(uid,None); s=None
    if s and msg.chat.type=='private' and uid in ADMINS:
        return await admin_input(msg,uid,s)
    player(uid)
    if command=='/start' or (msg.chat.type=='private' and not command and text.casefold() not in DB.setting('triggers')):
        return await msg.answer(tr('welcome'),reply_markup=menu(uid))
    if command=='/help': return await msg.answer(tr('help',triggers=', '.join(DB.setting('triggers'))),reply_markup=menu(uid))
    if command=='/bao' or text.casefold() in DB.setting('triggers'):
        return await draw(msg,uid,f'm:{msg.chat.id}:{msg.message_id}')
    if command in ('/cards','/buyer'): return await inventory(msg,uid,command[1:])
    if command=='/card': return await show_card(msg,DB.owned(uid,bits[1]))
    if command=='/profile': return await profile(msg,uid)
    if command=='/top': return await top(msg)
    if command=='/history': return await history(msg,uid)
    if command=='/upgrade': return await upgrade(msg,uid)
    if command=='/fav':
        value=DB.favorite(uid,bits[1]); return await msg.answer(tr('favorite',id=bits[1],value='включена' if value else 'выключена'))
    if command=='/send':
        if len(bits)==2 and msg.reply_to_message and msg.reply_to_message.from_user and not msg.reply_to_message.sender_chat:
            other=msg.reply_to_message.from_user
            if other.is_bot: raise GameError('unknown_user')
            DB.touch(other.id,other.username,other.full_name)
            target=player(other.id); iid=bits[1]
        elif len(bits)==3:
            target=DB.one('SELECT * FROM users WHERE username=?',(bits[1].lstrip('@').lower(),))
            if not target: raise GameError('unknown_user')
            iid=bits[2]
        else: raise ValueError()
        if target['id']==uid: raise GameError('self_send')
        if target['banned']: raise GameError('unknown_user')
        item=DB.owned(uid,iid)
        if item['favorite']: raise GameError('favorite_locked')
        token=DB.pending(uid,'send',{'item':iid,'target':target['id']})
        return await msg.answer(tr('send_confirm',name=item['name'],id=iid,target_name=label(target),target=target['id']),reply_markup=confirm_keyboard(uid,token))
    if command: await msg.answer(tr('syntax'))


@dp.callback_query(F.data)
@guard
async def on_callback(cb: CallbackQuery):
    uid=cb.from_user.id; msg=cb.message
    owner,action=cb.data.split('|',1)
    if int(owner)!=uid: raise GameError('wrong_button')
    if action.startswith('a:'):
        await cb.answer()
        return await admin_action(msg,uid,action)
    player(uid)
    if action.startswith('confirm:'):
        action,result=DB.confirm(uid,action.split(':')[1])
        await cb.answer()
        try: await msg.edit_reply_markup(reply_markup=None)
        except TelegramAPIError: pass
        await msg.answer(tr({'sell':'sold','send':'sent','upgrade':'upgraded'}[action],**result))
        if action=='send':
            try: await cb.bot.send_message(result['target'],tr('received',**result,sender=label(player(uid))))
            except TelegramAPIError: pass
        return
    await cb.answer()
    if action.startswith('cancel:'):
        with DB.tx() as c: c.execute('DELETE FROM pending WHERE token=? AND actor=?',(action.split(':')[1],uid))
        try: await msg.edit_reply_markup(reply_markup=None)
        except TelegramAPIError: pass
        return await msg.answer(tr('cancelled'))
    if action=='draw': return await draw(msg,uid,'c:'+cb.id)
    if action=='profile': return await profile(msg,uid)
    if action=='top': return await top(msg)
    if action=='upgrade': return await upgrade(msg,uid)
    if action=='help': return await msg.answer(tr('help',triggers=', '.join(DB.setting('triggers'))))
    parts=action.split(':')
    if parts[0] in ('cards','buyer'): return await inventory(msg,uid,parts[0],int(parts[1]))
    if parts[0]=='card': return await show_card(msg,DB.owned(uid,parts[1]))
    if parts[0]=='sell': return await sell(msg,uid,parts[1])


async def main():
    global BOT_USERNAME
    token=os.getenv('BOT_TOKEN','')
    if not token or token=='PASTE_TOKEN_HERE': raise RuntimeError('Заполни BOT_TOKEN в .env')
    bot=Bot(token)
    try:
        me=await bot.get_me(); BOT_USERNAME=me.username
        # Explicitly replace webhooks when deploying as a polling worker.
        await bot.delete_webhook(drop_pending_updates=False)
        await bot.set_my_commands([BotCommand(command=c,description=d) for c,d in [
            ('start','Главное меню'),('bao','Открыть карту'),('cards','Коллекция'),('profile','Профиль'),
            ('buyer','Скупщик'),('send','Передать карту'),('upgrade','Ускорить открытия'),('top','Всемирный топ'),
            ('help','Помощь'),('admin','Панель администратора')]])
        LOG.info('Bao Cards started as @%s',BOT_USERNAME)
        await dp.start_polling(bot,allowed_updates=['message','callback_query'],close_bot_session=False)
    finally:
        await bot.session.close()


if __name__=='__main__':
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(name)s %(message)s')
    asyncio.run(main())

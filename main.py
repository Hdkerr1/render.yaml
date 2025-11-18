import asyncio
import logging
import os
import re
import sqlite3
import sys
import hashlib
import time
from datetime import datetime, timedelta
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils import executor
from telethon import TelegramClient, errors
from telethon.tl.functions.channels import LeaveChannelRequest
from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest

# ---------- CONFIG ----------
load_dotenv()
API_ID = int(os.getenv('TELETHON_API_ID') or 0)
API_HASH = os.getenv('TELETHON_API_HASH') or ""
BOT_TOKEN = os.getenv('BOT_TOKEN')
# ADMIN_IDS: comma separated admin IDs. Example: "12345,67890"
ADMIN_IDS_RAW = os.getenv('ADMIN_IDS') or os.getenv('ADMIN_ID') or ""
ADMIN_IDS = []
for part in re.split(r'[,;\s]+', ADMIN_IDS_RAW.strip()):
    if part.strip():
        try:
            ADMIN_IDS.append(int(part.strip()))
        except Exception:
            pass
USERBOT_SESSION = os.getenv('USERBOT_SESSION') or 'userbot.session'
DB_PATH = os.getenv('DB_PATH') or 'bot_database.db'
MAINTENANCE = os.getenv('MAINTENANCE') == '1'

if not BOT_TOKEN or not ADMIN_IDS:
    raise SystemExit('Please set BOT_TOKEN and ADMIN_IDS (or ADMIN_ID) in .env')

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

# Telethon client (admin userbot) - will be started on-demand
telethon_client = None

# ---------- DATABASE SETUP ----------
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cur = conn.cursor()

cur.execute('''
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    balance_usd REAL DEFAULT 0,
    balance_inr REAL DEFAULT 0,
    joined_at TEXT
)
''')

cur.execute('''
CREATE TABLE IF NOT EXISTS sold_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    group_link TEXT,
    group_title TEXT,
    group_year TEXT,
    messages_count INTEGER,
    price_usd REAL,
    price_inr REAL,
    sold_at TEXT
)
''')

cur.execute('''
CREATE TABLE IF NOT EXISTS withdrawals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    method TEXT,
    amount REAL,
    target TEXT,
    status TEXT DEFAULT 'pending',
    requested_at TEXT
)
''')

cur.execute('''
CREATE TABLE IF NOT EXISTS supports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    question TEXT,
    status TEXT DEFAULT 'open',
    admin_reply TEXT,
    asked_at TEXT
)
''')

cur.execute('''
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
)
''')
conn.commit()

# default settings if not present
def get_setting(key, default=None):
    cur.execute('SELECT value FROM settings WHERE key=?', (key,))
    r = cur.fetchone()
    return r[0] if r else default

def set_setting(key, value):
    cur.execute('REPLACE INTO settings(key,value) VALUES(?,?)', (key, str(value)))
    conn.commit()

if get_setting('welcome_message') is None:
    set_setting('welcome_message', 'Welcome! Use the menu below to start.')
if get_setting('mandatory_channel') is None:
    set_setting('mandatory_channel', '@WDDesire')
if get_setting('price_list') is None:
    set_setting('price_list', "📦 Today's Price\n• 2016-22:      ₹1035.00/$11.50\n• 2023:         ₹810.00/$9.00\n• Jan-Feb 2024: ₹360.00/$4.00\n• Mar 2024:     ₹405.00/$4.50\n• Apr 2024:     ₹315.00/$3.50")

# ---------- UTIL ----------
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def ensure_user(user_id: int):
    cur.execute('SELECT user_id FROM users WHERE user_id=?', (user_id,))
    if not cur.fetchone():
        cur.execute('INSERT INTO users(user_id, joined_at) VALUES(?, ?)', (user_id, datetime.utcnow().isoformat()))
        conn.commit()

def format_currency_usd(x):
    return f'${x:.2f}'

def format_currency_inr(x):
    return f'₹{x:.2f}'

def parse_price_list(text):
    items = []
    for line in text.splitlines():
        m = re.search(r'•\s*(.+?):\s*₹([0-9.,]+)/(\$?)([0-9.,]+)', line)
        if m:
            label = m.group(1).strip()
            inr = float(m.group(2).replace(',', ''))
            usd = float(m.group(4).replace(',', ''))
            items.append((label, inr, usd))
    return items

# ---------- TRANSFER KEY helpers ----------
def make_transfer_key(user_id: int, link: str) -> str:
    h = hashlib.sha1(f"{user_id}:{link}:{time.time()}".encode()).hexdigest()[:20]
    return f"t{h}"

def store_pending_transfer(key: str, link: str, price_inr: float, price_usd: float, title: str, expires_minutes=15):
    exp = (datetime.utcnow() + timedelta(minutes=expires_minutes)).isoformat()
    set_setting(f'pending_transfer:{key}', f'{link}|{price_inr}|{price_usd}|{title}|{exp}')

def load_pending_transfer(key: str):
    v = get_setting(f'pending_transfer:{key}')
    if not v:
        return None
    try:
        link, inr_s, usd_s, title, exp = v.split('|', 4)
        return dict(link=link, price_inr=float(inr_s), price_usd=float(usd_s), title=title, exp=exp)
    except Exception:
        return None

def clear_pending_transfer(key: str):
    set_setting(f'pending_transfer:{key}', '')

# ---------- KEYBOARDS ----------
def main_menu_kb():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(InlineKeyboardButton('🧑 Profile', callback_data='profile'),
           InlineKeyboardButton('💸 Withdraw', callback_data='withdraw'))
    kb.add(InlineKeyboardButton('🧑‍💻 Support', callback_data='support'),
           InlineKeyboardButton('📦 Price', callback_data='price'))
    return kb

back_kb = InlineKeyboardMarkup().add(InlineKeyboardButton('🔙 Back', callback_data='back'))

def reply_main_menu_kb():
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(KeyboardButton('🧑 Profile'), KeyboardButton('💸 Withdraw'))
    kb.add(KeyboardButton('🧑‍💻 Support'), KeyboardButton('📦 Price'))
    return kb

def reply_admin_kb():
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(KeyboardButton('Admin Panel'))
    return kb

# ---------- START / JOIN CHECK ----------
@dp.message_handler(commands=['start'])
async def cmd_start(message: types.Message):
    ensure_user(message.from_user.id)

    if MAINTENANCE and not is_admin(message.from_user.id):
        await message.answer('⚠️ Bot is under maintenance. Please try later.')
        return

    mandatory = get_setting('mandatory_channel')
    text = f"🚨 Please join the required channel before continuing:\n\n➡️ {mandatory}\n\n✅ Once you've joined, tap Continue below."
    kb = InlineKeyboardMarkup().add(InlineKeyboardButton('✅ Continue', callback_data='continue_after_join'))

    # Only send welcome + continue button; persistent keyboard only after verification
    await message.answer(get_setting('welcome_message'))
    await message.answer(text, reply_markup=kb)

@dp.callback_query_handler(lambda c: c.data == 'continue_after_join')
async def cb_continue_after_join(query: types.CallbackQuery):
    mandatory = get_setting('mandatory_channel')

    # check membership (username style). If mandatory is an invite link, we can't reliably check server-side.
    try:
        if mandatory.startswith('@'):
            member = await bot.get_chat_member(mandatory, query.from_user.id)
            if member.status in ('left', 'kicked'):
                raise Exception('not a member')
        else:
            # attempt get_chat_member - may fail for invite links; if it fails require manual verification
            member = await bot.get_chat_member(mandatory, query.from_user.id)
            if member.status in ('left', 'kicked'):
                raise Exception('not a member')
    except Exception:
        await query.answer('❌ You must join the required channel first or the bot cannot verify your membership.', show_alert=True)
        return

    # Passed membership -> show menus and persistent keyboard
    await query.message.edit_text('✅ Verified — Welcome!')
    await query.message.answer('Choose an option:', reply_markup=main_menu_kb())
    if is_admin(query.from_user.id):
        try:
            await bot.send_message(query.from_user.id, 'Admin controls are available below.', reply_markup=reply_admin_kb())
        except Exception:
            pass
    else:
        try:
            await bot.send_message(query.from_user.id, 'Use the persistent menu below for quick actions.', reply_markup=reply_main_menu_kb())
        except Exception:
            pass

# ---------- REPLY KEYBOARD TEXT HANDLERS ----------
@dp.message_handler(lambda m: m.text == '🧑 Profile')
async def msg_profile(message: types.Message):
    ensure_user(message.from_user.id)
    cur.execute('SELECT balance_usd, balance_inr FROM users WHERE user_id=?', (message.from_user.id,))
    r = cur.fetchone() or (0.0, 0.0)
    cur.execute('SELECT COUNT(*) FROM sold_groups WHERE user_id=?', (message.from_user.id,))
    sold_count = cur.fetchone()[0]

    text = f"👤 Your Profile\n🆔 User ID: {message.from_user.id}\n💰 Balance: {format_currency_inr(r[1])}/{format_currency_usd(r[0])}\n👥 Groups sold: {sold_count}"
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton('📜 Sold Groups History', callback_data='sold_history'))
    kb.add(InlineKeyboardButton('🔙 Back', callback_data='back'))
    await message.reply(text, reply_markup=kb)

@dp.message_handler(lambda m: m.text == '💸 Withdraw')
async def msg_withdraw(message: types.Message):
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton('$ USDT BEP20', callback_data='withdraw_usdt'), InlineKeyboardButton('₹ INR', callback_data='withdraw_inr'))
    kb.add(InlineKeyboardButton('💸 Withdraw History', callback_data='withdraw_history'))
    kb.add(InlineKeyboardButton('🔙 Back', callback_data='back'))
    await message.reply('💳 Select withdrawal method:', reply_markup=kb)

@dp.message_handler(lambda m: m.text == '🧑‍💻 Support')
async def msg_support(message: types.Message):
    await message.reply('🧑‍💻 Need help? Send your question below.')
    await dp.current_state(user=message.from_user.id).set_state('awaiting_support')

@dp.message_handler(lambda m: m.text == '📦 Price')
async def msg_price(message: types.Message):
    await message.reply(get_setting('price_list'), reply_markup=back_kb)

# ---------- MAIN MENU HANDLERS (inline callbacks) ----------
@dp.callback_query_handler(lambda c: c.data == 'profile')
async def cb_profile(query: types.CallbackQuery):
    ensure_user(query.from_user.id)
    cur.execute('SELECT balance_usd, balance_inr FROM users WHERE user_id=?', (query.from_user.id,))
    r = cur.fetchone() or (0.0, 0.0)
    cur.execute('SELECT COUNT(*) FROM sold_groups WHERE user_id=?', (query.from_user.id,))
    sold_count = cur.fetchone()[0]

    text = f"👤 Your Profile\n🆔 User ID: {query.from_user.id}\n💰 Balance: {format_currency_inr(r[1])}/{format_currency_usd(r[0])}\n👥 Groups sold: {sold_count}"
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton('📜 Sold Groups History', callback_data='sold_history'))
    kb.add(InlineKeyboardButton('🔙 Back', callback_data='back'))
    await query.message.edit_text(text, reply_markup=kb)

@dp.callback_query_handler(lambda c: c.data == 'sold_history')
async def cb_sold_history(query: types.CallbackQuery):
    cur.execute('SELECT group_title,group_year,price_inr,price_usd,sold_at FROM sold_groups WHERE user_id=? ORDER BY sold_at DESC', (query.from_user.id,))
    rows = cur.fetchall()
    if not rows:
        await query.message.edit_text('📜 No sold groups yet.', reply_markup=back_kb)
        return
    text = '📜 Sold Groups History:\n\n'
    for r in rows:
        text += f"• {r[0]} ({r[1]}) — {format_currency_inr(r[2])}/{format_currency_usd(r[3])} — {r[4][:19]}\n"
    kb = InlineKeyboardMarkup().add(InlineKeyboardButton('🔙 Back', callback_data='back'))
    await query.message.edit_text(text, reply_markup=kb)

@dp.callback_query_handler(lambda c: c.data == 'support')
async def cb_support(query: types.CallbackQuery):
    await query.message.edit_text('🧑‍💻 Need help? Send your question below.')
    await dp.current_state(user=query.from_user.id).set_state('awaiting_support')

@dp.message_handler(state='awaiting_support')
async def handle_support_msg(message: types.Message):
    ensure_user(message.from_user.id)
    cur.execute('INSERT INTO supports(user_id, question, asked_at) VALUES(?,?,?)', (message.from_user.id, message.text, datetime.utcnow().isoformat()))
    conn.commit()
    support_id = cur.lastrowid
    await message.answer('✅ Your message has been sent to support. We\'ll reply here soon.')
    # forward to ALL admins
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton('Reply', callback_data=f'admin_reply_support:{support_id}'))
    for aid in ADMIN_IDS:
        try:
            await bot.send_message(aid, f'🆕 Support #{support_id}\nFrom: {message.from_user.full_name} ({message.from_user.id})\nQuestion:\n{message.text}', reply_markup=kb)
        except Exception:
            pass
    await dp.current_state(user=message.from_user.id).reset_state()

@dp.callback_query_handler(lambda c: c.data and c.data.startswith('admin_reply_support:'))
async def cb_admin_reply_support(query: types.CallbackQuery):
    if not is_admin(query.from_user.id):
        await query.answer('Unauthorized', show_alert=True)
        return
    _id = int(query.data.split(':', 1)[1])
    await query.message.answer('Type your reply for support id '+str(_id))
    await dp.current_state(user=query.from_user.id).set_state(f'admin_reply_{_id}')

@dp.message_handler(lambda message: message.text and message.text.startswith('/'), state='*')
async def ignore_slash_commands(message: types.Message):
    pass

@dp.message_handler(state=lambda state: state and state.startswith('admin_reply_'))
async def handle_admin_reply(message: types.Message):
    st = (await dp.current_state(user=message.from_user.id).get_state())
    if not st:
        return
    support_id = int(st.split('_')[-1])
    if not is_admin(message.from_user.id):
        await message.answer('Unauthorized')
        await dp.current_state(user=message.from_user.id).reset_state()
        return
    cur.execute('UPDATE supports SET admin_reply=?, status=? WHERE id=?', (message.text, 'answered', support_id))
    conn.commit()
    cur.execute('SELECT user_id FROM supports WHERE id=?', (support_id,))
    row = cur.fetchone()
    if row:
        uid = row[0]
        try:
            await bot.send_message(uid, f'💬 Support reply:\n{message.text}')
        except Exception:
            pass
    await message.answer('Reply sent.')
    await dp.current_state(user=message.from_user.id).reset_state()

@dp.callback_query_handler(lambda c: c.data == 'price')
async def cb_price(query: types.CallbackQuery):
    text = get_setting('price_list')
    await query.message.edit_text(text, reply_markup=back_kb)

# ---------- WITHDRAWAL FLOWS ----------
@dp.callback_query_handler(lambda c: c.data == 'withdraw')
async def cb_withdraw(query: types.CallbackQuery):
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton('$ USDT BEP20', callback_data='withdraw_usdt'), InlineKeyboardButton('₹ INR', callback_data='withdraw_inr'))
    kb.add(InlineKeyboardButton('💸 Withdraw History', callback_data='withdraw_history'))
    kb.add(InlineKeyboardButton('🔙 Back', callback_data='back'))
    await query.message.edit_text('💳 Select withdrawal method:', reply_markup=kb)

@dp.callback_query_handler(lambda c: c.data == 'withdraw_history')
async def cb_withdraw_history(query: types.CallbackQuery):
    ensure_user(query.from_user.id)
    cur.execute('SELECT id,method,amount,target,status,requested_at FROM withdrawals WHERE user_id=? ORDER BY requested_at DESC', (query.from_user.id,))
    rows = cur.fetchall()
    if not rows:
        await query.message.edit_text('📜 No withdrawals yet.', reply_markup=back_kb)
        return
    text = '💸 Withdraw History:\n\n'
    for r in rows:
        text += f'#{r[0]} • {r[1]} {r[2]} -> {r[3]} ({r[4]}) at {r[5][:19]}\n'
    kb = InlineKeyboardMarkup().add(InlineKeyboardButton('🔙 Back', callback_data='back'))
    await query.message.edit_text(text, reply_markup=kb)

@dp.callback_query_handler(lambda c: c.data == 'withdraw_usdt')
async def cb_withdraw_usdt(query: types.CallbackQuery):
    await query.message.edit_text('💵 Enter withdrawal amount (USD):')
    await dp.current_state(user=query.from_user.id).set_state('awaiting_withdraw_usd')

@dp.callback_query_handler(lambda c: c.data == 'withdraw_inr')
async def cb_withdraw_inr(query: types.CallbackQuery):
    await query.message.edit_text('💵 Enter withdrawal amount (INR):')
    await dp.current_state(user=query.from_user.id).set_state('awaiting_withdraw_inr')

@dp.message_handler(state='awaiting_withdraw_usd')
async def handle_withdraw_usd(message: types.Message):
    ensure_user(message.from_user.id)
    try:
        amt = float(re.sub(r'[^0-9.]', '', message.text))
    except Exception:
        await message.answer('Invalid amount.')
        return
    cur.execute('SELECT balance_usd FROM users WHERE user_id=?', (message.from_user.id,))
    bal_row = cur.fetchone()
    bal = bal_row[0] if bal_row else 0.0
    if amt > bal:
        await message.answer('❌ Insufficient USD balance.')
        await dp.current_state(user=message.from_user.id).reset_state()
        return
    state = dp.current_state(user=message.from_user.id)
    await state.update_data(withdraw_amount=amt)
    await message.answer('Enter your USDT BEP20 address:')
    await state.set_state('awaiting_withdraw_usdt_addr')

@dp.message_handler(state='awaiting_withdraw_usdt_addr')
async def handle_withdraw_usdt_addr(message: types.Message):
    data = await dp.current_state(user=message.from_user.id).get_data()
    amt = data.get('withdraw_amount')
    addr = message.text.strip()
    cur.execute('INSERT INTO withdrawals(user_id,method,amount,target,status,requested_at) VALUES(?,?,?,?,?,?)', (message.from_user.id, 'USDT_BEP20', amt, addr, 'pending', datetime.utcnow().isoformat()))
    conn.commit()
    wid = cur.lastrowid
    await message.answer('✅ Withdrawal requested and is pending admin approval.')
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton('Approve', callback_data=f'admin_withdraw_approve:{wid}'), InlineKeyboardButton('Decline', callback_data=f'admin_withdraw_decline:{wid}'))
    for aid in ADMIN_IDS:
        try:
            await bot.send_message(aid, f'💸 New withdrawal #{wid}\nUser: {message.from_user.full_name} ({message.from_user.id})\nMethod: USDT_BEP20\nAmount: {amt}\nTarget: {addr}', reply_markup=kb)
        except Exception:
            pass
    await dp.current_state(user=message.from_user.id).reset_state()

@dp.message_handler(state='awaiting_withdraw_inr')
async def handle_withdraw_inr(message: types.Message):
    ensure_user(message.from_user.id)
    try:
        amt = float(re.sub(r'[^0-9.]', '', message.text))
    except Exception:
        await message.answer('Invalid amount.')
        return
    cur.execute('SELECT balance_inr FROM users WHERE user_id=?', (message.from_user.id,))
    bal_row = cur.fetchone()
    bal = bal_row[0] if bal_row else 0.0
    if amt > bal:
        await message.answer('❌ Insufficient INR balance.')
        await dp.current_state(user=message.from_user.id).reset_state()
        return
    state = dp.current_state(user=message.from_user.id)
    await state.update_data(withdraw_amount=amt)
    await message.answer('Enter your UPI ID:')
    await state.set_state('awaiting_withdraw_inr_upi')

@dp.message_handler(state='awaiting_withdraw_inr_upi')
async def handle_withdraw_inr_upi(message: types.Message):
    data = await dp.current_state(user=message.from_user.id).get_data()
    amt = data.get('withdraw_amount')
    upi = message.text.strip()
    cur.execute('INSERT INTO withdrawals(user_id,method,amount,target,status,requested_at) VALUES(?,?,?,?,?,?)', (message.from_user.id, 'INR_UPI', amt, upi, 'pending', datetime.utcnow().isoformat()))
    conn.commit()
    wid = cur.lastrowid
    await message.answer('✅ Withdrawal requested and is pending admin approval.')
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton('Approve', callback_data=f'admin_withdraw_approve:{wid}'), InlineKeyboardButton('Decline', callback_data=f'admin_withdraw_decline:{wid}'))
    for aid in ADMIN_IDS:
        try:
            await bot.send_message(aid, f'💸 New withdrawal #{wid}\nUser: {message.from_user.full_name} ({message.from_user.id})\nMethod: INR_UPI\nAmount: {amt}\nTarget: {upi}', reply_markup=kb)
        except Exception:
            pass
    await dp.current_state(user=message.from_user.id).reset_state()

# Admin approve/decline withdraw. Any admin can approve/decline.
@dp.callback_query_handler(lambda c: c.data and c.data.startswith('admin_withdraw_'))
async def cb_admin_withdraw_action(query: types.CallbackQuery):
    if not is_admin(query.from_user.id):
        await query.answer('Unauthorized', show_alert=True)
        return
    parts = query.data.split(':')
    action_wrapped = parts[0]  # admin_withdraw_approve or admin_withdraw_decline
    if len(parts) < 2:
        await query.answer('Invalid payload', show_alert=True)
        return
    try:
        wid = int(parts[1])
    except Exception:
        await query.answer('Invalid withdrawal id', show_alert=True)
        return
    action = action_wrapped.split('_')[-1]

    cur.execute('SELECT user_id,amount,method,status FROM withdrawals WHERE id=?', (wid,))
    row = cur.fetchone()
    if not row:
        await query.answer('Request not found', show_alert=True)
        return
    uid, amt, method, status = row

    if action == 'approve':
        if status != 'pending':
            await query.answer('Already processed', show_alert=True)
            return
        # check user's current balance and deduct only the right currency
        if method == 'USDT_BEP20':
            cur.execute('SELECT balance_usd FROM users WHERE user_id=?', (uid,))
            bal_row = cur.fetchone()
            bal = bal_row[0] if bal_row else 0.0
            if bal < amt:
                cur.execute('UPDATE withdrawals SET status=? WHERE id=?', ('declined', wid))
                conn.commit()
                await query.message.edit_text('❌ Withdrawal declined — user has insufficient USD balance at processing time.')
                try:
                    await bot.send_message(uid, f'❌ Your withdrawal #{wid} was declined due to insufficient balance at processing time. Contact support.')
                except Exception:
                    pass
                return
            cur.execute('UPDATE users SET balance_usd = balance_usd - ? WHERE user_id=?', (amt, uid))
        else:
            # INR
            cur.execute('SELECT balance_inr FROM users WHERE user_id=?', (uid,))
            bal_row = cur.fetchone()
            bal = bal_row[0] if bal_row else 0.0
            if bal < amt:
                cur.execute('UPDATE withdrawals SET status=? WHERE id=?', ('declined', wid))
                conn.commit()
                await query.message.edit_text('❌ Withdrawal declined — user has insufficient INR balance at processing time.')
                try:
                    await bot.send_message(uid, f'❌ Your withdrawal #{wid} was declined due to insufficient balance at processing time. Contact support.')
                except Exception:
                    pass
                return
            cur.execute('UPDATE users SET balance_inr = balance_inr - ? WHERE user_id=?', (amt, uid))

        cur.execute('UPDATE withdrawals SET status=? WHERE id=?', ('approved', wid))
        conn.commit()
        await query.message.edit_text('✅ Withdrawal approved.')
        try:
            await bot.send_message(uid, f'✅ Your withdrawal #{wid} has been approved. Amount: {amt} ({method})')
        except Exception:
            pass
    else:
        cur.execute('UPDATE withdrawals SET status=? WHERE id=?', ('declined', wid))
        conn.commit()
        await query.message.edit_text('❌ Withdrawal declined.')
        try:
            await bot.send_message(uid, f'❌ Your withdrawal #{wid} has been declined. Contact support.')
        except Exception:
            pass

# ---------- BACK handler ----------
@dp.callback_query_handler(lambda c: c.data == 'back')
async def cb_back(query: types.CallbackQuery):
    await query.message.edit_text('Choose an option:', reply_markup=main_menu_kb())

# ---------- GROUP SELL FLOW ----------
@dp.message_handler(regexp=r't.me/|telegram.me/|\+\w{8,}')
async def handle_group_link(message: types.Message):
    ensure_user(message.from_user.id)
    if MAINTENANCE and not is_admin(message.from_user.id):
        await message.answer('⚠️ Bot is under maintenance. Please try later.')
        return

    link = message.text.strip()
    pending_msg = await message.answer('⏳ Checking Group Details...')

    try:
        await ensure_telethon_client()
    except Exception as e:
        await pending_msg.edit_text('❌ Telethon userbot not ready: ' + str(e))
        return

    # try to resolve entity; Telethon will raise if not member and not invite
    entity = None
    try:
        entity = await telethon_client.get_entity(link)
    except Exception:
        # try invite join
        try:
            m = re.search(r'(?:t\.me/\+|joinchat/)([A-Za-z0-9_-]+)', link)
            invite_hash = m.group(1) if m else None
            if invite_hash:
                try:
                    await telethon_client(CheckChatInviteRequest(invite_hash))
                    try:
                        await telethon_client(ImportChatInviteRequest(invite_hash))
                    except Exception:
                        pass
                    entity = await telethon_client.get_entity(link)
                except Exception:
                    pass
        except Exception:
            pass

    if entity is None:
        await pending_msg.edit_text('❌ Failed to resolve group. Ensure group link is valid and the userbot can access it.')
        return

    try:
        title = getattr(entity, 'title', str(entity))
        history = await telethon_client.get_messages(entity, limit=200)
        messages_count = len(history)
        earliest = history[-1].date if history else datetime.utcnow()
        year_label = earliest.strftime('%b %Y')
    except Exception:
        await pending_msg.edit_text('❌ Unable to read messages from the group.')
        return

    price_list = parse_price_list(get_setting('price_list'))
    chosen = None
    for label, inr, usd in price_list:
        if '2023' in label and '2023' in year_label:
            chosen = (label, inr, usd)
            break
    if not chosen:
        chosen = price_list[0] if price_list else ('Default', 0.0, 0.0)

    price_inr = chosen[1]
    price_usd = chosen[2]

    text = f"🔹 Group: {year_label}\n🛡️ Status: Private supergroup\n🕒 First message: {earliest.strftime('%B %Y')}\n💬 Messages: {messages_count}\n💰 Price: {format_currency_inr(price_inr)}\n\n💰 Total price: {format_currency_inr(price_inr)}\n\n👇 Choose an option:"

    transfer_key = make_transfer_key(message.from_user.id, link)
    store_pending_transfer(transfer_key, link, price_inr, price_usd, title, expires_minutes=15)

    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton('✅ Confirm', callback_data=f'confirm_sell:{transfer_key}'), InlineKeyboardButton('🚫 Cancel', callback_data=f'cancel_sell:{transfer_key}'))
    await pending_msg.edit_text(text, reply_markup=kb)

@dp.callback_query_handler(lambda c: c.data and c.data.startswith('cancel_sell:'))
async def cb_cancel_sell(query: types.CallbackQuery):
    # on cancel, try to remove userbot from the group (leave) to ensure no lingering membership
    try:
        transfer_key = query.data.split(':', 1)[1]
    except Exception:
        transfer_key = None
    if transfer_key:
        pending = load_pending_transfer(transfer_key)
        if pending:
            try:
                # attempt to get entity and leave
                await ensure_telethon_client()
                ent = await telethon_client.get_entity(pending['link'])
                try:
                    await telethon_client(LeaveChannelRequest(ent))
                except Exception:
                    pass
                clear_pending_transfer(transfer_key)
            except Exception:
                pass
    await query.message.edit_text('❌ Cancelled — transfer aborted and userbot left the chat (if it was joined).')

@dp.callback_query_handler(lambda c: c.data and c.data.startswith('confirm_sell:'))
async def cb_confirm_sell(query: types.CallbackQuery):
    try:
        transfer_key = query.data.split(':', 1)[1]
    except Exception:
        await query.answer('Invalid payload', show_alert=True)
        return

    pending = load_pending_transfer(transfer_key)
    if not pending:
        await query.answer('No pending transfer found or time expired.', show_alert=True)
        return

    link = pending['link']
    price_inr = pending['price_inr']
    price_usd = pending['price_usd']
    title = pending['title']

    try:
        me = await telethon_client.get_me()
        admin_name = me.username or me.first_name or 'admin'
    except Exception:
        admin_name = 'admin'

    text = f"⚡ Ownership Transfer Required\nTransfer each group to its assigned userbot, then tap Verify to confirm.\n\n⏳ You have 15 minutes to complete this step.\n\n1. {title} ({link}) -> {admin_name}\n"
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton('✅ Verify', callback_data=f'verify_transfer:{transfer_key}'), InlineKeyboardButton('❌ Cancel', callback_data=f'cancel_sell:{transfer_key}'))
    await query.message.edit_text(text, reply_markup=kb)

@dp.callback_query_handler(lambda c: c.data and c.data.startswith('verify_transfer:'))
async def cb_verify_transfer(query: types.CallbackQuery):
    try:
        transfer_key = query.data.split(':', 1)[1]
    except Exception:
        await query.answer('Invalid payload', show_alert=True)
        return

    pending = load_pending_transfer(transfer_key)
    if not pending:
        await query.answer('No pending transfer found or time expired.', show_alert=True)
        return

    if datetime.utcnow() > datetime.fromisoformat(pending['exp']):
        clear_pending_transfer(transfer_key)
        await query.message.edit_text('❌ Ownership transfer time expired. Cancelled.')
        return

    link = pending['link']
    price_inr = pending['price_inr']
    price_usd = pending['price_usd']
    title = pending['title']

    await query.message.edit_text('⏳ Checking ownership...')
    try:
        await ensure_telethon_client()
        entity = await telethon_client.get_entity(link)
        # check whether telethon_client is in admins of the chat
        participants = await telethon_client.get_participants(entity, limit=300)
        me = await telethon_client.get_me()
        is_admin = any(getattr(p, 'id', None) == getattr(me, 'id', None) for p in participants if getattr(p, 'id', None) is not None and getattr(p, 'bot', False) is False or True)
        # Note: sometimes participants are not annotated as admin; we'll also try to fetch full admin list via iter_participants with filter if necessary.
        # More robust check:
        try:
            from telethon.tl.types import ChannelParticipantsAdmins
            admins = await telethon_client.get_participants(entity, filter=ChannelParticipantsAdmins())
            is_admin = any(getattr(a, 'id', None) == getattr(me, 'id', None) for a in admins)
        except Exception:
            pass
    except Exception:
        is_admin = False

    if not is_admin:
        await query.message.edit_text(f'❌ Ownership not transferred for:\n1. {title} ({link})\n\n⏳ Time remains. Tap "✅ Verify" after transferring ownership.')
        return

    # success -> mark sold and credit only once
    sold_at = datetime.utcnow().isoformat()
    cur.execute('INSERT INTO sold_groups(user_id,group_link,group_title,group_year,messages_count,price_usd,price_inr,sold_at) VALUES(?,?,?,?,?,?,?,?)',
                (query.from_user.id, link, title, title, 0, price_usd, price_inr, sold_at))
    # credit only those currency balances; don't double-credit
    cur.execute('UPDATE users SET balance_usd = balance_usd + ?, balance_inr = balance_inr + ? WHERE user_id=?', (price_usd, price_inr, query.from_user.id))
    conn.commit()
    clear_pending_transfer(transfer_key)

    cur.execute('SELECT balance_usd, balance_inr FROM users WHERE user_id=?', (query.from_user.id,))
    b = cur.fetchone() or (0.0, 0.0)
    msg = f"✅ Group Sold!\n\nGroup: {title}\nPrice: {format_currency_inr(price_inr)}/{format_currency_usd(price_usd)}\nDate: {sold_at[:19]}\nAccount balance: {format_currency_inr(b[1])}/{format_currency_usd(b[0])}"
    await query.message.edit_text(msg)

# ---------- ADMIN PANEL ----------
@dp.callback_query_handler(lambda c: c.data == 'admin_panel')
async def cb_admin_panel(query: types.CallbackQuery):
    if not is_admin(query.from_user.id):
        await query.answer('Unauthorized', show_alert=True)
        return
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton('Set Prices', callback_data='admin_set_prices'))
    kb.add(InlineKeyboardButton('Set Welcome', callback_data='admin_set_welcome'))
    kb.add(InlineKeyboardButton('Set Mandatory Channel', callback_data='admin_set_channel'))
    kb.add(InlineKeyboardButton('Broadcast', callback_data='admin_broadcast'))
    kb.add(InlineKeyboardButton('Toggle Maintenance', callback_data='admin_toggle_maint'))
    kb.add(InlineKeyboardButton('User Management', callback_data='admin_user_mgmt'))
    await query.message.edit_text('Admin Panel', reply_markup=kb)

@dp.callback_query_handler(lambda c: c.data == 'admin_set_prices')
async def cb_admin_set_prices(query: types.CallbackQuery):
    if not is_admin(query.from_user.id):
        await query.answer('Unauthorized', show_alert=True)
        return
    await query.message.answer('Send the full price list text exactly as you want it to appear:')
    await dp.current_state(user=query.from_user.id).set_state('admin_setting_prices')

@dp.message_handler(state='admin_setting_prices')
async def handle_admin_prices(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer('Unauthorized')
        await dp.current_state(user=message.from_user.id).reset_state()
        return
    set_setting('price_list', message.text)
    await message.answer('✅ Price list updated.')
    await dp.current_state(user=message.from_user.id).reset_state()

@dp.callback_query_handler(lambda c: c.data == 'admin_set_welcome')
async def cb_admin_set_welcome(query: types.CallbackQuery):
    if not is_admin(query.from_user.id):
        await query.answer('Unauthorized', show_alert=True)
        return
    await query.message.answer('Send new welcome message:')
    await dp.current_state(user=query.from_user.id).set_state('admin_setting_welcome')

@dp.message_handler(state='admin_setting_welcome')
async def handle_admin_welcome(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer('Unauthorized')
        await dp.current_state(user=message.from_user.id).reset_state()
        return
    set_setting('welcome_message', message.text)
    await message.answer('✅ Welcome message updated.')
    await dp.current_state(user=message.from_user.id).reset_state()

@dp.callback_query_handler(lambda c: c.data == 'admin_set_channel')
async def cb_admin_set_channel(query: types.CallbackQuery):
    if not is_admin(query.from_user.id):
        await query.answer('Unauthorized', show_alert=True)
        return
    await query.message.answer('Send mandatory channel username (e.g. @escrow_pagal):')
    await dp.current_state(user=query.from_user.id).set_state('admin_setting_channel')

@dp.message_handler(state='admin_setting_channel')
async def handle_admin_channel(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer('Unauthorized')
        await dp.current_state(user=message.from_user.id).reset_state()
        return
    set_setting('mandatory_channel', message.text.strip())
    await message.answer('✅ Mandatory channel updated.')
    await dp.current_state(user=message.from_user.id).reset_state()

@dp.callback_query_handler(lambda c: c.data == 'admin_broadcast')
async def cb_admin_broadcast(query: types.CallbackQuery):
    if not is_admin(query.from_user.id):
        await query.answer('Unauthorized', show_alert=True)
        return
    await query.message.answer('Send broadcast message to all users:')
    await dp.current_state(user=query.from_user.id).set_state('admin_broadcast_msg')

@dp.message_handler(state='admin_broadcast_msg')
async def handle_admin_broadcast(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer('Unauthorized')
        await dp.current_state(user=message.from_user.id).reset_state()
        return
    cur.execute('SELECT user_id FROM users')
    users = [r[0] for r in cur.fetchall()]
    sent = 0
    for uid in users:
        try:
            await bot.send_message(uid, message.text)
            sent += 1
        except Exception:
            pass
    await message.answer(f'Broadcast sent to {sent} users.')
    await dp.current_state(user=message.from_user.id).reset_state()

@dp.callback_query_handler(lambda c: c.data == 'admin_toggle_maint')
async def cb_admin_toggle_maint(query: types.CallbackQuery):
    if not is_admin(query.from_user.id):
        await query.answer('Unauthorized', show_alert=True)
        return
    global MAINTENANCE
    MAINTENANCE = not MAINTENANCE
    set_setting('maintenance', '1' if MAINTENANCE else '0')
    await query.message.edit_text(f'Maintenance mode is now {"ON" if MAINTENANCE else "OFF"}.')

# Admin command and reply keyboard
@dp.message_handler(commands=['admin'])
async def admin_show_panel_cmd(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.reply("Unauthorized.")
        return
    await message.reply("Admin menu:", reply_markup=reply_admin_kb())

@dp.message_handler(lambda m: m.text == 'Admin Panel')
async def admin_panel_button(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.reply("Unauthorized.")
        return
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton('Set Prices', callback_data='admin_set_prices'))
    kb.add(InlineKeyboardButton('Set Welcome', callback_data='admin_set_welcome'))
    kb.add(InlineKeyboardButton('Set Mandatory Channel', callback_data='admin_set_channel'))
    kb.add(InlineKeyboardButton('Broadcast', callback_data='admin_broadcast'))
    kb.add(InlineKeyboardButton('Toggle Maintenance', callback_data='admin_toggle_maint'))
    kb.add(InlineKeyboardButton('User Management', callback_data='admin_user_mgmt'))
    await message.reply('Admin Panel', reply_markup=kb)

# Admin user management flows (same approach as earlier but for multiple admins)
@dp.callback_query_handler(lambda c: c.data == 'admin_user_mgmt')
async def cb_admin_user_mgmt(query: types.CallbackQuery):
    if not is_admin(query.from_user.id):
        await query.answer('Unauthorized', show_alert=True)
        return
    await query.message.answer('Send the user ID you want to manage (integer):')
    await dp.current_state(user=query.from_user.id).set_state('admin_user_mgmt_await_id')

@dp.message_handler(state='admin_user_mgmt_await_id')
async def handle_admin_user_mgmt_id(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.reply('Unauthorized')
        await dp.current_state(user=message.from_user.id).reset_state()
        return
    try:
        uid = int(re.sub(r'[^0-9]', '', message.text))
    except Exception:
        await message.reply('Invalid user id. Send a numeric Telegram user id.')
        await dp.current_state(user=message.from_user.id).reset_state()
        return
    ensure_user(uid)
    cur.execute('SELECT user_id,balance_usd,balance_inr,joined_at FROM users WHERE user_id=?', (uid,))
    row = cur.fetchone()
    if not row:
        await message.reply('User not found.')
        await dp.current_state(user=message.from_user.id).reset_state()
        return
    uid, bal_usd, bal_inr, joined_at = row
    text = f'User: {uid}\nBalance USD: {format_currency_usd(bal_usd)}\nBalance INR: {format_currency_inr(bal_inr)}\nJoined: {joined_at}'
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton('Add Balance', callback_data=f'admin_user_add:{uid}'), InlineKeyboardButton('Subtract Balance', callback_data=f'admin_user_sub:{uid}'))
    kb.add(InlineKeyboardButton('Set Balance', callback_data=f'admin_user_set:{uid}'))
    kb.add(InlineKeyboardButton('Show Withdrawals', callback_data=f'admin_user_wd:{uid}'))
    await message.reply(text, reply_markup=kb)
    await dp.current_state(user=message.from_user.id).reset_state()

@dp.callback_query_handler(lambda c: c.data and c.data.startswith('admin_user_'))
async def cb_admin_user_actions(query: types.CallbackQuery):
    if not is_admin(query.from_user.id):
        await query.answer('Unauthorized', show_alert=True)
        return
    parts = query.data.split(':', 1)
    action = parts[0].split('_')[-1]
    if len(parts) < 2:
        await query.answer('Invalid payload', show_alert=True)
        return
    uid = int(parts[1])
    if action in ('add', 'sub'):
        await query.message.answer(f'Enter amount and currency type to {action} (example: 100 USD OR 500 INR):')
        await dp.current_state(user=query.from_user.id).set_state(f'admin_user_{action}_await:{uid}')
    elif action == 'set':
        await query.message.answer('Enter balances to set in format: <USD_amount> <INR_amount> (example: 10 750):')
        await dp.current_state(user=query.from_user.id).set_state(f'admin_user_set_await:{uid}')
    elif action == 'wd':
        cur.execute('SELECT id,method,amount,status,requested_at FROM withdrawals WHERE user_id=? ORDER BY requested_at DESC', (uid,))
        rows = cur.fetchall()
        if not rows:
            await query.message.answer('No withdrawals found for this user.')
            return
        text = 'Withdrawals:\n\n'
        for r in rows:
            text += f'#{r[0]} {r[1]} {r[2]} -> {r[3]} at {r[4][:19]}\n'
        await query.message.answer(text)

@dp.message_handler(state=lambda s: s and (s.startswith('admin_user_add_await:') or s.startswith('admin_user_sub_await:')))
async def handle_admin_user_add_sub(message: types.Message):
    st = (await dp.current_state(user=message.from_user.id).get_state())
    if not st or not is_admin(message.from_user.id):
        return
    m = re.search(r':(\d+)$', st)
    if not m:
        await message.reply('Internal error: cannot parse user id')
        await dp.current_state(user=message.from_user.id).reset_state()
        return
    uid = int(m.group(1))
    t = message.text.strip().split()
    if len(t) < 2:
        await message.reply('Invalid format. Example: 100 USD')
        await dp.current_state(user=message.from_user.id).reset_state()
        return
    try:
        amt = float(re.sub(r'[^0-9.]', '', t[0]))
    except Exception:
        await message.reply('Invalid amount number.')
        await dp.current_state(user=message.from_user.id).reset_state()
        return
    cur_action = 'add' if st.startswith('admin_user_add_await:') else 'sub'
    currency = t[1].upper()
    if currency == 'USD':
        if cur_action == 'add':
            cur.execute('UPDATE users SET balance_usd = balance_usd + ? WHERE user_id=?', (amt, uid))
        else:
            cur.execute('UPDATE users SET balance_usd = balance_usd - ? WHERE user_id=?', (amt, uid))
    else:
        if cur_action == 'add':
            cur.execute('UPDATE users SET balance_inr = balance_inr + ? WHERE user_id=?', (amt, uid))
        else:
            cur.execute('UPDATE users SET balance_inr = balance_inr - ? WHERE user_id=?', (amt, uid))
    conn.commit()
    await message.reply(f'Balance updated for user {uid}.')
    await dp.current_state(user=message.from_user.id).reset_state()

@dp.message_handler(state=lambda s: s and s.startswith('admin_user_set_await:'))
async def handle_admin_user_set(message: types.Message):
    st = (await dp.current_state(user=message.from_user.id).get_state())
    if not st or not is_admin(message.from_user.id):
        return
    m = re.search(r':(\d+)$', st)
    if not m:
        await message.reply('Internal error: cannot parse user id')
        await dp.current_state(user=message.from_user.id).reset_state()
        return
    uid = int(m.group(1))
    parts = message.text.strip().split()
    if len(parts) < 2:
        await message.reply('Invalid format. Example: 10 750 (USD INR)')
        await dp.current_state(user=message.from_user.id).reset_state()
        return
    try:
        usd_amt = float(re.sub(r'[^0-9.]', '', parts[0]))
        inr_amt = float(re.sub(r'[^0-9.]', '', parts[1]))
    except Exception:
        await message.reply('Invalid numbers')
        await dp.current_state(user=message.from_user.id).reset_state()
        return
    cur.execute('UPDATE users SET balance_usd=?, balance_inr=? WHERE user_id=?', (usd_amt, inr_amt, uid))
    conn.commit()
    await message.reply(f'Balances set for user {uid}.')
    await dp.current_state(user=message.from_user.id).reset_state()

# ---------- TELETHON USERBOT START HELPER ----------
async def ensure_telethon_client():
    global telethon_client
    if telethon_client and getattr(telethon_client, 'is_connected', lambda: False)():
        return
    if not API_ID or not API_HASH:
        raise Exception('Telethon API_ID/API_HASH not configured. Set TELETHON_API_ID and TELETHON_API_HASH in env.')
    telethon_client = TelegramClient(USERBOT_SESSION, API_ID, API_HASH)
    await telethon_client.connect()
    if not await telethon_client.is_user_authorized():
        logging.error('Telethon userbot is not authorized. Please run this script with --create-session to create the session file interactively.')
        raise Exception('Userbot not authorized. Run with --create-session to create session.')

async def create_telethon_session_interactive():
    if not API_ID or not API_HASH:
        print('Set TELETHON_API_ID and TELETHON_API_HASH in .env before creating session.')
        return
    print('Starting interactive Telethon login...')
    client = TelegramClient(USERBOT_SESSION, API_ID, API_HASH)
    await client.start()  # will prompt for phone + code in the terminal
    if await client.is_user_authorized():
        print('Session created at', USERBOT_SESSION)
    else:
        print('Failed to authorize session')
    await client.disconnect()

# ---------- DEBUG ----------
@dp.message_handler(commands=['whoami'])
async def whoami(m: types.Message):
    await m.reply(f'Your Telegram ID = {m.from_user.id}')

# ---------- ENTRY POINT ----------
if __name__ == '__main__':
    if '--create-session' in sys.argv:
        asyncio.run(create_telethon_session_interactive())
        sys.exit(0)

    print('Starting bot...')
    if API_ID and API_HASH and not os.path.exists(USERBOT_SESSION):
        print('\nUserbot session not found. To create it run this script with:')
        print('python', sys.argv[0], '--create-session')
        print('This will prompt for phone + code in your terminal (one-time).')

    executor.start_polling(dp, skip_updates=True)

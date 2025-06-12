import os
import logging
import uuid
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
    ConversationHandler
)
import asyncpg
from dotenv import load_dotenv
from aiohttp import web
import aiohttp
# تنظیمات محیطی
load_dotenv()
BOT_TOKEN = os.getenv('BOT_TOKEN')
ADMIN_IDS = [int(id) for id in os.getenv('ADMIN_IDS', '').split(',') if id]

# تنظیمات لاگ
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# حالت‌های گفتگو
UPLOADING, WAITING_CHANNEL_INFO = range(2)

class Database:
    """مدیریت دیتابیس PostgreSQL بهینه‌شده"""
    
    def __init__(self):
        self.pool = None

    async def connect(self):
        """اتصال به دیتابیس"""
        self.pool = await asyncpg.create_pool(os.getenv('DATABASE_URL'))
        await self.init_db()
    
    async def init_db(self):
        """ایجاد جداول مورد نیاز"""
        async with self.pool.acquire() as conn:
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS categories (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    created_by BIGINT NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            ''')
            
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS files (
                    id SERIAL PRIMARY KEY,
                    category_id TEXT NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
                    file_id TEXT NOT NULL UNIQUE,
                    file_name TEXT NOT NULL,
                    file_size BIGINT NOT NULL,
                    file_type TEXT NOT NULL,
                    caption TEXT,
                    upload_date TIMESTAMP DEFAULT NOW()
                )
            ''')
            
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS channels (
                    id SERIAL PRIMARY KEY,
                    channel_id TEXT NOT NULL UNIQUE,
                    channel_name TEXT NOT NULL,
                    invite_link TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            ''')
            
            # ایندکس‌های بهینه‌سازی
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_files_category ON files(category_id)')
            logger.info("Database initialized")

    # --- مدیریت دسته‌ها ---
    async def add_category(self, name: str, created_by: int) -> str:
        """ایجاد دسته جدید"""
        category_id = str(uuid.uuid4())[:8]
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO categories(id, name, created_by) VALUES($1, $2, $3)",
                category_id, name, created_by
            )
        return category_id
    
    async def get_categories(self) -> dict:
        """دریافت تمام دسته‌ها"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT id, name FROM categories")
            return {row['id']: row['name'] for row in rows}
    
    async def get_category(self, category_id: str) -> dict:
        """دریافت اطلاعات یک دسته"""
        async with self.pool.acquire() as conn:
            category = await conn.fetchrow(
                "SELECT name, created_by FROM categories WHERE id = $1", category_id
            )
            if not category:
                return None
                
            files = await conn.fetch(
                "SELECT file_id, file_type, caption FROM files WHERE category_id = $1", category_id
            )
            return {
                'name': category['name'],
                'files': [dict(file) for file in files]
            }

    # --- مدیریت فایل‌ها ---
    async def add_file(self, category_id: str, file_info: dict) -> bool:
        """افزودن فایل به دسته"""
        async with self.pool.acquire() as conn:
            try:
                await conn.execute(
                    "INSERT INTO files(category_id, file_id, file_name, file_size, file_type, caption) "
                    "VALUES($1, $2, $3, $4, $5, $6)",
                    category_id,
                    file_info['file_id'],
                    file_info['file_name'],
                    file_info['file_size'],
                    file_info['file_type'],
                    file_info.get('caption', '')
                )
                return True
            except asyncpg.UniqueViolationError:
                return False
    
    async def add_files(self, category_id: str, files: list) -> int:
        async with self.pool.acquire() as conn:
            inserted_count = 0
            for f in files:
                try:
                    await conn.execute(
                        "INSERT INTO files(category_id, file_id, file_name, file_size, file_type, caption) "
                        "VALUES($1, $2, $3, $4, $5, $6)",
                        category_id,
                        f['file_id'],
                        f['file_name'],
                        f['file_size'],
                        f['file_type'],
                        f.get('caption', '')
                    )
                    inserted_count += 1
                except asyncpg.UniqueViolationError:
                    continue
            return inserted_count

    # --- مدیریت کانال‌ها ---
    async def add_channel(self, channel_id: str, name: str, link: str) -> bool:
        """افزودن کانال اجباری"""
        async with self.pool.acquire() as conn:
            try:
                await conn.execute(
                    "INSERT INTO channels(channel_id, channel_name, invite_link) VALUES($1, $2, $3)",
                    channel_id, name, link
                )
                return True
            except asyncpg.UniqueViolationError:
                return False
    
    async def get_channels(self) -> list:
        """دریافت لیست کانال‌ها"""
        async with self.pool.acquire() as conn:
            return await conn.fetch("SELECT channel_id, channel_name, invite_link FROM channels")
    
    async def delete_channel(self, channel_id: str) -> bool:
        """حذف کانال"""
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM channels WHERE channel_id = $1", channel_id
            )
            return result.split()[-1] == '1'

class BotManager:
    """مدیریت اصلی ربات"""
    
    def __init__(self):
        self.db = Database()
        self.pending_uploads = {}  # {user_id: {'category_id': str, 'files': list}}
        self.pending_channels = {}  # {user_id: {'channel_id': str, 'name': str, 'link': str}}
        self.bot_username = None
    
    async def init(self, bot_username: str):
        """راه‌اندازی اولیه"""
        self.bot_username = bot_username
        await self.db.connect()
    
    def is_admin(self, user_id: int) -> bool:
        """بررسی ادمین بودن کاربر"""
        return user_id in ADMIN_IDS
    
    def generate_link(self, category_id: str) -> str:
        """تولید لینک دسته با یوزرنیم صحیح"""
        if self.bot_username:
            return f"https://t.me/{self.bot_username}?start=cat_{category_id}"
        # Fallback در صورت عدم وجود یوزرنیم
        bot_id = BOT_TOKEN.split(':')[0]
        return f"https://t.me/{bot_id}?start=cat_{category_id}"
    
    def extract_file_info(self, update: Update) -> dict:
        msg = update.message

        if msg.document:
            file = msg.document
            file_type = 'document'
            file_name = file.file_name or f"document_{file.file_id[:8]}"
        elif msg.photo:
            file = msg.photo[-1]  # بالاترین کیفیت
            file_type = 'photo'
            file_name = f"photo_{file.file_id[:8]}.jpg"
        elif msg.video:
            file = msg.video
            file_type = 'video'
            file_name = f"video_{file.file_id[:8]}.mp4"
        elif msg.audio:
            file = msg.audio
            file_type = 'audio'
            file_name = f"audio_{file.file_id[:8]}.mp3"
        else:
            return None

        return {
            'file_id': file.file_id,
            'file_name': file_name,
            'file_size': file.file_size,
            'file_type': file_type,
            'caption': msg.caption or ''
        }

# ایجاد نمونه
bot_manager = BotManager()

# ========================
# ==== HANDLER FUNCTIONS ===
# ========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """دستور شروع"""
    user_id = update.effective_user.id
    
    # دسترسی از طریق لینک دسته
    if context.args and context.args[0].startswith('cat_'):
        category_id = context.args[0][4:]
        await handle_category(update, context, category_id)
        return
    
    if bot_manager.is_admin(user_id):
        await update.message.reply_text(
            "👋 سلام ادمین!\n\n"
            "دستورات:\n"
            "/new_category - ساخت دسته جدید\n"
            "/upload - شروع آپلود فایل\n"
            "/finish_upload - پایان آپلود\n"
            "/categories - نمایش دسته‌ها\n"
            "/add_channel - افزودن کانال\n"
            "/remove_channel - حذف کانال\n"
            "/channels - لیست کانال‌ها"
        )
    else:
        await update.message.reply_text("👋 سلام! برای دریافت فایل‌ها از لینک‌ها استفاده کنید.")

async def is_user_member(context, channel_id, user_id):
    """بررسی عضویت کاربر با تلاش مجدد"""
    for _ in range(3):  # 3 بار تلاش
        try:
            member = await context.bot.get_chat_member(chat_id=channel_id, user_id=user_id)
            if member.status in ['member', 'administrator', 'creator']:
                return True
        except Exception as e:
            logger.warning(f"خطا در بررسی عضویت: {e}")
        
        await asyncio.sleep(2)  # تاخیر 2 ثانیه‌ای بین هر تلاش
    
    return False

async def handle_category(update: Update, context: ContextTypes.DEFAULT_TYPE, category_id: str):
    """مدیریت دسترسی به دسته"""
    # استخراج user_id و message بسته به نوع update
    if update.message:
        user_id = update.message.from_user.id
        message = update.message
    elif update.callback_query:
        user_id = update.callback_query.from_user.id
        message = update.callback_query.message
    else:
        logger.error("Unsupported update type")
        return

    # بررسی ادمین
    if bot_manager.is_admin(user_id):
        await admin_category_menu(message, category_id)
        return
    
    # بررسی عضویت در کانال‌ها
    channels = await bot_manager.db.get_channels()
    if not channels:
        await send_category_files(message, context, category_id)
        return
    
    non_joined = []
    for channel in channels:
        is_member = await is_user_member(context, channel['channel_id'], user_id)
        if not is_member:
            non_joined.append(channel)
    
    if not non_joined:
        await send_category_files(message, context, category_id)
        return
    
    # ایجاد صفحه عضویت
    keyboard = []
    for channel in non_joined:
        button = InlineKeyboardButton(
            text=f"📢 {channel['channel_name']}",
            url=channel['invite_link']
        )
        keyboard.append([button])
    
    keyboard.append([
        InlineKeyboardButton(
            "✅ عضو شدم", 
            callback_data=f"check_{category_id}"
        )
    ])
    
    await message.reply_text(
        "⚠️ برای دسترسی ابتدا در کانال‌های زیر عضو شوید:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def admin_category_menu(message: Message, category_id: str):
    """منوی مدیریت دسته برای ادمین"""
    try:
        category = await bot_manager.db.get_category(category_id)
        if not category:
            await message.reply_text("❌ دسته یافت نشد!")
            return
        
        keyboard = [
            [InlineKeyboardButton("📁 مشاهده فایل‌ها", callback_data=f"view_{category_id}")],
            [InlineKeyboardButton("➕ افزودن فایل", callback_data=f"add_{category_id}")],
            [InlineKeyboardButton("🗑 حذف دسته", callback_data=f"delcat_{category_id}")]
        ]
        
        await message.reply_text(
            f"📂 دسته: {category['name']}\n"
            f"📦 تعداد فایل‌ها: {len(category['files'])}\n\n"
            "لطفا عملیات مورد نظر را انتخاب کنید:",
            reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e:
        logger.error(f"خطا در منوی ادمین: {e}")
        await message.reply_text("❌ خطایی در نمایش منو رخ داد")

async def send_category_files(message: Message, context: ContextTypes.DEFAULT_TYPE, category_id: str):
    """ارسال فایل‌های یک دسته"""
    try:
        chat_id = message.chat_id
        
        category = await bot_manager.db.get_category(category_id)
        if not category or not category['files']:
            await message.reply_text("❌ فایلی برای نمایش وجود ندارد!")
            return
        
        await message.reply_text(f"📤 ارسال فایل‌های '{category['name']}'...")
        
        for file in category['files']:
            try:
                send_func = {
                    'document': context.bot.send_document,
                    'photo': context.bot.send_photo,
                    'video': context.bot.send_video,
                    'audio': context.bot.send_audio
                }.get(file['file_type'])
                
                if send_func:
                    await send_func(
                        chat_id=chat_id,
                        **{file['file_type']: file['file_id']},
                        caption=file.get('caption', '')[:1024]
                    )
                await asyncio.sleep(0.5)  # افزایش تاخیر برای جلوگیری از محدودیت
            except Exception as e:
                logger.error(f"ارسال فایل خطا: {e}")
                await asyncio.sleep(2)
    except Exception as e:
        logger.error(f"خطا در ارسال فایل‌ها: {e}")
        await message.reply_text("❌ خطایی در ارسال فایل‌ها رخ داد")

# ========================
# ==== ADMIN COMMANDS ====
# ========================

async def new_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ایجاد دسته جدید"""
    user_id = update.effective_user.id
    if not bot_manager.is_admin(user_id):
        await update.message.reply_text("❌ دسترسی ممنوع!")
        return
    
    if not context.args:
        await update.message.reply_text("لطفا نام دسته را وارد کنید.\nمثال: /new_category نام_دسته")
        return
    
    name = ' '.join(context.args)
    category_id = await bot_manager.db.add_category(name, user_id)
    link = bot_manager.generate_link(category_id)
    
    await update.message.reply_text(
        f"✅ دسته '{name}' ایجاد شد!\n\n"
        f"🔗 لینک دسته:\n{link}\n\n"
        f"برای آپلود فایل:\n/upload {category_id}")

async def upload_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """شروع آپلود فایل"""
    user_id = update.effective_user.id
    if not bot_manager.is_admin(user_id):
        await update.message.reply_text("❌ دسترسی ممنوع!")
        return
    
    if not context.args:
        await update.message.reply_text("لطفا آیدی دسته را مشخص کنید.\nمثال: /upload CAT_ID")
        return
    
    category_id = context.args[0]
    category = await bot_manager.db.get_category(category_id)
    if not category:
        await update.message.reply_text("❌ دسته یافت نشد!")
        return
    
    bot_manager.pending_uploads[user_id] = {
        'category_id': category_id,
        'files': []
    }
    
    await update.message.reply_text(
        f"📤 حالت آپلود فعال شد! فایل‌ها را ارسال کنید.\n"
        f"برای پایان: /finish_upload\n"
        f"برای لغو: /cancel")
    return UPLOADING

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """پردازش فایل‌های ارسالی"""
    user_id = update.effective_user.id
    if user_id not in bot_manager.pending_uploads:
        return
    
    file_info = bot_manager.extract_file_info(update)
    if not file_info:
        await update.message.reply_text("❌ نوع فایل پشتیبانی نمی‌شود!")
        return
    
    upload = bot_manager.pending_uploads[user_id]
    upload['files'].append(file_info)
    
    await update.message.reply_text(f"✅ فایل دریافت شد! (تعداد: {len(upload['files'])})")

async def finish_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """پایان آپلود فایل‌ها"""
    user_id = update.effective_user.id
    if user_id not in bot_manager.pending_uploads:
        await update.message.reply_text("❌ هیچ آپلودی فعال نیست!")
        return ConversationHandler.END
    
    upload = bot_manager.pending_uploads.pop(user_id)
    if not upload['files']:
        await update.message.reply_text("❌ فایلی دریافت نشد!")
        return ConversationHandler.END
    
    count = await bot_manager.db.add_files(upload['category_id'], upload['files'])
    link = bot_manager.generate_link(upload['category_id'])
    
    await update.message.reply_text(
        f"✅ {count} فایل با موفقیت ذخیره شد!\n\n"
        f"🔗 لینک دسته:\n{link}")
    return ConversationHandler.END

async def categories_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """نمایش لیست دسته‌ها"""
    if not bot_manager.is_admin(update.effective_user.id):
        await update.message.reply_text("❌ دسترسی ممنوع!")
        return
    
    categories = await bot_manager.db.get_categories()
    if not categories:
        await update.message.reply_text("📂 هیچ دسته‌ای وجود ندارد!")
        return
    
    message = "📁 لیست دسته‌ها:\n\n"
    for cid, name in categories.items():
        message += f"• {name} [ID: {cid}]\n"
        message += f"  لینک: {bot_manager.generate_link(cid)}\n\n"
    
    await update.message.reply_text(message)

# ========================
# === CHANNEL MANAGEMENT ==
# ========================

async def add_channel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """شروع افزودن کانال"""
    if not bot_manager.is_admin(update.effective_user.id):
        await update.message.reply_text("❌ دسترسی ممنوع!")
        return
    
    bot_manager.pending_channels[update.effective_user.id] = {}
    await update.message.reply_text(
        "لطفا اطلاعات کانال را به ترتیب ارسال کنید:\n\n"
        "1. آیدی کانال (مثال: -1001234567890)\n"
        "2. نام کانال\n"
        "3. لینک دعوت")
    return WAITING_CHANNEL_INFO

async def handle_channel_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """پردازش اطلاعات کانال"""
    user_id = update.effective_user.id
    text = update.message.text.strip()
    
    if user_id not in bot_manager.pending_channels:
        return ConversationHandler.END
    
    chan_data = bot_manager.pending_channels[user_id]
    
    if 'channel_id' not in chan_data:
        chan_data['channel_id'] = text
        await update.message.reply_text("✅ آیدی دریافت شد! لطفا نام کانال را ارسال کنید:")
        return WAITING_CHANNEL_INFO
    
    if 'name' not in chan_data:
        chan_data['name'] = text
        await update.message.reply_text("✅ نام دریافت شد! لطفا لینک دعوت را ارسال کنید:")
        return WAITING_CHANNEL_INFO
    
    chan_data['link'] = text
    success = await bot_manager.db.add_channel(
        chan_data['channel_id'], 
        chan_data['name'], 
        chan_data['link']
    )
    
    del bot_manager.pending_channels[user_id]
    
    if success:
        await update.message.reply_text("✅ کانال با موفقیت افزوده شد!")
    else:
        await update.message.reply_text("❌ خطا در افزودن کانال (احتمالا تکراری است)")
    
    return ConversationHandler.END

async def remove_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """حذف کانال"""
    if not bot_manager.is_admin(update.effective_user.id):
        await update.message.reply_text("❌ دسترسی ممنوع!")
        return
    
    if not context.args:
        await update.message.reply_text("لطفا آیدی کانال را مشخص کنید.\nمثال: /remove_channel -1001234567890")
        return
    
    success = await bot_manager.db.delete_channel(context.args[0])
    await update.message.reply_text(
        "✅ کانال حذف شد!" if success else "❌ کانال یافت نشد!")

async def list_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """نمایش لیست کانال‌ها"""
    if not bot_manager.is_admin(update.effective_user.id):
        await update.message.reply_text("❌ دسترسی ممنوع!")
        return

    if not context.args:
        await update.message.reply_text("لطفا آیدی دسته را مشخص کنید.\nمثال: /upload CAT_ID")
        return
    
    category_id = context.args[0]
    category = await bot_manager.db.get_category(category_id)
    if not category:
        await update.message.reply_text("❌ دسته یافت نشد!")
        return
    
    bot_manager.pending_uploads[user_id] = {
        'category_id': category_id,
        'files': []
    }
    
    await update.message.reply_text(
        f"📤 حالت آپلود فعال شد! فایل‌ها را ارسال کنید.\n"
        f"برای پایان: /finish_upload\n"
        f"برای لغو: /cancel")
    return UPLOADING

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """پردازش فایل‌های ارسالی"""
    user_id = update.effective_user.id
    if user_id not in bot_manager.pending_uploads:
        return
    
    file_info = bot_manager.extract_file_info(update)
    if not file_info:
        await update.message.reply_text("❌ نوع فایل پشتیبانی نمی‌شود!")
        return
    
    upload = bot_manager.pending_uploads[user_id]
    upload['files'].append(file_info)
    
    await update.message.reply_text(f"✅ فایل دریافت شد! (تعداد: {len(upload['files'])})")

async def finish_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """پایان آپلود فایل‌ها"""
    user_id = update.effective_user.id
    if user_id not in bot_manager.pending_uploads:
        await update.message.reply_text("❌ هیچ آپلودی فعال نیست!")
        return ConversationHandler.END
    
    upload = bot_manager.pending_uploads.pop(user_id)
    if not upload['files']:
        await update.message.reply_text("❌ فایلی دریافت نشد!")
        return ConversationHandler.END
    
    count = await bot_manager.db.add_files(upload['category_id'], upload['files'])
    link = bot_manager.generate_link(upload['category_id'])
    
    await update.message.reply_text(
        f"✅ {count} فایل با موفقیت ذخیره شد!\n\n"
        f"🔗 لینک دسته:\n{link}")
    return ConversationHandler.END

async def categories_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """نمایش لیست دسته‌ها"""
    if not bot_manager.is_admin(update.effective_user.id):
        await update.message.reply_text("❌ دسترسی ممنوع!")
        return
    
    categories = await bot_manager.db.get_categories()
    if not categories:
        await update.message.reply_text("📂 هیچ دسته‌ای وجود ندارد!")
        return
    
    message = "📁 لیست دسته‌ها:\n\n"
    for cid, name in categories.items():
        message += f"• {name} [ID: {cid}]\n"
        message += f"  لینک: {bot_manager.generate_link(cid)}\n\n"
    
    await update.message.reply_text(message)

# ========================
# === CHANNEL MANAGEMENT ==
# ========================

async def add_channel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """شروع افزودن کانال"""
    if not bot_manager.is_admin(update.effective_user.id):
        await update.message.reply_text("❌ دسترسی ممنوع!")
        return
    
    bot_manager.pending_channels[update.effective_user.id] = {}
    await update.message.reply_text(
        "لطفا اطلاعات کانال را به ترتیب ارسال کنید:\n\n"
        "1. آیدی کانال (مثال: -1001234567890)\n"
        "2. نام کانال\n"
        "3. لینک دعوت")
    return WAITING_CHANNEL_INFO

async def handle_channel_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """پردازش اطلاعات کانال"""
    user_id = update.effective_user.id
    text = update.message.text.strip()
    
    if user_id not in bot_manager.pending_channels:
        return ConversationHandler.END
    
    chan_data = bot_manager.pending_channels[user_id]
    
    if 'channel_id' not in chan_data:
        chan_data['channel_id'] = text
        await update.message.reply_text("✅ آیدی دریافت شد! لطفا نام کانال را ارسال کنید:")
        return WAITING_CHANNEL_INFO
    
    if 'name' not in chan_data:
        chan_data['name'] = text
        await update.message.reply_text("✅ نام دریافت شد! لطفا لینک دعوت را ارسال کنید:")
        return WAITING_CHANNEL_INFO
    
    chan_data['link'] = text
    success = await bot_manager.db.add_channel(
        chan_data['channel_id'], 
        chan_data['name'], 
        chan_data['link']
    )
    
    del bot_manager.pending_channels[user_id]
    
    if success:
        await update.message.reply_text("✅ کانال با موفقیت افزوده شد!")
    else:
        await update.message.reply_text("❌ خطا در افزودن کانال (احتمالا تکراری است)")
    
    return ConversationHandler.END

async def remove_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """حذف کانال"""
    if not bot_manager.is_admin(update.effective_user.id):
        await update.message.reply_text("❌ دسترسی ممنوع!")
        return
    
    if not context.args:
        await update.message.reply_text("لطفا آیدی کانال را مشخص کنید.\nمثال: /remove_channel -1001234567890")
        return
    
    success = await bot_manager.db.delete_channel(context.args[0])
    await update.message.reply_text(
        "✅ کانال حذف شد!" if success else "❌ کانال یافت نشد!")

async def list_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """نمایش لیست کانال‌ها"""
    if not bot_manager.is_admin(update.effective_user.id):
        await update.message.reply_text("❌ دسترسی ممنوع!")
        return
    
    channels = await bot_manager.db.get_channels()
    if not channels:
        await update.message.reply_text("📢 هیچ کانالی ثبت نشده است!")
        return
    
    message = "📢 کانال‌های اجباری:\n\n"
    for i, ch in enumerate(channels, 1):
        message += (
            f"{i}. {ch['channel_name']}\n"
            f"   آیدی: {ch['channel_id']}\n"
            f"   لینک: {ch['invite_link']}\n\n"
        )
    
    await update.message.reply_text(message)

# ========================
# === BUTTON HANDLERS ====
# ========================

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """مدیریت کلیک روی دکمه‌ها"""
    query = update.callback_query
    await query.answer()
    data = query.data
    
    # بررسی عضویت در کانال‌ها
    if data.startswith('check_'):
        category_id = data[6:]
        user_id = query.from_user.id
        
        # بررسی مجدد عضویت
        channels = await bot_manager.db.get_channels()
        non_joined = []
        for channel in channels:
            is_member = await is_user_member(context, channel['channel_id'], user_id)
            if not is_member:
                non_joined.append(channel)
        
        if non_joined:
            # هنوز در برخی کانال‌ها عضو نیست
            keyboard = []
            for channel in non_joined:
                button = InlineKeyboardButton(
                    text=f"📢 {channel['channel_name']}",
                    url=channel['invite_link']
                )
                keyboard.append([button])
            
            keyboard.append([
                InlineKeyboardButton(
                    "✅ عضو شدم", 
                    callback_data=f"check_{category_id}"
                )
            ])
            
            await query.edit_message_text(
                "⚠️ هنوز در کانال‌های زیر عضو نشده‌اید:",
                reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            # حالا عضو شده است
            await query.edit_message_text("✅ عضویت شما تأیید شد! در حال آماده‌سازی فایل‌ها...")
            await send_category_files(query.message, context, category_id)
        return
    
    # دستورات ادمین
    user_id = query.from_user.id
    if not bot_manager.is_admin(user_id):
        await query.edit_message_text("❌ دسترسی ممنوع!")
        return
    
    if data.startswith('view_'):
        category_id = data[5:]
        await send_category_files(query.message, context, category_id)
    
    elif data.startswith('add_'):
        category_id = data[4:]
        bot_manager.pending_uploads[user_id] = {
            'category_id': category_id,
            'files': []
        }
        await query.edit_message_text(
            "📤 فایل‌ها را ارسال کنید.\n"
            "برای پایان: /finish_upload\n"
            "برای لغو: /cancel")
    
    elif data.startswith('delcat_'):
        category_id = data[7:]
        category = await bot_manager.db.get_category(category_id)
        if not category:
            await query.edit_message_text("❌ دسته یافت نشد!")
            return
        
        # حذف دسته
        async with bot_manager.db.pool.acquire() as conn:
            await conn.execute("DELETE FROM categories WHERE id = $1", category_id)
        
        await query.edit_message_text(f"✅ دسته '{category['name']}' حذف شد!")

# ========================
# === UTILITY HANDLERS ===
# ========================

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """لغو عملیات جاری"""
    user_id = update.effective_user.id
    if user_id in bot_manager.pending_uploads:
        del bot_manager.pending_uploads[user_id]
    if user_id in bot_manager.pending_channels:
        del bot_manager.pending_channels[user_id]
    
    await update.message.reply_text("❌ عملیات لغو شد.")
    return ConversationHandler.END

# ========================
# === WEB SERVER SETUP ===
# ========================

async def health_check(request):
    """صفحه سلامت برای بررسی وضعیت ربات"""
    return web.Response(text="🤖 Telegram Bot is Running!")

async def keep_alive():
    """ارسال درخواست به health endpoint هر 5 دقیقه"""
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get("https://uploader-bot-ely6.onrender.com") as resp:
                    if resp.status == 200:
                        logger.info("✅ Keep-alive ping sent successfully")
                    else:
                        logger.warning(f"⚠️ Keep-alive failed: {resp.status}")
        except Exception as e:
            logger.warning(f"⚠️ Keep-alive exception: {e}")
        
        await asyncio.sleep(450)  # هر ۵ دقیقه (۳۰۰ ثانیه)


async def run_web_server():
    """اجرای سرور وب ساده"""
    app = web.Application()
    app.router.add_get('/health', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 10000)
    await site.start()
    logger.info("Web server started at port 10000")
    
    # اجرای نامحدود
    while True:
        await asyncio.sleep(3600)

# ========================
# ==== BOT SETUP =========
# ========================

async def run_telegram_bot():
    """اجرای اصلی ربات تلگرام"""
    application = Application.builder().token(BOT_TOKEN).build()
    
    # دریافت یوزرنیم ربات
    await application.initialize()
    bot = await application.bot.get_me()
    bot_username = bot.username
    logger.info(f"Bot username: @{bot_username}")
    await bot_manager.init(bot_username)
    
    # دستورات اصلی
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("new_category", new_category))
    application.add_handler(CommandHandler("categories", categories_list))
    
    # آپلود فایل‌ها
    upload_handler = ConversationHandler(
        entry_points=[CommandHandler("upload", upload_command)],
        states={
            UPLOADING: [
                MessageHandler(
                    filters.Document.ALL | filters.PHOTO | filters.VIDEO | filters.AUDIO,
                    handle_file
                )
            ]
        },
        fallbacks=[
            CommandHandler("finish_upload", finish_upload),
            CommandHandler("cancel", cancel)
        ]
    )
    application.add_handler(upload_handler)
    application.add_handler(
    MessageHandler(
        filters.Document.ALL | filters.PHOTO | filters.VIDEO | filters.AUDIO,
        handle_file
        )
    )
    application.add_handler(CommandHandler("finish_upload", finish_upload))

    # مدیریت کانال‌ها
    channel_handler = ConversationHandler(
        entry_points=[CommandHandler("add_channel", add_channel_cmd)],
        states={
            WAITING_CHANNEL_INFO: [MessageHandler(filters.TEXT, handle_channel_info)]
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    application.add_handler(channel_handler)
    application.add_handler(CommandHandler("remove_channel", remove_channel))
    application.add_handler(CommandHandler("channels", list_channels))
    
    # دکمه‌های اینلاین
    application.add_handler(CallbackQueryHandler(button_handler))
    
    # اجرای ربات
    logger.info("Starting Telegram bot...")
    await application.start()
    await application.updater.start_polling()
    
    # نگه داشتن ربات در حالت اجرا
    while True:
        await asyncio.sleep(3600)

async def main():
    """اجرای همزمان سرور وب و ربات تلگرام"""
    await asyncio.gather(
        run_web_server(),
        run_telegram_bot(),
        keep_alive()
    )

if __name__ == '__main__':
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.exception(f"Critical error: {e}")
    finally:
        loop.close()

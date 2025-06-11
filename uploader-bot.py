import os
import logging
import uuid
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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
            files = await conn.fetch(
                "SELECT file_id, file_type, caption FROM files WHERE category_id = $1", category_id
            )
            return {
                'name': category['name'],
                'files': [dict(file) for file in files]
            } if category else None

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
        """افزودن چندین فایل به صورت گروهی"""
        async with self.pool.acquire() as conn:
            records = [
                (category_id, f['file_id'], f['file_name'], 
                 f['file_size'], f['file_type'], f.get('caption', ''))
                for f in files
            ]
            await conn.executemany(
                "INSERT INTO files(category_id, file_id, file_name, file_size, file_type, caption) "
                "VALUES($1, $2, $3, $4, $5, $6) ON CONFLICT (file_id) DO NOTHING",
                records
            )
            return len(files)

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
    
    async def init(self):
        """راه‌اندازی اولیه"""
        await self.db.connect()
    
    def is_admin(self, user_id: int) -> bool:
        """بررسی ادمین بودن کاربر"""
        return user_id in ADMIN_IDS
    
    def generate_link(self, category_id: str) -> str:
        """تولید لینک دسته"""
        return f"https://t.me/{BOT_TOKEN.split(':')[0]}?start=cat_{category_id}"
    
    def extract_file_info(self, update: Update) -> dict:
        """استخراج اطلاعات فایل"""
        msg = update.message
        file = msg.document or msg.photo[-1] if msg.photo else msg.video or msg.audio
        
        if not file:
            return None
        
        return {
            'file_id': file.file_id,
            'file_name': getattr(file, 'file_name', 'file'),
            'file_size': file.file_size,
            'file_type': 'document' if msg.document else 'photo' if msg.photo else 'video' if msg.video else 'audio',
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

async def handle_category(update: Update, context: ContextTypes.DEFAULT_TYPE, category_id: str):
    """مدیریت دسترسی به دسته"""
    user_id = update.effective_user.id
    
    # بررسی ادمین
    if bot_manager.is_admin(user_id):
        await admin_category_menu(update, category_id)
        return
    
    # بررسی عضویت در کانال‌ها
    channels = await bot_manager.db.get_channels()
    if not channels:
        await send_category_files(update, context, category_id)
        return
    
    non_joined = []
    for channel in channels:
        try:
            member = await context.bot.get_chat_member(
                channel['channel_id'], user_id
            )
            if member.status not in ['member', 'administrator', 'creator']:
                non_joined.append(channel)
        except Exception:
            non_joined.append(channel)
    
    if not non_joined:
        await send_category_files(update, context, category_id)
        return
    
    # ایجاد صفحه عضویت
    keyboard = [
        [InlineKeyboardButton(f"📢 {ch['channel_name']}", url=ch['invite_link'])]
        for ch in non_joined
    ]
    keyboard.append([InlineKeyboardButton("✅ عضو شدم", callback_data=f"check_{category_id}")])
    
    await update.message.reply_text(
        "⚠️ برای دسترسی ابتدا در کانال‌های زیر عضو شوید:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def admin_category_menu(update: Update, category_id: str):
    """منوی مدیریت دسته برای ادمین"""
    category = await bot_manager.db.get_category(category_id)
    if not category:
        await update.message.reply_text("❌ دسته یافت نشد!")
        return
    
    keyboard = [
        [InlineKeyboardButton("📁 مشاهده فایل‌ها", callback_data=f"view_{category_id}")],
        [InlineKeyboardButton("➕ افزودن فایل", callback_data=f"add_{category_id}")],
        [InlineKeyboardButton("🗑 حذف دسته", callback_data=f"delcat_{category_id}")]
    ]
    
    await update.message.reply_text(
        f"📂 دسته: {category['name']}\n"
        f"📦 تعداد فایل‌ها: {len(category['files'])}\n\n"
        "لطفا عملیات مورد نظر را انتخاب کنید:",
        reply_markup=InlineKeyboardMarkup(keyboard))

async def send_category_files(update: Update, context: ContextTypes.DEFAULT_TYPE, category_id: str):
    """ارسال فایل‌های یک دسته"""
    category = await bot_manager.db.get_category(category_id)
    if not category or not category['files']:
        await update.message.reply_text("❌ فایلی برای نمایش وجود ندارد!")
        return
    
    await update.message.reply_text(f"📤 ارسال فایل‌های '{category['name']}'...")
    chat_id = update.effective_chat.id
    
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
            await asyncio.sleep(0.3)
        except Exception as e:
            logger.error(f"ارسال فایل خطا: {e}")

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
        await handle_category(query, context, category_id)
        return
    
    # دستورات ادمین
    user_id = query.from_user.id
    if not bot_manager.is_admin(user_id):
        await query.edit_message_text("❌ دسترسی ممنوع!")
        return
    
    if data.startswith('view_'):
        category_id = data[5:]
        await send_category_files(query, context, category_id)
    
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
        
        # در این نسخه ساده‌سازی شده، حذف مستقیم انجام می‌شود
        # برای نسخه پیشرفته‌تر می‌توان از تایید استفاده کرد
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
# ==== BOT SETUP =========
# ========================

async def init_bot():
    """راه‌اندازی اولیه ربات"""
    await bot_manager.init()
    application = Application.builder().token(BOT_TOKEN).build()
    
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
    logger.info("Starting bot...")
    await application.run_polling()

if __name__ == '__main__':
    asyncio.run(init_bot())

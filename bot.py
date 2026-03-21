import asyncio
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler,
    ConversationHandler, CallbackQueryHandler, filters, ContextTypes
)
from motor.motor_asyncio import AsyncIOMotorClient

ADMIN_ID = int(os.environ.get("ADMIN_ID"))
BOT_TOKEN = os.environ.get("BOT_TOKEN")
MONGO_URI = os.environ.get("MONGO_URI")
PORT = int(os.environ.get("PORT", 8080))

mongo_client = AsyncIOMotorClient(MONGO_URI)
db = mongo_client["telebot"]
users_col = db["users"]

user_map = {}
WAIT_MSG, WAIT_CHOICE, WAIT_BUTTON_INPUT, WAIT_CONFIRM = range(4)

class KeepAliveHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running!")
    def log_message(self, format, *args):
        pass

def run_web_server():
    server = HTTPServer(("0.0.0.0", PORT), KeepAliveHandler)
    server.serve_forever()

async def save_user(user_id, first_name):
    await users_col.update_one({"_id": user_id}, {"$set": {"first_name": first_name}}, upsert=True)

async def get_all_users():
    return [doc["_id"] async for doc in users_col.find({})]

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    user_id = update.message.from_user.id
    if user_id == ADMIN_ID:
        if update.message.reply_to_message:
            original_id = update.message.reply_to_message.message_id
            if original_id in user_map:
                try:
                    await update.message.copy_to(chat_id=user_map[original_id])
                except Exception as e:
                    await update.message.reply_text(f"❌ Failed: {e}")
            else:
                await update.message.reply_text("⚠️ User not found. Ask them to send a new message.")
        return
    user = update.message.from_user
    await save_user(user.id, user.first_name)
    try:
        username = f" (@{user.username})" if user.username else ""
        await context.bot.send_message(chat_id=ADMIN_ID, text=f"👤 *{user.first_name}*{username}\n`ID: {user.id}`", parse_mode="Markdown")
        forwarded = await update.message.forward(chat_id=ADMIN_ID)
        user_map[forwarded.message_id] = user_id
        status = await update.message.reply_text("✅ Message sent!")
    except Exception:
        status = await update.message.reply_text("❌ Failed to send. Try again!")
    await asyncio.sleep(3)
    try:
        await status.delete()
    except:
        pass

async def broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID:
        return ConversationHandler.END
    context.user_data.clear()
    await update.message.reply_text("📢 *Broadcast Setup*\n\nSend the message you want to broadcast.\n_Supports: text, photo, video, voice, sticker, document_", parse_mode="Markdown")
    return WAIT_MSG

async def broadcast_got_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["bc_chat_id"] = update.message.chat_id
    context.user_data["bc_msg_id"] = update.message.message_id
    context.user_data["buttons"] = []
    await update.message.reply_text("👁 *Preview:*", parse_mode="Markdown")
    await update.message.copy_to(chat_id=ADMIN_ID)
    kb = [[InlineKeyboardButton("➕ Add URL Buttons", callback_data="add_btns"), InlineKeyboardButton("📤 Send Now", callback_data="send_now")]]
    await update.message.reply_text("Want to add inline URL buttons?", reply_markup=InlineKeyboardMarkup(kb))
    return WAIT_CHOICE

async def choice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "send_now":
        total = await users_col.count_documents({})
        await query.edit_message_text(f"📤 Sending to {total} users...")
        await _do_broadcast(context, query.message.chat_id)
        return ConversationHandler.END
    await query.edit_message_text("✏️ *Add Inline Buttons*\n\nFormat:\n`Button Name - https://example.com`\n\nMax *10 buttons*. Send /done when finished.", parse_mode="Markdown")
    return WAIT_BUTTON_INPUT

async def collect_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    buttons = context.user_data.get("buttons", [])
    if len(buttons) >= 10:
        await update.message.reply_text("⚠️ Max 10 buttons! Send /done.")
        return WAIT_BUTTON_INPUT
    if " - " not in text:
        await update.message.reply_text("❌ Wrong format!\n`Button Name - https://example.com`", parse_mode="Markdown")
        return WAIT_BUTTON_INPUT
    name, url = text.split(" - ", 1)
    name, url = name.strip(), url.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        await update.message.reply_text("❌ URL must start with `http://` or `https://`", parse_mode="Markdown")
        return WAIT_BUTTON_INPUT
    buttons.append({"name": name, "url": url})
    context.user_data["buttons"] = buttons
    remaining = 10 - len(buttons)
    await update.message.reply_text(f"✅ *Button {len(buttons)}* added! {'Send /done to finish.' if remaining == 0 else f'{remaining} slots left.'}", parse_mode="Markdown")
    return WAIT_BUTTON_INPUT

async def buttons_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    buttons = context.user_data.get("buttons", [])
    keyboard = [[InlineKeyboardButton(b["name"], url=b["url"])] for b in buttons]
    total = await users_col.count_documents({})
    await update.message.reply_text("👁 *Final Preview:*", parse_mode="Markdown")
    await context.bot.copy_message(chat_id=ADMIN_ID, from_chat_id=context.user_data["bc_chat_id"], message_id=context.user_data["bc_msg_id"], reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None)
    btn_list = "\n".join([f"  • {b['name']}" for b in buttons]) if buttons else "  None"
    kb = [[InlineKeyboardButton("✅ Confirm & Send", callback_data="confirm"), InlineKeyboardButton("❌ Cancel", callback_data="cancel")]]
    await update.message.reply_text(f"📊 *Broadcast Summary*\n\n👥 Recipients: *{total} users*\n🔘 Buttons ({len(buttons)}):\n{btn_list}", reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return WAIT_CONFIRM

async def confirm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel":
        await query.edit_message_text("❌ Broadcast cancelled.")
        return ConversationHandler.END
    total = await users_col.count_documents({})
    await query.edit_message_text(f"📤 Broadcasting to {total} users...")
    await _do_broadcast(context, query.message.chat_id)
    return ConversationHandler.END

async def _do_broadcast(context, admin_chat_id):
    buttons = context.user_data.get("buttons", [])
    keyboard = [[InlineKeyboardButton(b["name"], url=b["url"])] for b in buttons]
    rm = InlineKeyboardMarkup(keyboard) if keyboard else None
    users = await get_all_users()
    success, failed = 0, 0
    for uid in users:
        try:
            await context.bot.copy_message(chat_id=uid, from_chat_id=context.user_data["bc_chat_id"], message_id=context.user_data["bc_msg_id"], reply_markup=rm)
            success += 1
        except:
            failed += 1
        await asyncio.sleep(0.05)
    await context.bot.send_message(chat_id=admin_chat_id, text=f"✅ *Broadcast Complete!*\n\n📤 Sent: *{success}*\n❌ Failed: *{failed}*\n👥 Total: *{success + failed}*", parse_mode="Markdown")

async def cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Broadcast cancelled.")
    return ConversationHandler.END

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID:
        return
    total = await users_col.count_documents({})
    await update.message.reply_text(f"📊 *Bot Stats*\n\n👥 Total Users: *{total}*", parse_mode="Markdown")

threading.Thread(target=run_web_server, daemon=True).start()
print(f"✅ Keep-alive server on port {PORT}")

app = ApplicationBuilder().token(BOT_TOKEN).build()

broadcast_conv = ConversationHandler(
    entry_points=[CommandHandler("broadcast", broadcast_start)],
    states={
        WAIT_MSG: [MessageHandler(filters.ALL & ~filters.COMMAND, broadcast_got_msg)],
        WAIT_CHOICE: [CallbackQueryHandler(choice_handler, pattern="^(add_btns|send_now)$")],
        WAIT_BUTTON_INPUT: [CommandHandler("done", buttons_done), MessageHandler(filters.TEXT & ~filters.COMMAND, collect_button)],
        WAIT_CONFIRM: [CallbackQueryHandler(confirm_handler, pattern="^(confirm|cancel)$")],
    },
    fallbacks=[CommandHandler("cancel", cancel_conv)],
    per_message=False,
)

app.add_handler(broadcast_conv)
app.add_handler(CommandHandler("stats", stats))
app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))

print("🤖 Bot is running...")
app.run_polling()
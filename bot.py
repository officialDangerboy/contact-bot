import asyncio
import os
import threading
from collections import defaultdict
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    InputMediaPhoto, InputMediaVideo, InputMediaDocument,
    InputMediaAudio, InputMediaAnimation
)
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes
)
from motor.motor_asyncio import AsyncIOMotorClient

# ─── Config ───────────────────────────────────────────────────────
ADMIN_ID  = int(os.environ.get("ADMIN_ID", "0"))
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
MONGO_URI = os.environ.get("MONGO_URI", "")
PORT      = int(os.environ.get("PORT", 8080))

# ─── MongoDB ──────────────────────────────────────────────────────
mongo_client = AsyncIOMotorClient(MONGO_URI, tls=True, tlsAllowInvalidCertificates=True)
db        = mongo_client["telebot"]
users_col = db["users"]

# ─── user_map: forwarded msg id -> user id ────────────────────────
user_map = {}

# ─── Buffer user messages for 1.5s to collect albums+text ─────────
# uid -> list of messages
user_msg_buffer  = defaultdict(list)
user_task_buffer = {}

# ─── Admin broadcast state (simple global) ────────────────────────
# Using a class so it's always the same object reference
class BroadcastState:
    def __init__(self):
        self.reset()

    def reset(self):
        self.step     = 0   # 0=idle 1=waiting_msg 2=collecting_buttons 3=confirm
        self.buttons  = []
        self.chat_id  = None
        self.msg_id   = None
        self.is_album = False
        self.album_msgs = []  # list of message objects for album

BC = BroadcastState()

# Buffer for admin broadcast album collection
bc_album_msgs = []
bc_album_task = None

BC_IDLE    = 0
BC_WAIT    = 1
BC_BUTTONS = 2
BC_CONFIRM = 3


# ─── Keep-Alive ───────────────────────────────────────────────────
class _H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
    def log_message(self, *a): pass

threading.Thread(target=lambda: HTTPServer(("0.0.0.0", PORT), _H).serve_forever(), daemon=True).start()


# ─── DB ───────────────────────────────────────────────────────────
async def save_user(uid, name):
    await users_col.update_one({"_id": uid}, {"$set": {"name": name}}, upsert=True)

async def get_all_uids():
    return [d["_id"] async for d in users_col.find({})]


# ─── Vertical keyboard (1 button per row) ─────────────────────────
def vertical_kb(buttons):
    """Returns InlineKeyboardMarkup with one button per row."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(b["name"], url=b["url"])] for b in buttons]
    )


# ─── Control keyboards ────────────────────────────────────────────
def kb_options():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("➕ Add Buttons", callback_data="bc_addbtns"),
        InlineKeyboardButton("📤 Send Now",    callback_data="bc_sendnow"),
    ],[
        InlineKeyboardButton("❌ Cancel",      callback_data="bc_cancel"),
    ]])

def kb_confirm():
    rows = [[
        InlineKeyboardButton("✅ Send to All", callback_data="bc_confirm"),
        InlineKeyboardButton("❌ Cancel",      callback_data="bc_cancel"),
    ]]
    if len(BC.buttons) < 10:
        rows.append([InlineKeyboardButton("➕ Add More Buttons", callback_data="bc_addbtns")])
    return InlineKeyboardMarkup(rows)

def kb_cancel():
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="bc_cancel")]])


# ─── CMD menu ─────────────────────────────────────────────────────
CMD_MENU = (
    "📋 *Admin Commands*\n\n"
    "`.cmd` — This menu\n"
    "`.stats` — Total users\n"
    "`.users` — List all users\n"
    "`.broadcast` — Send to all users\n\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "💬 *To reply a user:*\n"
    "Just reply to their forwarded message\n\n"
    "📢 *Broadcast steps:*\n"
    "1. `.broadcast`\n"
    "2. Send your message\n"
    "3. Tap Add Buttons or Send Now\n"
    "4. For buttons, send one per line:\n"
    "   `Name - https://url.com`\n"
    "5. Confirm & send ✅"
)


# ─── Build InputMedia for album sending ───────────────────────────
def to_input_media(msg, caption=None):
    c = caption  # None means no caption
    if msg.photo:
        return InputMediaPhoto(media=msg.photo[-1].file_id, caption=c)
    if msg.video:
        return InputMediaVideo(media=msg.video.file_id, caption=c)
    if msg.document:
        return InputMediaDocument(media=msg.document.file_id, caption=c)
    if msg.audio:
        return InputMediaAudio(media=msg.audio.file_id, caption=c)
    if msg.animation:
        return InputMediaAnimation(media=msg.animation.file_id, caption=c)
    return None


# ─── Show broadcast preview ───────────────────────────────────────
async def show_preview(bot, chat_id, step_after=BC_CONFIRM):
    """Show preview of broadcast message with current buttons."""
    rm = vertical_kb(BC.buttons) if BC.buttons else None
    total = await users_col.count_documents({})

    await bot.send_message(chat_id=chat_id, text="👁 *Preview:*", parse_mode="Markdown")

    if BC.is_album:
        msgs = sorted(BC.album_msgs, key=lambda m: m.message_id)
        media = []
        for i, m in enumerate(msgs):
            # Caption only on first item, rest are empty
            cap = m.caption if i == 0 else None
            obj = to_input_media(m, cap)
            if obj:
                media.append(obj)
        if media:
            await bot.send_media_group(chat_id=chat_id, media=media)
        # Buttons sent separately for albums
        if rm:
            await bot.send_message(chat_id=chat_id, text="🔗 *Buttons:*", parse_mode="Markdown", reply_markup=rm)
    else:
        await bot.copy_message(
            chat_id=chat_id,
            from_chat_id=BC.chat_id,
            message_id=BC.msg_id,
            reply_markup=rm
        )

    # Build button list for summary
    btn_list = "\n".join(f"  {i+1}. {b['name']}" for i, b in enumerate(BC.buttons)) or "  None"
    BC.step = step_after

    await bot.send_message(
        chat_id=chat_id,
        text=(
            f"📊 *Summary*\n\n"
            f"👥 Recipients: *{total}*\n"
            f"🔘 Buttons ({len(BC.buttons)}):\n{btn_list}"
        ),
        parse_mode="Markdown",
        reply_markup=kb_confirm()
    )


# ─── Do broadcast ─────────────────────────────────────────────────
async def do_broadcast(bot, admin_chat_id):
    rm    = vertical_kb(BC.buttons) if BC.buttons else None
    uids  = await get_all_uids()
    ok = fail = 0

    for uid in uids:
        try:
            if BC.is_album:
                msgs = sorted(BC.album_msgs, key=lambda m: m.message_id)
                media = []
                for i, m in enumerate(msgs):
                    cap = m.caption if i == 0 else None
                    obj = to_input_media(m, cap)
                    if obj:
                        media.append(obj)
                if media:
                    await bot.send_media_group(chat_id=uid, media=media)
                if rm:
                    await bot.send_message(chat_id=uid, text="🔗", reply_markup=rm)
            else:
                await bot.copy_message(
                    chat_id=uid,
                    from_chat_id=BC.chat_id,
                    message_id=BC.msg_id,
                    reply_markup=rm
                )
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)

    await bot.send_message(
        chat_id=admin_chat_id,
        text=f"✅ *Done!*\n\n📤 Sent: *{ok}*\n❌ Failed: *{fail}*",
        parse_mode="Markdown"
    )


# ─── Flush user message buffer → forward all to admin ─────────────
async def flush_user(uid, context):
    """Wait 1.5s then forward everything (text + album) together."""
    await asyncio.sleep(1.5)

    msgs = user_msg_buffer.get(uid, [])
    if not msgs:
        return

    msgs_sorted = sorted(msgs, key=lambda m: m.message_id)
    user  = msgs_sorted[0].from_user
    first = msgs_sorted[0]

    try:
        uname = f" (@{user.username})" if user.username else ""
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"👤 *{user.first_name}*{uname}\n`ID: {user.id}`",
            parse_mode="Markdown"
        )
        # forward_messages keeps albums grouped AND preserves captions/text
        fwd_list = await context.bot.forward_messages(
            chat_id=ADMIN_ID,
            from_chat_id=first.chat_id,
            message_ids=[m.message_id for m in msgs_sorted]
        )
        # Map ALL forwarded IDs to this user
        for fwd in fwd_list:
            user_map[fwd.message_id] = uid

        status = await first.reply_text("✅ Sent!")
        await asyncio.sleep(3)
        try:
            await status.delete()
        except Exception:
            pass

    except Exception as e:
        try:
            await first.reply_text("❌ Failed. Try again!")
        except Exception:
            pass

    # Clear buffer
    user_msg_buffer[uid] = []
    user_task_buffer.pop(uid, None)


# ─── Flush admin broadcast album ──────────────────────────────────
async def flush_bc_album(bot, chat_id):
    global bc_album_task
    await asyncio.sleep(1.5)

    if not bc_album_msgs:
        return

    msgs = sorted(bc_album_msgs, key=lambda m: m.message_id)
    BC.is_album   = True
    BC.album_msgs = msgs
    BC.chat_id    = msgs[0].chat_id
    BC.step       = BC_CONFIRM

    await show_preview(bot, chat_id)

    bc_album_msgs.clear()
    bc_album_task = None


# ─── MAIN MESSAGE HANDLER ─────────────────────────────────────────
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global bc_album_task
    msg = update.message
    if not msg:
        return

    uid  = msg.from_user.id
    text = (msg.text or "").strip()

    # ══════════ ADMIN ═══════════════════════════════════════════
    if uid == ADMIN_ID:

        # ── State: waiting for broadcast message ──
        if BC.step == BC_WAIT:
            if msg.media_group_id:
                bc_album_msgs.append(msg)
                if bc_album_task is None or bc_album_task.done():
                    bc_album_task = asyncio.create_task(
                        flush_bc_album(context.bot, msg.chat_id)
                    )
                return
            else:
                BC.is_album = False
                BC.chat_id  = msg.chat_id
                BC.msg_id   = msg.message_id
                BC.step     = BC_CONFIRM

                await show_preview(context.bot, msg.chat_id)
                return

        # ── State: collecting buttons ──
        if BC.step == BC_BUTTONS:
            if not text:
                await msg.reply_text(
                    "❌ Send buttons as text:\n`Name - https://url.com`",
                    parse_mode="Markdown", reply_markup=kb_cancel()
                )
                return

            lines  = [l.strip() for l in text.splitlines() if l.strip()]
            added  = []
            errors = []

            for line in lines:
                if len(BC.buttons) >= 10:
                    errors.append("⚠️ Max 10 buttons reached.")
                    break
                if " - " not in line:
                    errors.append(f"❌ Wrong format: `{line}`")
                    continue
                name, url = line.split(" - ", 1)
                name, url = name.strip(), url.strip()
                if not url.startswith(("http://", "https://")):
                    errors.append(f"❌ Bad URL: `{url}`")
                    continue
                BC.buttons.append({"name": name, "url": url})
                added.append(name)

            if not added:
                reply = "\n".join(errors)
                reply += "\n\nFormat:\n`Button Name - https://example.com`\n_(one per line)_"
                await msg.reply_text(reply, parse_mode="Markdown", reply_markup=kb_cancel())
                return

            # Buttons added — show updated preview
            if errors:
                await msg.reply_text("\n".join(errors), parse_mode="Markdown")

            await show_preview(context.bot, msg.chat_id)
            return

        # ── Dot commands ──
        if text == ".cmd":
            await msg.reply_text(CMD_MENU, parse_mode="Markdown")
            return

        if text == ".stats":
            total = await users_col.count_documents({})
            await msg.reply_text(f"📊 Total Users: *{total}*", parse_mode="Markdown")
            return

        if text == ".users":
            lines = []
            async for doc in users_col.find({}):
                lines.append(f"• {doc.get('name','?')} — `{doc['_id']}`")
            if not lines:
                await msg.reply_text("No users yet.")
                return
            for chunk in [lines[i:i+30] for i in range(0, len(lines), 30)]:
                await msg.reply_text(
                    f"👥 *{len(lines)} Users:*\n\n" + "\n".join(chunk),
                    parse_mode="Markdown"
                )
            return

        if text == ".broadcast":
            BC.reset()
            bc_album_msgs.clear()
            BC.step = BC_WAIT
            await msg.reply_text(
                "📢 *Broadcast*\n\nSend the message to broadcast.\n"
                "_Text, photo, video, album, sticker, voice, file — all supported_",
                parse_mode="Markdown",
                reply_markup=kb_cancel()
            )
            return

        # ── Reply to user ──
        if msg.reply_to_message:
            orig_id = msg.reply_to_message.message_id
            if orig_id in user_map:
                try:
                    await context.bot.copy_message(
                        chat_id=user_map[orig_id],
                        from_chat_id=msg.chat_id,
                        message_id=msg.message_id
                    )
                except Exception as e:
                    await msg.reply_text(f"❌ {e}")
            else:
                await msg.reply_text("⚠️ User not found. Ask them to send a new message.")
        return

    # ══════════ USER ════════════════════════════════════════════
    user = msg.from_user
    await save_user(user.id, user.first_name)

    # Add to buffer
    user_msg_buffer[uid].append(msg)

    # Cancel existing task and restart (to wait for more messages)
    existing = user_task_buffer.get(uid)
    if existing and not existing.done():
        existing.cancel()

    user_task_buffer[uid] = asyncio.create_task(flush_user(uid, context))

    # Show status only on first message
    if len(user_msg_buffer[uid]) == 1:
        status = await msg.reply_text("⏳ Sending...")
        await asyncio.sleep(2.0)
        try:
            await status.delete()
        except Exception:
            pass


# ─── CALLBACK HANDLER ─────────────────────────────────────────────
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    await q.answer()
    data = q.data

    if data == "bc_cancel":
        BC.reset()
        await q.edit_message_text("❌ Broadcast cancelled.")
        return

    if data == "bc_sendnow":
        total = await users_col.count_documents({})
        await q.edit_message_text(f"📤 Sending to *{total} users*...", parse_mode="Markdown")
        await do_broadcast(context.bot, q.message.chat_id)
        BC.reset()
        return

    if data == "bc_addbtns":
        BC.step = BC_BUTTONS
        await q.edit_message_text(
            "✏️ *Add Buttons*\n\n"
            "Send all buttons in *one message*, one per line:\n\n"
            "`Button Name - https://example.com`\n"
            "`Join Channel - https://t.me/channel`\n\n"
            "_Max 10 buttons_",
            parse_mode="Markdown",
            reply_markup=kb_cancel()
        )
        return

    if data == "bc_confirm":
        total = await users_col.count_documents({})
        await q.edit_message_text(f"📤 Sending to *{total} users*...", parse_mode="Markdown")
        await do_broadcast(context.bot, q.message.chat_id)
        BC.reset()
        return


# ─── Run ──────────────────────────────────────────────────────────
app = ApplicationBuilder().token(BOT_TOKEN).build()
app.add_handler(CallbackQueryHandler(on_callback))
app.add_handler(MessageHandler(filters.ALL, on_message))

print("🤖 Bot running...")
app.run_polling()

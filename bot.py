import asyncio
asyncio.set_event_loop(asyncio.new_event_loop())

import os
import re
import time
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, ChatPermissions
from pyrogram.enums import ChatMemberStatus, ChatType
from pyrogram.errors import FloodWait
import motor.motor_asyncio

# --- CONFIGURATION (Loaded from Environment Variables) ---
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "") 
OWNER_ID = int(os.environ.get("OWNER_ID", 0))
MONGO_URI = os.environ.get("MONGO_URI", "")

# 60 seconds cooldown for bio checks to save API calls
CACHE_DURATION = 60  
MAX_WARNINGS = 3  

URL_PATTERN = re.compile(r"(https?://\S+|www\.\S+|t\.me/\S+)", re.IGNORECASE)
MENTION_PATTERN = re.compile(r"@[a-zA-Z0-9_]+")

# --- DATABASE SETUP ---
mongo_client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI)
db = mongo_client['telegram_bot']
users_db = db['users']       
warnings_db = db['warnings'] 
chats_db = db['chats']       
stats_db = db['stats']       
bypassed_db = db['bypassed'] 

# RAM Caches (Safe to lose on restart, prevents DB spam)
checked_users = {}
known_usernames = {} 

app = Client("bio_checker_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# --- TEXT RESOURCES ---
HELP_TEXT = (
    "**🛡️ Bot Help & Commands**\n\n"
    "**User Guide:**\n"
    "I automatically monitor messages and user bios to keep groups free from spam. "
    "If you receive a warning, completely remove the link/username from your bio and click my verification button!\n\n"
    "**Admin Commands (Group Only):**\n"
    "• `/status` (or `/stutas`, `/stats`) - View bot performance & security stats.\n"
    "• `/unwarn <reply/@username>` - Reset a user's warning count.\n"
    "• `/unban <reply/@username>` - Unban a user from the group.\n"
    "• `/bypass <reply/@username>` - Whitelist a user (ignore links/bio).\n"
    "• `/unbypass <reply/@username>` - Remove a user from the whitelist.\n\n"
    "**Owner Commands (DM Only):**\n"
    "• `/broadcast <reply>` - Send a message to all groups and users."
)

# --- HELPER FUNCTIONS ---
def get_warning_keyboard(user_id: int):
    dm_url = f"https://t.me/{BOT_USERNAME}?start=verify_{user_id}"
    return InlineKeyboardMarkup([[InlineKeyboardButton("🗑️ Remove Warning", url=dm_url)]])

async def safe_get_chat(client: Client, chat_id_or_username):
    """Safely fetches a chat/user profile and handles Telegram's rate limits."""
    try:
        return await client.get_chat(chat_id_or_username)
    except FloodWait as e:
        print(f"⚠️ API Limit hit! Sleeping for {e.value} seconds...")
        await asyncio.sleep(e.value)
        try:
            return await client.get_chat(chat_id_or_username)
        except Exception:
            return None
    except Exception:
        return None

async def is_admin(client: Client, chat_id: int, user_id: int) -> bool:
    try:
        member = await client.get_chat_member(chat_id, user_id)
        return member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]
    except Exception:
        return False

async def get_target_user(client: Client, message: Message):
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user
    elif len(message.command) > 1:
        target = message.command[1]
        try:
            return await client.get_users(target)
        except Exception:
            return None
    return None

async def is_spam(client: Client, text: str) -> bool:
    if not text: return False

    text_lower = text.lower()
    bot_mention = f"@{BOT_USERNAME.lower()}"
    bot_link = f"t.me/{BOT_USERNAME.lower()}"

    if bot_mention in text_lower or bot_link in text_lower:
        return True

    if URL_PATTERN.search(text): return True

    mentions = MENTION_PATTERN.findall(text)
    for mention in mentions:
        username = mention.lower()
        if username in known_usernames:
            if known_usernames[username]: return True  
            else: continue  
                
        try:
            chat = await safe_get_chat(client, username)
            if chat and chat.type in [ChatType.CHANNEL, ChatType.GROUP, ChatType.SUPERGROUP]:
                known_usernames[username] = True
                return True
            else:
                known_usernames[username] = False
        except Exception:
            known_usernames[username] = False
            
    return False

async def handle_strike(client: Client, chat_id: int, user, reason: str, message_to_reply: Message, is_bio_violation: bool = False):
    await stats_db.update_one({"_id": "global"}, {"$inc": {"warnings_issued": 1}}, upsert=True)
    
    user_doc = await users_db.find_one_and_update(
        {"_id": user.id},
        {"$inc": {"strikes": 1}},
        upsert=True,
        return_document=motor.motor_asyncio.AsyncIOMotorCollection.ReturnDocument.AFTER
    )
    strikes = user_doc.get("strikes", 1)
    
    if strikes >= MAX_WARNINGS:
        try:
            await client.ban_chat_member(chat_id, user.id)
            await stats_db.update_one({"_id": "global"}, {"$inc": {"users_banned": 1}}, upsert=True)
            await message_to_reply.reply_text(f"🚫 {user.mention} has been banned for repeatedly ignoring spam rules.")
            await warnings_db.delete_one({"_id": user.id})
        except Exception:
            pass 
    else:
        mute_text = ""
        if is_bio_violation:
            try:
                # Mute the user if they have a bad bio
                await client.restrict_chat_member(chat_id, user.id, ChatPermissions(can_send_messages=False))
                mute_text = "\n\n🔇 **You have been MUTED until you fix this.**"
            except Exception:
                pass 

        warn_msg = await message_to_reply.reply_text(
            f"⚠️ {user.mention}, {reason}{mute_text}\n"
            f"Please fix this immediately to comply with rules. (Warning {strikes}/{MAX_WARNINGS})",
            reply_markup=get_warning_keyboard(user.id)
        )
        
        await warnings_db.update_one(
            {"_id": user.id}, 
            {"$set": {"chat_id": chat_id, "message_id": warn_msg.id}}, 
            upsert=True
        )


# --- PRIVATE DM COMMANDS ---
@app.on_message(filters.private & filters.command("start"))
async def handle_dm_start(client: Client, message: Message):
    await chats_db.update_one({"_id": message.chat.id}, {"$set": {"_id": message.chat.id}}, upsert=True)
    
    if len(message.command) < 2:
        await message.reply_text(f"👋 **Hello! I am your Group Security Bot.**\n\n{HELP_TEXT}")
        return

    payload = message.command[1]
    if payload.startswith("verify_"):
        try:
            target_user_id = int(payload.split("_")[1])
        except ValueError:
            return

        if message.from_user.id != target_user_id:
            await message.reply_text("❌ This verification payload belongs to another user.")
            return

        active_warning = await warnings_db.find_one({"_id": target_user_id})
        if not active_warning:
            await message.reply_text("✅ No active group warnings found for your account, or it was already removed.")
            return

        try:
            user_info = await safe_get_chat(client, target_user_id)
            if not user_info:
                await message.reply_text("❌ Could not fetch your profile. Please try again later.")
                return

            bio = user_info.bio or ""
            
            if await is_spam(client, bio):
                await message.reply_text("❌ **You haven't changed your bio yet!**\nPlease completely remove any links or usernames.")
            else:
                chat_id = active_warning.get("chat_id")
                warn_message_id = active_warning.get("message_id")
                try:
                    await client.delete_messages(chat_id, warn_message_id)
                    
                    # Unmute the user back in the group
                    await client.restrict_chat_member(
                        chat_id, 
                        target_user_id, 
                        ChatPermissions(
                            can_send_messages=True,
                            can_send_media_messages=True,
                            can_send_other_messages=True,
                            can_add_web_page_previews=True
                        )
                    )
                except Exception:
                    pass 
                
                await stats_db.update_one({"_id": "global"}, {"$inc": {"warnings_resolved": 1}}, upsert=True)
                await warnings_db.delete_one({"_id": target_user_id})
                
                await message.reply_text("✅ **Thank you!** I verified your bio is clean. Your warning has been removed and you have been unmuted in the group.")
                
        except Exception as e:
            await message.reply_text(f"An error occurred during verification: `{e}`")


# --- GLOBAL / ADMIN COMMANDS ---
@app.on_message(filters.command("help"))
async def cmd_help(client: Client, message: Message):
    await chats_db.update_one({"_id": message.chat.id}, {"$set": {"_id": message.chat.id}}, upsert=True)
    await message.reply_text(HELP_TEXT)

@app.on_message(filters.command("broadcast") & filters.user(OWNER_ID))
async def cmd_broadcast(client: Client, message: Message):
    if not message.reply_to_message:
        await message.reply_text("❌ Please reply to the message you want to broadcast.")
        return

    chat_count = await chats_db.count_documents({})
    await message.reply_text(f"📢 Starting broadcast to {chat_count} chats...")
    
    sent = 0
    failed = 0
    cursor = chats_db.find({})
    
    async for chat in cursor:
        try:
            await message.reply_to_message.copy(chat["_id"])
            sent += 1
            await asyncio.sleep(0.1) 
        except Exception:
            failed += 1
            
    await message.reply_text(f"✅ **Broadcast Complete!**\n\n📤 Successfully sent: `{sent}`\n🚫 Failed: `{failed}`")

@app.on_message(filters.command(["stats", "status", "stutas"]) & filters.group)
async def show_stats(client: Client, message: Message):
    await chats_db.update_one({"_id": message.chat.id}, {"$set": {"_id": message.chat.id}}, upsert=True)
    if not message.from_user or not await is_admin(client, message.chat.id, message.from_user.id):
        return

    stats = await stats_db.find_one({"_id": "global"}) or {}
    stats_text = (
        "📊 **Bot Security Status**\n\n"
        f"🗑️ Spam Messages Deleted: `{stats.get('spam_messages_deleted', 0)}`\n"
        f"⚠️ Total Warnings Issued: `{stats.get('warnings_issued', 0)}`\n"
        f"✅ Warnings Resolved by Users: `{stats.get('warnings_resolved', 0)}`\n"
        f"🚫 Users Banned: `{stats.get('users_banned', 0)}`"
    )
    await message.reply_text(stats_text)

@app.on_message(filters.command("unwarn") & filters.group)
async def cmd_unwarn(client: Client, message: Message):
    await chats_db.update_one({"_id": message.chat.id}, {"$set": {"_id": message.chat.id}}, upsert=True)
    if not message.from_user or not await is_admin(client, message.chat.id, message.from_user.id):
        return

    target_user = await get_target_user(client, message)
    if not target_user:
        await message.reply_text("❌ Please reply to a user's message or type `/unwarn @username`.")
        return

    await users_db.update_one({"_id": target_user.id}, {"$set": {"strikes": 0}}, upsert=True)
    
    active_warning = await warnings_db.find_one({"_id": target_user.id})
    if active_warning:
        try:
            await client.delete_messages(active_warning["chat_id"], active_warning["message_id"])
            # Unmute them just in case they were muted by a bio warning
            await client.restrict_chat_member(
                active_warning["chat_id"], 
                target_user.id, 
                ChatPermissions(can_send_messages=True, can_send_media_messages=True, can_send_other_messages=True, can_add_web_page_previews=True)
            )
        except Exception:
            pass
        await warnings_db.delete_one({"_id": target_user.id})

    await message.reply_text(f"✅ All warnings for {target_user.mention} have been reset to zero.")

@app.on_message(filters.command("unban") & filters.group)
async def cmd_unban(client: Client, message: Message):
    await chats_db.update_one({"_id": message.chat.id}, {"$set": {"_id": message.chat.id}}, upsert=True)
    if not message.from_user or not await is_admin(client, message.chat.id, message.from_user.id):
        return

    target_user = await get_target_user(client, message)
    if not target_user:
        await message.reply_text("❌ Please reply to a user's message or type `/unban @username`.")
        return

    try:
        await client.unban_chat_member(message.chat.id, target_user.id)
        await users_db.update_one({"_id": target_user.id}, {"$set": {"strikes": 0}}, upsert=True)
        await message.reply_text(f"✅ {target_user.mention} has been unbanned and their warnings reset.")
    except Exception as e:
        await message.reply_text(f"❌ Failed to unban {target_user.mention}. Error: `{e}`")

@app.on_message(filters.command("bypass") & filters.group)
async def cmd_bypass(client: Client, message: Message):
    await chats_db.update_one({"_id": message.chat.id}, {"$set": {"_id": message.chat.id}}, upsert=True)
    if not message.from_user or not await is_admin(client, message.chat.id, message.from_user.id):
        return

    target_user = await get_target_user(client, message)
    if not target_user:
        await message.reply_text("❌ Please reply to a user's message or type `/bypass @username`.")
        return

    await bypassed_db.update_one({"_id": target_user.id}, {"$set": {"_id": target_user.id}}, upsert=True)
    await message.reply_text(f"✅ **Bypassed:** {target_user.mention} is now whitelisted. I will ignore their links and bio.")

@app.on_message(filters.command("unbypass") & filters.group)
async def cmd_unbypass(client: Client, message: Message):
    await chats_db.update_one({"_id": message.chat.id}, {"$set": {"_id": message.chat.id}}, upsert=True)
    if not message.from_user or not await is_admin(client, message.chat.id, message.from_user.id):
        return

    target_user = await get_target_user(client, message)
    if not target_user:
        await message.reply_text("❌ Please reply to a user's message or type `/unbypass @username`.")
        return

    await bypassed_db.delete_one({"_id": target_user.id})
    await message.reply_text(f"❌ **Unbypassed:** {target_user.mention} has been removed from the whitelist. Their messages will be checked again.")


# --- CORE LISTENERS ---
@app.on_message(filters.group & ~filters.bot & ~filters.command(["stats", "status", "stutas", "unwarn", "unban", "help", "bypass", "unbypass"]))
async def check_message_and_bio(client: Client, message: Message):
    await chats_db.update_one({"_id": message.chat.id}, {"$set": {"_id": message.chat.id}}, upsert=True)
    user = message.from_user
    if not user: return

    # Stop checking if user is whitelisted
    if await bypassed_db.find_one({"_id": user.id}):
        return  

    message_content = message.text or message.caption or ""
    if await is_spam(client, message_content):
        try:
            await message.delete()
            await stats_db.update_one({"_id": "global"}, {"$inc": {"spam_messages_deleted": 1}}, upsert=True)
        except Exception:
            pass 
        await handle_strike(client, message.chat.id, user, "you cannot send links or channel usernames in this group!", message, is_bio_violation=False)

    current_time = time.time()
    if user.id in checked_users and (current_time - checked_users[user.id]) < CACHE_DURATION:
        return  

    try:
        checked_users[user.id] = current_time
        user_info = await safe_get_chat(client, user.id)
        if not user_info:
            return 
            
        bio = user_info.bio or ""
        
        if await is_spam(client, bio):
            await handle_strike(client, message.chat.id, user, "your profile bio contains a link or channel username!", message, is_bio_violation=True)
    except Exception:
        pass

@app.on_message(filters.new_chat_members)
async def check_new_member_bio(client: Client, message: Message):
    await chats_db.update_one({"_id": message.chat.id}, {"$set": {"_id": message.chat.id}}, upsert=True)
    for new_member in message.new_chat_members:
        # Skip if new member is whitelisted
        if await bypassed_db.find_one({"_id": new_member.id}):
            continue  

        try:
            checked_users[new_member.id] = time.time()
            user_info = await safe_get_chat(client, new_member.id)
            if not user_info:
                continue

            bio = user_info.bio or ""
            
            if await is_spam(client, bio):
                await handle_strike(client, message.chat.id, new_member, "your profile bio contains a link or channel username!", message, is_bio_violation=True)
        except Exception:
            pass

from aiohttp import web
from pyrogram import idle

async def web_server(request):
    return web.Response(text="Bot is alive and running!")

async def main():
    # 1. Start the dummy web server instantly so Render doesn't fail the port scan
    web_app = web.Application()
    web_app.add_routes([web.get('/', web_server)])
    runner = web.AppRunner(web_app)
    await runner.setup()
    
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    
    # 2. Start the Telegram Bot
    print("Bot is starting with MongoDB connected...")
    await app.start()
    await idle()
    await app.stop()

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
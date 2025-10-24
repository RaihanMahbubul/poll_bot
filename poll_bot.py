
import telegram
from telegram.ext import (
    Application, 
    CommandHandler, 
    MessageHandler, 
    filters, 
    ContextTypes,
    # PicklePersistence বাদ দেওয়া হয়েছে
)
from telegram.ext._jobqueue import Job
import google.generativeai as genai
import json
import asyncio
import os # <-- নতুন ইম্পোর্ট
import psycopg2 # <-- নতুন ইম্পোর্ট
from urllib.parse import urlparse # <-- নতুন ইম্পোর্ট

# -----------------------------------------------------------------
# --- আপনার টোকেন এবং কী পরিবেশ থেকে লোড হবে ---
# --- এগুলো আর কোডে হার্ডকোড করা হবে না ---
TELEGRAM_BOT_TOKEN = os.environ.get("8433405847:AAFwxcEPofbRkZ8QLRF8SpLn4hbF-pPluG8")
GEMINI_API_KEY = os.environ.get("AIzaSyAVwCdnIDqK7bOwWbvSBK_UJCf6Ui3jA6Q")
DATABASE_URL = os.environ.get("postgresql://poll_bot_db_user:dYb9wICOkT6ulSFLwK2AWSDBTNhQOdgu@dpg-d3trgpqli9vc73bkq9pg-a/poll_bot_db") # <-- Render ডাটাবেস URL
# -----------------------------------------------------------------

# conversation-এর দুটি অবস্থা (state)
STATE_IDLE, STATE_AWAITING_INTRO = range(2)
TEXT_BUFFER_DELAY = 3  # সেকেন্ড

# --- নতুন ফাংশন: ডাটাবেস কানেকশন ---
def get_db_connection():
    """Render-এর DATABASE_URL থেকে কানেকশন তৈরি করে।"""
    try:
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    except Exception as e:
        print(f"ডাটাবেস কানেকশনে সমস্যা: {e}")
        return None

# --- নতুন ফাংশন: ডাটাবেস টেবিল তৈরি ---
def init_db():
    """বট চালু হওয়ার সময় এই ফাংশন ডাটাবেস টেবিল তৈরি করবে।"""
    conn = get_db_connection()
    if conn is None:
        print("ডাটাবেস ইনিশিয়ালাইজ করা যাচ্ছে না।")
        return
        
    try:
        with conn.cursor() as cur:
            # user_id কে PRIMARY KEY হিসেবে ব্যবহার করা হচ্ছে
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_settings (
                    user_id BIGINT PRIMARY KEY,
                    target_channel TEXT
                );
            """)
            conn.commit()
        print("ডাটাবেস টেবিল (user_settings) সফলভাবে চেক/তৈরি করা হয়েছে।")
    except Exception as e:
        print(f"টেবিল তৈরিতে সমস্যা: {e}")
    finally:
        conn.close()

# --- নতুন ফাংশন: ডাটাবেস থেকে চ্যানেল আইডি পড়া ---
def get_target_channel_from_db(user_id: int) -> str | None:
    conn = get_db_connection()
    if conn is None: return None
    
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT target_channel FROM user_settings WHERE user_id = %s", (user_id,))
            result = cur.fetchone()
            if result:
                return result[0] # target_channel
            return None
    except Exception as e:
        print(f"চ্যানেল আইডি পড়াতে সমস্যা: {e}")
        return None
    finally:
        conn.close()

# --- নতুন ফাংশন: ডাটাবেসে চ্যানেল আইডি সেভ করা ---
def save_target_channel_to_db(user_id: int, target_channel: str):
    conn = get_db_connection()
    if conn is None: return

    try:
        with conn.cursor() as cur:
            # ON CONFLICT... (UPSERT): যদি ইউজার আইডি আগে থেকেই থাকে, তবে আপডেট করো, না থাকলে নতুন করে ইনসার্ট করো।
            cur.execute("""
                INSERT INTO user_settings (user_id, target_channel)
                VALUES (%s, %s)
                ON CONFLICT (user_id) DO UPDATE SET target_channel = EXCLUDED.target_channel;
            """, (user_id, target_channel))
            conn.commit()
    except Exception as e:
        print(f"চ্যানেল আইডি সেভ করতে সমস্যা: {e}")
    finally:
        conn.close()

# জেমিনি এআই কনফিগার করুন
try:
    genai.configure(api_key=GEMINI_API_KEY)
    generation_config = genai.GenerationConfig(response_mime_type="application/json")
    ai_model = genai.GenerativeModel('gemini-flash-latest', generation_config=generation_config)
    print("Gemini AI সফলভাবে কনফিগার করা হয়েছে (JSON মোডে)।")
except Exception as e:
    print(f"Gemini AI কনফিগারেশনে সমস্যা: {e}")

# AI দিয়ে প্রশ্ন জেনারেট করার ফাংশন (পরিবর্তন নেই)
def get_questions_from_ai(text):
    prompt = f"""
    তুমি একজন দক্ষ টেলিগ্রাম বট। তোমার কাজ হলো নিচের টেক্সট থেকে শুধুমাত্র মাল্টিপল চয়েস প্রশ্ন (MCQ) বের করা।
    তোমার উত্তর অবশ্যই একটি JSON লিস্ট ফরম্যাটে হতে হবে। প্রতিটি অবজেক্টে ৪টি কী থাকবে:
    1. "question": (স্ট্রিং) প্রশ্নটি।
    2. "options": (লিস্ট) অপশনগুলোর লিস্ট (সর্বোচ্চ ১০টি)।
    3. "correct_option_index": (সংখ্যা) সঠিক অপশনের ইনডেক্স (0 থেকে শুরু)।
    4. "explanation": (স্ট্রিং) সঠিক উত্তরের একটি সংক্ষিপ্ত ব্যাখ্যা। যদি ব্যাখ্যা খুঁজে না পাও, তবে এর মান `null` দাও।
    টেক্সট:
    ---
    {text}
    ---
    """
    try:
        response = ai_model.generate_content(prompt)
        if not response.parts:
            print(f"AI রেসপন্স ব্লকড। কারণ: {response.prompt_feedback}")
            return None
        json_data = json.loads(response.text)
        return json_data
    except Exception as e:
        print(f"AI বা JSON পার্সিং-এ অজানা সমস্যা: {e}") 
        return None

# টুল ফাংশন: স্টেট রিসেট করার জন্য (পরিবর্তন নেই)
def clear_user_state(user_data: dict):
    user_data['CONV_STATE'] = STATE_IDLE
    if 'pending_quiz_data' in user_data: del user_data['pending_quiz_data']
    job_to_remove: Job | None = user_data.get('buffer_job')
    if job_to_remove:
        job_to_remove.remove()
        del user_data['buffer_job']
    if 'text_buffer' in user_data: del user_data['text_buffer']


# /start কমান্ড হ্যান্ডলার (পরিবর্তন নেই)
async def start_command(update: telegram.Update, context: ContextTypes.DEFAULT_TYPE):
    clear_user_state(context.user_data)
    await update.message.reply_text(
        "আসসালামু আলাইকুম!\n"
        "পোস্ট করার আগে, অনুগ্রহ করে /setchannel কমান্ড দিয়ে টার্গেট চ্যানেল সেট করুন।"
    )

# /setchannel কমান্ড হ্যান্ডলার (আপডেটেড)
async def set_channel(update: telegram.Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    clear_user_state(context.user_data)
    
    if not context.args:
        await update.message.reply_text("⚠️ ব্যবহার: /setchannel <channel_id_or_@username>")
        return
        
    target_channel = context.args[0]
    
    # --- পরিবর্তন: ডাটাবেসে সেভ করা ---
    save_target_channel_to_db(user_id, target_channel)
    
    await update.message.reply_text(
        f"✅ টার্গেট চ্যানেল সফলভাবে সেট করা হয়েছে: {target_channel}\n"
        "(এই সেটিংটি এখন স্থায়ীভাবে সেভ থাকবে)"
    )

# /cancel কমান্ড হ্যান্ডলার (পরিবর্তন নেই)
async def cancel_quiz(update: telegram.Update, context: ContextTypes.DEFAULT_TYPE):
    clear_user_state(context.user_data)
    await update.message.reply_text("বর্তমান কাজটি বাতিল করা হয়েছে। আপনি নতুন প্রশ্ন পাঠাতে পারেন।")


# টাইমার শেষ হলে এই ফাংশনটি রান হবে (আপডেটেড)
async def process_buffered_text(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data
    chat_id = job_data['chat_id']
    user_id = job_data['user_id']
    
    # user_data সরাসরি context.user_data দিয়ে পাওয়া যাবে না
    user_data = context.application.user_data[user_id] 

    # --- পরিবর্তন: ডাটাবেস থেকে চ্যানেল আইডি পড়া ---
    target_channel = get_target_channel_from_db(user_id)
    if not target_channel:
        await context.bot.send_message(chat_id=chat_id, text="⚠️ টার্গেট চ্যানেল সেট করা নেই। /setchannel ব্যবহার করুন।")
        clear_user_state(user_data)
        return

    full_text = "\n".join(user_data.get('text_buffer', []))
    
    if 'buffer_job' in user_data: del user_data['buffer_job']
    if 'text_buffer' in user_data: del user_data['text_buffer']
        
    if not full_text:
        await context.bot.send_message(chat_id=chat_id, text="⚠️ টেক্সট খুঁজে পাওয়া যায়নি।")
        clear_user_state(user_data)
        return

    await context.bot.send_message(chat_id=chat_id, text=f"সম্পূর্ণ টেক্সট পেয়েছি ({len(full_text)} অক্ষর)। জেমিনি এআই দিয়ে প্রসেস করছি... 🤖")
    
    questions_data = get_questions_from_ai(full_text)
    
    if not questions_data or not isinstance(questions_data, list) or len(questions_data) == 0:
        await context.bot.send_message(chat_id=chat_id, text="দুঃখিত, AI প্রশ্ন তৈরি করতে ব্যর্থ হয়েছে বা কোনো প্রশ্ন খুঁজে পায়নি।")
        clear_user_state(user_data)
        return
    
    user_data['pending_quiz_data'] = questions_data
    user_data['CONV_STATE'] = STATE_AWAITING_INTRO 
    
    await context.bot.send_message(
        chat_id=chat_id, 
        text=f"✅ {len(questions_data)} টি প্রশ্ন সফলভাবে প্রসেস করা হয়েছে।\n\n"
             "➡️ **এখন এই কুইজের জন্য একটি সূচনা বার্তা (intro text) পাঠান।**\n\n"
             "(অথবা /cancel লিখে বাতিল করুন)"
    )


# টেক্সট মেসেজ হ্যান্ডলার (আপডেটেড)
async def handle_text(update: telegram.Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = update.message.text
    chat_id = update.message.chat_id
    user = update.effective_user
    
    current_state = context.user_data.get('CONV_STATE', STATE_IDLE)
    
    # --- ধাপ ১: যদি বট সূচনার জন্য অপেক্ষা করে ---
    if current_state == STATE_AWAITING_INTRO:
        
        intro_text = user_message
        # --- পরিবর্তন: ডাটাবেস থেকে চ্যানেল আইডি পড়া ---
        target_channel = get_target_channel_from_db(user.id)
        questions_data = context.user_data.get('pending_quiz_data')
        
        if not target_channel or not questions_data:
            await context.bot.send_message(chat_id=chat_id, text="⚠️ একটি ত্রুটি হয়েছে। অনুগ্রহ করে /cancel করে আবার শুরু করুন।")
            clear_user_state(context.user_data)
            return

        await context.bot.send_message(chat_id=chat_id, text=f" সূচনা বার্তা পেয়েছি। '{target_channel}'-এ পোস্ট করা হচ্ছে...")

        count = 0
        errors = 0
        try:
            await context.bot.send_message(chat_id=target_channel, text=intro_text)
            for poll_data in questions_data:
                try:
                    await context.bot.send_poll(
                        chat_id=target_channel,
                        question=poll_data['question'],
                        options=poll_data['options'],
                        type=telegram.Poll.QUIZ,
                        correct_option_id=poll_data['correct_option_index'],
                        explanation=poll_data.get('explanation') 
                    )
                    count += 1
                    await asyncio.sleep(1) 
                except Exception as e:
                    print(f"পোল পাঠাতে সমস্যা (চ্যানেল {target_channel}): {e}")
                    errors += 1
        except Exception as e:
            print(f"চ্যানেল {target_channel}-এ মেসেজ পাঠানো যায়নি: {e}")
            await context.bot.send_message(chat_id=chat_id, text=f"⚠️ চ্যানেল '{target_channel}'-এ পোস্ট করতে মারাত্মক সমস্যা হয়েছে: {e}")
            clear_user_state(context.user_data)
            return

        clear_user_state(context.user_data)
        feedback_message = f"সফলভাবে চ্যানেল '{target_channel}'-এ {count} টি পোল পোস্ট করা হয়েছে!"
        if errors > 0: feedback_message += f"\n{errors} টি পোস্টে সমস্যা হয়েছে।"
        await context.bot.send_message(chat_id=chat_id, text=feedback_message)

    
    # --- ধাপ ২: যদি বট নতুন প্রশ্নের জন্য অপেক্ষা করে (IDLE) ---
    elif current_state == STATE_IDLE:
        
        # --- পরিবর্তন: ডাটাবেস থেকে চ্যানেল আইডি পড়া ---
        target_channel = get_target_channel_from_db(user.id)
        if not target_channel:
            await context.bot.send_message(chat_id=chat_id, text="⚠️ টার্গেট চ্যানেল সেট করা নেই। /setchannel ব্যবহার করুন।")
            return

        # (বাকি বাফারিং লজিক পরিবর্তন হয়নি)
        if 'text_buffer' not in context.user_data:
            context.user_data['text_buffer'] = []
            await context.bot.send_message(chat_id=chat_id, text="টেক্সট পেয়েছি... (আরও টেক্সট এলে সেগুলোর জন্য ৩ সেকেন্ড অপেক্ষা করছি)")
        
        if 'buffer_job' in context.user_data:
            context.user_data['buffer_job'].remove()
            
        context.user_data['text_buffer'].append(user_message)
        
        new_job = context.job_queue.run_once(
            process_buffered_text, 
            TEXT_BUFFER_DELAY, 
            data={'chat_id': chat_id, 'user_id': user.id},
            name=f"buffer-{user.id}"
        )
        context.user_data['buffer_job'] = new_job

# বট চালু করার মেইন ফাংশন (আপডেটেড)
def main():
    # --- ভেরিয়েবল চেক ---
    if not TELEGRAM_BOT_TOKEN or not GEMINI_API_KEY or not DATABASE_URL:
        print("---!!! ERROR: টোকেন বা এপিআই কী সেট করা হয়নি !!!---")
        print("অনুগ্রহ করে TELEGRAM_BOT_TOKEN, GEMINI_API_KEY, এবং DATABASE_URL এনভায়রনমেন্ট ভেরিয়েবল সেট করুন।")
        return

    print("বট চালু হচ্ছে...")
    
    # --- নতুন: ডাটাবেস চালু করা ---
    init_db()

    # --- PicklePersistence মুছে ফেলা হয়েছে ---
    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        # .persistence(persistence) - এটি আর নেই
        .build()
    )

    # হ্যান্ডলার (পরিবর্তন নেই)
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("setchannel", set_channel))
    application.add_handler(CommandHandler("cancel", cancel_quiz))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("বট সফলভাবে চালু হয়েছে এবং মেসেজের জন্য অপেক্ষা করছে...")
    application.run_polling()

if __name__ == "__main__":
    main()
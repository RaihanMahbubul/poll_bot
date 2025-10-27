import telegram
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from telegram.ext._jobqueue import Job
from telegram.constants import ParseMode # <-- হেল্প/স্টার্ট ফরম্যাটিং এর জন্য
import google.generativeai as genai
import json
import asyncio
import os
import psycopg2
from urllib.parse import urlparse
from flask import Flask
import threading
import traceback # <-- নতুন ইম্পোর্ট (বিস্তারিত এরর দেখার জন্য)

# -----------------------------------------------------------------
# --- টোকেন বা কী এখানে লোড করা হচ্ছে না ---
# --- এগুলো এখন main() ফাংশনের ভেতরে লোড হবে ---
# -----------------------------------------------------------------

# conversation-এর দুটি অবস্থা (state)
STATE_IDLE, STATE_AWAITING_INTRO = range(2)
TEXT_BUFFER_DELAY = 3  # সেকেন্ড

# --- Flask ওয়েব সার্ভার সেটআপ (বটকে জাগিয়ে রাখার জন্য) ---
app = Flask(__name__)
@app.route('/')
def home():
    """এটি UptimeRobot-কে দেখাবে যে বটটি সচল আছে।"""
    return "I am alive and polling!"

def run_web_server():
    """Flask সার্ভারটি চালু করে।"""
    # Render স্বয়ংক্রিয়ভাবে PORT এনভায়রনমেন্ট ভেরিয়েবল সেট করে।
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)

# --- নতুন ফাংশন: ডাটাবেস কানেকশন ---
def get_db_connection():
    """Render-এর DATABASE_URL থেকে কানেকশন তৈরি করে।"""
    try:
        # ভেরিয়েবলটি এখানে সরাসরি পড়া হচ্ছে
        db_url = os.environ.get("DATABASE_URL")
        if not db_url:
            print("❌ ডাটাবেস কানেকশনে সমস্যা: DATABASE_URL খুঁজে পাওয়া যায়নি।")
            return None
        conn = psycopg2.connect(db_url)
        return conn
    except Exception as e:
        print(f"❌ ডাটাবেস কানেকশনে সমস্যা: {e}")
        return None

# --- নতুন ফাংশন: ডাটাবেস টেবিল তৈরি ---
def init_db():
    """বট চালু হওয়ার সময় এই ফাংশন ডাটাবেস টেবিল তৈরি করবে।"""
    conn = get_db_connection()
    if conn is None:
        print("❌ ডাটাবেস ইনিশিয়ালাইজ করা যাচ্ছে না।")
        return

    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_settings (
                    user_id BIGINT PRIMARY KEY,
                    target_channel TEXT
                );
            """)
            conn.commit()
        print("✅ ডাটাবেস টেবিল (user_settings) সফলভাবে চেক/তৈরি করা হয়েছে।")
    except Exception as e:
        print(f"❌ টেবিল তৈরিতে সমস্যা: {e}")
    finally:
        if conn:
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
        print(f"❌ চ্যানেল আইডি পড়াতে সমস্যা: {e}")
        return None
    finally:
        if conn:
            conn.close()

# --- নতুন ফাংশন: ডাটাবেসে চ্যানেল আইডি সেভ করা ---
def save_target_channel_to_db(user_id: int, target_channel: str):
    conn = get_db_connection()
    if conn is None: return

    try:
        with conn.cursor() as cur:
            # ON CONFLICT... (UPSERT): যদি ইউজার আইডি আগে থেকেই থাকে, তবে আপডেট করো
            cur.execute("""
                INSERT INTO user_settings (user_id, target_channel)
                VALUES (%s, %s)
                ON CONFLICT (user_id) DO UPDATE SET target_channel = EXCLUDED.target_channel;
            """, (user_id, target_channel))
            conn.commit()
    except Exception as e:
        print(f"❌ চ্যানেল আইডি সেভ করতে সমস্যা: {e}")
    finally:
        if conn:
            conn.close()

# --- AI দিয়ে প্রশ্ন জেনারেট করার ফাংশন (ডাইনামিক সাফিক্স সহ) ---
def get_questions_from_ai(text, ai_model):
    # প্রম্পট আপডেট করা হয়েছে "suffix" নামে নতুন একটি ফিল্ড যোগ করার জন্য
    prompt = f"""
    তুমি একজন দক্ষ টেলিগ্রাম বট। তোমার কাজ হলো নিচের টেক্সট থেকে শুধুমাত্র মাল্টিপল চয়েস প্রশ্ন (MCQ) বের করা।
    তোমার উত্তর অবশ্যই একটি JSON লিস্ট ফরম্যাটে হতে হবে। প্রতিটি অবজেক্টে ৫টি কী থাকবে:
    1. "question": (স্ট্রিং) মূল প্রশ্নটি। (প্রশ্ন থেকে [SOT] বা [MAT 23-24] এর মতো ট্যাগ বাদ দিয়ে শুধু প্রশ্নটি বের করবে)।
    2. "options": (লিস্ট) অপশনগুলোর লিস্ট (সর্বোচ্চ ১০টি)।
    3. "correct_option_index": (সংখ্যা) সঠিক অপশনের ইনডেক্স (0 থেকে শুরু)।
    4. "explanation": (স্ট্রিং) সঠিক উত্তরের একটি সংক্ষিপ্ত ব্যাখ্যা। যদি ব্যাখ্যা খুঁজে না পাও, তবে এর মান `null` দাও।
    5. "suffix": (স্ট্রিং) প্রশ্নের লাইনের শেষে যদি [ব্র্যাকেটের মধ্যে] কোনো ট্যাগ (যেমন [MAT 23-24] বা [PHY-22]) থাকে, তবে সেটি এখানে হুবহু যুক্ত করো। যদি এমন কোনো ট্যাগ না থাকে, তবে এর মান `null` দাও।

    টেক্সট:
    ---
    {text}
    ---

    JSON আউটপুট উদাহরণ:
    [
      {{
        "question": "বাংলাদেশের রাজধানীর নাম কি?",
        "options": ["ঢাকা", "চট্টগ্রাম", "খুলনা", "রাজাহী"],
        "correct_option_index": 0,
        "explanation": "ঢাকা বাংলাদেশের রাজধানী ও বৃহত্তম শহর।",
        "suffix": "[MAT 23-24]"
      }},
      {{
        "question": "সূর্য কোন দিকে ওঠে?",
        "options": ["উত্তর", "দক্ষিণ", "পূর্ব", "পশ্চিম"],
        "correct_option_index": 2,
        "explanation": null,
        "suffix": null
      }}
    ]
    """
    try:
        response = ai_model.generate_content(prompt)
        if not response.parts:
            print(f"⚠️ AI রেসপন্স ব্লকড। কারণ: {response.prompt_feedback}")
            return None
        json_data = json.loads(response.text)
        return json_data
    except Exception as e:
        print(f"❌ AI বা JSON পার্সিং-এ অজানা সমস্যা: {e}")
        # --- বিস্তারিত এরর দেখানোর জন্য ---
        traceback.print_exc()
        # -------------------------------
        return None

# --- টুল ফাংশন: স্টেট রিসেট করার জন্য ---
def clear_user_state(user_data: dict):
    """ব্যবহারকারীর বর্তমান অবস্থা রিসেট করে, পেন্ডিং কুইজ এবং টাইমার মুছে ফেলে।"""
    user_data['CONV_STATE'] = STATE_IDLE
    if 'pending_quiz_data' in user_data: del user_data['pending_quiz_data']
    job_to_remove: Job | None = user_data.get('buffer_job')
    if job_to_remove:
        try:
            job_to_remove.remove() # টাইমারটি বন্ধ করা
        except Exception as e:
            print(f"⚠️ টাইমার রিমুভ করতে সমস্যা: {e}") # যদি জব আগে থেকেই রিমুভ হয়ে গিয়ে থাকে
        if 'buffer_job' in user_data: # ডাবল চেক
             del user_data['buffer_job']
    if 'text_buffer' in user_data: del user_data['text_buffer']


# --- /start কমান্ড হ্যান্ডলার (HTML ফরম্যাটে ফিক্স করা) ---
async def start_command(update: telegram.Update, context: ContextTypes.DEFAULT_TYPE):
    """
    নতুন ব্যবহারকারীকে /start কমান্ডে বিস্তারিত নির্দেশনা দেখায়।
    """
    clear_user_state(context.user_data) # স্টেট রিসেট করা

    # --- HTML ফরম্যাটে পরিবর্তন করা হয়েছে ---
    instructions = """
👋 আসসালামু আলাইকুম! <b>[SOT] পোল বট-এ আপনাকে স্বাগতম।</b>

এই বটটি আপনার টেক্সট মেসেজকে সুন্দর কুইজ পোলে রূপান্তর করে আপনার চ্যানেলে পোস্ট করতে পারে।

<b>বট ব্যবহারের সম্পূর্ণ নিয়মাবলী:</b>

<b>ধাপ ১: টার্গেট চ্যানেল সেট করা (শুধু প্রথমবার)</b>
বটকে বলুন কোন চ্যানেলে পোস্ট করতে হবে।
• কমান্ড দিন: <code>/setchannel &lt;channel_id_or_@username&gt;</code>
• উদাহরণ (প্রাইভেট চ্যানেল): <code>/setchannel -100123456789</code>
• উদাহরণ (পাবলিক চ্যানেল): <code>/setchannel @MyQuizChannel</code>
<i>(বটকে অবশ্যই সেই চ্যানেলের অ্যাডমিন হতে হবে এবং পোল পোস্ট করার অনুমতি থাকতে হবে)</i>

<b>ধাপ ২: প্রশ্ন পাঠানো</b>
আপনার প্রশ্ন, অপশন, সঠিক উত্তর, ব্যাখ্যা এবং সাফিক্স (ট্যাগ) নিচের মতো সাজিয়ে বটকে টেক্সট মেসেজ করুন:

<pre>
প্রশ্ন ১? [ট্যাগ-১]
(ক) অপশন ১
(খ) অপশন ২
(গ) অপশন ৩
সঠিক উত্তর: (ক)
ব্যাখ্যা: এটি হলো ব্যাখ্যা...

প্রশ্ন ২? [ট্যাগ-২]
(ক) অপশন ১
(খ) অপশন ২
সঠিক উত্তর: (খ)
</pre>
• <b>[ট্যাগ] (ঐচ্ছিক):</b> প্রতিটি প্রশ্নের শেষে <code>[ব্র্যাকেটের মধ্যে]</code> ট্যাগ দিলে, বট স্বয়ংক্রিয়ভাবে সেটিকে প্রশ্নের শেষে যোগ করবে (যেমন: <code>[MAT 23-24]</code>)।
• <b>ব্যাখ্যা (ঐচ্ছিক):</b> "ব্যাখ্যা:" লিখলে বট সেটি পোলে যুক্ত করবে।

<i>(<b>দ্রষ্টব্য:</b> বট স্বয়ংক্রিয়ভাবে প্রতিটি প্রশ্নের আগে <b>[SOT]</b> যোগ করে দেবে।)</i>

<b>ধাপ ৩: সূচনা বার্তা (Intro Message) পাঠানো</b>
প্রশ্নগুলো সফলভাবে প্রসেস করার পর, বট আপনাকে একটি "সূচনা বার্তা" পাঠাতে বলবে।
• আপনি তখন কুইজের শিরোনাম (যেমন: "আজকের রসায়ন কুইজ") লিখে পাঠান।
• বট সেই শিরোনামটি আগে পোস্ট করবে, তারপর পোলগুলো পোস্ট করা শুরু করবে।

<b>অন্যান্য কমান্ড:</b>
• <code>/cancel</code> - যেকোনো সময় কোনো কাজ (যেমন সূচনা বার্তার জন্য অপেক্ষা) বাতিল করতে এই কমান্ড দিন।
• <code>/help</code> - কমান্ডগুলোর একটি সংক্ষিপ্ত তালিকা দেখতে এই কমান্ড দিন।
"""

    await update.message.reply_text(
        instructions,
        parse_mode=ParseMode.HTML # <--!!! HTML-এ পরিবর্তন করা হয়েছে !!!
    )

# --- /setchannel কমান্ড হ্যান্ডলার ---
async def set_channel(update: telegram.Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    clear_user_state(context.user_data)
    if not context.args:
        await update.message.reply_text("⚠️ ব্যবহার: /setchannel <channel_id_or_@username>")
        return
    target_channel = context.args[0]
    save_target_channel_to_db(user_id, target_channel) # ডাটাবেসে সেভ
    await update.message.reply_text(
        f"✅ টার্গেট চ্যানেল সফলভাবে সেট করা হয়েছে: {target_channel}\n"
        "(এই সেটিংটি এখন স্থায়ীভাবে সেভ থাকবে)"
    )

# --- /cancel কমান্ড হ্যান্ডলার ---
async def cancel_quiz(update: telegram.Update, context: ContextTypes.DEFAULT_TYPE):
    """পেন্ডিং থাকা কুইজ পোস্ট বা টেক্সট বাফার বাতিল করে।"""
    clear_user_state(context.user_data)
    await update.message.reply_text("✅ বর্তমান কাজটি বাতিল করা হয়েছে। আপনি নতুন প্রশ্ন পাঠাতে পারেন।")


# --- /help কমান্ড হ্যান্ডলার ---
async def help_command(update: telegram.Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /help কমান্ডে কমান্ডগুলোর একটি সংক্ষিপ্ত তালিকা দেখায়।
    """
    help_text = """
ℹ️ <b>[SOT] পোল বট - হেল্প মেনু</b>

এখানে বটের প্রধান কমান্ডগুলো দেওয়া হলো:

• <code>/start</code> - বট সম্পর্কে বিস্তারিত নির্দেশনা ও সম্পূর্ণ গাইডলাইন দেখায়।
• <code>/setchannel &lt;ID&gt;</code> - কোন চ্যানেলে পোল পোস্ট করতে চান তা সেট করে। (যেমন: <code>/setchannel -100123...</code>)
• <code>/cancel</code> - কোনো চলমান কাজ (যেমন: সূচনা বার্তার জন্য অপেক্ষা) বাতিল করে।
• <code>/help</code> - এই হেল্প মেসেজটি দেখায়।
"""
    await update.message.reply_text(
        help_text,
        parse_mode=ParseMode.HTML # <-- এটিকেও HTML করা হলো
    )
# ------------------------------------


# --- টাইমার শেষ হলে এই ফাংশনটি রান হবে (বাফারিং এর জন্য) ---
async def process_buffered_text(context: ContextTypes.DEFAULT_TYPE):
    """
    বাফারে জমা হওয়া সম্পূর্ণ টেক্সটকে AI দিয়ে প্রসেস করে।
    """
    job_data = context.job.data
    chat_id = job_data['chat_id']
    user_id = job_data['user_id']

    # user_data JobQueue থেকে সরাসরি পাওয়া যায় না
    # ---!!! সেফটি চেক: যদি কোনো কারণে user_data না থাকে !!!---
    if user_id not in context.application.user_data:
         print(f"⚠️ process_buffered_text: user_id {user_id} এর জন্য user_data খুঁজে পাওয়া যায়নি।")
         return # ফাংশন থেকে বের হয়ে যাও

    user_data = context.application.user_data[user_id]
    ai_model = context.application.bot_data.get('ai_model') # <-- .get() ব্যবহার করা হলো

    # ---!!! সেফটি চেক: যদি ai_model লোড না হয়ে থাকে !!!---
    if not ai_model:
        print("❌ process_buffered_text: AI মডেল লোড হয়নি। বট রিস্টার্ট করুন।")
        await context.bot.send_message(chat_id=chat_id, text="❌ একটি অভ্যন্তরীণ ত্রুটি হয়েছে (AI মডেল লোড হয়নি)। অনুগ্রহ করে বট এডমিনকে জানান।")
        clear_user_state(user_data)
        return

    target_channel = get_target_channel_from_db(user_id) # ডাটাবেস থেকে চ্যানেল আইডি পড়া
    if not target_channel:
        await context.bot.send_message(chat_id=chat_id, text="⚠️ টার্গেট চ্যানেল সেট করা নেই। /setchannel ব্যবহার করুন।")
        clear_user_state(user_data)
        return

    full_text = "\n".join(user_data.get('text_buffer', []))

    # বাফার এবং জব ক্লিয়ার করা
    if 'buffer_job' in user_data: del user_data['buffer_job']
    if 'text_buffer' in user_data: del user_data['text_buffer']

    if not full_text:
        await context.bot.send_message(chat_id=chat_id, text="⚠️ টেক্সট খুঁজে পাওয়া যায়নি।")
        clear_user_state(user_data)
        return

    await context.bot.send_message(chat_id=chat_id, text=f"✅ সম্পূর্ণ টেক্সট পেয়েছি ({len(full_text)} অক্ষর)। জেমিনি এআই দিয়ে প্রসেস করছি... 🤖")

    questions_data = get_questions_from_ai(full_text, ai_model) # ai_model পাস করা

    if not questions_data or not isinstance(questions_data, list) or len(questions_data) == 0:
        await context.bot.send_message(chat_id=chat_id, text="❌ দুঃখিত, AI প্রশ্ন তৈরি করতে ব্যর্থ হয়েছে বা কোনো প্রশ্ন খুঁজে পায়নি। ইনপুট টেক্সট চেক করুন।")
        clear_user_state(user_data)
        return

    # প্রশ্ন সফল হলে, সেভ করা এবং সূচনার জন্য বলা
    user_data['pending_quiz_data'] = questions_data
    user_data['CONV_STATE'] = STATE_AWAITING_INTRO
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"✅ {len(questions_data)} টি প্রশ্ন সফলভাবে প্রসেস করা হয়েছে।\n\n"
             "➡️ **এখন এই কুইজের জন্য একটি সূচনা বার্তা (intro text) পাঠান।**\n\n"
             "(অথবা /cancel লিখে বাতিল করুন)"
    )


# ---!!! মূল টেক্সট মেসেজ হ্যান্ডলার (স্টেট ম্যানেজমেন্ট + এরর রিপোর্টিং) !!!---
async def handle_text(update: telegram.Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = update.message.text
    chat_id = update.message.chat_id
    user = update.effective_user

    # ইউজারের বর্তমান অবস্থা (state) চেক করা
    current_state = context.user_data.get('CONV_STATE', STATE_IDLE)

    # --- ধাপ ১: যদি বট সূচনার জন্য অপেক্ষা করে ---
    if current_state == STATE_AWAITING_INTRO:

        intro_text = user_message # এই মেসেজটিই হলো সূচনা বার্তা
        target_channel = get_target_channel_from_db(user.id) # ডাটাবেস থেকে চ্যানেল আইডি পড়া
        questions_data = context.user_data.get('pending_quiz_data')

        if not target_channel or not questions_data:
            await context.bot.send_message(chat_id=chat_id, text="⚠️ একটি ত্রুটি হয়েছে (চ্যানেল বা প্রশ্ন ডেটা পাওয়া যায়নি)। অনুগ্রহ করে /cancel করে আবার শুরু করুন।")
            clear_user_state(context.user_data)
            return

        await context.bot.send_message(chat_id=chat_id, text=f"✅ সূচনা বার্তা পেয়েছি। '{target_channel}'-এ পোস্ট করা হচ্ছে...")
        count = 0
        errors = 0
        failed_polls_info = [] # <--!!! নতুন: ব্যর্থ পোলগুলোর তথ্য রাখার জন্য লিস্ট !!!---

        try:
            # --- প্রথমে কাস্টম সূচনা বার্তাটি পোস্ট করা ---
            await context.bot.send_message(chat_id=target_channel, text=intro_text)

            # --- তারপর পোলগুলো পোস্ট করা ---
            for poll_data in questions_data:
                formatted_question = "Error: Question not formatted" # ডিফল্ট মান
                try:
                    # ---!!! ডাইনামিক লিগ্যাসি যোগ করা !!!---
                    original_question = poll_data.get('question', 'Unknown Question') # .get() ব্যবহার
                    dynamic_suffix = poll_data.get('suffix') # AI থেকে পাওয়া সাফিক্স (null হতে পারে)

                    static_prefix = "[SOT]" # <-- আপনার স্বয়ংক্রিয় প্রিফিক্স

                    # \u200B হলো একটি জিরো-উইডথ স্পেস (সুন্দর দেখানোর জন্য)
                    formatted_question = f"{static_prefix} \u200B {original_question}"

                    # যদি সাফিক্স থাকে (null না হয়), তবেই সেটি যোগ করা
                    if dynamic_suffix:
                        formatted_question = f"{formatted_question} \u200B {dynamic_suffix}"
                    # -----------------------------------------------

                    # টেলিগ্রাম পোলের প্রশ্নের অক্ষর সীমা চেক করা (৩০০ অক্ষর)
                    if len(formatted_question) > 300:
                        # যদি খুব লম্বা হয়, লিগ্যাসি ছাড়া শুধু প্রশ্নটি পাঠানো
                        print(f"⚠️ প্রশ্নটি ৩০০ অক্ষরের বেশি ({len(formatted_question)}), লিগ্যাসি বাদ দেওয়া হচ্ছে: {original_question[:50]}...")
                        if len(original_question) > 300:
                            formatted_question = original_question[:300]
                        else:
                            formatted_question = original_question
                    # -----------------------------------------------

                    # ---!!! অপশন এবং ইনডেক্স চেক করা !!!---
                    options = poll_data.get('options')
                    correct_option_index = poll_data.get('correct_option_index')

                    if not options or not isinstance(options, list) or len(options) < 2 or len(options) > 10:
                        raise ValueError(f"অবৈধ অপশন সংখ্যা ({len(options) if options else 0})")
                    if correct_option_index is None or not isinstance(correct_option_index, int) or correct_option_index < 0 or correct_option_index >= len(options):
                        raise ValueError(f"অবৈধ সঠিক অপশন ইনডেক্স ({correct_option_index}), অপশন সংখ্যা: {len(options)}")
                    #------------------------------------------


                    await context.bot.send_poll(
                        chat_id=target_channel,
                        question=formatted_question, # <-- এখানে পরিবর্তিত প্রশ্নটি ব্যবহার করা
                        options=options,
                        type=telegram.Poll.QUIZ,
                        correct_option_id=correct_option_index,
                        explanation=poll_data.get('explanation')
                    )
                    count += 1
                    await asyncio.sleep(1) # টেলিগ্রামের রেট লিমিট এড়ানোর জন্য

                except Exception as e:
                    print(f"❌ পোল পাঠাতে সমস্যা (চ্যানেল {target_channel}): {e}")
                    errors += 1
                    # ---!!! নতুন: ব্যর্থ পোলের তথ্য যোগ করা !!!---
                    failed_polls_info.append({
                        "question": original_question[:100] + ('...' if len(original_question) > 100 else ''), # প্রশ্ন সংক্ষিপ্ত করা
                        "error": str(e) # এররের কারণ
                    })
                    # -----------------------------------------

        except Exception as e:
            # যদি চ্যানেল আইডি ভুল হয় বা বট অ্যাডমিন না থাকে
            print(f"❌ চ্যানেল {target_channel}-এ মেসেজ পাঠানো যায়নি: {e}")
            await context.bot.send_message(chat_id=chat_id, text=f"❌ চ্যানেল '{target_channel}'-এ পোস্ট করতে মারাত্মক সমস্যা হয়েছে। আইডি/বট পারমিশন চেক করুন: {e}")
            clear_user_state(context.user_data)
            return

        # সফলভাবে পোস্ট করার পর স্টেট রিসেট করা
        clear_user_state(context.user_data)

        # ---!!! নতুন: বিস্তারিত ফিডব্যাক মেসেজ !!!---
        feedback_message = f"✅ সফলভাবে চ্যানেল '{target_channel}'-এ {count} টি পোল পোস্ট করা হয়েছে।"
        if errors > 0:
            feedback_message += f"\n\n⚠️ কিন্তু {errors} টি পোল পোস্ট করতে সমস্যা হয়েছে:"
            for i, failed in enumerate(failed_polls_info):
                 # টেলিগ্রাম মেসেজের অক্ষর সীমা ৪০০০ এর কাছাকাছি, তাই খুব বেশি এরর দেখানো যাবে না
                 if len(feedback_message) < 3800:
                      feedback_message += f"\n {i+1}. প্রশ্ন: \"{failed['question']}\"\n    কারণ: {failed['error'][:100]}" # এরর সংক্ষিপ্ত করা
                 else:
                      feedback_message += "\n... (আরও এরর আছে)"
                      break # মেসেজ খুব বড় হয়ে গেলে লুপ থামিয়ে দাও
        # -------------------------------------------
        await context.bot.send_message(chat_id=chat_id, text=feedback_message)


    # --- ধাপ ২: যদি বট নতুন প্রশ্নের জন্য অপেক্ষা করে (IDLE) (বাফারিং লজিক) ---
    elif current_state == STATE_IDLE:

        target_channel = get_target_channel_from_db(user.id) # ডাটাবেস থেকে চ্যানেল আইডি পড়া
        if not target_channel:
            await context.bot.send_message(chat_id=chat_id, text="⚠️ টার্গেট চ্যানেল সেট করা নেই। /setchannel ব্যবহার করুন।")
            return

        # --- বাফারিং লজিক শুরু ---

        # যদি কোনো টাইমার আগে থেকেই চালু থাকে (অর্থাৎ এটি একটি স্প্লিট মেসেজ)
        if 'buffer_job' in context.user_data:
            context.user_data['buffer_job'].remove() # পুরানো টাইমার বাতিল

        # টেক্সট বাফারে এই মেসেজটি যোগ করা
        if 'text_buffer' not in context.user_data:
            context.user_data['text_buffer'] = []
            # এটিই প্রথম মেসেজ, তাই ইউজারকে জানানো
            await context.bot.send_message(chat_id=chat_id, text="⏳ টেক্সট পেয়েছি... (আরও টেক্সট এলে সেগুলোর জন্য ৩ সেকেন্ড অপেক্ষা করছি)")

        context.user_data['text_buffer'].append(user_message)

        # একটি নতুন টাইমার সেট করা
        new_job = context.job_queue.run_once(
            process_buffered_text,
            TEXT_BUFFER_DELAY,
            data={'chat_id': chat_id, 'user_id': user.id},
            name=f"buffer-{user.id}"
        )
        context.user_data['buffer_job'] = new_job
        # --- বাফারিং লজিক শেষ ---


# ---!!! বট চালু করার মেইন ফাংশন (Race Condition ফিক্সড) !!!---
def main():
    print("⏳ বট চালু হচ্ছে...")

    # --- ভেরিয়েবলগুলো এখন main() এর ভেতরে লোড হচ্ছে ---
    TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
    GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
    DATABASE_URL = os.environ.get("DATABASE_URL")

    # --- ভেরিয়েবল চেক ---
    if not TELEGRAM_BOT_TOKEN or not GEMINI_API_KEY or not DATABASE_URL:
        print("---❌ ERROR: টোকেন বা এপিআই কী সেট করা হয়নি !!!---")
        print("Render-এর 'Environment' ট্যাবে ভেরিয়েবলগুলো সঠিকভাবে সেট করা আছে কিনা চেক করুন।")
        return # বট বন্ধ করে দাও

    print("✅ টোকেন এবং কী সফলভাবে লোড হয়েছে।")

    # --- জেমিনি এআই এখন এখানে কনফিগার হচ্ছে ---
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        generation_config = genai.GenerationConfig(response_mime_type="application/json")
        ai_model = genai.GenerativeModel('gemini-flash-latest', generation_config=generation_config)
        print("✅ Gemini AI সফলভাবে কনফিগার করা হয়েছে (JSON মোডে)।")
    except Exception as e:
        print(f"❌ Gemini AI কনফিগারেশনে সমস্যা: {e}")
        return

    # --- ডাটাবেস চালু করা ---
    init_db()

    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .build()
    )

    # --- ai_model কে অ্যাপ্লিকেশন কনটেক্সটে সেভ করা ---
    # যাতে process_buffered_text ফাংশনটি এটি ব্যবহার করতে পারে
    application.bot_data['ai_model'] = ai_model

    # --- হ্যান্ডলার সেকশন ---
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("setchannel", set_channel))
    application.add_handler(CommandHandler("cancel", cancel_quiz))
    application.add_handler(CommandHandler("help", help_command)) # <-- /help কমান্ড
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    # ------------------------------------

    print("⏳ টেলিগ্রাম বট পোলিং শুরু করছে...")

    # Flask সার্ভার চালু করা (বটকে জাগিয়ে রাখার জন্য)
    web_thread = threading.Thread(target=run_web_server)
    web_thread.daemon = True
    web_thread.start()
    print("✅ ওয়েব সার্ভার চালু হয়েছে (বটকে জাগিয়ে রাখার জন্য)।")

    application.run_polling()
    print("ℹ️ বট পোলিং বন্ধ হয়েছে।") # যদি কোনো কারণে run_polling() শেষ হয়ে যায়

if __name__ == "__main__":
    main()